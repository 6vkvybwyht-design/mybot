"""Ринкові дані з MetaTrader 5 (Bybit TradFi, CFD USOUSD.s) через офіційний пакет MetaTrader5.

Пакет синхронний і працює лише на Windows із запущеним терміналом, тому:
- кожен виклик іде через asyncio.to_thread під одним замком (пакет не потокобезпечний);
- модуль імпортується і без пакета (тести підставляють фейковий mt5).

Час у MT5 — це час сервера брокера, записаний як «секунди від 1970» (не UTC).
Тут він переводиться в UTC зсувом server_offset_sec; назовні все — UTC у мілісекундах.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Awaitable, Callable

import numpy as np
import pandas as pd

from bot.config import MT5Config, Secrets

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]

TIMEFRAME_NAMES = {1: "TIMEFRAME_M1", 5: "TIMEFRAME_M5", 15: "TIMEFRAME_M15", 60: "TIMEFRAME_H1"}
KLINE_COLUMNS = ["start", "open", "high", "low", "close", "volume", "spread", "closed"]
# last_error() <= -10000 — збій зв'язку Python ↔ термінал (термінал закрито, завис, перезапускається).
IPC_ERROR_MAX = -10000
_AUTH_FAILED = -6
# Зсув часу сервера шукаємо з кроком 30 хв; тік має бути свіжим, інакше зсув не визначити.
_OFFSET_STEP_SEC = 1800
_OFFSET_TOLERANCE_SEC = 120


class MT5Error(Exception):
    pass


class MT5Unavailable(MT5Error):
    """Немає зв'язку з терміналом або терміналу з сервером брокера."""


def _load_mt5() -> ModuleType | None:
    try:
        import MetaTrader5  # type: ignore[import-not-found]
    except ImportError:
        return None
    return MetaTrader5


@dataclass(frozen=True)
class Credentials:
    login: int | None = None
    password: str | None = field(default=None, repr=False)
    server: str | None = None
    path: str | None = None

    @classmethod
    def from_secrets(cls, s: Secrets) -> "Credentials":
        return cls(
            login=s.mt5_login,
            password=s.mt5_password.get_secret_value() if s.mt5_password else None,
            server=s.mt5_server,
            path=s.mt5_path,
        )

    @property
    def complete(self) -> bool:
        return bool(self.login and self.password and self.server)


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    description: str
    digits: int
    point: float
    tick_size: float
    contract_size: float
    volume_min: float
    volume_step: float
    trade_mode: int
    currency_profit: str


def offset_from_tick(tick_time_sec: float, now_sec: float) -> int | None:
    """Зсув сервера від UTC (с) за свіжим тіком; None, якщо тік старий і зсув не визначити."""
    diff = tick_time_sec - now_sec
    rounded = round(diff / _OFFSET_STEP_SEC) * _OFFSET_STEP_SEC
    if abs(diff - rounded) <= _OFFSET_TOLERANCE_SEC and abs(rounded) <= 14 * 3600:
        return int(rounded)
    return None


def rates_to_frame(rates: Any, interval_min: int, offset_sec: int, now_ms: int) -> pd.DataFrame:
    """Масив copy_rates_* -> DataFrame у форматі, який чекають індикатори (час — UTC, мс)."""
    if rates is None or len(rates) == 0:
        return pd.DataFrame(columns=KLINE_COLUMNS)
    r = np.asarray(rates)
    df = pd.DataFrame(
        {
            "start": (r["time"].astype("int64") - offset_sec) * 1000,
            "open": r["open"].astype(float),
            "high": r["high"].astype(float),
            "low": r["low"].astype(float),
            "close": r["close"].astype(float),
            # У CFD немає біржового обсягу — лише кількість тіків за свічку.
            "volume": r["tick_volume"].astype(float),
            "spread": r["spread"].astype(float),
        }
    )
    df = df.sort_values("start").drop_duplicates("start").reset_index(drop=True)
    df["closed"] = df["start"] + interval_min * 60_000 <= now_ms
    return df


def tick_activity(ticks: Any, offset_sec: int, now_ms: int, window_min: int, point: float) -> dict[str, Any]:
    """Тікова активність: скільки тіків за 1/5/15 хв проти звичайного темпу у вікні."""
    out: dict[str, Any] = {"window_min": window_min, "ticks_total": 0}
    if ticks is None or len(ticks) == 0:
        return out
    t = np.asarray(ticks)
    ts = t["time_msc"].astype("int64") - offset_sec * 1000
    bid, ask = t["bid"].astype(float), t["ask"].astype(float)

    def count(minutes: int) -> int:
        return int((ts > now_ms - minutes * 60_000).sum())

    # Базовий темп — вікно без останніх 5 хв, щоб сплеск не розмивав середнє.
    base_min = window_min - 5
    base_n = int((ts <= now_ms - 5 * 60_000).sum())
    per_min = base_n / base_min if base_min > 0 else None
    n1, n5, n15 = count(1), count(5), count(15)
    valid = (bid > 0) & (ask > 0)
    spreads = (ask - bid)[valid]
    out.update(
        {
            "ticks_total": int(len(t)),
            "ticks_1m": n1,
            "ticks_5m": n5,
            "ticks_15m": n15,
            "base_per_min": None if per_min is None else round(per_min, 2),
            "ratio_1m": round(n1 / per_min, 2) if per_min else None,
            "ratio_5m": round(n5 / 5 / per_min, 2) if per_min else None,
            "spread_median_points": round(float(np.median(spreads)) / point, 1) if len(spreads) and point else None,
            "spread_max_points": round(float(spreads.max()) / point, 1) if len(spreads) and point else None,
            "last_tick_age_sec": round((now_ms - int(ts.max())) / 1000, 1),
        }
    )
    return out


class MT5Source:
    def __init__(
        self,
        cfg: MT5Config,
        creds: Credentials,
        *,
        mt5_module: ModuleType | Any | None = None,
        notify: Notify | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.creds = creds
        self.mt5 = mt5_module if mt5_module is not None else _load_mt5()
        self.notify = notify
        self.clock = clock
        self.connected = False
        self.spec: SymbolSpec | None = None
        self.server_offset_sec: int | None = None
        self.offset_source = "unknown"   # config | tick | unknown
        self.last_error: str | None = None
        self.connects = 0
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()

    @property
    def symbol(self) -> str:
        return self.cfg.symbol

    @property
    def offset(self) -> int:
        return self.server_offset_sec or 0

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    # --- виклики пакета ---

    def _err(self) -> tuple[int, str]:
        try:
            code, msg = self.mt5.last_error()
            return int(code), str(msg)
        except Exception:  # noqa: BLE001
            return -1, "невідома помилка"

    async def _raw(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Виклик без перевірки результату (для функцій, що повертають bool)."""
        if self.mt5 is None:
            raise MT5Unavailable("пакет MetaTrader5 не встановлено (працює лише на Windows: pip install MetaTrader5)")
        async with self._lock:
            return await asyncio.to_thread(getattr(self.mt5, name), *args, **kwargs)

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Виклик, де None означає помилку. Збій IPC позначає зв'язок як втрачений."""
        res = await self._raw(name, *args, **kwargs)
        if res is None:
            code, msg = self._err()
            if code <= IPC_ERROR_MAX:
                self._lost(f"{name}: {code} {msg}")
                raise MT5Unavailable(f"немає зв'язку з терміналом MT5 ({name}: {code} {msg})")
            raise MT5Error(f"{name}: {code} {msg}")
        return res

    def _lost(self, reason: str) -> None:
        if self.connected:
            log.warning("Зв'язок з MT5 втрачено: %s", reason)
        self.connected = False
        self.last_error = reason

    # --- підключення ---

    async def connect(self) -> None:
        kwargs: dict[str, Any] = {"timeout": self.cfg.init_timeout_ms}
        if self.creds.complete:
            kwargs.update(login=self.creds.login, password=self.creds.password, server=self.creds.server)
        else:
            log.warning("MT5_LOGIN/MT5_PASSWORD/MT5_SERVER заповнені не всі — підключаюся до рахунку, "
                        "на якому вже залогінено термінал")
        args = (self.creds.path,) if self.creds.path else ()
        if not await self._raw("initialize", *args, **kwargs):
            code, msg = self._err()
            await self._raw("shutdown")
            hint = ""
            if code == _AUTH_FAILED:
                hint = " — перевірте MT5_LOGIN, MT5_PASSWORD і MT5_SERVER"
            elif code <= IPC_ERROR_MAX:
                hint = " — чи запущено термінал MT5 (або вкажіть MT5_PATH до terminal64.exe)?"
            self._lost(f"initialize: {code} {msg}")
            raise MT5Unavailable(f"initialize: {code} {msg}{hint}")

        term = await self._call("terminal_info")
        acc = await self._raw("account_info")
        log.info(
            "MT5: термінал %s build %s, з'єднання з брокером: %s; рахунок %s на %s (%s)",
            getattr(term, "name", "?"), getattr(term, "build", "?"), getattr(term, "connected", "?"),
            getattr(acc, "login", "?"), getattr(acc, "server", "?"), getattr(acc, "company", "?"),
        )
        if self.creds.server and acc is not None and getattr(acc, "server", None) != self.creds.server:
            log.warning("MT5: сервер рахунку %s ≠ MT5_SERVER %s", acc.server, self.creds.server)
        if not getattr(term, "connected", False):
            self._lost("термінал не з'єднаний з сервером брокера")
            raise MT5Unavailable("термінал MT5 запущено, але він не з'єднаний з сервером брокера")

        self.spec = await self._resolve_symbol()
        await self.refresh_offset()
        self.connected = True
        self.last_error = None
        self.connects += 1

    async def _resolve_symbol(self) -> SymbolSpec:
        found = await self._raw("symbols_get", group=self.cfg.symbol_search) or ()
        names = [s.name for s in found]
        log.info("MT5: символи за маскою %s: %s", self.cfg.symbol_search, ", ".join(names) or "немає")
        if self.symbol not in names and await self._raw("symbol_info", self.symbol) is None:
            log.error("MT5: символ %s НЕ ЗНАЙДЕНО у терміналі. За маскою %s є: %s. "
                      "Виправте mt5.symbol у config.yaml.",
                      self.symbol, self.cfg.symbol_search, ", ".join(names) or "нічого")
            raise MT5Error(f"символ {self.symbol} не знайдено (за маскою {self.cfg.symbol_search}: "
                           f"{', '.join(names) or 'нічого'})")
        if not await self._raw("symbol_select", self.symbol, True):
            code, msg = self._err()
            log.error("MT5: не вдалося додати %s в «Огляд ринку»: %s %s", self.symbol, code, msg)
            raise MT5Error(f"symbol_select({self.symbol}): {code} {msg}")
        info = await self._call("symbol_info", self.symbol)
        spec = SymbolSpec(
            symbol=info.name,
            description=getattr(info, "description", ""),
            digits=int(info.digits),
            point=float(info.point),
            tick_size=float(getattr(info, "trade_tick_size", 0) or info.point),
            contract_size=float(getattr(info, "trade_contract_size", 0) or 0),
            volume_min=float(getattr(info, "volume_min", 0) or 0),
            volume_step=float(getattr(info, "volume_step", 0) or 0),
            trade_mode=int(getattr(info, "trade_mode", -1)),
            currency_profit=getattr(info, "currency_profit", ""),
        )
        if spec.trade_mode == getattr(self.mt5, "SYMBOL_TRADE_MODE_DISABLED", 0):
            log.warning("MT5: торгівля %s вимкнена брокером (trade_mode=%s)", spec.symbol, spec.trade_mode)
        log.info("MT5: символ %s (%s): digits %d, tick %s, контракт %s", spec.symbol, spec.description,
                 spec.digits, spec.tick_size, spec.contract_size)
        return spec

    async def refresh_offset(self) -> int:
        """Зсув часу сервера: з конфігу або за свіжим тіком. Старий тік — лишаємо попередній."""
        cfg_off = self.cfg.server_utc_offset_hours
        if cfg_off != "auto":
            self.server_offset_sec, self.offset_source = int(float(cfg_off) * 3600), "config"
            return self.server_offset_sec
        tick = await self._raw("symbol_info_tick", self.symbol)
        off = offset_from_tick(tick.time_msc / 1000, self.clock()) if tick is not None else None
        if off is not None:
            if off != self.server_offset_sec:
                log.info("MT5: час сервера = UTC%+.1f год (визначено за тіком)", off / 3600)
            self.server_offset_sec, self.offset_source = off, "tick"
        elif self.server_offset_sec is None:
            log.warning("MT5: не вдалося визначити зсув часу сервера (останній тік старий — ринок закритий?). "
                        "Беру 0; задайте mt5.server_utc_offset_hours у config.yaml.")
            self.server_offset_sec, self.offset_source = 0, "unknown"
        return self.offset

    # --- дані ---

    async def klines(
        self,
        interval_min: int,
        limit: int | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        now_ms: int | None = None,
    ) -> pd.DataFrame:
        """Свічки за зростанням часу; остання може бути незакритою (колонка closed)."""
        if interval_min not in TIMEFRAME_NAMES:
            raise MT5Error(f"таймфрейм {interval_min} хв не підтримується")
        tf = getattr(self.mt5, TIMEFRAME_NAMES[interval_min])
        limit = limit or self.cfg.bars
        now_ms = now_ms if now_ms is not None else self.now_ms()
        if start_ms is None and end_ms is None:
            rates = await self._call("copy_rates_from_pos", self.symbol, tf, 0, limit)
        else:
            # Межі — у часі сервера, як і час у самих свічках.
            frm = (start_ms if start_ms is not None else now_ms - limit * interval_min * 60_000) // 1000
            to = (end_ms if end_ms is not None else now_ms) // 1000
            rates = await self._call("copy_rates_range", self.symbol, tf, frm + self.offset, to + self.offset)
        df = rates_to_frame(rates, interval_min, self.offset, now_ms)
        return df.tail(limit).reset_index(drop=True)

    async def tick(self, now_ms: int | None = None) -> dict[str, Any]:
        t = await self._call("symbol_info_tick", self.symbol)
        now_ms = now_ms if now_ms is not None else self.now_ms()
        bid, ask = float(t.bid), float(t.ask)
        point = self.spec.point if self.spec else 0.0
        nd = self.spec.digits + 1 if self.spec else 6   # прибирає хвости float: 0.0259999… -> 0.026
        spread = round(ask - bid, nd) if bid > 0 and ask > 0 else None
        ts = int(t.time_msc) - self.offset * 1000
        return {
            "bid": bid,
            "ask": ask,
            "mid": round((bid + ask) / 2, nd) if spread is not None else None,
            "spread": spread,
            "spread_points": round(spread / point, 1) if spread is not None and point else None,
            "spread_pct": round(spread / bid * 100, 4) if spread is not None else None,
            "time_ms": ts,
            "age_sec": round((now_ms - ts) / 1000, 1),
        }

    async def tick_activity(self, now_ms: int | None = None) -> dict[str, Any]:
        now_ms = now_ms if now_ms is not None else self.now_ms()
        window = self.cfg.tick_window_min
        frm = now_ms // 1000 - window * 60 + self.offset
        flags = getattr(self.mt5, "COPY_TICKS_ALL", -1)
        ticks = await self._call("copy_ticks_from", self.symbol, frm, self.cfg.tick_max_count, flags)
        return tick_activity(ticks, self.offset, now_ms, window, self.spec.point if self.spec else 0.0)

    async def snapshot(self, intervals: list[int] | None = None) -> dict[str, Any]:
        """Усе з MT5 за один прохід: свічки по таймфреймах, тік, тікова активність."""
        if not self.connected:
            raise MT5Unavailable(self.last_error or "немає підключення до MT5")
        if self.offset_source != "config":
            await self.refresh_offset()
        now_ms = self.now_ms()
        klines = {i: await self.klines(i, now_ms=now_ms) for i in intervals or self.cfg.timeframes}
        return {
            "ts_ms": now_ms,
            "klines": klines,
            "tick": await self.tick(now_ms),
            "tick_activity": await self.tick_activity(now_ms),
        }

    async def info(self) -> tuple[Any, Any]:
        """(terminal_info, account_info) — для діагностики."""
        return await self._raw("terminal_info"), await self._raw("account_info")

    # --- нагляд за зв'язком ---

    async def healthy(self) -> bool:
        try:
            term = await self._raw("terminal_info")
        except Exception as exc:  # noqa: BLE001
            self._lost(f"terminal_info: {exc!r}")
            return False
        if term is None:
            code, msg = self._err()
            self._lost(f"terminal_info: {code} {msg}")
            return False
        if not getattr(term, "connected", False):
            self._lost("термінал втратив з'єднання з сервером брокера")
            return False
        return True

    async def _notify(self, text: str) -> None:
        if self.notify is not None:
            await self.notify(text)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        """Підключається і тримає зв'язок: перевіряє термінал, перепідключається з паузами,
        власнику пише один раз про втрату і один раз про відновлення."""
        delay, notified = 5.0, False
        while not self._stop.is_set():
            if self.connected and await self.healthy():
                await self._wait(self.cfg.health_check_sec)
                continue
            try:
                await self._raw("shutdown")
                await self.connect()
            except MT5Error as exc:
                self.last_error = str(exc)
                log.error("MT5 недоступний: %s; повтор через %.0f с", exc, delay)
                if not notified:
                    notified = True
                    await self._notify(f"⚠️ Не вдається отримати дані MT5: {exc}. Пробую далі.")
                await self._wait(delay)
                delay = min(delay * 2, self.cfg.reconnect_max_sec)
                continue
            log.info("MT5 підключено (%s)", self.symbol)
            if notified:
                await self._notify("✅ Дані MT5 знову доступні.")
            delay, notified = 5.0, False

    async def stop(self) -> None:
        self._stop.set()
        self.connected = False
        if self.mt5 is not None:
            try:
                await self._raw("shutdown")
            except Exception:  # noqa: BLE001
                pass
