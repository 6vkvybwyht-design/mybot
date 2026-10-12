"""Фейковий модуль MetaTrader5 для тестів: та сама сигнатура функцій і формат масивів, що в справжнього."""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np

RATES_DTYPE = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
               ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")]
TICKS_DTYPE = [("time", "<i8"), ("bid", "<f8"), ("ask", "<f8"), ("last", "<f8"), ("volume", "<u8"),
               ("time_msc", "<i8"), ("flags", "<u4"), ("volume_real", "<f8")]
IPC_ERROR = (-10004, "No IPC connection")


class FakeMT5:
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_H1 = 1, 5, 15, 16385
    COPY_TICKS_ALL = -1
    SYMBOL_TRADE_MODE_DISABLED, SYMBOL_TRADE_MODE_FULL = 0, 4
    _MINUTES = {1: 1, 5: 5, 15: 15, 16385: 60}

    def __init__(self, *, offset_h=3.0, base=61.0, now=None, symbols=("USOUSD.s", "USOIL"), seed=7,
                 spread=0.03, tick_age_sec=1.0, ticks_per_min=20, burst_last_min=0):
        self.offset = int(offset_h * 3600)
        self.base = base
        self.now = now or time.time()
        self.symbols = list(symbols)
        self.rng = np.random.default_rng(seed)
        self.spread = spread
        self.history_spread: float | None = None   # спред у історії тіків, якщо відрізняється від поточного
        self.tick_age_sec = tick_age_sec
        self.ticks_per_min = ticks_per_min
        self.burst_last_min = burst_last_min
        self.down = False            # термінал закрито / IPC зламано
        self.broker_connected = True
        self.auth_fail = False
        self.error = (1, "Success")
        self.init_calls: list[tuple] = []
        self.calls: list[str] = []
        self.selected: set[str] = set()

    # --- службове ---
    def _srv_now(self) -> float:
        return self.now + self.offset

    def _fail(self, err=IPC_ERROR):
        self.error = err
        return None

    def last_error(self):
        return self.error

    def initialize(self, *args, **kwargs):
        self.init_calls.append((args, kwargs))
        if self.down:
            self.error = IPC_ERROR
            return False
        if self.auth_fail:
            self.error = (-6, "Terminal: Authorization failed")
            return False
        self.error = (1, "Success")
        return True

    def shutdown(self):
        return True

    def terminal_info(self):
        self.calls.append("terminal_info")
        if self.down:
            return self._fail()
        return SimpleNamespace(name="Bybit MT5", build=5200, connected=self.broker_connected)

    def account_info(self):
        if self.down:
            return self._fail()
        return SimpleNamespace(login=123, server="Bybit-Live-6", company="Infra Capital Limited")

    # --- символи ---
    def symbols_get(self, group="*"):
        if self.down:
            return self._fail()
        pat = group.strip("*").lower()
        return tuple(SimpleNamespace(name=s) for s in self.symbols if pat in s.lower())

    def symbol_info(self, symbol):
        if self.down:
            return self._fail()
        if symbol not in self.symbols:
            return self._fail((-4, "Not found"))
        return SimpleNamespace(name=symbol, description="WTI Crude Oil", digits=3, point=0.001,
                               trade_tick_size=0.001, trade_contract_size=100.0, volume_min=0.01,
                               volume_step=0.01, trade_mode=self.SYMBOL_TRADE_MODE_FULL, currency_profit="USD")

    def symbol_select(self, symbol, enable=True):
        if self.down or symbol not in self.symbols:
            self.error = IPC_ERROR if self.down else (-4, "Not found")
            return False
        self.selected.add(symbol)
        return True

    def symbol_info_tick(self, symbol):
        self.calls.append("symbol_info_tick")
        if self.down:
            return self._fail()
        t = self._srv_now() - self.tick_age_sec
        bid = self.base
        return SimpleNamespace(time=int(t), bid=bid, ask=round(bid + self.spread, 3), last=0.0, volume=0,
                               time_msc=int(t * 1000), flags=6, volume_real=0.0)

    # --- історія ---
    def _rates(self, tf, starts):
        out = np.zeros(len(starts), dtype=RATES_DTYPE)
        price = self.base
        for n, s in enumerate(starts):
            o = price
            c = round(o + float(self.rng.normal(0, 0.03)), 3)
            out[n] = (s, o, max(o, c) + 0.01, min(o, c) - 0.01, c, int(self.rng.integers(50, 150)), 30, 0)
            price = c
        return out

    def copy_rates_from_pos(self, symbol, tf, start_pos, count):
        self.calls.append("copy_rates_from_pos")
        if self.down:
            return self._fail()
        step = self._MINUTES[tf] * 60
        last = int(self._srv_now()) // step * step
        return self._rates(tf, [last - (start_pos + i) * step for i in range(count)][::-1])

    def copy_rates_range(self, symbol, tf, date_from, date_to):
        self.calls.append("copy_rates_range")
        if self.down:
            return self._fail()
        step = self._MINUTES[tf] * 60
        first = -(-int(date_from) // step) * step
        return self._rates(tf, list(range(first, int(date_to) + 1, step)))

    def copy_ticks_from(self, symbol, date_from, count, flags):
        self.calls.append("copy_ticks_from")
        if self.down:
            return self._fail()
        end = self._srv_now() - self.tick_age_sec
        times = list(np.arange(int(date_from), end, 60 / self.ticks_per_min))
        if self.burst_last_min:
            times += list(np.linspace(end - 59, end, self.burst_last_min))
        times = sorted(times)[:count]
        out = np.zeros(len(times), dtype=TICKS_DTYPE)
        spread = self.spread if self.history_spread is None else self.history_spread
        for n, t in enumerate(times):
            out[n] = (int(t), self.base, self.base + spread, 0, 0, int(t * 1000), 6, 0)
        return out
