"""Команди і кнопки."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, Message

from bot.app import App
from bot.news.calendar import parse_event_command
from bot.telegram.format import fmt_dt, format_stats, format_status, plural_triggers
from bot.telegram.keyboard import (
    BTN_ALERTS_OFF, BTN_ALERTS_ON, BTN_ANALYZE, BTN_STATS, BTN_STATUS, main_keyboard,
)
from bot.tracking.stats import compute, export_csv

log = logging.getLogger(__name__)

HELP = (
    "Команди:\n"
    "/analyze — аналіз зараз\n"
    "/status — стан бота\n"
    "/stats — статистика точності\n"
    "/alerts on | off — автосповіщення\n"
    "/sensitivity low | medium | high — чутливість тригерів\n"
    "/event YYYY-MM-DD HH:MM назва — додати подію (UTC)\n"
    "/events — найближчі події\n"
    "/export — усі прогнози в CSV"
)


def build_router(app: App) -> Router:
    r = Router(name="main")
    kb = lambda: main_keyboard(app.alerts_enabled)  # noqa: E731

    @r.message(CommandStart())
    async def start(m: Message) -> None:
        await m.answer(
            f"Привіт! Я аналізую <b>{escape(app.cfg.mt5.symbol)}</b> (CFD на нафту WTI, MT5), таймфрейм 5 хв.\n"
            "Відсотки — оцінка моделі, а не гарантія; рішення про угоду завжди за вами.\n\n" + HELP,
            reply_markup=kb(),
        )

    @r.message(Command("analyze"))
    @r.message(F.text == BTN_ANALYZE)
    async def analyze(m: Message) -> None:
        if app.manual_busy:
            await m.answer("⏳ Аналіз уже виконується, зачекайте.")
            return
        app.manual_busy = True
        try:
            msg = await m.answer("⏳ Збираю дані…")
            outcome = await app.analyze("manual")
            await msg.edit_text(app.render(outcome, kind="manual"))
        finally:
            app.manual_busy = False

    @r.message(Command("status"))
    @r.message(F.text == BTN_STATUS)
    async def status(m: Message) -> None:
        market = app.market
        now = datetime.now(timezone.utc)
        ev = app.calendar.next_event(now)
        next_ev = None
        if ev:
            mins = ev.minutes_from(now)
            left = f"{mins:.0f} хв" if mins < 120 else f"{mins / 60:.1f} год"
            next_ev = f"{escape(ev.name)} — {fmt_dt(ev.at, app.tz)} (через {left})"
        last = market.last_tick
        if market.connected:
            try:
                await market.fetch_raw([5], activity=False)
                last = market.last_tick
            except Exception as exc:  # noqa: BLE001
                log.warning("Статус: не вдалося оновити тік: %s", exc)
        await m.answer(
            format_status(
                symbol=app.cfg.mt5.symbol, price=last["bid"] if last else None, tick=app.tick,
                mt5_connected=market.connected, tick_age=last["age_sec"] if last else None,
                mt5_error=market.source.last_error,
                alerts_on=app.alerts_enabled, sensitivity=app.sensitivity, need=app.need_triggers,
                dry_run=app.cfg.alerts.dry_run, next_event=next_ev, tz=app.tz,
                news_ok=bool(app.news and app.news.available), predictions=await app.db.count_predictions(),
                model=app.analyzer.model,
            ),
            reply_markup=kb(),
        )

    @r.message(Command("stats"))
    @r.message(F.text == BTN_STATS)
    async def stats(m: Message) -> None:
        now_ms = int(time.time() * 1000)
        horizons = app.cfg.tracking.horizons_min
        rt = (app.market.last_tick or {}).get("spread_pct") or 0.0
        periods = []
        for title, days in (("7 днів", 7), ("30 днів", 30), ("Весь час", None)):
            since = now_ms - days * 86_400_000 if days else 0
            rows = await app.db.predictions_with_outcomes(since)
            periods.append((title, compute(rows, horizons, rt)))
        await m.answer(format_stats(periods, rt), reply_markup=kb())

    @r.message(Command("export"))
    async def export(m: Message) -> None:
        rows = await app.db.predictions_with_outcomes(0)
        if not rows:
            await m.answer("Прогнозів ще немає.")
            return
        name = f"predictions_{datetime.now(timezone.utc):%Y%m%d_%H%M}.csv"
        await m.answer_document(BufferedInputFile(export_csv(rows, app.cfg.tracking.horizons_min), name),
                                caption=f"Прогнозів: {len(rows)}")

    async def _set_alerts(m: Message, on: bool) -> None:
        await app.set_alerts(on)
        extra = " (dry-run: сигнали лише логуються)" if on and app.cfg.alerts.dry_run else ""
        await m.answer(f"Автосповіщення {'увімкнено' if on else 'вимкнено'}{extra}.", reply_markup=kb())

    @r.message(Command("alerts"))
    async def alerts_cmd(m: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip().lower()
        if arg not in ("on", "off"):
            await m.answer(f"Використання: /alerts on | off (зараз {'on' if app.alerts_enabled else 'off'})")
            return
        await _set_alerts(m, arg == "on")

    @r.message(F.text.in_({BTN_ALERTS_ON, BTN_ALERTS_OFF}))
    async def alerts_toggle(m: Message) -> None:
        await _set_alerts(m, not app.alerts_enabled)

    @r.message(Command("sensitivity"))
    async def sensitivity(m: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip().lower()
        levels = app.cfg.alerts.sensitivity_levels
        if arg not in levels:
            await m.answer(
                f"Використання: /sensitivity low | medium | high\nЗараз: {app.sensitivity} "
                f"(потрібно тригерів: {app.need_triggers})"
            )
            return
        await app.set_sensitivity(arg)
        await m.answer(f"Чутливість: {arg} — потрібно {plural_triggers(levels[arg])} в одному напрямку.")

    @r.message(Command("event"))
    async def add_event(m: Message) -> None:
        try:
            ev = parse_event_command(m.text or "")
        except ValueError as exc:
            await m.answer(f"Не вдалося додати подію: {escape(str(exc))}\nПриклад: /event 2026-10-28 18:00 Рішення ФРС")
            return
        if ev.at < datetime.now(timezone.utc):
            await m.answer("Ця дата вже минула. Час вказується в UTC.")
            return
        await app.add_event(ev)
        await m.answer(f"Додано: <b>{escape(ev.name)}</b> — {fmt_dt(ev.at, 'UTC')}"
                       + (f" ({fmt_dt(ev.at, app.tz)})" if app.tz != "UTC" else ""))

    @r.message(Command("events"))
    async def events(m: Message) -> None:
        now = datetime.now(timezone.utc)
        upcoming = app.calendar.upcoming(now, hours=24 * 7, limit=15)
        if not upcoming:
            await m.answer("Найближчим тижнем подій немає.")
            return
        lines = ["<b>Найближчі події</b>"] + [f"• {fmt_dt(e.at, app.tz)} — {escape(e.name)}" for e in upcoming]
        await m.answer("\n".join(lines))

    @r.message()
    async def fallback(m: Message) -> None:
        await m.answer(HELP, reply_markup=kb())

    return r
