"""Фоновий цикл автосповіщень: тригери, паузи, тихий режим, ліміти, фільтр моделлю."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from html import escape

from bot.app import App, Notify
from bot.market.mt5_source import MT5Error
from bot.snapshot import RawMarket
from bot.telegram.format import fmt_dt
from bot.triggers import NAMES_UA, UP, TriggerHit, evaluate_all, spread_warning, strongest_direction

log = logging.getLogger(__name__)


class AlertManager:
    def __init__(self, app: App, notify: Notify, dry_run: bool | None = None):
        self.app = app
        self.cfg = app.cfg
        self.notify = notify
        self.dry_run = self.cfg.alerts.dry_run if dry_run is None else dry_run
        self.last_sent: tuple[str, int] | None = None      # (напрямок, час)
        self.last_filtered_ms: int | None = None
        self.model_calls: deque[int] = deque()
        self.warned_events: set[tuple[str, int]] = set()
        self.spread_alarm = False
        self.last_hits: list[TriggerHit] = []
        self._stop = asyncio.Event()

    # ---------- тригери ----------

    async def compute_hits(self, raw: RawMarket) -> list[TriggerHit]:
        if 5 not in raw.klines:
            return []
        price = self.app.market.price(raw)
        if price is None:
            return []
        return evaluate_all(
            self.cfg,
            k5=raw.klines[5],
            activity=raw.activity,
            price_now=price,
            spread=(raw.tick or {}).get("spread"),
        )

    async def check_spread(self, raw: RawMarket) -> None:
        """Розширення спреду — окреме попередження (один раз на епізод), не сигнал."""
        spread = (raw.tick or {}).get("spread")
        median = (raw.activity or {}).get("spread_median")
        warn = spread_warning(spread, median, self.cfg.alerts.triggers.spread_widen_mult)
        if warn and not self.spread_alarm:
            self.spread_alarm = True
            spec = self.app.market.instrument
            point = spec.point if spec else 0.001
            text = (f"⚠️ Спред {escape(self.cfg.mt5.symbol)} розширився: {warn.spread / point:.0f} п. "
                    f"(×{warn.ratio:.1f} від звичайних {warn.median / point:.0f} п.). "
                    "Угоди зараз дорожчі — це попередження, не сигнал.")
            log.warning("Спред розширено: %s / медіана %s", warn.spread, warn.median)
            if self.app.alerts_enabled and not self.dry_run:
                await self.notify(text)
        elif warn is None and spread is not None and self.spread_alarm:
            self.spread_alarm = False
            log.info("Спред повернувся до звичайного")

    # ---------- рішення ----------

    def _quiet_events(self, now_ms: int):
        now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
        a = self.cfg.alerts
        return now, self.app.calendar.near(now, a.quiet_before_event_min, a.quiet_after_event_min)

    async def quiet_mode(self, now_ms: int) -> bool:
        """Тихий режим біля подій; одне попередження на кожну подію."""
        now, events = self._quiet_events(now_ms)
        if not events:
            return False
        for ev in events:
            key = (ev.name, int(ev.at.timestamp()))
            if key in self.warned_events or not self.app.alerts_enabled:
                continue
            self.warned_events.add(key)
            mins = ev.minutes_from(now)
            when = f"за {mins:.0f} хв" if mins > 0 else f"{-mins:.0f} хв тому"
            await self.notify(
                f"⚠️ {when}: <b>{escape(ev.name)}</b> ({fmt_dt(ev.at, self.app.tz)}). Ризиковано — "
                f"автосигнали призупинено на ±{self.cfg.alerts.quiet_before_event_min} хв."
            )
        return True

    def cooldown_left(self, direction: str, now_ms: int) -> float:
        a = self.cfg.alerts
        left = 0.0
        if self.last_sent:
            last_dir, ts = self.last_sent
            pause = a.cooldown_min if last_dir == direction else a.opposite_cooldown_min
            left = max(left, ts + pause * 60_000 - now_ms)
        if self.last_filtered_ms is not None:
            left = max(left, self.last_filtered_ms + a.filtered_recheck_min * 60_000 - now_ms)
        return left / 1000

    def rate_limited(self, now_ms: int) -> bool:
        while self.model_calls and self.model_calls[0] <= now_ms - 3_600_000:
            self.model_calls.popleft()
        return len(self.model_calls) >= self.cfg.alerts.max_model_calls_per_hour

    async def process(self, hits: list[TriggerHit], now_ms: int) -> str:
        """Повертає короткий підсумок рішення (для логів і тестів)."""
        self.last_hits = hits
        if not self.app.alerts_enabled:
            return "disabled"
        if await self.quiet_mode(now_ms):
            return "quiet"
        direction = strongest_direction(hits, self.app.need_triggers)
        if direction is None:
            return "no_signal"
        if self.cooldown_left(direction, now_ms) > 0:
            log.info("Сигнал %s пропущено: пауза ще %.0f с", direction, self.cooldown_left(direction, now_ms))
            return "cooldown"
        if self.rate_limited(now_ms):
            log.warning("Сигнал %s пропущено: ліміт %d запитів до моделі на годину",
                        direction, self.cfg.alerts.max_model_calls_per_hour)
            return "rate_limited"

        used = [h for h in hits if h.direction == direction]
        trig = [h.to_dict() for h in used]
        log.info("Сигнал %s: %s", direction, "; ".join(f"{h.name}: {h.detail}" for h in used))
        self.model_calls.append(now_ms)
        outcome = await self.app.analyze("alert", triggers=trig, trigger_direction=direction, save=False)
        if outcome.result is None or outcome.snapshot is None:
            log.warning("Автосигнал: аналіз не вдався: %s", outcome.error)
            self.last_filtered_ms = now_ms
            return "analysis_failed"

        data = outcome.result.data
        pct = data["long_pct"] if direction == UP else data["short_pct"]
        if data["action"] == "wait" or pct < self.cfg.alerts.min_model_pct:
            await self.app.save("alert_filtered", outcome.snapshot, outcome.result, trig, direction)
            self.last_filtered_ms = now_ms
            log.info("Сигнал %s відфільтровано моделлю: %s, %d%%", direction, data["action"], pct)
            return "filtered"

        await self.app.save("alert", outcome.snapshot, outcome.result, trig, direction)
        self.last_sent = (direction, now_ms)
        text = self.app.render(outcome, kind="alert", trigger_names=[NAMES_UA.get(h.name, h.name) for h in used])
        if self.dry_run:
            log.info("DRY-RUN, сповіщення не надіслано:\n%s", text)
            return "dry_run"
        await self.notify(text)
        return "sent"

    async def tick(self, now_ms: int | None = None) -> str:
        now_ms = now_ms or int(time.time() * 1000)
        market = self.app.market
        if not market.connected:
            return "no_connection"   # про втрату зв'язку власнику пише MT5Source
        try:
            raw = await market.fetch_raw([5])
        except MT5Error as exc:
            log.warning("Тригери: дані MT5 недоступні: %s", exc)
            return "no_data"
        await self.check_spread(raw)
        return await self.process(await self.compute_hits(raw), now_ms)

    # ---------- цикли ----------

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("Помилка циклу автосповіщень")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.alerts.loop_interval_sec)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
