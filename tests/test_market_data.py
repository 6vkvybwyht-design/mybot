import asyncio
import json

import httpx
import numpy as np
import pytest
import websockets

from bot.market.bybit_rest import BybitError, BybitRest, parse_klines
from bot.market.bybit_ws import BybitWS
from bot.market.orderbook import OrderBook
from bot.market.trades import Trade, TradeStore
from bot.market.whales import WhaleAnalyzer, merge_trades
from tests.helpers import CFG

# ---------- свічки ----------

def test_parse_klines_sorts_and_marks_closed():
    rows = [
        ["1700000600000", "3", "4", "2", "3.5", "10", "35"],
        ["1700000300000", "2", "3", "1", "3", "10", "30"],
        ["1700000000000", "1", "2", "0.5", "2", "10", "20"],
    ]
    df = parse_klines(rows, 5, now_ms=1700000700000)
    assert df["start"].tolist() == [1700000000000, 1700000300000, 1700000600000]
    assert df["closed"].tolist() == [True, True, False]


# ---------- REST ----------

def _resp(result, code=0):
    return httpx.Response(200, json={"retCode": code, "retMsg": "OK" if code == 0 else "bad", "result": result})


def _rest(handler, retries=2):
    net = CFG.network.model_copy(update={"retries": retries, "backoff_base_sec": 0.01})
    client = httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler))
    return BybitRest(CFG.market, net, client)


async def test_instrument_parsing():
    def h(req):
        assert req.url.params["category"] == "linear" and req.url.params["symbol"] == "USOUSDT"
        return _resp({"list": [{"symbol": "USOUSDT", "status": "Trading",
                                "priceFilter": {"tickSize": "0.001"},
                                "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1", "minNotionalValue": "5"}}]})
    inst = await _rest(h).instrument()
    assert (inst.tick_size, inst.min_qty, inst.min_notional, inst.status) == (0.001, 0.1, 5.0, "Trading")


async def test_unknown_symbol_raises():
    rest = _rest(lambda req: _resp({"list": []}))
    with pytest.raises(BybitError):
        await rest.instrument()


async def test_api_error_not_retried():
    calls = []
    def h(req):
        calls.append(1)
        return _resp({}, code=10001)
    with pytest.raises(BybitError):
        await _rest(h).ticker()
    assert len(calls) == 1


async def test_http_error_retried_then_ok():
    calls = []
    def h(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(502)
        return _resp({"list": [{"lastPrice": "61.5", "bid1Price": "61.49", "ask1Price": "61.51",
                                "fundingRate": "0.0001", "openInterest": "1000", "price24hPcnt": "0.012"}]})
    t = await _rest(h).ticker()
    assert len(calls) == 3
    assert t["last_price"] == 61.5
    assert t["spread_pct"] == pytest.approx(0.02 / 61.49 * 100)
    assert t["price_24h_pct"] == pytest.approx(1.2)


async def test_http_error_gives_up():
    with pytest.raises(BybitError):
        await _rest(lambda req: httpx.Response(500), retries=1).ticker()


async def test_open_interest_sorted():
    def h(req):
        assert req.url.params["intervalTime"] == "5min"
        return _resp({"list": [{"openInterest": "2", "timestamp": "200"}, {"openInterest": "1", "timestamp": "100"}]})
    oi = await _rest(h).open_interest()
    assert oi["oi"].tolist() == [1.0, 2.0]


# ---------- угоди і кити ----------

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
    w = WhaleAnalyzer(CFG.whales, s)
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
    rep = WhaleAnalyzer(CFG.whales, s).report(now, force=True)
    assert rep.threshold_source == "fixed_fallback" and rep.avg_abs_whale_delta_5m is None
    fixed = CFG.whales.model_copy(update={"threshold_mode": "fixed", "fixed_usdt": 10.0})
    rep = WhaleAnalyzer(fixed, s).report(now, force=True)
    assert rep.threshold_source == "fixed" and rep.windows[1].whale_buys == 1


# ---------- стакан ----------

def test_orderbook_snapshot_delta_metrics():
    b = OrderBook()
    b.apply("delta", {"b": [["1", "1"]], "a": [], "u": 5})
    assert not b.ready  # дельта без знімку ігнорується
    b.apply("snapshot", {"b": [["99.9", "10"], ["99.0", "100"]], "a": [["100.1", "5"], ["101", "1"]], "u": 1})
    b.apply("delta", {"b": [["99.0", "0"], ["99.95", "2"]], "a": [["100.1", "3"]], "u": 2})
    assert 99.0 not in b.bids and b.bids[99.95] == 2 and b.asks[100.1] == 3
    m = b.metrics(band_pct=0.3)
    assert m.best_bid == 99.95 and m.best_ask == 100.1
    bid_n = 99.9 * 10 + 99.95 * 2
    ask_n = 100.1 * 3  # 101 поза смугою ±0.3%
    assert m.imbalance == pytest.approx(bid_n / (bid_n + ask_n))
    assert m.bid_walls[0][0] == 99.9


# ---------- WebSocket ----------

def _ws_cfg(url, stale=1):
    market = CFG.market.model_copy(update={"ws_public_url": url})
    net = CFG.network.model_copy(update={"ws_ping_interval_sec": 1, "ws_stale_sec": stale})
    return market, net


async def test_ws_reconnects_and_parses():
    sessions = []

    async def server(conn):
        sub = json.loads(await conn.recv())
        sessions.append(sub)
        await conn.send(json.dumps({"op": "subscribe", "success": True}))
        await conn.send(json.dumps({"topic": "publicTrade.USOUSDT", "type": "snapshot", "data": [
            {"T": 1_760_000_000_000 + len(sessions), "s": "USOUSDT", "S": "Buy", "v": "2", "p": "61.5",
             "i": f"id{len(sessions)}"}]}))
        await conn.send(json.dumps({"topic": "orderbook.50.USOUSDT", "type": "snapshot",
                                    "data": {"b": [["61.4", "5"]], "a": [["61.6", "5"]], "u": 1}}))
        if len(sessions) == 1:
            await conn.close()  # розрив — клієнт має перепідключитися
        else:
            await asyncio.sleep(5)

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        market, net = _ws_cfg(f"ws://127.0.0.1:{port}", stale=10)
        ws = BybitWS(market, net, TradeStore(10**13))
        task = asyncio.create_task(ws.run())
        for _ in range(100):
            if ws.connects >= 2 and len(ws.trades) == 2:
                break
            await asyncio.sleep(0.05)
        ws.stop()
        task.cancel()
    assert ws.connects == 2
    assert sessions[0]["args"] == ["publicTrade.USOUSDT", "orderbook.50.USOUSDT"]
    assert len(ws.trades) == 2 and ws.trades.last.side == 1
    assert ws.book.ready and ws.last_msg_ms is not None


async def test_ws_sends_ping_and_detects_silence():
    got = []

    async def server(conn):
        await conn.recv()
        async for raw in conn:  # мовчимо, тільки читаємо пінги
            got.append(json.loads(raw))

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        market, net = _ws_cfg(f"ws://127.0.0.1:{port}")
        ws = BybitWS(market, net)
        task = asyncio.create_task(ws.run())
        await asyncio.sleep(3.8)  # ping кожну 1 с; мовчання > 2 с -> пауза 1 с -> перепідключення
        ws.stop()
        task.cancel()
    assert {"op": "ping"} in got
    assert ws.connects >= 2


def test_ws_seed_trades():
    ws = BybitWS(CFG.market, CFG.network, TradeStore(10**13))
    n = ws.seed_trades([{"execId": "x", "time": "2", "price": "1", "size": "1", "side": "Sell"},
                        {"execId": "y", "time": "1", "price": "1", "size": "1", "side": "Buy"}])
    assert n == 2 and ws.trades.last.side == -1
