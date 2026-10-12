
import pandas as pd
import pytest

from bot.tracking.db import SCHEMA, Database, PredictionRow
from bot.tracking.outcomes import OutcomeChecker, evaluate
from bot.tracking.stats import LOSS, NEUTRAL, WIN, bucket, classify, compute, export_csv
from tests.conftest import mt5_market

T0 = 1_760_000_000_000 // 60_000 * 60_000 + 20_000   # прогноз о :20 с


def pred(action="long", price=100.0, stop=99.0, take=101.0, created=T0):
    return PredictionRow(1, created, "manual", price, action, 60, 40, 100.0, stop, take, None, [], "up")


def k1(rows, closed_upto=None, spread=0):
    """rows: [(хвилина від T0, high, low, close)]; spread — спред свічок у пунктах."""
    base = (T0 // 60_000) * 60_000
    df = pd.DataFrame([{"start": base + m * 60_000, "open": c, "high": h, "low": low, "close": c, "volume": 1.0,
                        "spread": spread} for m, h, low, c in rows])
    df["closed"] = True if closed_upto is None else df["start"] <= base + closed_upto * 60_000
    return df


def test_evaluate_take_hit_and_horizon_price():
    rows = [(m, 100.2, 99.8, 100.1) for m in range(5)] + [(5, 101.5, 100.5, 101.2)]
    o = evaluate(pred(), 5, k1(rows))
    # горизонт 5 хв від :20 с потрапляє в свічку хвилини 5
    assert o.price == 101.2 and o.change_pct == pytest.approx(1.2)
    assert o.hit == "take" and o.hit_after_min == pytest.approx((6 * 60 - 20) / 60, abs=0.1)


def test_evaluate_stop_first_and_conservative_same_candle():
    rows = [(0, 100.1, 99.9, 100), (1, 101.5, 98.5, 100), (2, 100, 100, 100), (3, 100, 100, 100),
            (4, 100, 100, 100), (5, 100, 100, 100)]
    assert evaluate(pred(), 5, k1(rows)).hit == "stop"


def test_evaluate_short_and_wait():
    rows = [(m, 100.0, 98.9, 99.0) for m in range(6)]
    o = evaluate(pred("short", stop=101.0, take=99.0), 5, k1(rows))
    assert o.hit == "take" and o.change_pct == pytest.approx(-1.0)
    assert evaluate(pred("wait", stop=None, take=None), 5, k1(rows)).hit is None


def test_short_checked_at_ask():
    # Bid торкнувся тейку 99.0, але Ask = Bid + 30 п. (0.03) — шорт по тейку не закрився б.
    rows = [(m, 100.0, 99.0, 99.5) for m in range(6)]
    short = pred("short", stop=101.0, take=99.0)
    assert evaluate(short, 5, k1(rows, spread=30)).hit == "take"            # без point — як раніше, по Bid
    assert evaluate(short, 5, k1(rows, spread=30), point=0.001).hit is None
    # І стоп шорту спрацьовує раніше: high 100.98 + 0.03 >= 101.0
    rows = [(m, 100.98, 99.9, 100.0) for m in range(6)]
    assert evaluate(short, 5, k1(rows, spread=30), point=0.001).hit == "stop"
    # Лонг закривається по Bid — спред на нього не впливає.
    rows = [(m, 101.0, 99.9, 100.5) for m in range(6)]
    assert evaluate(pred(), 5, k1(rows, spread=30), point=0.001).hit == "take"


def test_horizon_inside_trading_break():
    # Торги до хвилини 3, перерва, відкриття на хвилині 70: ціна горизонту — останнє закриття перед перервою.
    rows = [(m, 100.1, 99.9, 100.0 + m * 0.1) for m in range(4)]
    assert evaluate(pred(), 5, k1(rows)) is None                           # торги ще не відновилися
    o = evaluate(pred(), 5, k1(rows + [(70, 102, 101, 101.5)]))
    assert o is not None and o.price == pytest.approx(100.3) and o.hit is None


def test_evaluate_waits_for_closed_candle():
    rows = [(m, 100, 100, 100) for m in range(6)]
    assert evaluate(pred(), 5, k1(rows, closed_upto=4)) is None
    assert evaluate(pred(), 5, k1(rows[:4])) is None


async def test_checker_writes_three_horizons(tmp_path):
    db = Database(tmp_path / "x.db")
    await db.connect()
    market, fake = mt5_market()
    await market.source.connect()
    now = int(fake.now * 1000)
    pid = await db.insert_prediction(created_ms=now - 17 * 60_000, source="manual", price=61.0,
                                     response={"action": "long", "long_pct": 60, "short_pct": 40,
                                               "entry": 61.0, "stop_loss": 60.0, "take_profit": 62.0},
                                     snapshot={"x": 1}, baseline_direction="up")
    young = await db.insert_prediction(created_ms=now - 7 * 60_000, source="manual", price=61.0,
                                       response={"action": "wait"}, snapshot={})
    checker = OutcomeChecker(db, market.source, [5, 10, 15])
    assert await checker.check_once(now) == 4   # 3 для старого + 5 хв для молодшого
    rows = {r["id"]: r for r in await db.predictions_with_outcomes()}
    assert set(rows[pid]["outcomes"]) == {5, 10, 15}
    assert set(rows[young]["outcomes"]) == {5}
    assert await checker.check_once(now) == 0   # повторно не пише
    assert "copy_rates_range" in fake.calls     # ціни для перевірки — з MT5
    market.source.connected = False
    await db.insert_prediction(created_ms=now - 30 * 60_000, source="manual", price=61.0,
                               response={"action": "wait"}, snapshot={})
    assert await checker.check_once(now) == 0   # без зв'язку з MT5 — тихо пропускаємо
    await db.close()


async def test_db_settings_and_events(tmp_path):
    from datetime import datetime, timezone
    from bot.news.calendar import Event
    db = Database(tmp_path / "y.db")
    await db.connect()
    assert await db.get_setting("alerts_enabled", True) is True
    await db.set_setting("alerts_enabled", False)
    await db.set_setting("alerts_enabled", False)
    assert await db.get_setting("alerts_enabled") is False
    at = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)
    await db.add_event(Event("ФРС", at, "user"), 0)
    assert (await db.load_events())[0] == Event("ФРС", at, "user")
    await db.close()
    db2 = Database(tmp_path / "y.db")  # переживає перезапуск
    await db2.connect()
    assert await db2.get_setting("alerts_enabled") is False and len(await db2.load_events()) == 1
    await db2.close()


# ---------- статистика ----------

RT = 0.11


def test_classify():
    assert classify("long", 0.2, RT) == WIN
    assert classify("long", -0.2, RT) == LOSS
    assert classify("short", -0.2, RT) == WIN
    assert classify("long", 0.05, RT) == NEUTRAL


def test_bucket():
    assert [bucket(p) for p in (55, 60, 69, 70, 90)] == ["50–60%", "60–70%", "60–70%", "70%+", "70%+"]


def row(i, source, action, lp, ch, trig_dir=None, triggers=(), base="up", cost=None):
    outs = {h: {"price": 1, "change_pct": ch, "hit": None} for h in (5, 10, 15)}
    return {"id": i, "created_ms": T0, "source": source, "price": 100, "action": action, "long_pct": lp,
            "short_pct": 100 - lp, "confidence": "medium", "entry": None, "stop_loss": None, "take_profit": None,
            "trigger_direction": trig_dir, "triggers": [{"name": t, "direction": trig_dir} for t in triggers],
            "baseline_direction": base, "model": "m", "prompt_version": "p", "outcomes": outs, "cost_pct": cost}


def test_compute_stats():
    rows = [
        row(1, "manual", "long", 65, 0.5),            # вдалий, кошик 60–70, напрям вгадано
        row(2, "manual", "short", 30, 0.5),           # невдалий, кошик 70+, не вгадано
        row(3, "alert", "long", 72, 0.05, "up", ["tick_volume", "impulse"]),  # нейтральний
        row(4, "manual", "wait", 52, 0.02),           # wait і руху немає
        row(5, "alert_filtered", "wait", 50, -0.5, "up", ["tick_volume"], base="down"),
    ]
    st = compute(rows, [5, 10, 15], RT)
    assert st.total == 5 and st.by_source_count == {"manual": 3, "alert": 1, "alert_filtered": 1}
    t15 = st.horizons[15]
    assert (t15.n, t15.win, t15.loss, t15.neutral) == (3, 1, 1, 1)
    assert st.wait_ok[15].n == 1 and st.wait_ok[15].hits == 1
    assert st.calibration["60–70%"].n == 1 and st.calibration["60–70%"].hits == 1
    assert st.calibration["70%+"].n == 2 and st.calibration["70%+"].hits == 1
    assert st.calibration["50–60%"].n == 1   # wait 52/48 теж калібрується
    assert st.sources["alert_filtered"].n == 1 and st.sources["alert_filtered"].loss == 1
    assert st.triggers["tick_volume"].n == 2 and st.triggers["tick_volume"].hits == 1
    m, b = st.model_vs_baseline
    # базова лінія «up» на тих самих 3 прогнозах: +0.5, +0.5, +0.05 -> 2 вдалих; модель — 1
    assert (m.n, m.win, b.n, b.win) == (3, 1, 3, 2)
    assert st.baseline_all.n == 5


def test_cost_per_prediction():
    # Рух +0.05%: при витратах 0.028% (звичайний спред) — вдалий, при 0.2% (широкий спред) — нейтральний.
    st = compute([row(1, "manual", "long", 65, 0.05, cost=0.028), row(2, "manual", "long", 65, 0.05, cost=0.2),
                  row(3, "manual", "long", 65, 0.05)], [5, 10, 15], RT)
    t = st.horizons[15]
    assert (t.win, t.neutral) == (1, 2)          # третій — без записаних витрат, береться RT = 0.11


def test_export_csv():
    data = export_csv([row(1, "manual", "long", 65, 0.5, triggers=["impulse"], cost=0.028)],
                      [5, 10, 15]).decode("utf-8-sig")
    head, line = data.strip().splitlines()
    assert head.startswith("id,time_utc,source") and "change_15m_pct" in head and "cost_pct" in head
    assert ",manual," in line and "impulse" in line and "0.028" in line


async def test_db_migrates_old_schema_and_stores_cost(tmp_path):
    import aiosqlite
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:      # база попередньої версії — без cost_pct
        await conn.executescript(SCHEMA.replace(",\n    cost_pct REAL", ""))
        await conn.commit()
    db = Database(path)
    await db.connect()
    await db.insert_prediction(created_ms=1, source="manual", price=93.7, response={"action": "wait"},
                               snapshot={}, cost_pct=0.028)
    assert (await db.predictions_with_outcomes())[0]["cost_pct"] == 0.028
    await db.close()
