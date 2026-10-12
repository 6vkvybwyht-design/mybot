"""Завантаження конфігурації: секрети з .env, решта з config.yaml."""

from __future__ import annotations

import os
from datetime import datetime, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Sensitivity = Literal["low", "medium", "high"]


class _Model(BaseModel):
    # Невідомий ключ у config.yaml — найчастіше одруківка, тому це помилка.
    model_config = ConfigDict(extra="forbid")


def _check_tz(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"невідомий часовий пояс: {value}") from exc
    return value


class WeeklyTime(_Model):
    weekday: Weekday
    time: time


class SessionConfig(_Model):
    """Розклад торгів у поясі tz (літній/зимовий час враховується автоматично)."""
    tz: str
    week_open: WeeklyTime
    week_close: WeeklyTime
    daily_break: tuple[time, time] | None = None

    @field_validator("tz")
    @classmethod
    def _tz(cls, v: str) -> str:
        return _check_tz(v)


class MarketConfig(_Model):
    # Комісія брокера за одну сторону угоди, % (0 — якщо брокер бере лише спред)
    commission_pct: float = Field(ge=0, lt=1, default=0.0)
    # Нових тіків немає довше — вважаємо ринок закритим (на відкритому ринку ~30 тіків за хвилину)
    no_ticks_closed_sec: int = Field(ge=10)
    session: SessionConfig


MT5_TIMEFRAMES = (1, 5, 15, 60)


class MT5Config(_Model):
    symbol: str
    symbol_search: str = "*USO*"
    timeframes: list[int] = list(MT5_TIMEFRAMES)
    bars: int = Field(ge=50, le=5000, default=200)
    # Час сервера MT5 зазвичай не UTC. auto — визначити за свіжим тіком; або число годин, напр. 3
    server_utc_offset_hours: float | Literal["auto"] = "auto"
    # Якщо auto не спрацювало (ринок закритий на старті) — до першого свіжого тіка
    server_utc_offset_fallback_hours: float = Field(ge=-14, le=14, default=3)
    tick_window_min: int = Field(ge=10, le=24 * 60, default=60)
    tick_max_count: int = Field(gt=0, default=200_000)
    init_timeout_ms: int = Field(gt=0, default=60_000)
    health_check_sec: int = Field(ge=1, default=15)
    reconnect_max_sec: int = Field(ge=1, default=300)

    @field_validator("timeframes")
    @classmethod
    def _tf(cls, v: list[int]) -> list[int]:
        bad = [x for x in v if x not in MT5_TIMEFRAMES]
        if bad:
            raise ValueError(f"непідтримувані таймфрейми {bad}, можна: {list(MT5_TIMEFRAMES)}")
        if 5 not in v:
            raise ValueError("таймфрейм 5 обов'язковий — на ньому рахуються індикатори")
        return v


class NetworkConfig(_Model):
    timeout_sec: float = Field(gt=0)
    retries: int = Field(ge=0, le=10)
    backoff_base_sec: float = Field(gt=0)


class WhalesConfig(_Model):
    """Аналіз китів зі стрічки угод. Для CFD у MT5 стрічки немає — зараз не використовується."""

    threshold_mode: Literal["percentile", "fixed"]
    percentile: float = Field(gt=50, lt=100)
    fixed_usdt: float = Field(gt=0)
    merge_window_ms: int = Field(ge=0)
    windows_min: list[int]


class IndicatorsConfig(_Model):
    rsi_period: int = Field(gt=1)
    ema_periods: list[int]
    atr_period: int = Field(gt=1)
    volume_avg_period: int = Field(gt=1)
    levels_lookback_hours: tuple[int, int]
    levels_pivot_width: int = Field(ge=1)
    trend_ema_fast: int = Field(gt=1)
    trend_ema_slow: int = Field(gt=1)


class NewsFeed(_Model):
    name: str
    url: str


class NewsConfig(_Model):
    poll_interval_sec: int = Field(ge=30)
    max_age_min: int = Field(gt=0)
    max_headlines: int = Field(gt=0)
    feeds: list[NewsFeed]
    keywords: list[str]

    @field_validator("keywords")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [k.strip().lower() for k in v if k.strip()]


class AnalysisConfig(_Model):
    timeout_sec: float = Field(gt=0)
    max_tokens: int = Field(gt=0)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"
    refusal_fallback: bool = True
    prompt_file: str
    max_pct_without_confirmation: int = Field(gt=50, le=100)
    event_warning_min: int = Field(ge=0)


DIRECTIONAL_TRIGGERS = 3   # тіковий обсяг, імпульс, пробій (спред — лише попередження)


class TriggersConfig(_Model):
    tick_volume_mult: float = Field(gt=0)        # тіків за 5 хв > N × звичайний темп
    impulse_atr_mult: float = Field(gt=0)        # рух за 1 хв > N × ATR(14) на 5 хв
    breakout_spread_mult: float = Field(ge=0)    # закриття за рівнем щонайменше на N спредів
    spread_widen_mult: float = Field(gt=1)       # спред > N × медіана за годину — попередження


class AlertsConfig(_Model):
    enabled_by_default: bool
    dry_run: bool
    loop_interval_sec: int = Field(ge=5)
    sensitivity: Sensitivity
    sensitivity_levels: dict[Sensitivity, int]
    cooldown_min: int = Field(ge=0)
    opposite_cooldown_min: int = Field(ge=0)
    min_model_pct: int = Field(ge=50, le=100)
    max_model_calls_per_hour: int = Field(ge=0)
    filtered_recheck_min: int = Field(ge=0, default=3)
    quiet_before_event_min: int = Field(ge=0)
    quiet_after_event_min: int = Field(ge=0)
    triggers: TriggersConfig

    @field_validator("sensitivity_levels")
    @classmethod
    def _all_levels(cls, v: dict[str, int]) -> dict[str, int]:
        missing = {"low", "medium", "high"} - v.keys()
        if missing:
            raise ValueError(f"не задано рівні чутливості: {sorted(missing)}")
        bad = {k: n for k, n in v.items() if not 1 <= n <= DIRECTIONAL_TRIGGERS}
        if bad:
            raise ValueError(f"кількість тригерів має бути від 1 до {DIRECTIONAL_TRIGGERS}: {bad}")
        return v


class TrackingConfig(_Model):
    horizons_min: list[int]


class StorageConfig(_Model):
    db_path: str


class LoggingConfig(_Model):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: str
    max_bytes: int = Field(gt=0)
    backup_count: int = Field(ge=0)


class DisplayConfig(_Model):
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return _check_tz(v)


class RecurringEvent(_Model):
    name: str
    weekday: Weekday
    time: time
    tz: str = "UTC"

    @field_validator("tz")
    @classmethod
    def _tz(cls, v: str) -> str:
        return _check_tz(v)


class OneTimeEvent(_Model):
    name: str
    at: datetime

    @field_validator("at", mode="before")
    @classmethod
    def _parse(cls, v: object) -> object:
        if isinstance(v, str):
            return datetime.strptime(v.strip(), "%Y-%m-%d %H:%M")
        return v

    @field_validator("at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        # Час у конфізі завжди UTC.
        return v.replace(tzinfo=ZoneInfo("UTC")) if v.tzinfo is None else v.astimezone(ZoneInfo("UTC"))


class CalendarConfig(_Model):
    recurring: list[RecurringEvent] = []
    one_time: list[OneTimeEvent] = []

    @field_validator("recurring", "one_time", mode="before")
    @classmethod
    def _none_to_list(cls, v: object) -> object:
        return [] if v is None else v


class AppConfig(_Model):
    mt5: MT5Config
    market: MarketConfig
    network: NetworkConfig
    indicators: IndicatorsConfig
    news: NewsConfig
    analysis: AnalysisConfig
    alerts: AlertsConfig
    tracking: TrackingConfig
    storage: StorageConfig
    logging: LoggingConfig
    display: DisplayConfig
    calendar: CalendarConfig

    @model_validator(mode="after")
    def _paths(self) -> "AppConfig":
        if not self.prompt_path.exists():
            raise ValueError(f"файл промпту не знайдено: {self.prompt_path}")
        return self

    @property
    def prompt_path(self) -> Path:
        return _resolve(self.analysis.prompt_file)

    @property
    def db_path(self) -> Path:
        return _resolve(self.storage.db_path)

    @property
    def log_path(self) -> Path:
        return _resolve(self.logging.file)


class Secrets(BaseModel):
    telegram_bot_token: SecretStr | None
    telegram_owner_id: int | None
    anthropic_api_key: SecretStr | None
    anthropic_model: str | None
    mt5_login: int | None = None
    mt5_password: SecretStr | None = None
    mt5_server: str | None = None
    mt5_path: str | None = None

    def missing(self) -> list[str]:
        names = {
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_OWNER_ID": self.telegram_owner_id,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "ANTHROPIC_MODEL": self.anthropic_model,
        }
        return [k for k, v in names.items() if v in (None, "") or (isinstance(v, SecretStr) and not v.get_secret_value())]


class Settings(BaseModel):
    config: AppConfig
    secrets: Secrets


class ConfigError(Exception):
    pass


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def load_secrets(env_file: Path | None = None) -> Secrets:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    owner = _env("TELEGRAM_OWNER_ID")
    if owner is not None and not owner.lstrip("-").isdigit():
        raise ConfigError("TELEGRAM_OWNER_ID має бути числом")
    token = _env("TELEGRAM_BOT_TOKEN")
    key = _env("ANTHROPIC_API_KEY")
    mt5_login = _env("MT5_LOGIN")
    if mt5_login is not None and not mt5_login.isdigit():
        raise ConfigError("MT5_LOGIN має бути числом (номер рахунку MT5)")
    mt5_password = _env("MT5_PASSWORD")
    return Secrets(
        telegram_bot_token=SecretStr(token) if token else None,
        telegram_owner_id=int(owner) if owner else None,
        anthropic_api_key=SecretStr(key) if key else None,
        anthropic_model=_env("ANTHROPIC_MODEL"),
        mt5_login=int(mt5_login) if mt5_login else None,
        mt5_password=SecretStr(mt5_password) if mt5_password else None,
        mt5_server=_env("MT5_SERVER"),
        mt5_path=_env("MT5_PATH"),
    )


def load_config(path: Path | None = None) -> AppConfig:
    path = path or Path(os.getenv("BOT_CONFIG") or PROJECT_ROOT / "config.yaml")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"не знайдено файл конфігурації: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"помилка синтаксису YAML у {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: очікувався словник на верхньому рівні")
    try:
        return AppConfig.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"помилка в {path}:\n{exc}") from exc


def load_settings(config_path: Path | None = None, env_file: Path | None = None) -> Settings:
    return Settings(config=load_config(config_path), secrets=load_secrets(env_file))
