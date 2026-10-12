"""Знімок ринку: усе, що бачить модель, в одному JSON-сумісному словнику.

Запуск з консолі: python -m bot.snapshot (потрібен запущений термінал MT5)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from bot.config import AppConfig, Secrets
from bot.market import indicators as ind
from bot.market.mt5_source import Credentials, MT5Source, MT5Unavailable, Notify, SymbolSpec
from bot.news.calendar import EventCalendar
from bot.news.rss import NewsService
from bot.triggers import spread_warning

log = logging.getLogger(__name__)


class SnapshotError(Exception):
    pass


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or not old:
        return None
    return round((new - old) / old * 100, 3)


@dataclass
class RawMarket:
    """Сирі дані з MT5 за один прохід. Відсутні джерела — None."""
    ts_ms: int
    klines: dict[int, pd.DataFrame]
    tick: dict[str, Any] | None
    activity: dict[str, Any] | None
    missing: list[str] = field(default_factory=list)


class MarketService:
    """Обгортка над MT5Source: нагляд за підключенням і збір сирих даних для знімка."""

    def __init__(
        self,
        cfg: AppConfig,
        source: MT5Source | None = None,
        *,
        secrets: Secrets | None = None,
        notify: Notify | None = None,
    ):
        self.cfg = cfg
        creds = Credentials.from_secrets(secrets) if secrets else Credentials()
        self.source = source or MT5Source(cfg.mt5, creds, notify=notify)
        if notify is not None and self.source.notify is None:
            self.source.notify = notify
        self.last_tick: dict[str, Any] | None = None
        self._task: asyncio.Task | None = None

    @property
    def instrument(self) -> SymbolSpec | None:
        return self.source.spec

    @property
    def connected(self) -> bool:
        return self.source.connected

    async def start(self) -> None:
        """Запускає фоновий нагляд: підключення, перепідключення, повідомлення власнику."""
        self._task = asyncio.create_task(self.source.run(), name="mt5")

    async def wait_connected(self, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not self.connected and loop.time() < end:
            await asyncio.sleep(0.2)
        return self.connected

    async def stop(self) -> None:
        await self.source.stop()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def fetch_raw(self, intervals: list[int] | None = None, activity: bool = True) -> RawMarket:
        if not self.connected:
            raise MT5Unavailable(self.source.last_error or "немає підключення до MT5")
        intervals = intervals or self.cfg.mt5.timeframes
        if self.source.offset_source != "config":
            await self.source.refresh_offset()   # зсув сервера змінюється при переході на літній/зимовий час
        now_ms = self.source.now_ms()
        coros: dict[str, Any] = {f"kline_{i}": self.source.klines(i, now_ms=now_ms) for i in intervals}
        coros["tick"] = self.source.tick(now_ms)
        if activity:
            coros["tick_activity"] = self.source.tick_activity(now_ms)
        results = dict(zip(coros, await asyncio.gather(*coros.values(), return_exceptions=True)))
        missing = []
        for name, res in results.items():
            if isinstance(res, MT5Unavailable):
                raise res
            if isinstance(res, BaseException):
                log.warning("Джерело %s недоступне: %s", name, res)
                missing.append(name)
                results[name] = None
        klines = {i: results[f"kline_{i}"] for i in intervals if results[f"kline_{i}"] is not None}
        if results["tick"] is not None:
            self.last_tick = results["tick"]
        return RawMarket(now_ms, klines, results["tick"], results.get("tick_activity"), missing)

    def price(self, raw: RawMarket) -> float | None:
        """Ціна графіка — Bid (по ньому будуються свічки MT5)."""
        if raw.tick and raw.tick.get("bid"):
            return float(raw.tick["bid"])
        for tf in (1, 5):
            if tf in raw.klines and not raw.klines[tf].empty:
                return float(raw.klines[tf]["close"].iloc[-1])
        return None


@dataclass
class Snapshot:
    data: dict[str, Any]
    ts_ms: int
    price: float
    missing: list[str]
    baseline_direction: str      # напрямок останньої закритої 5-хв свічки (для базової лінії)
    atr: float | None
    spread_warning: str | None = None

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
    spec = market.instrument

    try:
        tf5 = ind.compute_5m(raw.klines[5], cfg.indicators, now_ms, price)
    except ValueError as exc:
        raise SnapshotError(str(exc)) from exc

    t = raw.tick or {}
    spread_pct = t.get("spread_pct")
    data: dict[str, Any] = {
        "symbol": cfg.mt5.symbol,
        "instrument": "CFD на нафту WTI (MetaTrader 5, Bybit TradFi)",
        "time_utc": datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "price": price,
        "quote": {
            "bid": t.get("bid"),
            "ask": t.get("ask"),
            "spread": t.get("spread"),
            "spread_points": t.get("spread_points"),
            "spread_pct": spread_pct,
            "last_tick_age_sec": t.get("age_sec"),
        },
        "costs": {
            "spread_pct": spread_pct,
            "round_trip_pct": spread_pct,
        },
        "tick_size": spec.tick_size if spec else None,
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

    a = raw.activity
    if a and a.get("ticks_total"):
        data["tick_activity"] = {k: a.get(k) for k in (
            "ticks_1m", "ticks_5m", "ticks_15m", "base_per_min", "ratio_1m", "ratio_5m")}
    elif "tick_activity" not in missing:
        missing.append("tick_activity")

    warn = None
    if a and t.get("spread") is not None:
        median = a.get("spread_median")
        warn = spread_warning(t["spread"], median, cfg.alerts.triggers.spread_widen_mult)
        point = spec.point if spec else None
        data["spread_state"] = {
            "current_points": t.get("spread_points"),
            "median_60m_points": round(median / point, 1) if median and point else None,
            "widened": warn is not None,
        }

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
        spread_warning=warn.text if warn else None,
    )


async def collect(
    cfg: AppConfig, market: MarketService, news: NewsService | None = None, calendar: EventCalendar | None = None
) -> Snapshot:
    raw = await market.fetch_raw()
    return build_snapshot(cfg, market, raw, news, calendar)


async def _main() -> None:
    from bot.config import load_settings
    from bot.logging_setup import setup_logging
    from bot.tracking.db import Database

    settings = load_settings()
    cfg = settings.config
    setup_logging(cfg, settings.secrets)
    market = MarketService(cfg, secrets=settings.secrets)
    news = NewsService(cfg.news, cfg.network)
    db = Database(cfg.db_path)
    await db.connect()
    calendar = EventCalendar(cfg.calendar, await db.load_events())
    try:
        await market.start()
        await news.poll_once()
        if not await market.wait_connected(cfg.mt5.init_timeout_ms / 1000 + 5):
            raise SystemExit(f"MT5 недоступний: {market.source.last_error}")
        snap = await collect(cfg, market, news, calendar)
        print(json.dumps(snap.data, ensure_ascii=False, indent=2))
        print(f"\nВідсутні джерела: {snap.missing or 'немає'}")
    finally:
        await market.stop()
        await news.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main())
