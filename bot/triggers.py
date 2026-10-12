"""Дешеві тригери для автосповіщень (без звернення до моделі).

Для CFD у MT5 немає стрічки угод, стакану, OI і funding, тому тригери будуються лише
на свічках і тіках: сплеск тікового обсягу, імпульс за ATR, пробій рівня.
Розширення спреду — окреме попередження, напрямку воно не дає.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from bot.config import AppConfig, TriggersConfig
from bot.market import indicators as ind

log = logging.getLogger(__name__)

UP, DOWN = "up", "down"
NAMES_UA = {
    "tick_volume": "тіковий обсяг",
    "impulse": "імпульс",
    "breakout": "пробій рівня",
    # Тригери Bybit-версії — щоб статистика старих прогнозів лишалася читабельною.
    "whales": "кити",
    "volume": "обсяг",
    "open_interest": "open interest",
    "orderbook": "стакан",
}


@dataclass(frozen=True)
class TriggerHit:
    name: str
    direction: str     # up | down
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SpreadWarning:
    spread: float
    median: float
    ratio: float

    @property
    def text(self) -> str:
        return f"спред розширено ×{self.ratio:.1f} від звичайного"


def _sign(x: float) -> str | None:
    return UP if x > 0 else DOWN if x < 0 else None


def tick_volume_trigger(activity: dict[str, Any] | None, price_now: float, mult: float) -> TriggerHit | None:
    """Тіків за 5 хв більше за N × звичайний темп; напрямок — куди пішла ціна за ці 5 хв."""
    if not activity:
        return None
    ratio, p5 = activity.get("ratio_5m"), activity.get("price_5m_ago")
    if ratio is None or p5 is None:
        return None
    d = _sign(price_now - p5)
    if ratio > mult and d:
        return TriggerHit(
            "tick_volume", d,
            f"тіків за 5 хв ×{ratio:.1f} від звичайного, ціна {'↑' if d == UP else '↓'} {price_now - p5:+.3f}",
        )
    return None


def breakout_trigger(
    k5: pd.DataFrame, lookback: tuple[int, int], width: int, margin: float = 0.0
) -> TriggerHit | None:
    """Остання закрита 5-хв свічка закрилася за рівнем (побудованим без неї) щонайменше на margin."""
    closed = k5[k5["closed"]].reset_index(drop=True)
    if len(closed) < 2 * width + 3:
        return None
    last, prev = closed.iloc[-1], closed.iloc[-2]
    history = closed.iloc[:-1]
    levels = ind.nearest_levels(history, float(prev["close"]), lookback, width)
    if levels.resistance and prev["close"] <= levels.resistance and last["close"] > levels.resistance + margin:
        return TriggerHit("breakout", UP, f"закриття {last['close']} вище опору {levels.resistance}")
    if levels.support and prev["close"] >= levels.support and last["close"] < levels.support - margin:
        return TriggerHit("breakout", DOWN, f"закриття {last['close']} нижче підтримки {levels.support}")
    return None


def impulse_trigger(price_now: float, price_1m_ago: float | None, atr5: float | None, mult: float) -> TriggerHit | None:
    if not price_1m_ago or not atr5:
        return None
    move = price_now - price_1m_ago
    if abs(move) > mult * atr5 and _sign(move):
        return TriggerHit("impulse", _sign(move), f"рух за 1 хв {move:+.3f} (×{abs(move) / atr5:.1f} ATR)")
    return None


def spread_warning(spread: float | None, median: float | None, mult: float) -> SpreadWarning | None:
    """Спред різко ширший за звичайний (медіана за годину) — угоди дорожчі, ринок нервовий."""
    if spread is None or not median:
        return None
    ratio = spread / median
    return SpreadWarning(spread, median, round(ratio, 2)) if ratio > mult else None


def evaluate_all(
    cfg: AppConfig,
    *,
    k5: pd.DataFrame,
    activity: dict[str, Any] | None,
    price_now: float,
    spread: float | None,
) -> list[TriggerHit]:
    t: TriggersConfig = cfg.alerts.triggers
    closed = k5[k5["closed"]]
    atr5 = float(ind.atr(closed, cfg.indicators.atr_period).iloc[-1]) if len(closed) > cfg.indicators.atr_period else None
    hits = [
        tick_volume_trigger(activity, price_now, t.tick_volume_mult),
        impulse_trigger(price_now, (activity or {}).get("price_1m_ago"), atr5, t.impulse_atr_mult),
        breakout_trigger(k5, cfg.indicators.levels_lookback_hours, cfg.indicators.levels_pivot_width,
                         t.breakout_spread_mult * (spread or 0.0)),
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
