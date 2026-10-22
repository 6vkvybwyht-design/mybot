"""Ковзне вікно угод у пам'яті."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class Trade:
    ts: int        # мс, UTC
    price: float
    size: float    # у базовій монеті
    side: int      # +1 покупка (taker buy), -1 продаж

    @property
    def notional(self) -> float:
        return self.price * self.size


class TradeStore:
    def __init__(self, window_ms: int, dedupe_size: int = 20_000):
        self.window_ms = window_ms
        self._trades: deque[Trade] = deque()
        self._ids: deque[str] = deque()
        self._id_set: set[str] = set()
        self._dedupe_size = dedupe_size
        self._arrays: tuple[np.ndarray, ...] | None = None

    def __len__(self) -> int:
        return len(self._trades)

    def add(self, trade: Trade, trade_id: str | None = None) -> bool:
        if trade_id is not None:
            if trade_id in self._id_set:
                return False
            self._ids.append(trade_id)
            self._id_set.add(trade_id)
            if len(self._ids) > self._dedupe_size:
                self._id_set.discard(self._ids.popleft())
        if self._trades and trade.ts < self._trades[-1].ts:
            # Рідкісний випадок (засів з REST після WS): вставляємо з сортуванням.
            items = sorted([*self._trades, trade], key=lambda t: t.ts)
            self._trades = deque(items)
        else:
            self._trades.append(trade)
        self._arrays = None
        return True

    def prune(self, now_ms: int) -> None:
        cutoff = now_ms - self.window_ms
        changed = False
        while self._trades and self._trades[0].ts < cutoff:
            self._trades.popleft()
            changed = True
        if changed:
            self._arrays = None

    @property
    def last(self) -> Trade | None:
        return self._trades[-1] if self._trades else None

    @property
    def first_ts(self) -> int | None:
        return self._trades[0].ts if self._trades else None

    def arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """(ts, price, size, side) як numpy-масиви, кешуються до наступної зміни."""
        if self._arrays is None:
            n = len(self._trades)
            ts = np.fromiter((t.ts for t in self._trades), dtype=np.int64, count=n)
            price = np.fromiter((t.price for t in self._trades), dtype=np.float64, count=n)
            size = np.fromiter((t.size for t in self._trades), dtype=np.float64, count=n)
            side = np.fromiter((t.side for t in self._trades), dtype=np.int8, count=n)
            self._arrays = (ts, price, size, side)
        return self._arrays  # type: ignore[return-value]

    def price_at(self, ts_ms: int) -> float | None:
        """Ціна останньої угоди не пізніше ts_ms."""
        ts, price, _, _ = self.arrays()
        idx = int(np.searchsorted(ts, ts_ms, side="right")) - 1
        return float(price[idx]) if idx >= 0 else None
