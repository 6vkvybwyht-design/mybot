"""Години роботи ринку: розклад сесії і «свіжість» тіків."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from bot.config import SessionConfig

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAYS_UA = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")
OPEN, CLOSED, NO_CONNECTION = "open", "closed", "no_connection"


@dataclass(frozen=True)
class MarketState:
    status: str                        # open | closed | no_connection
    reason: str | None = None
    tick: dict[str, Any] | None = None

    @property
    def is_open(self) -> bool:
        return self.status == OPEN


def _week_minute(weekday: str, t: time) -> int:
    return WEEKDAYS.index(weekday) * 1440 + t.hour * 60 + t.minute


def _in_range(x: int, start: int, end: int) -> bool:
    """x у [start, end) з урахуванням переходу через кінець циклу."""
    return start <= x < end if start <= end else x >= start or x < end


def session_closed_reason(cfg: SessionConfig, now_utc: datetime) -> str | None:
    """None, якщо за розкладом сесія відкрита; інакше — пояснення українською."""
    local = now_utc.astimezone(ZoneInfo(cfg.tz))
    minute = local.weekday() * 1440 + local.hour * 60 + local.minute
    wo, wc = cfg.week_open, cfg.week_close
    city = cfg.tz.split("/")[-1].replace("_", " ")
    start, end = _week_minute(wo.weekday, wo.time), _week_minute(wc.weekday, wc.time)
    if start != end and not _in_range(minute, start, end):   # однаковий час відкриття і закриття — без вихідних
        return (f"вихідні: торги з {WEEKDAYS_UA[WEEKDAYS.index(wc.weekday)]} {wc.time:%H:%M} "
                f"до {WEEKDAYS_UA[WEEKDAYS.index(wo.weekday)]} {wo.time:%H:%M} ({city}) не йдуть")
    if cfg.daily_break:
        b_start, b_end = cfg.daily_break
        day_min = local.hour * 60 + local.minute
        if _in_range(day_min, b_start.hour * 60 + b_start.minute, b_end.hour * 60 + b_end.minute):
            return f"щоденна перерва {b_start:%H:%M}–{b_end:%H:%M} ({city})"
    return None


def stale_ticks_reason(tick_age_sec: float | None, limit_sec: int) -> str | None:
    if tick_age_sec is None:
        return "немає жодного тіка"
    if tick_age_sec > limit_sec:
        mins = tick_age_sec / 60
        return f"нових тіків немає {mins:.0f} хв" if mins >= 2 else f"нових тіків немає {tick_age_sec:.0f} с"
    return None
