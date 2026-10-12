"""Дешеві тригери для автосповіщень (без звернення до моделі)."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

import pandas as pd

from bot.config import AppConfig, TriggersConfig
from bot.market import indicators as ind
from bot.market.orderbook import BookMetrics
from bot.market.whales import WhaleReport

log = logging.getLogger(__name__)

UP, DOWN = "up", "down"
NAMES_UA = {
    "whales": "кити",
    "volume": "обсяг",
    "open_interest": "open interest",
    "orderbook": "стакан",
    "breakout": "пробій рівня",
    "impulse": "імпульс",
}


@dataclass(frozen=True)
class TriggerHit:
    name: str
    direction: str     # up | down
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def _sign(x: float) -> str | None:
    return UP if x > 0 else DOWN if x < 0 else None


def whales_trigger(report: WhaleReport, mult: float) -> TriggerHit | None:
    w5 = report.windows.get(5)
    avg = report.avg_abs_whale_delta_5m
    if w5 is None or not avg:
        return None
    delta = w5.whale_delta_usdt
    if abs(delta) > mult * avg and _sign(delta):
        return TriggerHit("whales", _sign(delta), f"дельта китів 5 хв {delta:+,.0f} USDT (×{abs(delta) / avg:.1f})")
    return None


def volume_trigger(k5: pd.DataFrame, mult: float, period: int) -> TriggerHit | None:
    closed, current = k5[k5["closed"]], k5[~k5["closed"]]
    if current.empty or len(closed) < period:
        return None
    cur = current.iloc[-1]
    avg = float(closed["volume"].tail(period).mean())
    d = _sign(float(cur["close"]) - float(cur["open"]))
    if avg > 0 and cur["volume"] > mult * avg and d:
        return TriggerHit("volume", d, f"обсяг свічки ×{cur['volume'] / avg:.1f} від середнього")
    return None


def oi_trigger(oi: pd.DataFrame | None, price_now: float, price_15m_ago: float | None, pct: float) -> TriggerHit | None:
    if oi is None or len(oi) < 4 or not price_15m_ago:
        return None
    old, new = float(oi["oi"].iloc[-4]), float(oi["oi"].iloc[-1])
    if old <= 0:
        return None
    change = (new - old) / old * 100
    d = _sign(price_now - price_15m_ago)
    if abs(change) > pct and d:
        return TriggerHit("open_interest", d, f"OI {change:+.2f}% за 15 хв, ціна {'↑' if d == UP else '↓'}")
    return None


def breakout_trigger(k5: pd.DataFrame, lookback: tuple[int, int], width: int) -> TriggerHit | None:
    """Остання закрита 5-хв свічка закрилася за рівнем, побудованим без неї."""
    closed = k5[k5["closed"]].reset_index(drop=True)
    if len(closed) < 2 * width + 3:
        return None
    last, prev = closed.iloc[-1], closed.iloc[-2]
    history = closed.iloc[:-1]
    levels = ind.nearest_levels(history, float(prev["close"]), lookback, width)
    if levels.resistance and prev["close"] <= levels.resistance < last["close"]:
        return TriggerHit("breakout", UP, f"закриття {last['close']} вище опору {levels.resistance}")
    if levels.support and prev["close"] >= levels.support > last["close"]:
        return TriggerHit("breakout", DOWN, f"закриття {last['close']} нижче підтримки {levels.support}")
    return None


def impulse_trigger(price_now: float, price_1m_ago: float | None, atr5: float | None, mult: float) -> TriggerHit | None:
    if not price_1m_ago or not atr5:
        return None
    move = price_now - price_1m_ago
    if abs(move) > mult * atr5 and _sign(move):
        return TriggerHit("impulse", _sign(move), f"рух за 1 хв {move:+.4f} (×{abs(move) / atr5:.1f} ATR)")
    return None


class OrderbookTracker:
    """Дисбаланс стакану має триматися щонайменше hold_sec."""

    def __init__(self, threshold: float, hold_sec: float):
        self.threshold = threshold
        self.hold_ms = hold_sec * 1000
        self.side: str | None = None
        self.since_ms: int | None = None
        self.last_share: float | None = None

    def update(self, metrics: BookMetrics | None, now_ms: int) -> None:
        if metrics is None:
            self.side = self.since_ms = None
            return
        share = metrics.imbalance
        self.last_share = share
        side = UP if share >= self.threshold else DOWN if share <= 1 - self.threshold else None
        if side != self.side:
            self.side, self.since_ms = side, (now_ms if side else None)

    def hit(self, now_ms: int) -> TriggerHit | None:
        if self.side and self.since_ms is not None and now_ms - self.since_ms >= self.hold_ms:
            bid = self.last_share or 0
            return TriggerHit(
                "orderbook", self.side,
                f"bid/ask {bid * 100:.0f}/{(1 - bid) * 100:.0f} тримається {(now_ms - self.since_ms) / 1000:.0f} с",
            )
        return None


def evaluate_all(
    cfg: AppConfig,
    *,
    k5: pd.DataFrame,
    whales: WhaleReport | None,
    oi: pd.DataFrame | None,
    price_now: float,
    price_1m_ago: float | None,
    price_15m_ago: float | None,
    book: OrderbookTracker,
    now_ms: int,
) -> list[TriggerHit]:
    t: TriggersConfig = cfg.alerts.triggers
    closed = k5[k5["closed"]]
    atr5 = float(ind.atr(closed, cfg.indicators.atr_period).iloc[-1]) if len(closed) > cfg.indicators.atr_period else None
    hits = [
        whales_trigger(whales, t.whales_delta_mult) if whales else None,
        volume_trigger(k5, t.volume_mult, cfg.indicators.volume_avg_period),
        oi_trigger(oi, price_now, price_15m_ago, t.oi_change_pct_15m),
        book.hit(now_ms),
        breakout_trigger(k5, cfg.indicators.levels_lookback_hours, cfg.indicators.levels_pivot_width),
        impulse_trigger(price_now, price_1m_ago, atr5, t.impulse_atr_mult),
    ]
    return [h for h in hits if h is not None]


def strongest_direction(hits: list[TriggerHit], need: int) -> str | None:
    up = sum(h.direction == UP for h in hits)
    down = sum(h.direction == DOWN for h in hits)
    if up >= need and up > down:
        return UP
    if down >= need and down > up:
        return DOWN
    return None
