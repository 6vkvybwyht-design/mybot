"""Аналіз китів зі стрічки угод. Для CFD у MT5 не використовується, але код лишається робочим."""

import numpy as np
import pytest

from bot.config import WhalesConfig
from bot.market.trades import Trade, TradeStore
from bot.market.whales import WhaleAnalyzer, merge_trades

WHALES = WhalesConfig(threshold_mode="percentile", percentile=99, fixed_usdt=50_000, merge_window_ms=1000,
                      windows_min=[1, 5, 15])


def test_trade_store_dedupe_prune_price_at():
    s = TradeStore(window_ms=10_000)
    assert s.add(Trade(1000, 1.0, 1, 1), "a")
    assert not s.add(Trade(1000, 1.0, 1, 1), "a")
    s.add(Trade(5000, 2.0, 1, -1), "b")
    s.add(Trade(3000, 1.5, 1, 1), "c")  # не по порядку
    assert [t.ts for t in s._trades] == [1000, 3000, 5000]
    assert s.price_at(4000) == 1.5
    assert s.price_at(500) is None
    s.prune(12_500)
    assert len(s) == 2 and s.first_ts == 3000


def test_merge_trades():
    ts = np.array([0, 300, 900, 1200, 2500, 100])
    n = np.array([10.0, 20, 30, 40, 50, 5])
    side = np.array([1, 1, 1, 1, 1, -1])
    c_ts, c_n, c_side = merge_trades(ts, n, side, 1000)
    # покупки: [0,300,900] -> 60; [1200] -> 40 (1200-0 > 1000); [2500] -> 50; продаж 5
    assert list(zip(c_ts.tolist(), c_n.tolist(), c_side.tolist())) == [
        (0, 60.0, 1), (100, 5.0, -1), (1200, 40.0, 1), (2500, 50.0, 1)]


def _store_with_noise(now, n=2000, seed=1):
    rng = np.random.default_rng(seed)
    s = TradeStore(24 * 3_600_000)
    times = np.sort(rng.integers(now - 3 * 3_600_000, now - 20 * 60_000, n))
    for i, t in enumerate(times):
        s.add(Trade(int(t), 60.0, float(rng.uniform(1, 10)), 1 if i % 2 else -1))
    return s


def test_whale_report_percentile_and_windows():
    now = 1_760_000_000_000
    s = _store_with_noise(now)
    # Великий покупець за 2 хв до now, розбитий на 3 частини в межах секунди.
    for k in range(3):
        s.add(Trade(now - 120_000 + k * 200, 60.0, 1000.0, 1))
    s.add(Trade(now - 30_000, 60.0, 2.0, -1))
    w = WhaleAnalyzer(WHALES, s)
    rep = w.report(now, force=True)
    assert rep.threshold_source == "percentile"
    assert 300 < rep.threshold_usdt < 60.0 * 1000  # 99-й персентиль шуму (до 600 USDT)
    w5, w1 = rep.windows[5], rep.windows[1]
    assert w5.whale_buys == 1 and w5.whale_buy_usdt == pytest.approx(180_000)
    assert w5.whale_sells == 0
    assert w5.cvd_usdt == pytest.approx(180_000 - 120)
    assert w1.whale_buys == 0 and w1.trades == 1
    assert rep.avg_abs_whale_delta_5m is not None
    assert rep.to_dict()["windows"]["5m"]["whale_delta_usdt"] == pytest.approx(180_000)


def test_whale_threshold_fallback_and_fixed():
    now = 1_760_000_000_000
    s = TradeStore(24 * 3_600_000)
    s.add(Trade(now - 1000, 60.0, 1.0, 1))
    rep = WhaleAnalyzer(WHALES, s).report(now, force=True)
    assert rep.threshold_source == "fixed_fallback" and rep.avg_abs_whale_delta_5m is None
    fixed = WHALES.model_copy(update={"threshold_mode": "fixed", "fixed_usdt": 10.0})
    rep = WhaleAnalyzer(fixed, s).report(now, force=True)
    assert rep.threshold_source == "fixed" and rep.windows[1].whale_buys == 1
