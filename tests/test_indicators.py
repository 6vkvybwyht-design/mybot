import math

import numpy as np
import pandas as pd
import pytest

from bot.market import indicators as ind
from tests.helpers import CFG, candles, now_after

# Класичний приклад RSI(14) Уайлдера (StockCharts).
WILDER = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
          46.28, 46.28, 46.00, 46.03, 46.41, 46.22]
WILDER_RSI = [70.53, 66.32, 66.55, 69.41, 66.36]


def _textbook_rsi(closes, n):
    """Незалежна реалізація «як у книжці» Уайлдера."""
    ch = [b - a for a, b in zip(closes, closes[1:])]
    g = sum(max(c, 0) for c in ch[:n]) / n
    l = sum(max(-c, 0) for c in ch[:n]) / n
    out = [100 - 100 / (1 + g / l)]
    for c in ch[n:]:
        g = (g * (n - 1) + max(c, 0)) / n
        l = (l * (n - 1) + max(-c, 0)) / n
        out.append(100 - 100 / (1 + g / l))
    return out


def test_rsi_matches_reference():
    r = ind.rsi(pd.Series(WILDER), 14)
    assert r.iloc[:14].isna().all()
    assert r.iloc[14:].tolist() == pytest.approx(_textbook_rsi(WILDER, 14), abs=1e-9)
    # StockCharts округлює проміжні середні, тому розбіжність до ~0.1
    assert r.iloc[14:].tolist() == pytest.approx(WILDER_RSI, abs=0.1)


def test_rsi_extremes():
    assert ind.rsi(pd.Series(np.arange(30, dtype=float)), 14).iloc[-1] == 100
    assert ind.rsi(pd.Series(np.full(30, 5.0)), 14).iloc[-1] == 50
    assert ind.rsi(pd.Series(np.arange(30, 0, -1, dtype=float)), 14).iloc[-1] == 0


def test_ema_recursion():
    s = pd.Series([1.0, 2, 3, 4, 5])
    k = 2 / (3 + 1)
    expected = 1.0
    for x in [2, 3, 4, 5]:
        expected = x * k + expected * (1 - k)
    assert ind.ema(s, 3).iloc[-1] == pytest.approx(expected)


def test_atr_constant_range():
    df = candles([10.0] * 30, spread=1.0)  # high-low = 2, без гепів
    assert ind.atr(df, 14).iloc[-1] == pytest.approx(2.0)


def test_atr_counts_gaps():
    df = pd.DataFrame({"high": [11, 21], "low": [9, 19], "close": [10, 20]})
    assert ind.true_range(df).tolist() == [2, 11]


def test_vwap_today_only_counts_today():
    day = 1_760_054_400_000  # 00:00 UTC
    df = pd.DataFrame(
        {
            "start": [day - 300_000, day, day + 300_000],
            "high": [100.0, 12, 22],
            "low": [100.0, 8, 18],
            "close": [100.0, 10, 20],
            "volume": [1000.0, 1, 3],
        }
    )
    # (10*1 + 20*3) / 4 = 17.5
    assert ind.vwap_today(df, day + 600_000) == pytest.approx(17.5)


def test_levels_and_pivots():
    closes = [10, 10.5, 11, 12, 11, 10.5, 10, 9, 8, 9, 10, 10.2, 10.1, 10.3, 10.2, 10.25]
    df = candles(closes, spread=0.0, unclosed_last=False)
    lv = ind.nearest_levels(df, 10.25, (4, 12), 2)
    assert lv.resistance == 10.3  # локальний максимум свічки 13
    assert lv.support == 8
    assert ind.nearest_levels(df, 10.31, (4, 12), 2).resistance == 12


def test_levels_fallback_to_window_extreme():
    df = candles(list(np.linspace(10, 20, 60)), spread=0.0, unclosed_last=False)
    lv = ind.nearest_levels(df, 25.0, (4, 12), 3)
    assert lv.resistance is None
    assert lv.support is not None and lv.support < 25


def test_trend():
    up = candles(list(np.linspace(10, 20, 80)), unclosed_last=False)
    down = candles(list(np.linspace(20, 10, 80)), unclosed_last=False)
    assert ind.trend(up, 21, 50) == "up"
    assert ind.trend(down, 21, 50) == "down"
    assert ind.trend(up.head(10), 21, 50) == "unknown"


def test_compute_5m_ignores_unclosed_candle():
    closes = list(np.linspace(60, 61, 100)) + [99.0]  # остання незакрита — аномальна
    vols = [100.0] * 100 + [500.0]
    df = candles(closes, volume=vols)
    out = ind.compute_5m(df, CFG.indicators, now_after(df))
    assert out["last_closed"]["close"] == pytest.approx(61.0)
    assert out["current_unclosed"]["close"] == 99.0
    assert out["rsi"] == 100.0
    assert out["rel_volume_current"] == 5.0
    assert out["rel_volume_last_closed"] == 1.0
    assert out["ema"]["9"] < 61.01


def test_compute_5m_needs_history():
    df = candles([1.0] * 10)
    with pytest.raises(ValueError):
        ind.compute_5m(df, CFG.indicators, now_after(df))
