"""Статистика точності прогнозів."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

WIN, LOSS, NEUTRAL = "win", "loss", "neutral"


def classify(direction: str, change_pct: float, round_trip_pct: float) -> str:
    """Вдалий — рух у передбаченому напрямку більший за витрати на угоду (спред + комісія за вхід і вихід);
    невдалий — такий самий рух проти; інакше нейтральний."""
    favorable = change_pct if direction == "long" else -change_pct
    if favorable > round_trip_pct:
        return WIN
    if favorable < -round_trip_pct:
        return LOSS
    return NEUTRAL


def _dir(word: str | None) -> str | None:
    return {"long": "long", "short": "short", "up": "long", "down": "short"}.get(word or "")


@dataclass
class Tally:
    n: int = 0
    win: int = 0
    loss: int = 0
    neutral: int = 0

    def add(self, result: str) -> None:
        self.n += 1
        setattr(self, result, getattr(self, result) + 1)

    @property
    def win_rate(self) -> float | None:
        return self.win / self.n * 100 if self.n else None


@dataclass
class HitRate:
    n: int = 0
    hits: int = 0

    def add(self, ok: bool) -> None:
        self.n += 1
        self.hits += int(ok)

    @property
    def rate(self) -> float | None:
        return self.hits / self.n * 100 if self.n else None


@dataclass
class Stats:
    total: int = 0
    by_source_count: dict[str, int] = field(default_factory=dict)
    horizons: dict[int, Tally] = field(default_factory=dict)       # напрямкові прогнози (manual+alert)
    wait_ok: dict[int, HitRate] = field(default_factory=dict)       # wait: чи справді не було сильного руху
    calibration: dict[str, HitRate] = field(default_factory=dict)   # кошик -> чи пішла ціна в бік більшого %
    sources: dict[str, Tally] = field(default_factory=dict)         # на головному горизонті
    triggers: dict[str, HitRate] = field(default_factory=dict)
    model_vs_baseline: tuple[Tally, Tally] = field(default_factory=lambda: (Tally(), Tally()))
    baseline_all: Tally = field(default_factory=Tally)
    main_horizon: int = 15


BUCKETS = (("50–60%", 50, 60), ("60–70%", 60, 70), ("70%+", 70, 101))


def bucket(pct: int) -> str | None:
    for name, lo, hi in BUCKETS:
        if lo <= pct < hi:
            return name
    return None


def compute(rows: list[dict], horizons: list[int], round_trip_pct: float) -> Stats:
    """round_trip_pct — витрати за замовчуванням для прогнозів, у яких їх не записано
    (у нових прогнозах — спред + комісія на момент прогнозу, колонка cost_pct)."""
    default_rt = round_trip_pct
    main_h = max(horizons)
    st = Stats(main_horizon=main_h)
    st.horizons = {h: Tally() for h in horizons}
    st.wait_ok = {h: HitRate() for h in horizons}
    st.calibration = {name: HitRate() for name, _, _ in BUCKETS}
    st.sources = {s: Tally() for s in ("manual", "alert", "alert_filtered")}
    model_t, base_t = st.model_vs_baseline

    for r in rows:
        st.total += 1
        st.by_source_count[r["source"]] = st.by_source_count.get(r["source"], 0) + 1
        outs = r["outcomes"]
        action = r["action"]
        model_dir = _dir(action)
        trusted = r["source"] in ("manual", "alert")
        rt = r.get("cost_pct")
        round_trip_pct = default_rt if rt is None else rt

        for h in horizons:
            o = outs.get(h)
            if o is None:
                continue
            if trusted and model_dir:
                st.horizons[h].add(classify(model_dir, o["change_pct"], round_trip_pct))
            if trusted and action == "wait":
                st.wait_ok[h].add(abs(o["change_pct"]) <= round_trip_pct)

        o = outs.get(main_h)
        if o is None:
            continue
        ch = o["change_pct"]

        lp, sp = r.get("long_pct"), r.get("short_pct")
        if lp is not None and sp is not None and lp != sp:
            b = bucket(max(lp, sp))
            if b:
                st.calibration[b].add(ch > 0 if lp > sp else ch < 0)

        # Для відфільтрованих сигналів оцінюємо напрямок тригерів: чи фільтр моделі був корисним.
        src_dir = _dir(r.get("trigger_direction")) if r["source"] == "alert_filtered" else model_dir
        if src_dir:
            st.sources[r["source"]].add(classify(src_dir, ch, round_trip_pct))

        for t in r.get("triggers") or []:
            td = _dir(t.get("direction"))
            if td:
                st.triggers.setdefault(t["name"], HitRate()).add(ch > 0 if td == "long" else ch < 0)

        base_dir = _dir(r.get("baseline_direction"))
        if base_dir:
            st.baseline_all.add(classify(base_dir, ch, round_trip_pct))
            if trusted and model_dir:
                model_t.add(classify(model_dir, ch, round_trip_pct))
                base_t.add(classify(base_dir, ch, round_trip_pct))
    return st


def export_csv(rows: list[dict], horizons: list[int]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    head = ["id", "time_utc", "source", "price", "action", "long_pct", "short_pct", "confidence", "entry",
            "stop_loss", "take_profit", "trigger_direction", "triggers", "baseline_direction", "model",
            "prompt_version", "cost_pct"]
    for h in horizons:
        head += [f"price_{h}m", f"change_{h}m_pct", f"hit_{h}m"]
    w.writerow(head)
    for r in rows:
        line = [
            r["id"], datetime.fromtimestamp(r["created_ms"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            r["source"], r["price"], r["action"], r["long_pct"], r["short_pct"], r["confidence"], r["entry"],
            r["stop_loss"], r["take_profit"], r["trigger_direction"],
            json.dumps([t.get("name") for t in r.get("triggers") or []], ensure_ascii=False),
            r["baseline_direction"], r["model"], r["prompt_version"], r.get("cost_pct"),
        ]
        for h in horizons:
            o = r["outcomes"].get(h)
            line += [o["price"], o["change_pct"], o["hit"] or ""] if o else ["", "", ""]
        w.writerow(line)
    return buf.getvalue().encode("utf-8-sig")  # BOM — щоб Excel правильно відкрив кирилицю
