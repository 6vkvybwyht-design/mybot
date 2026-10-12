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


async def run(settings: Settings) -> None:
    # Telegram і фонові задачі з'являться на наступних етапах.
    log.info("Каркас готовий. Робочий режим буде додано на етапі 5.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot", description="Telegram-бот аналітики USOUSDT")
    parser.add_argument("--check-config", action="store_true", help="лише перевірити конфіг і вийти")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Помилка конфігурації: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.config, settings.secrets)
    log.info("Конфіг прочитано:\n%s", _summary(settings))

    missing = settings.secrets.missing()
    if missing:
        log.warning("У .env не заповнено: %s", ", ".join(missing))

    if args.check_config:
        return 0

    asyncio.run(run(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
