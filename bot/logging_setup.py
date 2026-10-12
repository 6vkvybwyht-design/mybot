"""Логування у файл з ротацією і в консоль. Секрети маскуються."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot.config import AppConfig, Secrets

# Токен Telegram має вигляд 123456:ABC..., ключ Anthropic — sk-ant-...
_PATTERNS = [
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
]


class RedactSecretsFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s]

    def _redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        for pattern in _PATTERNS:
            text = pattern.sub("***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self._redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def setup_logging(config: AppConfig, secrets: Secrets) -> None:
    log_path: Path = config.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    known = [
        s.get_secret_value()
        for s in (secrets.telegram_bot_token, secrets.anthropic_api_key)
        if s is not None
    ]
    redact = RedactSecretsFilter(known)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=config.logging.max_bytes,
        backupCount=config.logging.backup_count,
        encoding="utf-8",
    )
    console = logging.StreamHandler()
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
        handler.addFilter(redact)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(config.logging.level)
    root.addHandler(file_handler)
    root.addHandler(console)
    # httpx на рівні INFO пише кожен URL — забагато шуму.
    logging.getLogger("httpx").setLevel(logging.WARNING)
