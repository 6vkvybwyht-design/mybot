import logging
from pathlib import Path

import pytest
import yaml

from bot.config import PROJECT_ROOT, ConfigError, load_config, load_secrets
from bot.logging_setup import RedactSecretsFilter


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


@pytest.fixture
def raw() -> dict:
    return yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))


def test_default_config_loads():
    cfg = load_config()
    assert cfg.mt5.symbol == "USOUSD.s"
    assert cfg.alerts.sensitivity_levels["medium"] == 2
    assert cfg.prompt_path.exists()
    assert all(k == k.lower() for k in cfg.news.keywords)
    assert cfg.calendar.recurring[0].tz == "America/New_York"


def test_unknown_key_rejected(tmp_path, raw):
    raw["mt5"]["simbol"] = "X"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, raw))


def test_sensitivity_above_trigger_count_rejected(tmp_path, raw):
    raw["alerts"]["sensitivity_levels"]["low"] = 4      # напрямкових тригерів лише 3
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, raw))


def test_bad_timezone_rejected(tmp_path, raw):
    raw["display"]["timezone"] = "Mars/Olympus"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, raw))


def test_one_time_event_is_utc(tmp_path, raw):
    raw["calendar"]["one_time"] = [{"name": "ФРС", "at": "2026-10-28 18:00"}]
    cfg = load_config(_write(tmp_path, raw))
    at = cfg.calendar.one_time[0].at
    assert (at.hour, at.utcoffset().total_seconds()) == (18, 0)


def test_secrets_from_env(tmp_path, monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
                 "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_OWNER_ID=12345\nANTHROPIC_MODEL=m\nMT5_LOGIN=4741298\nMT5_PASSWORD=inv\n"
                   "MT5_SERVER=Bybit-Live-6\n", encoding="utf-8")
    s = load_secrets(env)
    assert s.telegram_owner_id == 12345
    assert (s.mt5_login, s.mt5_password.get_secret_value(), s.mt5_server) == (4741298, "inv", "Bybit-Live-6")
    assert "inv" not in repr(s)
    assert set(s.missing()) == {"TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY"}


def test_bad_owner_id(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "abc")
    with pytest.raises(ConfigError):
        load_secrets(tmp_path / "missing.env")


def test_bad_mt5_login(tmp_path, monkeypatch):
    monkeypatch.setenv("MT5_LOGIN", "abc")
    with pytest.raises(ConfigError):
        load_secrets(tmp_path / "missing.env")


def test_secrets_redacted_in_logs():
    f = RedactSecretsFilter(["my-secret"])
    rec = logging.LogRecord("x", logging.INFO, "", 0, "key=%s tok=%s", ("my-secret", "123456789:" + "A" * 35), None)
    f.filter(rec)
    assert "my-secret" not in rec.getMessage()
    assert "AAAA" not in rec.getMessage()
