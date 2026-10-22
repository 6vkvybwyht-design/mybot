"""Перевірка прогнозів проти реальної ціни через 5/10/15 хв (1-хв свічки з MT5).

Свічки MT5 будуються по Bid. Лонг закривається по Bid, тож його тейк і стоп перевіряються
по самій свічці; шорт закривається по Ask = Bid + спред свічки.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import pandas as pd

from bot.market.mt5_source import MT5Error, MT5Source
from bot.tracking.db import Database, PredictionRow

log = logging.getLogger(__name__)

MAX_AGE_MS = 2 * 86_400_000   # старші прогнози без результату вже не перевіряємо
MINUTE = 60_000
# Якщо горизонт припав на перерву в торгах, ціну горизонту видно лише після відкриття,
# тому свічки тягнемо до «зараз», але не далі ніж на стільки від прогнозу.
MAX_FETCH_MS = 6 * 3_600_000


@dataclass
class Outcome:
    price: float
    change_pct: float
    hit: str | None          # take | stop | None
    hit_after_min: float | None


def levels_direction(p: PredictionRow) -> str | None:
    """Напрямок, для якого перевіряємо тейк/стоп."""
    if p.action in ("long", "short") and p.take_profit and p.stop_loss:
        return p.action
    return None


def evaluate(p: PredictionRow, horizon_min: int, k1: pd.DataFrame, point: float | None = None) -> Outcome | None:
    """k1 — 1-хв свічки (Bid) від хвилини прогнозу. None, якщо ціну горизонту ще не відомо.

    point — розмір пункту: з ним спред свічки (у пунктах) переводиться в ціну для перевірки шорту по Ask.
    """
    t0 = p.created_ms
    t_end = t0 + horizon_min * MINUTE
    horizon_start = ((t_end - 1) // MINUTE) * MINUTE   # хвилина, що містить момент горизонту
    window = k1[(k1["start"] >= (t0 // MINUTE) * MINUTE) & (k1["start"] <= horizon_start)]
    last = window[window["start"] == horizon_start]
    if not last.empty:
        if not bool(last["closed"].iloc[0]):
            return None
    elif window.empty or not (k1["start"] > horizon_start).any():
        return None
    else:
        # Свічки на хвилину горизонту немає (перерва в торгах, а торги вже пішли далі) —
        # ціна горизонту дорівнює останній ціні перед перервою.
        last = window.tail(1)
    price = float(last["close"].iloc[0])
    change = (price - p.price) / p.price * 100

    hit = hit_after = None
    direction = levels_direction(p)
    if direction:
        use_spread = bool(point) and "spread" in window.columns
        for row in window.itertuples():
            ask_adj = row.spread * point if use_spread and direction == "short" else 0.0
            take_hit = row.high >= p.take_profit if direction == "long" else row.low + ask_adj <= p.take_profit
            stop_hit = row.low <= p.stop_loss if direction == "long" else row.high + ask_adj >= p.stop_loss
            if stop_hit or take_hit:
                # Обидва в одній свічці — невідомо, що першим; рахуємо обережно як стоп.
                hit = "stop" if stop_hit else "take"
                hit_after = round(max(row.start + MINUTE - t0, 0) / MINUTE, 1)
                break
    return Outcome(price, round(change, 4), hit, hit_after)


class OutcomeChecker:
    def __init__(self, db: Database, source: MT5Source, horizons: list[int], interval_sec: float = 30):
        self.db = db
        self.source = source
        self.horizons = horizons
        self.interval_sec = interval_sec
        self._stop = asyncio.Event()

    async def check_once(self, now_ms: int | None = None) -> int:
        if not self.source.connected:
            return 0   # MT5 недоступний — перевіримо, коли зв'язок відновиться
        now_ms = now_ms or self.source.now_ms()
        point = self.source.spec.point if self.source.spec else None
        pending = await self.db.pending_outcomes(self.horizons, now_ms, MAX_AGE_MS)
        done = 0
        cache: dict[int, pd.DataFrame] = {}
        for p, h in pending:
            try:
                if p.id not in cache:
                    start = (p.created_ms // MINUTE) * MINUTE
                    end = min(start + MAX_FETCH_MS, now_ms)
                    limit = (end - start) // MINUTE + 2
                    cache[p.id] = await self.source.klines(1, limit=limit, start_ms=start, end_ms=end, now_ms=now_ms)
                outcome = evaluate(p, h, cache[p.id], point)
            except MT5Error as exc:
                log.warning("Перевірка прогнозу %d: %s", p.id, exc)
                continue
            if outcome is None:
                continue
            await self.db.insert_outcome(p.id, h, outcome.price, outcome.change_pct, outcome.hit,
                                         outcome.hit_after_min, now_ms)
            done += 1
        if done:
            log.info("Перевірено результатів: %d", done)
        return done

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.check_once()
            except Exception:  # noqa: BLE001
                log.exception("Помилка перевірки прогнозів")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_sec)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
