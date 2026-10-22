
import pandas as pd
import pytest

from bot.tracking.db import SCHEMA, Database, PredictionRow
from bot.tracking.outcomes import OutcomeChecker, evaluate
from bot.tracking.stats import LOSS, NEUTRAL, WIN, PnL, bucket, classify, compute, export_csv, wilson
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
    assert [bucket(p) for p in (50, 51, 54, 55, 59, 60, 64, 65, 69, 70, 100)] == [
        None, "50–55%", "50–55%", "55–60%", "55–60%", "60–65%", "60–65%", "65–70%", "65–70%", "70%+", "70%+"]


def test_wilson_interval():
    assert wilson(0, 0) is None
    lo, hi = wilson(5, 10)
    assert (round(lo, 1), round(hi, 1)) == (23.7, 76.3)
    lo, hi = wilson(0, 10)
    assert lo == 0 and round(hi, 1) == 27.8
    lo, hi = wilson(10, 10)
    assert round(lo, 1) == 72.2 and hi == pytest.approx(100)
    lo, hi = wilson(600, 1000)       # велика вибірка — вузький інтервал
    assert hi - lo < 7


def test_pnl_total_and_drawdown():
    p = PnL()
    for t in (1.0, -2.0, 0.5, -1.0, 3.0):
        p.add(t)
    # крива: 1, -1, -0.5, -1.5, 1.5; максимум 1 -> мінімум після нього -1.5
    assert p.total == pytest.approx(1.5) and p.max_drawdown == pytest.approx(2.5)
    assert p.mean == pytest.approx(0.3) and (p.wins.hits, p.wins.n) == (3, 5)
    q = PnL()
    q.add(-0.4)
    assert q.max_drawdown == pytest.approx(0.4)   # просадка від старту 0
    assert PnL().max_drawdown == 0 and PnL().mean is None


def row(i, source, action, lp, ch, trig_dir=None, triggers=(), base="up", cost=None, created=None):
    """ch — зміна у % на всіх горизонтах або словник {горизонт: зміна}."""
    chs = ch if isinstance(ch, dict) else {h: ch for h in (5, 10, 15)}
    outs = {h: {"price": 1, "change_pct": c, "hit": None} for h, c in chs.items()}
    return {"id": i, "created_ms": created if created is not None else T0 + i * 60_000, "source": source,
            "price": 100, "action": action, "long_pct": lp,
            "short_pct": 100 - lp, "confidence": "medium", "entry": None, "stop_loss": None, "take_profit": None,
            "trigger_direction": trig_dir, "triggers": [{"name": t, "direction": trig_dir} for t in triggers],
            "baseline_direction": base, "model": "m", "prompt_version": "p", "outcomes": outs, "cost_pct": cost}


def test_compute_main_horizon_is_5_minutes():
    # На 5 хв і 15 хв рух різний — основні метрики мають братися з 5 хв.
    rows = [
        row(1, "manual", "long", 65, {5: 0.5, 10: 0.1, 15: -0.5}),
        row(2, "manual", "short", 30, {5: 0.5, 10: 0.1, 15: -0.5}),
        row(3, "alert", "long", 72, {5: 0.05, 10: 0.1, 15: 0.3}, "up", ["breakout", "impulse"]),
        row(4, "manual", "wait", 53, {5: 0.02, 10: 0.1, 15: 0.4}),
        row(5, "alert_filtered", "wait", 57, {5: -0.5, 10: 0.1, 15: 0.5}, "up", ["tick_volume"], base="down"),
    ]
    st = compute(rows, [5, 10, 15], RT)
    assert st.main_horizon == 5 and st.main is st.horizons[5]
    assert st.total == 5 and st.by_source_count == {"manual": 3, "alert": 1, "alert_filtered": 1}

    m = st.main
    # сигнали (ручні + авто, лонг/шорт) на 5 хв: +0.5 лонг вдалий, +0.5 шорт невдалий, +0.05 нейтральний
    assert (m.signals.n, m.signals.win, m.signals.loss, m.signals.neutral) == (3, 1, 1, 1)
    # «чекати» на 5 хв: рух 0.02% < 0.11% — вірно (на 15 хв рух 0.4% — було б невірно)
    assert (m.wait_ok.n, m.wait_ok.hits) == (1, 1)
    assert (st.horizons[15].wait_ok.n, st.horizons[15].wait_ok.hits) == (1, 0)

    # калібрування на 5 хв по всіх прогнозах з напрямком > 50% (включно з wait і відфільтрованими)
    cal = {k: (v.n, v.hits) for k, v in m.calibration.items()}
    assert cal == {"50–55%": (1, 1),      # 53% лонг, +0.02
                   "55–60%": (1, 0),      # 57% лонг, -0.5
                   "60–65%": (0, 0),
                   "65–70%": (1, 1),      # 65% лонг, +0.5
                   "70%+": (2, 1)}        # 70% шорт (+0.5 — ні), 72% лонг (+0.05 — так)

    # бот vs свічка на тих самих 5 прогнозах (у всіх є напрямок і непласка свічка)
    assert (m.model_dir.n, m.model_dir.hits) == (5, 3)
    # свічка: up, up, up, up -> +0.5,+0.5,+0.05,+0.02 вгадано; down при -0.5 -> вгадано
    assert (m.baseline_dir.n, m.baseline_dir.hits) == (5, 5)

    # тригери — напрямок на 5 хв
    assert {k: (v.n, v.hits) for k, v in m.triggers.items()} == {
        "breakout": (1, 1), "impulse": (1, 1), "tick_volume": (1, 0)}

    # P&L на 5 хв: кожен прогноз з напрямком, мінус 0.11%
    expected_all = [0.5 - RT, -0.5 - RT, 0.05 - RT, 0.02 - RT, -0.5 - RT]
    assert m.pnl_all.trades == pytest.approx(expected_all)
    assert m.pnl_signals.trades == pytest.approx([0.5 - RT, -0.5 - RT, 0.05 - RT])
    assert m.sources["alert_filtered"].loss == 1   # тригери казали вгору, на 5 хв -0.5


def test_compute_requires_main_horizon():
    with pytest.raises(ValueError):
        compute([], [10, 15], RT)


def test_pnl_uses_cost_per_prediction_and_time_order():
    rows = [row(2, "manual", "long", 65, 0.3, cost=0.05), row(1, "manual", "short", 30, 0.2)]  # не по порядку
    p = compute(rows, [5, 10, 15], RT).main.pnl_all
    assert p.trades == pytest.approx([-0.2 - RT, 0.3 - 0.05])   # спершу ранній прогноз (id=1)
    assert p.max_drawdown == pytest.approx(0.2 + RT)


def test_baseline_skips_flat_candle_and_5050():
    rows = [row(1, "manual", "long", 60, 0.3, base="flat"), row(2, "manual", "wait", 50, 0.3)]
    m = compute(rows, [5, 10, 15], RT).main
    assert m.model_dir.n == 0 and m.pnl_all.n == 1   # 50/50 — без напрямку, пласка свічка — без порівняння


def test_cost_per_prediction():
    # Рух +0.05%: при витратах 0.028% (звичайний спред) — вдалий, при 0.2% (широкий спред) — нейтральний.
    st = compute([row(1, "manual", "long", 65, 0.05, cost=0.028), row(2, "manual", "long", 65, 0.05, cost=0.2),
                  row(3, "manual", "long", 65, 0.05)], [5, 10, 15], RT)
    t = st.main.signals
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
