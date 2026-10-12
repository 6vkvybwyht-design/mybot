"""Індикатори. Рахуються лише по закритих свічках (колонка `closed`)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from bot.config import IndicatorsConfig


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _wilder(values: np.ndarray, period: int) -> np.ndarray:
    """Згладжування Уайлдера: перше значення — просте середнє за period."""
    out = np.full(len(values), np.nan)
    if len(values) < period:
        return out
    out[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    diff = close.diff().to_numpy()[1:]
    gains = _wilder(np.clip(diff, 0, None), period)
    losses = _wilder(np.clip(-diff, 0, None), period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gains / losses
        values = 100 - 100 / (1 + rs)
    values = np.where((losses == 0) & (gains > 0), 100.0, values)
    values = np.where((losses == 0) & (gains == 0), 50.0, values)
    return pd.Series(np.concatenate([[np.nan], values]), index=close.index)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return pd.Series(_wilder(true_range(df).to_numpy(), period), index=df.index)


def vwap_today(df: pd.DataFrame, now_ms: int) -> float | None:
    """VWAP від початку доби UTC по закритих свічках."""
    day_start = (now_ms // 86_400_000) * 86_400_000
    d = df[df["start"] >= day_start]
    vol = d["volume"].sum()
    if d.empty or vol <= 0:
        return None
    typical = (d["high"] + d["low"] + d["close"]) / 3
    return float((typical * d["volume"]).sum() / vol)


def pivots(df: pd.DataFrame, width: int) -> tuple[list[float], list[float]]:
    """Локальні максимуми і мінімуми: екстремум серед width свічок з кожного боку."""
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    res, sup = [], []
    for i in range(width, len(df) - width):
        window_h = highs[i - width : i + width + 1]
        window_l = lows[i - width : i + width + 1]
        if highs[i] == window_h.max():
            res.append(float(highs[i]))
        if lows[i] == window_l.min():
            sup.append(float(lows[i]))
    return res, sup


@dataclass
class Levels:
    support: float | None
    resistance: float | None
    lookback_hours: int


def nearest_levels(
    closed: pd.DataFrame, price: float, lookback_hours: tuple[int, int], width: int, interval_min: int = 5
) -> Levels:
    """Найближчі рівні: спершу шукаємо за коротке вікно, якщо не знайшли — розширюємо."""
    lo_h, hi_h = lookback_hours
    support = resistance = None
    hours = lo_h
    for hours in sorted({lo_h, hi_h}):
        d = closed.tail(hours * 60 // interval_min)
        highs, lows = pivots(d, width)
        if resistance is None:
            above = [h for h in highs if h > price]
            resistance = min(above) if above else None
        if support is None:
            below = [low for low in lows if low < price]
            support = max(below) if below else None
        if support is not None and resistance is not None:
            break
    if resistance is None or support is None:
        # Ціна на екстремумі вікна — беремо максимум/мінімум вікна, якщо він з потрібного боку.
        d = closed.tail(hi_h * 60 // interval_min)
        if resistance is None and not d.empty and d["high"].max() > price:
            resistance = float(d["high"].max())
        if support is None and not d.empty and d["low"].min() < price:
            support = float(d["low"].min())
    return Levels(support, resistance, hours)


def trend(closed: pd.DataFrame, fast: int, slow: int) -> str:
    """up | down | flat за взаємним розташуванням ціни і двох EMA."""
    if len(closed) < slow:
        return "unknown"
    c = closed["close"]
    f, s, last = ema(c, fast).iloc[-1], ema(c, slow).iloc[-1], c.iloc[-1]
    if last > f > s:
        return "up"
    if last < f < s:
        return "down"
    return "flat"


def _r(x: float | None, nd: int = 4) -> float | None:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    return round(float(x), nd)


def compute_5m(df: pd.DataFrame, cfg: IndicatorsConfig, now_ms: int, price: float | None = None) -> dict:
    """Повний набір індикаторів для основного таймфрейму."""
    closed = df[df["closed"]].reset_index(drop=True)
    current = df[~df["closed"]]
    if len(closed) < max(cfg.ema_periods + [cfg.rsi_period + 1, cfg.atr_period + 1, cfg.volume_avg_period]):
        raise ValueError(f"замало закритих 5-хв свічок: {len(closed)}")
    c = closed["close"]
    last = closed.iloc[-1]
    price = price if price is not None else float(last["close"])
    vol_avg = float(closed["volume"].tail(cfg.volume_avg_period).mean())
    cur = current.iloc[-1] if not current.empty else None
    levels = nearest_levels(closed, price, cfg.levels_lookback_hours, cfg.levels_pivot_width)
    atr_v = float(atr(closed, cfg.atr_period).iloc[-1])

    return {
        "last_closed": {
            "start_ms": int(last["start"]),
            "open": _r(last["open"]),
            "high": _r(last["high"]),
            "low": _r(last["low"]),
            "close": _r(last["close"]),
            "volume": _r(last["volume"], 2),
            "direction": "up" if last["close"] > last["open"] else "down" if last["close"] < last["open"] else "flat",
        },
        "current_unclosed": None
        if cur is None
        else {
            "start_ms": int(cur["start"]),
            "open": _r(cur["open"]),
            "high": _r(cur["high"]),
            "low": _r(cur["low"]),
            "close": _r(cur["close"]),
            "volume": _r(cur["volume"], 2),
            "elapsed_sec": int((now_ms - int(cur["start"])) / 1000),
        },
        "rsi": _r(rsi(c, cfg.rsi_period).iloc[-1], 2),
        "ema": {str(p): _r(ema(c, p).iloc[-1]) for p in cfg.ema_periods},
        "atr": _r(atr_v),
        "atr_pct": _r(atr_v / price * 100, 3),
        "vwap_today": _r(vwap_today(closed, now_ms)),
        "volume_avg": _r(vol_avg, 2),
        "rel_volume_last_closed": _r(float(last["volume"]) / vol_avg if vol_avg else None, 2),
        "rel_volume_current": _r(float(cur["volume"]) / vol_avg if cur is not None and vol_avg else None, 2),
        "support": _r(levels.support),
        "resistance": _r(levels.resistance),
        "levels_lookback_hours": levels.lookback_hours,
    }


def recent_candles(df: pd.DataFrame, n: int) -> list[list[float]]:
    """Компактний список останніх свічок [o, h, l, c, v] для моделі (разом з поточною)."""
    tail = df.tail(n)
    return [[_r(r.open), _r(r.high), _r(r.low), _r(r.close), _r(r.volume, 2)] for r in tail.itertuples()]
