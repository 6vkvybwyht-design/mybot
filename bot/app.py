"""Ядро застосунку: стан, знімок + аналіз + збереження. Спільне для кнопки і автосповіщень."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from bot.analysis import AnalysisError, AnalysisResult, Analyzer
from bot.config import AppConfig
from bot.market.hours import CLOSED
from bot.market.mt5_source import MT5Error, MT5Unavailable
from bot.news.calendar import Event, EventCalendar
from bot.news.rss import NewsService
from bot.snapshot import MarketService, Snapshot, SnapshotError, collect
from bot.telegram.format import format_analysis, format_failure, format_market_closed
from bot.tracking.db import Database

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]


@dataclass
class AnalysisOutcome:
    snapshot: Snapshot | None
    result: AnalysisResult | None
    error: str | None
    prediction_id: int | None = None
    market_closed: bool = False


class App:
    def __init__(
        self,
        cfg: AppConfig,
        *,
        db: Database,
        market: MarketService,
        news: NewsService | None,
        calendar: EventCalendar,
        analyzer: Analyzer,
    ):
        self.cfg = cfg
        self.db = db
        self.market = market
        self.news = news
        self.calendar = calendar
        self.analyzer = analyzer
        self.alerts_enabled = cfg.alerts.enabled_by_default
        self.sensitivity = cfg.alerts.sensitivity
        self.manual_busy = False
        self.started_at = time.time()

    @property
    def tz(self) -> str:
        return self.cfg.display.timezone

    @property
    def tick(self) -> float | None:
        return self.market.instrument.tick_size if self.market.instrument else None

    @property
    def need_triggers(self) -> int:
        return self.cfg.alerts.sensitivity_levels[self.sensitivity]

    async def load_state(self) -> None:
        self.alerts_enabled = bool(await self.db.get_setting("alerts_enabled", self.alerts_enabled))
        sens = await self.db.get_setting("sensitivity", self.sensitivity)
        if sens in self.cfg.alerts.sensitivity_levels:
            self.sensitivity = sens
        for ev in await self.db.load_events(since_ms=int((time.time() - 86_400) * 1000)):
            self.calendar.add(ev)

    async def set_alerts(self, enabled: bool) -> None:
        self.alerts_enabled = enabled
        await self.db.set_setting("alerts_enabled", enabled)

    async def set_sensitivity(self, level: str) -> None:
        if level not in self.cfg.alerts.sensitivity_levels:
            raise ValueError(level)
        self.sensitivity = level
        await self.db.set_setting("sensitivity", level)

    async def add_event(self, event: Event) -> None:
        await self.db.add_event(event, int(time.time() * 1000))
        self.calendar.add(event)

    async def make_snapshot(self) -> Snapshot:
        return await collect(self.cfg, self.market, self.news, self.calendar)

    async def analyze(
        self,
        source: str,
        *,
        snapshot: Snapshot | None = None,
        triggers: list[dict] | None = None,
        trigger_direction: str | None = None,
        save: bool = True,
    ) -> AnalysisOutcome:
        """Знімок -> модель -> (за потреби) запис у базу. Помилки не кидаються, а повертаються."""
        if snapshot is None:
            state = await self.market.state()
            if state.status == CLOSED:
                return AnalysisOutcome(None, None, state.reason, market_closed=True)
        try:
            snapshot = snapshot or await self.make_snapshot()
        except SnapshotError as exc:
            return AnalysisOutcome(None, None, f"Не вдалося зібрати дані: {exc}")
        except MT5Unavailable as exc:
            return AnalysisOutcome(None, None, f"Немає зв'язку з MT5: {exc}")
        except MT5Error as exc:
            log.warning("Дані MT5: %s", exc)
            return AnalysisOutcome(None, None, f"Не вдалося отримати дані MT5: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Помилка збору знімка")
            return AnalysisOutcome(None, None, f"Не вдалося зібрати дані: {exc}")
        try:
            result = await self.analyzer.analyze(snapshot.data, snapshot.price)
        except AnalysisError as exc:
            log.warning("Аналіз не вдався: %s", exc)
            return AnalysisOutcome(snapshot, None, f"Помилка аналізу: {exc}")
        self._ensure_event_flag(snapshot, result)
        if snapshot.spread_warning and not any("спред" in f.lower() for f in result.data["risk_flags"]):
            result.data["risk_flags"].append(snapshot.spread_warning)
        pred_id = None
        if save:
            pred_id = await self.save(source, snapshot, result, triggers, trigger_direction)
        return AnalysisOutcome(snapshot, result, None, pred_id)

    def _ensure_event_flag(self, snapshot: Snapshot, result: AnalysisResult) -> None:
        """Попередження про подію поруч обов'язкове — додаємо, якщо модель його пропустила."""
        now = datetime.fromtimestamp(snapshot.ts_ms / 1000, timezone.utc)
        warn = self.cfg.analysis.event_warning_min
        flags = result.data.setdefault("risk_flags", [])
        for ev in self.calendar.near(now, warn, warn):
            if not any(ev.name.lower() in f.lower() for f in flags):
                mins = ev.minutes_from(now)
                when = f"через {mins:.0f} хв" if mins >= 0 else f"{-mins:.0f} хв тому"
                flags.append(f"{ev.name} {when}")

    async def save(
        self, source: str, snapshot: Snapshot, result: AnalysisResult,
        triggers: list[dict] | None = None, trigger_direction: str | None = None,
    ) -> int:
        return await self.db.insert_prediction(
            created_ms=snapshot.ts_ms,
            source=source,
            price=snapshot.price,
            response=result.data,
            snapshot=snapshot.data,
            triggers=triggers,
            trigger_direction=trigger_direction,
            baseline_direction=snapshot.baseline_direction,
            prompt_version=result.prompt_version,
            model=result.model,
            cost_pct=snapshot.cost_pct,
        )

    def render(self, outcome: AnalysisOutcome, kind: str = "manual", trigger_names: list[str] | None = None) -> str:
        symbol = self.cfg.mt5.symbol
        if outcome.market_closed:
            return format_market_closed(symbol, outcome.error)
        if outcome.result is None or outcome.snapshot is None:
            snap = outcome.snapshot
            return format_failure(
                outcome.error or "невідома помилка", snap.data if snap else None,
                symbol=symbol, tz=self.tz, tick=self.tick, ts_ms=snap.ts_ms if snap else None,
            )
        return format_analysis(
            outcome.result.data, symbol=symbol, ts_ms=outcome.snapshot.ts_ms, tz=self.tz, tick=self.tick,
            kind=kind, triggers=trigger_names, missing=outcome.snapshot.missing,
        )
