from bot.triggers import (
    DOWN, UP, TriggerHit, breakout_trigger, evaluate_all, impulse_trigger, spread_warning, strongest_direction,
    tick_volume_trigger,
)
from tests.helpers import CFG, candles


def act(ratio_5m, p5=61.0, p1=None):
    return {"ratio_5m": ratio_5m, "price_5m_ago": p5, "price_1m_ago": p1}


def test_tick_volume_trigger():
    assert tick_volume_trigger(act(3.0), 61.2, 2.5).direction == UP
    assert tick_volume_trigger(act(3.0), 60.8, 2.5).direction == DOWN
    assert tick_volume_trigger(act(2.0), 61.2, 2.5) is None          # сплеску немає
    assert tick_volume_trigger(act(3.0), 61.0, 2.5) is None          # ціна стоїть — напрямку немає
    assert tick_volume_trigger(act(None), 61.2, 2.5) is None
    assert tick_volume_trigger(None, 61.2, 2.5) is None


def test_breakout_trigger_needs_margin():
    base = [60.0, 60.2, 60.5, 60.2, 60.0] * 8          # опір 60.5
    df = candles(base + [60.3, 60.8, 60.8], spread=0.0)
    hit = breakout_trigger(df, (4, 12), 2)
    assert hit is not None and hit.direction == UP and "60.5" in hit.detail
    assert breakout_trigger(df, (4, 12), 2, margin=0.2) is not None   # 60.8 > 60.5 + 0.2
    assert breakout_trigger(df, (4, 12), 2, margin=0.4) is None       # закриття ближче за 2 спреди
    df = candles(base + [60.3, 60.4, 60.4], spread=0.0)
    assert breakout_trigger(df, (4, 12), 2) is None
    down = candles([60.0, 59.8, 59.5, 59.8, 60.0] * 8 + [59.7, 59.2, 59.2], spread=0.0)
    assert breakout_trigger(down, (4, 12), 2, margin=0.2).direction == DOWN


def test_impulse_trigger():
    assert impulse_trigger(61.3, 61.0, 0.2, 1.0).direction == UP
    assert impulse_trigger(60.7, 61.0, 0.2, 1.0).direction == DOWN
    assert impulse_trigger(61.1, 61.0, 0.2, 1.0) is None
    assert impulse_trigger(61.3, None, 0.2, 1.0) is None


def test_spread_warning():
    w = spread_warning(0.06, 0.026, 2.0)
    assert w is not None and w.ratio > 2 and "спред" in w.text
    assert spread_warning(0.026, 0.026, 2.0) is None
    assert spread_warning(0.06, None, 2.0) is None


def test_strongest_direction():
    h = lambda d: TriggerHit("x", d, "")  # noqa: E731
    assert strongest_direction([h(UP), h(UP), h(DOWN)], 2) == UP
    assert strongest_direction([h(UP), h(DOWN)], 1) is None             # нічия
    assert strongest_direction([h(DOWN)] * 3, 3) == DOWN
    assert strongest_direction([h(UP)], 2) is None


def test_evaluate_all_synthetic_pump():
    # Синтетичний памп: пробій опору на 2+ спреди, сплеск тіків ×3, імпульс за хвилину.
    closes = [60.0, 60.2, 60.5, 60.2, 60.0] * 8 + [60.3, 60.8, 61.3]
    df = candles(closes, spread=0.05)
    hits = evaluate_all(CFG, k5=df, activity=act(3.0, p5=60.6, p1=60.8), price_now=61.3, spread=0.026)
    assert {h.name for h in hits} == {"tick_volume", "impulse", "breakout"}
    assert all(h.direction == UP for h in hits)
    calm = evaluate_all(CFG, k5=df, activity=act(1.0, p5=61.3, p1=61.3), price_now=61.3, spread=0.026)
    assert [h.name for h in calm] == ["breakout"]


def test_sensitivity_levels_within_trigger_count():
    assert CFG.alerts.sensitivity_levels == {"high": 1, "medium": 2, "low": 3}
