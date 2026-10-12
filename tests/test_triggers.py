import numpy as np
import pandas as pd
import pytest

from bot.market.orderbook import BookMetrics
from bot.market.whales import WhaleReport, WindowStats
from bot.triggers import (
    DOWN, UP, OrderbookTracker, TriggerHit, breakout_trigger, evaluate_all, impulse_trigger, oi_trigger,
    strongest_direction, volume_trigger, whales_trigger,
)
from tests.helpers import CFG, candles


def report(delta, avg):
    w = WindowStats(5, 10, 1e6, delta, 1 if delta > 0 else 0, max(delta, 0), 1 if delta < 0 else 0, max(-delta, 0))
    return WhaleReport(1000, "percentile", 1440, {5: w}, avg)


def test_whales_trigger():
    assert whales_trigger(report(400_000, 100_000), 3).direction == UP
    assert whales_trigger(report(-400_000, 100_000), 3).direction == DOWN
    assert whales_trigger(report(250_000, 100_000), 3) is None
    assert whales_trigger(report(400_000, None), 3) is None   # немає історії


def test_volume_trigger():
    df = candles([60.0] * 25 + [60.5], volume=[100.0] * 25 + [300.0])
    assert volume_trigger(df, 2.5, 20).direction == UP
    df = candles([60.0] * 25 + [59.5], volume=[100.0] * 25 + [300.0])
    assert volume_trigger(df, 2.5, 20).direction == DOWN
    df = candles([60.0] * 25 + [60.5], volume=[100.0] * 25 + [200.0])
    assert volume_trigger(df, 2.5, 20) is None
    closed_only = candles([60.0] * 26, volume=[100.0] * 25 + [900.0], unclosed_last=False)
    assert volume_trigger(closed_only, 2.5, 20) is None   # немає поточної свічки


def test_oi_trigger():
    oi = pd.DataFrame({"ts": range(13), "oi": [100.0] * 9 + [100, 100, 100, 101.5]})
    assert oi_trigger(oi, 61.2, 61.0, 1.0).direction == UP
    assert oi_trigger(oi, 60.8, 61.0, 1.0).direction == DOWN       # OI росте, ціна падає -> шорти
    assert oi_trigger(oi, 61.0, 61.0, 1.0) is None                 # ціна не рухалась
    assert oi_trigger(oi, 61.2, 61.0, 2.0) is None
    assert oi_trigger(None, 61.2, 61.0, 1.0) is None


def test_breakout_trigger():
    base = [60.0, 60.2, 60.5, 60.2, 60.0] * 8          # опір 60.5
    df = candles(base + [60.3, 60.8, 60.8], spread=0.0)  # передостання закрита 60.8 > 60.5, попередня 60.3
    hit = breakout_trigger(df, (4, 12), 2)
    assert hit is not None and hit.direction == UP and "60.5" in hit.detail
    df = candles(base + [60.3, 60.4, 60.4], spread=0.0)
    assert breakout_trigger(df, (4, 12), 2) is None
    down = candles([60.0, 59.8, 59.5, 59.8, 60.0] * 8 + [59.7, 59.2, 59.2], spread=0.0)
    assert breakout_trigger(down, (4, 12), 2).direction == DOWN


def test_impulse_trigger():
    assert impulse_trigger(61.3, 61.0, 0.2, 1.0).direction == UP
    assert impulse_trigger(60.7, 61.0, 0.2, 1.0).direction == DOWN
    assert impulse_trigger(61.1, 61.0, 0.2, 1.0) is None
    assert impulse_trigger(61.3, None, 0.2, 1.0) is None


def _m(share):
    return BookMetrics(1, 1, 1, 0, 0.3, share * 100, (1 - share) * 100, share, [], [])


def test_orderbook_tracker_needs_hold():
    t = OrderbookTracker(0.7, 30)
    t.update(_m(0.75), 0)
    assert t.hit(29_000) is None
    t.update(_m(0.8), 15_000)
    assert t.hit(30_000).direction == UP
    t.update(_m(0.6), 31_000)             # дисбаланс зник — відлік з нуля
    t.update(_m(0.75), 32_000)
    assert t.hit(50_000) is None
    t.update(_m(0.2), 60_000)
    assert t.hit(95_000).direction == DOWN
    t.update(None, 96_000)                # немає даних — скидаємо
    assert t.hit(200_000) is None


def test_strongest_direction():
    h = lambda d: TriggerHit("x", d, "")  # noqa: E731
    assert strongest_direction([h(UP), h(UP), h(UP), h(DOWN)], 3) == UP
    assert strongest_direction([h(UP), h(UP), h(DOWN)], 3) is None
    assert strongest_direction([h(UP), h(UP), h(DOWN), h(DOWN)], 2) is None   # нічия
    assert strongest_direction([h(DOWN)] * 4, 4) == DOWN


def test_evaluate_all_synthetic_pump():
    # Синтетичний памп: пробій опору, свічка з обсягом ×5, імпульс за хвилину.
    closes = [60.0, 60.2, 60.5, 60.2, 60.0] * 8 + [60.3, 60.8, 61.3]
    vols = [100.0] * (len(closes) - 1) + [500.0]
    df = candles(closes, spread=0.05, volume=vols)
    book = OrderbookTracker(0.7, 30)
    book.update(_m(0.8), 0)
    hits = evaluate_all(CFG, k5=df, whales=report(500_000, 100_000),
                        oi=pd.DataFrame({"oi": [100.0] * 10 + [100, 100, 102]}),
                        price_now=61.3, price_1m_ago=60.8, price_15m_ago=60.3, book=book, now_ms=60_000)
    names = {h.name for h in hits}
    assert names == {"whales", "volume", "open_interest", "orderbook", "breakout", "impulse"}
    assert all(h.direction == UP for h in hits)
