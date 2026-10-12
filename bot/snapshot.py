"""Знімок ринку: усе, що бачить модель, в одному JSON-сумісному словнику.

Запуск з консолі (етап 2/3): python -m bot.snapshot
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from bot.config import AppConfig
from bot.market import indicators as ind
from bot.market.bybit_rest import BybitError, BybitRest, Instrument
from bot.market.bybit_ws import BybitWS
from bot.market.whales import WhaleAnalyzer
from bot.news.calendar import EventCalendar
from bot.news.rss import NewsService

log = logging.getLogger(__name__)


class SnapshotError(Exception):
    pass


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or not old:
        return None
    return round((new - old) / old * 100, 3)


@dataclass
class RawMarket:
    """Сирі дані з REST за один прохід. Відсутні джерела — None."""
    ts_ms: int
    klines: dict[int, pd.DataFrame]
    ticker: dict[str, Any] | None
    oi: pd.DataFrame | None
    ls: pd.DataFrame | None
    missing: list[str] = field(default_factory=list)


class MarketService:
    """Тримає REST-клієнт, WebSocket і аналізатор китів."""

    def __init__(self, cfg: AppConfig, rest: BybitRest | None = None, ws: BybitWS | None = None):
        self.cfg = cfg
        self.rest = rest or BybitRest(cfg.market, cfg.network)
        self.ws = ws or BybitWS(cfg.market, cfg.network)
        self.whales = WhaleAnalyzer(cfg.whales, self.ws.trades)
        self.instrument: Instrument | None = None
        self._ws_task: asyncio.Task | None = None

    async def start(self, with_ws: bool = True) -> None:
        self.instrument = await self.rest.instrument()
        if self.instrument.status and self.instrument.status != "Trading":
            log.warning("Інструмент %s має статус %s", self.instrument.symbol, self.instrument.status)
        log.info(
            "Інструмент %s: tick %s, мін. обсяг %s",
            self.instrument.symbol, self.instrument.tick_size, self.instrument.min_qty,
        )
        try:
            seeded = self.ws.seed_trades(await self.rest.recent_trades())
            log.info("Засіяно %d останніх угод з REST", seeded)
        except BybitError as exc:
            log.warning("Не вдалося взяти останні угоди: %s", exc)
        if with_ws:
            self._ws_task = asyncio.create_task(self.ws.run(), name="bybit-ws")

    async def stop(self) -> None:
        self.ws.stop()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.rest.close()

    async def fetch_raw(self, intervals: list[int] | None = None, oi: bool = True, ls: bool = True) -> RawMarket:
        intervals = intervals or self.cfg.market.kline_intervals
        now_ms = int(time.time() * 1000)
        coros: dict[str, Any] = {f"kline_{i}": self.rest.klines(i, now_ms=now_ms) for i in intervals}
        coros["ticker"] = self.rest.ticker()
        if oi:
            coros["oi"] = self.rest.open_interest(limit=13)
        if ls:
            coros["ls"] = self.rest.long_short_ratio(limit=13)
        results = dict(zip(coros, await asyncio.gather(*coros.values(), return_exceptions=True)))
        missing = []
        for name, res in results.items():
            if isinstance(res, BaseException):
                log.warning("Джерело %s недоступне: %s", name, res)
                missing.append(name)
                results[name] = None
        klines = {i: results[f"kline_{i}"] for i in intervals if results[f"kline_{i}"] is not None}
        return RawMarket(now_ms, klines, results["ticker"], results.get("oi"), results.get("ls"), missing)

    def price(self, raw: RawMarket | None = None) -> float | None:
        last = self.ws.trades.last
        if last is not None and (self.ws.seconds_since_data() or 1e9) < self.cfg.network.ws_stale_sec:
            return last.price
        if raw and raw.ticker and raw.ticker.get("last_price"):
            return raw.ticker["last_price"]
        if raw and 5 in raw.klines and not raw.klines[5].empty:
            return float(raw.klines[5]["close"].iloc[-1])
        return last.price if last else None


@dataclass
class Snapshot:
    data: dict[str, Any]
    ts_ms: int
    price: float
    missing: list[str]
    baseline_direction: str      # напрямок останньої закритої 5-хв свічки (для базової лінії)
    atr: float | None

    def to_json(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))


def build_snapshot(
    cfg: AppConfig,
    market: MarketService,
    raw: RawMarket,
    news: NewsService | None = None,
    calendar: EventCalendar | None = None,
) -> Snapshot:
    if 5 not in raw.klines:
        raise SnapshotError("немає 5-хв свічок — аналіз неможливий")
    now_ms = raw.ts_ms
    price = market.price(raw)
    if price is None:
        raise SnapshotError("немає поточної ціни")
    missing = list(raw.missing)
    fee = cfg.market.taker_fee_pct

    tf5 = ind.compute_5m(raw.klines[5], cfg.indicators, now_ms, price)

    data: dict[str, Any] = {
        "symbol": cfg.market.symbol,
        "time_utc": datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "price": price,
        "fees": {
            "taker_pct": fee,
            "round_trip_pct": round(2 * fee, 4),
        },
        "tick_size": market.instrument.tick_size if market.instrument else None,
        "tf_5m": tf5,
    }

    if 1 in raw.klines:
        k1 = raw.klines[1]
        closed1 = k1[k1["closed"]]
        data["tf_1m"] = {
            "last_10_ohlcv_incl_current": ind.recent_candles(k1, 10),
            "change_5m_pct": _pct(price, float(closed1["close"].iloc[-5])) if len(closed1) >= 5 else None,
        }

    data["trend"] = {}
    for tf in (15, 60):
        if tf in raw.klines:
            k = raw.klines[tf]
            data["trend"][f"{tf}m"] = ind.trend(
                k[k["closed"]], cfg.indicators.trend_ema_fast, cfg.indicators.trend_ema_slow
            )

    t = raw.ticker
    if t:
        next_funding = t.get("next_funding_ms")
        data["ticker"] = {
            "mark_price": t.get("mark_price"),
            "index_price": t.get("index_price"),
            "spread_pct": None if t.get("spread_pct") is None else round(t["spread_pct"], 4),
            "funding_rate_pct": None if t.get("funding_rate") is None else round(t["funding_rate"] * 100, 4),
            "next_funding_in_min": None if not next_funding else round((next_funding - now_ms) / 60_000),
            "open_interest": t.get("open_interest"),
            "price_24h_pct": round(t.get("price_24h_pct") or 0, 3),
            "high_24h": t.get("high_24h"),
            "low_24h": t.get("low_24h"),
        }

    if raw.oi is not None and len(raw.oi) >= 2:
        s = raw.oi["oi"]
        data["open_interest"] = {
            "series_5m_last_hour": [round(x, 2) for x in s.tolist()],
            "change_15m_pct": _pct(s.iloc[-1], s.iloc[-4]) if len(s) >= 4 else None,
            "change_1h_pct": _pct(s.iloc[-1], s.iloc[0]),
        }
    elif "oi" not in missing:
        missing.append("oi")

    if raw.ls is not None and len(raw.ls):
        data["long_short_accounts"] = {
            "buy_ratio": raw.ls["buy"].iloc[-1],
            "sell_ratio": raw.ls["sell"].iloc[-1],
            "buy_ratio_change_1h": round(raw.ls["buy"].iloc[-1] - raw.ls["buy"].iloc[0], 4),
        }

    ws_age = market.ws.seconds_since_data(now_ms)
    ws_fresh = ws_age is not None and ws_age < cfg.network.ws_stale_sec
    if len(market.ws.trades):
        data["order_flow"] = market.whales.report(now_ms).to_dict()
        if not ws_fresh:
            data["order_flow"]["stale"] = True
    else:
        missing.append("trades_ws")
    book = market.ws.book.metrics(cfg.alerts.triggers.orderbook_band_pct) if ws_fresh else None
    if book:
        data["orderbook"] = {
            "best_bid": book.best_bid,
            "best_ask": book.best_ask,
            "spread_pct": round(book.spread_pct, 4),
            "band_pct": book.band_pct,
            "bid_usdt_in_band": round(book.bid_notional),
            "ask_usdt_in_band": round(book.ask_notional),
            "bid_share": round(book.imbalance, 3),
            "bid_walls": [[p, round(n)] for p, n in book.bid_walls],
            "ask_walls": [[p, round(n)] for p, n in book.ask_walls],
        }
    else:
        missing.append("orderbook_ws")

    if news is not None:
        if news.available:
            data["news_last_60m"] = news.headlines(now_ms / 1000)
        else:
            missing.append("news")
    if calendar is not None:
        now_dt = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
        data["events"] = calendar.snapshot(now_dt, cfg.analysis.event_warning_min)

    data["missing_sources"] = missing
    return Snapshot(
        data=data,
        ts_ms=now_ms,
        price=price,
        missing=missing,
        baseline_direction=tf5["last_closed"]["direction"],
        atr=tf5["atr"],
    )


async def collect(
    cfg: AppConfig, market: MarketService, news: NewsService | None = None, calendar: EventCalendar | None = None
) -> Snapshot:
    raw = await market.fetch_raw()
    return build_snapshot(cfg, market, raw, news, calendar)


async def _main(ws_seconds: float) -> None:
    from bot.config import load_settings
    from bot.logging_setup import setup_logging
    from bot.tracking.db import Database

    settings = load_settings()
    cfg = settings.config
    setup_logging(cfg, settings.secrets)
    market = MarketService(cfg)
    news = NewsService(cfg.news, cfg.network)
    db = Database(cfg.db_path)
    await db.connect()
    calendar = EventCalendar(cfg.calendar, await db.load_events())
    try:
        await market.start()
        news_task = asyncio.create_task(news.poll_once())
        log.info("Збираю дані WebSocket %.0f с…", ws_seconds)
        await asyncio.sleep(ws_seconds)
        await news_task
        snap = await collect(cfg, market, news, calendar)
        print(json.dumps(snap.data, ensure_ascii=False, indent=2))
        print(f"\nWS: підключень {market.ws.connects}, угод у вікні {len(market.ws.trades)}, "
              f"відсутні джерела: {snap.missing or 'немає'}")
    finally:
        await market.stop()
        await news.close()
        await db.close()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Надрукувати знімок ринку")
    p.add_argument("--ws-seconds", type=float, default=15, help="скільки слухати WebSocket перед знімком")
    asyncio.run(_main(p.parse_args().ws_seconds))
