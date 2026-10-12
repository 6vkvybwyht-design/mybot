"""Публічний REST API Bybit v5 (без ключів)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx
import pandas as pd

from bot.config import MarketConfig, NetworkConfig

log = logging.getLogger(__name__)

KLINE_COLUMNS = ["start", "open", "high", "low", "close", "volume", "turnover"]


class BybitError(Exception):
    pass


@dataclass(frozen=True)
class Instrument:
    symbol: str
    status: str
    tick_size: float
    qty_step: float
    min_qty: float
    min_notional: float | None


def _f(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_klines(rows: list[list[str]], interval_min: int, now_ms: int) -> pd.DataFrame:
    """Свічки від Bybit (нові спочатку) -> DataFrame за зростанням часу.

    Колонка `closed` позначає закриті свічки; незакрита (поточна) буде останньою.
    """
    if not rows:
        df = pd.DataFrame(columns=KLINE_COLUMNS + ["closed"])
        return df
    df = pd.DataFrame(rows, columns=KLINE_COLUMNS).astype(float)
    df["start"] = df["start"].astype("int64")
    df = df.sort_values("start").drop_duplicates("start").reset_index(drop=True)
    df["closed"] = df["start"] + interval_min * 60_000 <= now_ms
    return df


class BybitRest:
    def __init__(self, market: MarketConfig, network: NetworkConfig, client: httpx.AsyncClient | None = None):
        self.market = market
        self.network = network
        self._client = client or httpx.AsyncClient(base_url=market.rest_base_url, timeout=network.timeout_sec)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self.network.retries + 1):
            try:
                resp = await self._client.get(path, params=params)
                resp.raise_for_status()
                data = resp.json()
                if data.get("retCode") != 0:
                    # Помилки API (невірний символ тощо) повтором не виправити.
                    raise BybitError(f"{path}: retCode={data.get('retCode')} {data.get('retMsg')}")
                return data["result"]
            except BybitError:
                raise
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                last_exc = exc
                if attempt < self.network.retries:
                    delay = self.network.backoff_base_sec * 2**attempt
                    log.warning("Bybit %s: %s; повтор через %.1f с", path, exc, delay)
                    await asyncio.sleep(delay)
        raise BybitError(f"{path}: {last_exc}") from last_exc

    def _base(self) -> dict[str, Any]:
        return {"category": self.market.category, "symbol": self.market.symbol}

    async def instrument(self) -> Instrument:
        result = await self._get("/v5/market/instruments-info", self._base())
        items = result.get("list") or []
        if not items:
            raise BybitError(f"інструмент {self.market.symbol} ({self.market.category}) не знайдено")
        it = items[0]
        lot, price = it.get("lotSizeFilter", {}), it.get("priceFilter", {})
        return Instrument(
            symbol=it["symbol"],
            status=it.get("status", ""),
            tick_size=float(price["tickSize"]),
            qty_step=float(lot.get("qtyStep", 0) or 0),
            min_qty=float(lot.get("minOrderQty", 0) or 0),
            min_notional=_f(lot.get("minNotionalValue")),
        )

    async def klines(
        self,
        interval_min: int,
        limit: int | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        now_ms: int | None = None,
    ) -> pd.DataFrame:
        params = {**self._base(), "interval": str(interval_min), "limit": limit or self.market.kline_limit}
        if start_ms is not None:
            params["start"] = start_ms
        if end_ms is not None:
            params["end"] = end_ms
        result = await self._get("/v5/market/kline", params)
        now_ms = now_ms if now_ms is not None else int(pd.Timestamp.now("UTC").timestamp() * 1000)
        return parse_klines(result.get("list") or [], interval_min, now_ms)

    async def ticker(self) -> dict[str, float | None]:
        result = await self._get("/v5/market/tickers", self._base())
        items = result.get("list") or []
        if not items:
            raise BybitError("порожній тікер")
        t = items[0]
        bid, ask = _f(t.get("bid1Price")), _f(t.get("ask1Price"))
        spread_pct = (ask - bid) / bid * 100 if bid and ask else None
        return {
            "last_price": _f(t.get("lastPrice")),
            "mark_price": _f(t.get("markPrice")),
            "index_price": _f(t.get("indexPrice")),
            "bid": bid,
            "ask": ask,
            "spread_pct": spread_pct,
            "funding_rate": _f(t.get("fundingRate")),
            "next_funding_ms": _f(t.get("nextFundingTime")),
            "open_interest": _f(t.get("openInterest")),
            "open_interest_value": _f(t.get("openInterestValue")),
            "turnover_24h": _f(t.get("turnover24h")),
            "price_24h_pct": (_f(t.get("price24hPcnt")) or 0) * 100,
            "high_24h": _f(t.get("highPrice24h")),
            "low_24h": _f(t.get("lowPrice24h")),
        }

    async def open_interest(self, limit: int = 13) -> pd.DataFrame:
        """OI з кроком 5 хв за зростанням часу. 13 точок = остання година."""
        params = {**self._base(), "intervalTime": "5min", "limit": limit}
        result = await self._get("/v5/market/open-interest", params)
        rows = [(int(r["timestamp"]), float(r["openInterest"])) for r in result.get("list") or []]
        return pd.DataFrame(rows, columns=["ts", "oi"]).sort_values("ts").reset_index(drop=True)

    async def long_short_ratio(self, limit: int = 13) -> pd.DataFrame:
        params = {**self._base(), "period": "5min", "limit": limit}
        result = await self._get("/v5/market/account-ratio", params)
        rows = [
            (int(r["timestamp"]), float(r["buyRatio"]), float(r["sellRatio"]))
            for r in result.get("list") or []
        ]
        return pd.DataFrame(rows, columns=["ts", "buy", "sell"]).sort_values("ts").reset_index(drop=True)

    async def recent_trades(self, limit: int = 1000) -> list[dict[str, Any]]:
        """Останні угоди — щоб не стартувати з порожнім вікном угод."""
        result = await self._get("/v5/market/recent-trade", {**self._base(), "limit": limit})
        return result.get("list") or []
