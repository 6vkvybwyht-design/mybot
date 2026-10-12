import json

import pytest

from bot.market.mt5_source import MT5Unavailable
from bot.news.calendar import EventCalendar
from bot.snapshot import SnapshotError, build_snapshot, collect
from tests.conftest import mt5_market
from tests.fake_mt5 import FakeMT5
from tests.helpers import CFG_24_7 as CFG

BYBIT_ONLY = {"order_flow", "orderbook", "open_interest", "ticker", "long_short_accounts", "fees"}


async def _connected(fake=None):
    market, fake = mt5_market(fake)
    await market.source.connect()
    return market, fake


async def test_full_snapshot():
    market, _ = await _connected()
    assert market.instrument.tick_size == 0.001
    snap = await collect(CFG, market, calendar=EventCalendar(CFG.calendar))
    d = snap.data
    assert d["symbol"] == "USOUSD.s" and d["price"] == 61.0          # ціна графіка — Bid
    assert d["quote"]["ask"] == pytest.approx(61.03) and d["quote"]["spread_points"] == pytest.approx(30)
    assert d["costs"]["round_trip_pct"] == pytest.approx(0.03 / 61 * 100, abs=1e-4)
    assert set(d["tf_5m"]) >= {"rsi", "ema", "atr", "vwap_today", "support", "resistance", "current_unclosed"}
    assert d["trend"].keys() == {"15m", "60m"}
    assert d["tick_activity"]["ticks_5m"] > 0 and d["tick_activity"]["ratio_5m"] == pytest.approx(1, rel=0.1)
    assert d["spread_state"]["widened"] is False and snap.spread_warning is None
    assert not BYBIT_ONLY & d.keys()                                   # жодних китів, стакану, OI, funding
    assert "events" in d and snap.missing == []
    assert snap.baseline_direction in {"up", "down", "flat"}
    json.loads(snap.to_json())
    assert len(snap.to_json()) < 8_000


async def test_spread_widening_flagged():
    fake = FakeMT5()
    market, _ = await _connected(fake)
    fake.history_spread = 0.03                    # медіана за годину — звичайний спред
    fake.spread = 0.09                            # поточний тік — утричі ширший
    snap = await collect(CFG, market)
    assert snap.data["spread_state"]["widened"] is True
    assert snap.spread_warning and "спред" in snap.spread_warning


async def test_snapshot_survives_missing_source():
    fake = FakeMT5()
    market, _ = await _connected(fake)
    fake.copy_ticks_from = lambda *a: fake._fail((-1, "boom"))
    snap = await collect(CFG, market)
    assert "tick_activity" in snap.missing and "tick_activity" not in snap.data


async def test_snapshot_without_5m_fails():
    fake = FakeMT5()
    market, _ = await _connected(fake)
    raw = await market.fetch_raw([1, 15])
    with pytest.raises(SnapshotError):
        build_snapshot(CFG, market, raw)


async def test_not_connected_raises():
    market, _ = mt5_market()
    with pytest.raises(MT5Unavailable):
        await market.fetch_raw()
