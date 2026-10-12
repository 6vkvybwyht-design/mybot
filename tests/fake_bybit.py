"""Фейковий публічний REST Bybit v5 для тестів (формат відповідей як у справжнього API)."""

from __future__ import annotations

import time

import httpx
import numpy as np


class FakeBybit:
    def __init__(self, base_price=61.0, now_ms=None, seed=7, fail=()):
        self.now_ms = now_ms or int(time.time() * 1000)
        self.base = base_price
        self.rng = np.random.default_rng(seed)
        self.fail = set(fail)          # шляхи, які повертають 500
        self.calls: list[str] = []
        self.price_path: dict[int, list] = {}

    def _ok(self, result):
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result, "time": self.now_ms})

    def klines(self, interval: int, limit: int, end_ms: int | None = None, start_ms: int | None = None):
        step = interval * 60_000
        last_start = ((end_ms or self.now_ms) // step) * step
        rows = []
        price = self.base
        starts = [last_start - i * step for i in range(limit)][::-1]
        if start_ms is not None:
            starts = [s for s in starts if s >= start_ms]
        for s in starts:
            o = price
            c = round(o + float(self.rng.normal(0, 0.03)), 3)
            h = round(max(o, c) + abs(float(self.rng.normal(0, 0.02))), 3)
            low = round(min(o, c) - abs(float(self.rng.normal(0, 0.02))), 3)
            v = round(float(self.rng.uniform(500, 1500)), 1)
            rows.append([str(s), str(o), str(h), str(low), str(c), str(v), str(round(v * c, 2))])
            price = c
        return rows[::-1]  # Bybit віддає нові спочатку

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(path)
        if path in self.fail:
            return httpx.Response(500)
        q = req.url.params
        if path == "/v5/market/instruments-info":
            return self._ok({"category": "linear", "list": [{
                "symbol": q["symbol"], "contractType": "LinearPerpetual", "status": "Trading",
                "priceFilter": {"minPrice": "0.001", "maxPrice": "1999", "tickSize": "0.001"},
                "lotSizeFilter": {"maxOrderQty": "100000", "minOrderQty": "0.1", "qtyStep": "0.1",
                                  "minNotionalValue": "5"}}]})
        if path == "/v5/market/kline":
            interval = int(q["interval"])
            limit = int(q.get("limit", 200))
            end = int(q["end"]) if "end" in q else None
            start = int(q["start"]) if "start" in q else None
            return self._ok({"symbol": q["symbol"], "category": "linear",
                             "list": self.klines(interval, limit, end, start)})
        if path == "/v5/market/tickers":
            return self._ok({"category": "linear", "list": [{
                "symbol": q["symbol"], "lastPrice": str(self.base), "markPrice": str(self.base),
                "indexPrice": str(self.base - 0.01), "bid1Price": str(self.base - 0.005),
                "ask1Price": str(self.base + 0.005), "fundingRate": "0.0001",
                "nextFundingTime": str(self.now_ms + 3_600_000), "openInterest": "250000",
                "openInterestValue": "15250000", "turnover24h": "9000000", "price24hPcnt": "0.0123",
                "highPrice24h": "62", "lowPrice24h": "60"}]})
        if path == "/v5/market/open-interest":
            n = int(q.get("limit", 13))
            return self._ok({"symbol": q["symbol"], "category": "linear", "list": [
                {"openInterest": str(250000 - i * 500), "timestamp": str(self.now_ms - i * 300_000)}
                for i in range(n)]})
        if path == "/v5/market/account-ratio":
            n = int(q.get("limit", 13))
            return self._ok({"list": [
                {"symbol": q["symbol"], "buyRatio": f"{0.55 - i * 0.001:.4f}", "sellRatio": f"{0.45 + i * 0.001:.4f}",
                 "timestamp": str(self.now_ms - i * 300_000)} for i in range(n)]})
        if path == "/v5/market/recent-trade":
            return self._ok({"category": "linear", "list": [
                {"execId": f"r{i}", "symbol": q["symbol"], "price": str(self.base), "size": "5",
                 "side": "Buy" if i % 2 else "Sell", "time": str(self.now_ms - i * 1000), "isBlockTrade": False}
                for i in range(50)]})
        return httpx.Response(404)


def rest_client(fake: FakeBybit, cfg):
    from bot.market.bybit_rest import BybitRest

    net = cfg.network.model_copy(update={"retries": 0, "backoff_base_sec": 0.01})
    client = httpx.AsyncClient(base_url="https://api.bybit.com", transport=httpx.MockTransport(fake))
    return BybitRest(cfg.market, net, client)
