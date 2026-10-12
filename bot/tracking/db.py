"""SQLite: прогнози, результати перевірки, налаштування, події користувача."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from bot.news.calendar import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ms INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('manual', 'alert', 'alert_filtered')),
    price REAL NOT NULL,
    action TEXT,
    long_pct INTEGER,
    short_pct INTEGER,
    confidence TEXT,
    entry REAL,
    stop_loss REAL,
    take_profit REAL,
    trigger_direction TEXT,
    triggers_json TEXT NOT NULL DEFAULT '[]',
    response_json TEXT,
    snapshot_json TEXT NOT NULL,
    baseline_direction TEXT,
    prompt_version TEXT,
    model TEXT
);
CREATE INDEX IF NOT EXISTS ix_predictions_created ON predictions(created_ms);

CREATE TABLE IF NOT EXISTS outcomes (
    prediction_id INTEGER NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
    horizon_min INTEGER NOT NULL,
    price REAL NOT NULL,
    change_pct REAL NOT NULL,
    hit TEXT CHECK (hit IN ('take', 'stop')),
    hit_after_min REAL,
    checked_ms INTEGER NOT NULL,
    PRIMARY KEY (prediction_id, horizon_min)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    created_ms INTEGER NOT NULL
);
"""


@dataclass
class PredictionRow:
    id: int
    created_ms: int
    source: str
    price: float
    action: str | None
    long_pct: int | None
    short_pct: int | None
    entry: float | None
    stop_loss: float | None
    take_profit: float | None
    trigger_direction: str | None
    triggers: list[dict]
    baseline_direction: str | None


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("база не підключена")
        return self._db

    # ---------- налаштування ----------

    async def get_setting(self, key: str, default: Any = None) -> Any:
        async with self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return json.loads(row["value"]) if row else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        await self.db.commit()

    # ---------- події ----------

    async def add_event(self, event: Event, now_ms: int) -> int:
        cur = await self.db.execute(
            "INSERT INTO events(name, at_ms, created_ms) VALUES(?, ?, ?)",
            (event.name, int(event.at.timestamp() * 1000), now_ms),
        )
        await self.db.commit()
        return cur.lastrowid or 0

    async def load_events(self, since_ms: int = 0) -> list[Event]:
        async with self.db.execute(
            "SELECT name, at_ms FROM events WHERE at_ms >= ? ORDER BY at_ms", (since_ms,)
        ) as cur:
            rows = await cur.fetchall()
        return [Event(r["name"], datetime.fromtimestamp(r["at_ms"] / 1000, timezone.utc), "user") for r in rows]

    # ---------- прогнози ----------

    async def insert_prediction(
        self,
        *,
        created_ms: int,
        source: str,
        price: float,
        response: dict | None,
        snapshot: dict,
        triggers: list[dict] | None = None,
        trigger_direction: str | None = None,
        baseline_direction: str | None = None,
        prompt_version: str | None = None,
        model: str | None = None,
    ) -> int:
        r = response or {}
        cur = await self.db.execute(
            """INSERT INTO predictions(created_ms, source, price, action, long_pct, short_pct, confidence,
                   entry, stop_loss, take_profit, trigger_direction, triggers_json, response_json,
                   snapshot_json, baseline_direction, prompt_version, model)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                created_ms, source, price, r.get("action"), r.get("long_pct"), r.get("short_pct"),
                r.get("confidence"), r.get("entry"), r.get("stop_loss"), r.get("take_profit"),
                trigger_direction, json.dumps(triggers or [], ensure_ascii=False),
                json.dumps(response, ensure_ascii=False) if response is not None else None,
                json.dumps(snapshot, ensure_ascii=False), baseline_direction, prompt_version, model,
            ),
        )
        await self.db.commit()
        return cur.lastrowid or 0

    @staticmethod
    def _row(r: aiosqlite.Row) -> PredictionRow:
        return PredictionRow(
            id=r["id"], created_ms=r["created_ms"], source=r["source"], price=r["price"], action=r["action"],
            long_pct=r["long_pct"], short_pct=r["short_pct"], entry=r["entry"], stop_loss=r["stop_loss"],
            take_profit=r["take_profit"], trigger_direction=r["trigger_direction"],
            triggers=json.loads(r["triggers_json"] or "[]"), baseline_direction=r["baseline_direction"],
        )

    async def pending_outcomes(self, horizons: list[int], now_ms: int, max_age_ms: int) -> list[tuple[PredictionRow, int]]:
        """Пари (прогноз, горизонт), для яких час перевірки настав, а результату ще немає."""
        out = []
        oldest = now_ms - max_age_ms
        for h in horizons:
            async with self.db.execute(
                """SELECT p.* FROM predictions p
                   WHERE p.created_ms <= ? AND p.created_ms >= ?
                     AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.prediction_id = p.id AND o.horizon_min = ?)
                   ORDER BY p.created_ms""",
                (now_ms - h * 60_000, oldest, h),
            ) as cur:
                out += [(self._row(r), h) for r in await cur.fetchall()]
        return out

    async def insert_outcome(
        self, prediction_id: int, horizon_min: int, price: float, change_pct: float,
        hit: str | None, hit_after_min: float | None, checked_ms: int,
    ) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO outcomes(prediction_id, horizon_min, price, change_pct, hit, hit_after_min,
                   checked_ms) VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (prediction_id, horizon_min, price, change_pct, hit, hit_after_min, checked_ms),
        )
        await self.db.commit()

    async def predictions_with_outcomes(self, since_ms: int = 0) -> list[dict]:
        """Плоский список для статистики: один рядок на прогноз, результати по горизонтах у dict."""
        async with self.db.execute(
            "SELECT * FROM predictions WHERE created_ms >= ? ORDER BY created_ms", (since_ms,)
        ) as cur:
            preds = await cur.fetchall()
        async with self.db.execute(
            """SELECT o.* FROM outcomes o JOIN predictions p ON p.id = o.prediction_id
               WHERE p.created_ms >= ?""",
            (since_ms,),
        ) as cur:
            outs = await cur.fetchall()
        by_pred: dict[int, dict[int, dict]] = {}
        for o in outs:
            by_pred.setdefault(o["prediction_id"], {})[o["horizon_min"]] = dict(o)
        result = []
        for p in preds:
            d = dict(p)
            d["triggers"] = json.loads(d.pop("triggers_json") or "[]")
            d["outcomes"] = by_pred.get(p["id"], {})
            result.append(d)
        return result

    async def count_predictions(self) -> int:
        async with self.db.execute("SELECT COUNT(*) FROM predictions") as cur:
            row = await cur.fetchone()
        return int(row[0])
