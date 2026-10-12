"""Перевірка прогнозів проти реальної ціни через 5/10/15 хв."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import pandas as pd

from bot.market.mt5_source import MT5Error, MT5Source
from bot.tracking.db import Database, PredictionRow

log = logging.getLogger(__name__)

MAX_AGE_MS = 2 * 86_400_000   # старші прогнози без результату вже не перевіряємо
MINUTE = 60_000


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


def evaluate(p: PredictionRow, horizon_min: int, k1: pd.DataFrame) -> Outcome | None:
    """k1 — 1-хв свічки від хвилини прогнозу. None, якщо свічка горизонту ще не закрита."""
    t0 = p.created_ms
    t_end = t0 + horizon_min * MINUTE
    horizon_start = ((t_end - 1) // MINUTE) * MINUTE   # хвилина, що містить момент горизонту
    window = k1[(k1["start"] >= (t0 // MINUTE) * MINUTE) & (k1["start"] <= horizon_start)]
    last = window[window["start"] == horizon_start]
    if last.empty or not bool(last["closed"].iloc[0]):
        return None
    price = float(last["close"].iloc[0])
    change = (price - p.price) / p.price * 100

    hit = hit_after = None
    direction = levels_direction(p)
    if direction:
        for row in window.itertuples():
            take_hit = row.high >= p.take_profit if direction == "long" else row.low <= p.take_profit
            stop_hit = row.low <= p.stop_loss if direction == "long" else row.high >= p.stop_loss
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
        now_ms = now_ms or int(time.time() * 1000)
        pending = await self.db.pending_outcomes(self.horizons, now_ms, MAX_AGE_MS)
        done = 0
        cache: dict[int, pd.DataFrame] = {}
        for p, h in pending:
            try:
                if p.id not in cache:
                    start = (p.created_ms // MINUTE) * MINUTE
                    max_h = max(self.horizons)
                    end = min(start + (max_h + 1) * MINUTE, now_ms)
                    cache[p.id] = await self.source.klines(1, limit=max_h + 3, start_ms=start, end_ms=end, now_ms=now_ms)
                outcome = evaluate(p, h, cache[p.id])
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
