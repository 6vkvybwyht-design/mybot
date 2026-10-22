"""Спільні генератори синтетичних даних для тестів."""

from __future__ import annotations

import numpy as np
import pandas as pd

from bot.config import load_config

CFG = load_config()
# Для тестів ринок цілодобовий, щоб результат не залежав від дня тижня і години запуску.
# Розклад сесій перевіряється окремо в test_market_hours.py з фіксованим часом.
_ALWAYS = CFG.market.session.model_copy(update={"week_close": CFG.market.session.week_open, "daily_break": None})
CFG_24_7 = CFG.model_copy(update={"market": CFG.market.model_copy(update={"session": _ALWAYS})})


def candles(closes, interval_min=5, start_ms=1_760_000_000_000 // 300_000 * 300_000, spread=0.1, volume=100.0,
            unclosed_last=True):
    rows = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        rows.append(
            {
                "start": start_ms + i * interval_min * 60_000,
                "open": o,
                "high": max(o, c) + spread,
                "low": min(o, c) - spread,
                "close": c,
                "volume": volume if np.isscalar(volume) else volume[i],
                "turnover": 0.0,
            }
        )
        prev = c
    df = pd.DataFrame(rows)
    df["closed"] = True
    if unclosed_last:
        df.loc[df.index[-1], "closed"] = False
    return df


def now_after(df, interval_min=5, extra_ms=60_000):
    """Час, коли остання свічка ще не закрита."""
    return int(df["start"].iloc[-1]) + extra_ms
