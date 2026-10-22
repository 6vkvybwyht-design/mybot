"""Статистика точності прогнозів."""

from __future__ import annotations

import csv
import io
import json
import math
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


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% довірчий інтервал Вілсона для частки, у відсотках. На малих вибірках він широкий —
    саме так і видно, що даних замало (на відміну від простого p ± 1.96·σ, який там бреше)."""
    if n <= 0:
        return None
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half) * 100, min(1.0, centre + half) * 100


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

    @property
    def ci(self) -> tuple[float, float] | None:
        return wilson(self.win, self.n)


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

    @property
    def ci(self) -> tuple[float, float] | None:
        return wilson(self.hits, self.n)


@dataclass
class PnL:
    """Гіпотетичні угоди на горизонт: результат кожної = рух у бік прогнозу мінус витрати (у %).

    Сума — проста (без реінвестування), просадка — найбільше падіння кривої суми від її максимуму
    (у процентних пунктах; старт кривої — 0).
    """
    trades: list[float] = field(default_factory=list)

    def add(self, net_pct: float) -> None:
        self.trades.append(net_pct)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def total(self) -> float:
        return sum(self.trades)

    @property
    def mean(self) -> float | None:
        return self.total / self.n if self.n else None

    @property
    def max_drawdown(self) -> float:
        peak = equity = dd = 0.0
        for t in self.trades:
            equity += t
            peak = max(peak, equity)
            dd = max(dd, peak - equity)
        return dd

    @property
    def wins(self) -> HitRate:
        h = HitRate()
        for t in self.trades:
            h.add(t > 0)
        return h


BUCKETS = (
    ("50–55%", 50, 55), ("55–60%", 55, 60), ("60–65%", 60, 65), ("65–70%", 65, 70), ("70%+", 70, 101),
)


def bucket(pct: int) -> str | None:
    """Кошик калібрування для ймовірності сильнішої сторони; рівно 50% — без напрямку."""
    if pct <= 50:
        return None
    for name, lo, hi in BUCKETS:
        if lo <= pct < hi:
            return name
    return None


@dataclass
class HorizonStats:
    horizon: int
    signals: Tally = field(default_factory=Tally)            # явні лонг/шорт (ручні + авто)
    wait_ok: HitRate = field(default_factory=HitRate)        # «чекати»: руху більшого за витрати не було
    calibration: dict[str, HitRate] = field(default_factory=lambda: {b[0]: HitRate() for b in BUCKETS})
    sources: dict[str, Tally] = field(
        default_factory=lambda: {s: Tally() for s in ("manual", "alert", "alert_filtered")})
    triggers: dict[str, HitRate] = field(default_factory=dict)
    # Усі прогнози з напрямком > 50%: чи вгадано напрямок — бот і «як остання свічка» на тих самих.
    model_dir: HitRate = field(default_factory=HitRate)
    baseline_dir: HitRate = field(default_factory=HitRate)
    pnl_all: PnL = field(default_factory=PnL)                # угода на кожен прогноз з напрямком > 50%
    pnl_signals: PnL = field(default_factory=PnL)            # угода лише на явні лонг/шорт (ручні + авто)


@dataclass
class Stats:
    total: int = 0
    by_source_count: dict[str, int] = field(default_factory=dict)
    main_horizon: int = 5
    horizons: dict[int, HorizonStats] = field(default_factory=dict)

    @property
    def main(self) -> HorizonStats:
        return self.horizons[self.main_horizon]


def lean(r: dict) -> str | None:
    """Напрямок, на який схиляється модель (ймовірність > 50%), незалежно від action."""
    lp, sp = r.get("long_pct"), r.get("short_pct")
    if lp is None or sp is None or lp == sp:
        return None
    return "long" if lp > sp else "short"


def _favorable(direction: str, change_pct: float) -> float:
    return change_pct if direction == "long" else -change_pct


def compute(rows: list[dict], horizons: list[int], round_trip_pct: float, main_horizon: int = 5) -> Stats:
    """round_trip_pct — витрати за замовчуванням для прогнозів, у яких їх не записано
    (у нових прогнозах — спред + комісія на момент прогнозу, колонка cost_pct).
    rows мають бути відсортовані за часом (для кривої P&L)."""
    if main_horizon not in horizons:
        raise ValueError(f"основний горизонт {main_horizon} хв відсутній серед {horizons}")
    st = Stats(main_horizon=main_horizon, horizons={h: HorizonStats(h) for h in horizons})

    for r in sorted(rows, key=lambda r: r["created_ms"]):
        st.total += 1
        st.by_source_count[r["source"]] = st.by_source_count.get(r["source"], 0) + 1
        action = r["action"]
        model_dir = _dir(action)
        trusted = r["source"] in ("manual", "alert")
        cost = r.get("cost_pct")
        cost = round_trip_pct if cost is None else cost
        leaning = lean(r)
        b = bucket(max(r["long_pct"], r["short_pct"])) if leaning else None
        base_dir = _dir(r.get("baseline_direction"))
        # Для відфільтрованих сигналів оцінюємо напрямок тригерів: чи фільтр моделі був корисним.
        src_dir = _dir(r.get("trigger_direction")) if r["source"] == "alert_filtered" else model_dir

        for h, hs in st.horizons.items():
            o = r["outcomes"].get(h)
            if o is None:
                continue
            ch = o["change_pct"]
            if trusted and model_dir:
                hs.signals.add(classify(model_dir, ch, cost))
                hs.pnl_signals.add(_favorable(model_dir, ch) - cost)
            if trusted and action == "wait":
                hs.wait_ok.add(abs(ch) <= cost)
            if src_dir:
                hs.sources[r["source"]].add(classify(src_dir, ch, cost))
            if leaning:
                if b:
                    hs.calibration[b].add(_favorable(leaning, ch) > 0)
                hs.pnl_all.add(_favorable(leaning, ch) - cost)
                if base_dir:
                    hs.model_dir.add(_favorable(leaning, ch) > 0)
                    hs.baseline_dir.add(_favorable(base_dir, ch) > 0)
            for t in r.get("triggers") or []:
                td = _dir(t.get("direction"))
                if td:
                    hs.triggers.setdefault(t["name"], HitRate()).add(_favorable(td, ch) > 0)
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
