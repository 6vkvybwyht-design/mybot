"""Календар подій: регулярні (з конфігу) і разові (з конфігу та з Телеграму)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.config import CalendarConfig, RecurringEvent

UTC = timezone.utc
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class Event:
    name: str
    at: datetime        # UTC
    source: str = "config"   # config | recurring | user

    def minutes_from(self, now: datetime) -> float:
        return (self.at - now).total_seconds() / 60


def _occurrences(ev: RecurringEvent, start: datetime, end: datetime) -> list[datetime]:
    """Усі настання регулярної події між start і end (UTC). Літній час враховується через tz."""
    tz = ZoneInfo(ev.tz)
    day = start.astimezone(tz).date() - timedelta(days=1)
    last_day = end.astimezone(tz).date() + timedelta(days=1)
    out = []
    while day <= last_day:
        if WEEKDAYS[day.weekday()] == ev.weekday:
            at = datetime.combine(day, ev.time, tzinfo=tz).astimezone(UTC)
            if start <= at <= end:
                out.append(at)
        day += timedelta(days=1)
    return out


class EventCalendar:
    def __init__(self, cfg: CalendarConfig, user_events: list[Event] | None = None):
        self.cfg = cfg
        self.user_events: list[Event] = list(user_events or [])

    def add(self, event: Event) -> None:
        self.user_events.append(event)

    def remove_past(self, now: datetime, keep: timedelta = timedelta(days=1)) -> None:
        self.user_events = [e for e in self.user_events if e.at >= now - keep]

    def between(self, start: datetime, end: datetime) -> list[Event]:
        events = [Event(e.name, e.at, "config") for e in self.cfg.one_time if start <= e.at <= end]
        events += [e for e in self.user_events if start <= e.at <= end]
        for rec in self.cfg.recurring:
            events += [Event(rec.name, at, "recurring") for at in _occurrences(rec, start, end)]
        return sorted(events, key=lambda e: e.at)

    def upcoming(self, now: datetime, hours: float = 24 * 7, limit: int | None = None) -> list[Event]:
        events = self.between(now, now + timedelta(hours=hours))
        return events[:limit] if limit else events

    def next_event(self, now: datetime) -> Event | None:
        ev = self.upcoming(now, hours=24 * 8, limit=1)
        return ev[0] if ev else None

    def near(self, now: datetime, before_min: float, after_min: float) -> list[Event]:
        """Події, до яких менше before_min хв або які були менше after_min хв тому."""
        return self.between(now - timedelta(minutes=after_min), now + timedelta(minutes=before_min))

    def snapshot(self, now: datetime, warn_min: int) -> dict:
        """Блок подій для знімку ринку."""
        upcoming = self.upcoming(now, hours=24, limit=5)
        recent = self.between(now - timedelta(minutes=60), now)
        return {
            "upcoming_24h": [{"name": e.name, "in_min": round(e.minutes_from(now))} for e in upcoming],
            "recent_60m": [{"name": e.name, "min_ago": round(-e.minutes_from(now))} for e in recent],
            "event_risk_window": bool(self.near(now, warn_min, warn_min)),
        }


def parse_event_command(text: str) -> Event:
    """'/event 2026-10-28 18:00 Рішення ФРС' -> Event (UTC)."""
    parts = text.split(maxsplit=3)
    if len(parts) < 4:
        raise ValueError("формат: /event YYYY-MM-DD HH:MM назва")
    _, date_s, time_s, name = parts
    at = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    name = name.strip()
    if not name:
        raise ValueError("назва події порожня")
    return Event(name[:200], at, "user")
