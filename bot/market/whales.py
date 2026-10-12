"""Угоди китів: склеювання, поріг, метрики за вікнами."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

import numpy as np

from bot.config import WhalesConfig
from bot.market.trades import TradeStore

MIN_CLUSTERS_FOR_PERCENTILE = 200
BUCKET_MS = 5 * 60_000


def merge_trades(
    ts: np.ndarray, notional: np.ndarray, side: np.ndarray, merge_ms: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Склеює угоди одного напрямку в межах merge_ms від першої угоди групи.

    Повертає (час початку, сума USDT, напрямок), відсортовані за часом.
    """
    out_ts: list[int] = []
    out_n: list[float] = []
    out_side: list[int] = []
    for s in (1, -1):
        mask = side == s
        t_s, n_s = ts[mask], notional[mask]
        if len(t_s) == 0:
            continue
        start, total = int(t_s[0]), float(n_s[0])
        for t, n in zip(t_s[1:].tolist(), n_s[1:].tolist()):
            if t - start <= merge_ms:
                total += n
            else:
                out_ts.append(start)
                out_n.append(total)
                out_side.append(s)
                start, total = t, n
        out_ts.append(start)
        out_n.append(total)
        out_side.append(s)
    order = np.argsort(np.asarray(out_ts, dtype=np.int64), kind="stable")
    return (
        np.asarray(out_ts, dtype=np.int64)[order],
        np.asarray(out_n, dtype=np.float64)[order],
        np.asarray(out_side, dtype=np.int8)[order],
    )


@dataclass
class WindowStats:
    minutes: int
    trades: int
    volume_usdt: float
    cvd_usdt: float           # покупки - продажі по всіх угодах
    whale_buys: int
    whale_buy_usdt: float
    whale_sells: int
    whale_sell_usdt: float

    @property
    def whale_delta_usdt(self) -> float:
        return self.whale_buy_usdt - self.whale_sell_usdt

    def to_dict(self) -> dict:
        d = asdict(self)
        d["whale_delta_usdt"] = self.whale_delta_usdt
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in d.items()}


@dataclass
class WhaleReport:
    threshold_usdt: float
    threshold_source: str          # percentile | fixed | fixed_fallback
    history_minutes: float         # скільки історії реально є у вікні
    windows: dict[int, WindowStats]
    avg_abs_whale_delta_5m: float | None   # середнє |дельти китів| за 5 хв на всій історії

    def to_dict(self) -> dict:
        return {
            "threshold_usdt": round(self.threshold_usdt, 2),
            "threshold_source": self.threshold_source,
            "history_minutes": round(self.history_minutes, 1),
            "avg_abs_whale_delta_5m_usdt": None
            if self.avg_abs_whale_delta_5m is None
            else round(self.avg_abs_whale_delta_5m, 2),
            "windows": {f"{m}m": w.to_dict() for m, w in self.windows.items()},
        }


class WhaleAnalyzer:
    """Поріг і 24-годинна статистика дорогі — перераховуються не частіше ніж раз на refresh_sec."""

    def __init__(self, cfg: WhalesConfig, store: TradeStore, refresh_sec: float = 60):
        self.cfg = cfg
        self.store = store
        self.refresh_sec = refresh_sec
        self._cached_at: float = 0.0
        self._cached_len: int = -1
        self._threshold: tuple[float, str] = (cfg.fixed_usdt, "fixed")
        self._avg_abs_delta: float | None = None

    def _refresh_heavy(self, now_ms: int) -> None:
        ts, price, size, side = self.store.arrays()
        if len(ts) == 0:
            self._threshold, self._avg_abs_delta = (self.cfg.fixed_usdt, "fixed"), None
            return
        c_ts, c_n, c_side = merge_trades(ts, price * size, side, self.cfg.merge_window_ms)

        if self.cfg.threshold_mode == "fixed":
            self._threshold = (self.cfg.fixed_usdt, "fixed")
        elif len(c_n) < MIN_CLUSTERS_FOR_PERCENTILE:
            self._threshold = (self.cfg.fixed_usdt, "fixed_fallback")
        else:
            self._threshold = (float(np.percentile(c_n, self.cfg.percentile)), "percentile")

        threshold = self._threshold[0]
        span = now_ms - int(ts[0])
        n_buckets = span // BUCKET_MS
        if n_buckets < 12:  # менше години історії — середнє ненадійне
            self._avg_abs_delta = None
            return
        start = now_ms - n_buckets * BUCKET_MS
        m = (c_n >= threshold) & (c_ts >= start)
        idx = ((c_ts[m] - start) // BUCKET_MS).astype(np.int64)
        deltas = np.zeros(n_buckets)
        np.add.at(deltas, np.minimum(idx, n_buckets - 1), c_n[m] * c_side[m])
        self._avg_abs_delta = float(np.mean(np.abs(deltas)))

    def report(self, now_ms: int | None = None, force: bool = False) -> WhaleReport:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        if force or time.monotonic() - self._cached_at > self.refresh_sec or self._cached_len < 0:
            self._refresh_heavy(now_ms)
            self._cached_at = time.monotonic()
            self._cached_len = len(self.store)
        threshold, source = self._threshold

        ts, price, size, side = self.store.arrays()
        windows: dict[int, WindowStats] = {}
        longest = max(self.cfg.windows_min) * 60_000
        lo = int(np.searchsorted(ts, now_ms - longest - self.cfg.merge_window_ms, side="left"))
        t_w, notional_w, side_w = ts[lo:], (price * size)[lo:], side[lo:]
        c_ts, c_n, c_side = merge_trades(t_w, notional_w, side_w, self.cfg.merge_window_ms)
        for minutes in self.cfg.windows_min:
            since = now_ms - minutes * 60_000
            tm = t_w >= since
            cm = c_ts >= since
            whale = cm & (c_n >= threshold)
            buys, sells = whale & (c_side > 0), whale & (c_side < 0)
            windows[minutes] = WindowStats(
                minutes=minutes,
                trades=int(tm.sum()),
                volume_usdt=float(notional_w[tm].sum()),
                cvd_usdt=float((notional_w[tm] * side_w[tm]).sum()),
                whale_buys=int(buys.sum()),
                whale_buy_usdt=float(c_n[buys].sum()),
                whale_sells=int(sells.sum()),
                whale_sell_usdt=float(c_n[sells].sum()),
            )
        history = (now_ms - int(ts[0])) / 60_000 if len(ts) else 0.0
        return WhaleReport(threshold, source, history, windows, self._avg_abs_delta)
