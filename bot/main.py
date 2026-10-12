"""Точка входу: python -m bot.main"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from bot.config import ConfigError, Settings, load_settings
from bot.logging_setup import setup_logging

log = logging.getLogger("bot")


def _summary(settings: Settings) -> str:
    c, s = settings.config, settings.secrets
    n_events = len(c.calendar.recurring) + len(c.calendar.one_time)
    return "\n".join(
        [
            f"Символ: {c.market.symbol} ({c.market.category}), комісія taker {c.market.taker_fee_pct}%",
            f"Кити: {c.whales.threshold_mode}, чутливість: {c.alerts.sensitivity} "
            f"(N={c.alerts.sensitivity_levels[c.alerts.sensitivity]})",
            f"RSS-стрічок: {len(c.news.feeds)}, ключових слів: {len(c.news.keywords)}, подій у календарі: {n_events}",
            f"База: {c.db_path}",
            f"Лог: {c.log_path}",
            f"Модель: {s.anthropic_model or '—'}",
            f"Власник: {'задано' if s.telegram_owner_id else '—'}",
        ]
    )


async def _start_market(market, notify) -> None:
    """Bybit може бути недоступним при старті — пробуємо, доки не вийде, а бот тим часом працює."""
    from bot.market.bybit_rest import BybitError

    delay, notified = 5.0, False
    while True:
        try:
            await market.start()
            if notified:
                await notify("✅ Дані Bybit знову доступні.")
            return
        except (BybitError, OSError) as exc:
            log.error("Bybit недоступний при старті: %s; повтор через %.0f с", exc, delay)
            if not notified:
                notified = True
                await notify(f"⚠️ Не вдається отримати дані Bybit: {exc}. Пробую далі.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


class FatalError(Exception):
    pass


async def _wait_for_telegram(bot) -> None:
    """Після перезавантаження сервера мережа може з'явитися не одразу — чекаємо, а не падаємо."""
    from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

    delay = 2.0
    while True:
        try:
            me = await bot.get_me()
            log.info("Telegram: бот @%s", me.username)
            return
        except TelegramUnauthorizedError as exc:
            raise FatalError("TELEGRAM_BOT_TOKEN невірний (Telegram відповів Unauthorized)") from exc
        except TelegramNetworkError as exc:
            log.warning("Telegram недоступний: %s; повтор через %.0f с", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def run(settings: Settings, dry_run: bool = False) -> None:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    from bot.alerts import AlertManager
    from bot.analysis import Analyzer
    from bot.app import App
    from bot.news.calendar import EventCalendar
    from bot.news.rss import NewsService
    from bot.snapshot import MarketService
    from bot.telegram.handlers import build_router
    from bot.telegram.keyboard import main_keyboard
    from bot.telegram.middleware import OwnerOnlyMiddleware
    from bot.tracking.db import Database
    from bot.tracking.outcomes import OutcomeChecker

    cfg, secrets = settings.config, settings.secrets
    assert secrets.telegram_bot_token and secrets.telegram_owner_id
    owner = secrets.telegram_owner_id

    bot = Bot(secrets.telegram_bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await _wait_for_telegram(bot)
    except BaseException:
        await bot.session.close()
        raise

    db = Database(cfg.db_path)
    await db.connect()
    market = MarketService(cfg)
    news = NewsService(cfg.news, cfg.network)
    app = App(cfg, db=db, market=market, news=news, calendar=EventCalendar(cfg.calendar),
              analyzer=Analyzer(cfg, secrets))
    await app.load_state()

    dp = Dispatcher()
    dp.update.outer_middleware(OwnerOnlyMiddleware(owner))
    dp.include_router(build_router(app))

    async def notify(text: str) -> None:
        try:
            await bot.send_message(owner, text)
        except Exception:  # noqa: BLE001 — збій Telegram не має валити фонові задачі
            log.exception("Не вдалося надіслати повідомлення власнику")

    alerts = AlertManager(app, notify, dry_run=dry_run or None)
    checker = OutcomeChecker(db, market.rest, cfg.tracking.horizons_min)
    tasks = [
        asyncio.create_task(_start_market(market, notify), name="market-start"),
        asyncio.create_task(news.run(), name="news"),
        asyncio.create_task(checker.run(), name="outcomes"),
        asyncio.create_task(alerts.run(), name="alerts"),
    ]

    async def on_startup() -> None:
        mode = " (dry-run)" if alerts.dry_run else ""
        try:
            await bot.send_message(
                owner,
                f"✅ Бот запущено{mode}. Сповіщення {'увімкнені' if app.alerts_enabled else 'вимкнені'}, "
                f"чутливість {app.sensitivity}.",
                reply_markup=main_keyboard(app.alerts_enabled),
            )
        except Exception:  # noqa: BLE001
            log.exception("Не вдалося надіслати повідомлення про запуск — перевірте TELEGRAM_OWNER_ID "
                          "і чи ви написали боту /start")

    dp.startup.register(on_startup)
    log.info("Запуск Telegram-бота (alerts dry_run=%s)", alerts.dry_run)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("Зупинка…")
        news.stop()
        checker.stop()
        alerts.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await market.stop()
        await news.close()
        await db.close()
        await bot.session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot", description="Telegram-бот аналітики USOUSDT")
    parser.add_argument("--check-config", action="store_true", help="лише перевірити конфіг і вийти")
    parser.add_argument("--dry-run", action="store_true", help="автосигнали лише логуються, не надсилаються")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Помилка конфігурації: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.config, settings.secrets)
    log.info("Конфіг прочитано:\n%s", _summary(settings))

    missing = settings.secrets.missing()
    if args.check_config:
        if missing:
            log.warning("У .env не заповнено: %s", ", ".join(missing))
        return 0
    if missing:
        log.error("У .env не заповнено: %s — бот не може стартувати", ", ".join(missing))
        return 2

    try:
        asyncio.run(run(settings, dry_run=args.dry_run))
    except KeyboardInterrupt:
        pass
    except FatalError as exc:
        log.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
