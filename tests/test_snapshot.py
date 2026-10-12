import json
import time

import pytest

from bot.market.bybit_ws import BybitWS
from bot.market.trades import Trade
from bot.news.calendar import EventCalendar
from bot.snapshot import MarketService, SnapshotError, build_snapshot, collect
from tests.fake_bybit import FakeBybit, rest_client
from tests.helpers import CFG


def _market(fake):
    ws = BybitWS(CFG.market, CFG.network)
    return MarketService(CFG, rest=rest_client(fake, CFG), ws=ws)


def _feed_ws(market, price=61.0):
    now = int(time.time() * 1000)
    for i in range(300):
        market.ws.trades.add(Trade(now - 600_000 + i * 2000, price, 3.0, 1 if i % 3 else -1))
    market.ws.book.apply("snapshot", {"b": [[str(price - 0.01), "500"], [str(price - 0.05), "2000"]],
                                      "a": [[str(price + 0.01), "300"]], "u": 1})
    market.ws.last_msg_ms = now


async def test_full_snapshot():
    fake = FakeBybit()
    market = _market(fake)
    await market.start(with_ws=False)
    assert market.instrument.tick_size == 0.001
    assert len(market.ws.trades) == 50  # засів з recent-trade
    _feed_ws(market)
    snap = await collect(CFG, market, calendar=EventCalendar(CFG.calendar))
    d = snap.data
    assert d["symbol"] == "USOUSDT" and d["price"] == 61.0
    assert d["fees"]["round_trip_pct"] == pytest.approx(0.11)
    assert set(d["tf_5m"]) >= {"rsi", "ema", "atr", "vwap_today", "support", "resistance", "current_unclosed"}
    assert d["trend"].keys() == {"15m", "60m"}
    assert d["open_interest"]["change_1h_pct"] > 0
    assert d["ticker"]["funding_rate_pct"] == pytest.approx(0.01)
    assert d["orderbook"]["bid_share"] > 0.5
    assert "5m" in d["order_flow"]["windows"]
    assert "events" in d and snap.missing == []
    assert snap.baseline_direction in {"up", "down", "flat"}
    json.loads(snap.to_json())  # серіалізується
    assert len(snap.to_json()) < 12_000  # компактний


async def test_snapshot_survives_missing_sources():
    fake = FakeBybit(fail={"/v5/market/open-interest", "/v5/market/account-ratio"})
    market = _market(fake)
    await market.start(with_ws=False)
    snap = await collect(CFG, market)
    assert "oi" in snap.missing and "ls" in snap.missing
    assert "orderbook_ws" in snap.missing  # WS не запущено -> стакан застарілий
    assert "open_interest" not in snap.data
    assert snap.data["missing_sources"] == snap.missing


async def test_snapshot_without_5m_fails():
    fake = FakeBybit(fail={"/v5/market/kline"})
    market = _market(fake)
    raw = await market.fetch_raw()
    with pytest.raises(SnapshotError):
        build_snapshot(CFG, market, raw)
