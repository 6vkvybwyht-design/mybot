"""Локальна копія стакану з WebSocket (snapshot + delta)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BookMetrics:
    best_bid: float
    best_ask: float
    mid: float
    spread_pct: float
    band_pct: float
    bid_notional: float      # USDT у смузі ±band від mid
    ask_notional: float
    imbalance: float         # частка bid у смузі, 0..1
    bid_walls: list[tuple[float, float]]   # (ціна, USDT), найбільші рівні
    ask_walls: list[tuple[float, float]]


class OrderBook:
    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.update_id: int | None = None
        self.ts: int | None = None

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.update_id = None

    @property
    def ready(self) -> bool:
        return bool(self.bids) and bool(self.asks)

    @staticmethod
    def _apply_side(side: dict[float, float], levels: list[list[str]]) -> None:
        for price_s, size_s in levels:
            price, size = float(price_s), float(size_s)
            if size == 0:
                side.pop(price, None)
            else:
                side[price] = size

    def apply(self, msg_type: str, data: dict, ts: int | None = None) -> None:
        # u == 1 означає перезапуск сервісу Bybit — це теж знімок.
        if msg_type == "snapshot" or data.get("u") == 1:
            self.bids.clear()
            self.asks.clear()
        elif self.update_id is None:
            return  # дельта без попереднього знімку — ігноруємо до нового знімку
        self._apply_side(self.bids, data.get("b", []))
        self._apply_side(self.asks, data.get("a", []))
        self.update_id = data.get("u", self.update_id)
        self.ts = ts

    def metrics(self, band_pct: float, n_walls: int = 3) -> BookMetrics | None:
        if not self.ready:
            return None
        best_bid, best_ask = max(self.bids), min(self.asks)
        mid = (best_bid + best_ask) / 2
        lo, hi = mid * (1 - band_pct / 100), mid * (1 + band_pct / 100)
        bid_n = sum(p * s for p, s in self.bids.items() if p >= lo)
        ask_n = sum(p * s for p, s in self.asks.items() if p <= hi)
        total = bid_n + ask_n
        walls = lambda side: sorted(((p, p * s) for p, s in side.items()), key=lambda x: -x[1])[:n_walls]
        return BookMetrics(
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            spread_pct=(best_ask - best_bid) / mid * 100,
            band_pct=band_pct,
            bid_notional=bid_n,
            ask_notional=ask_n,
            imbalance=bid_n / total if total else 0.5,
            bid_walls=walls(self.bids),
            ask_walls=walls(self.asks),
        )
