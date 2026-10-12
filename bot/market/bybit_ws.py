"""Публічний WebSocket Bybit: стрічка угод і стакан, з автоперепідключенням."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import websockets

from bot.config import MarketConfig, NetworkConfig
from bot.market.orderbook import OrderBook
from bot.market.trades import Trade, TradeStore

log = logging.getLogger(__name__)


def now_ms() -> int:
    return int(time.time() * 1000)


class BybitWS:
    def __init__(self, market: MarketConfig, network: NetworkConfig, trades: TradeStore | None = None):
        self.market = market
        self.network = network
        self.url = market.ws_public_url
        self.trades = trades if trades is not None else TradeStore(market.trades_window_hours * 3_600_000)
        self.book = OrderBook()
        self.connected = False
        self.last_msg_ms: int | None = None
        self.connects = 0
        self._stop = asyncio.Event()
        self._max_backoff = 60.0

    @property
    def topics(self) -> list[str]:
        s = self.market.symbol
        return [f"publicTrade.{s}", f"orderbook.{self.market.orderbook_depth}.{s}"]

    def seconds_since_data(self, now: int | None = None) -> float | None:
        if self.last_msg_ms is None:
            return None
        return ((now or now_ms()) - self.last_msg_ms) / 1000

    def seed_trades(self, rows: list[dict[str, Any]]) -> int:
        """Засів вікна угод з REST /v5/market/recent-trade."""
        added = 0
        for r in sorted(rows, key=lambda r: int(r["time"])):
            trade = Trade(int(r["time"]), float(r["price"]), float(r["size"]), 1 if r["side"] == "Buy" else -1)
            added += self.trades.add(trade, r.get("execId"))
        return added

    def handle(self, msg: dict[str, Any]) -> None:
        topic = msg.get("topic", "")
        if topic.startswith("publicTrade."):
            for t in msg.get("data", []):
                trade = Trade(int(t["T"]), float(t["p"]), float(t["v"]), 1 if t["S"] == "Buy" else -1)
                self.trades.add(trade, t.get("i"))
            self.trades.prune(now_ms())
        elif topic.startswith("orderbook."):
            self.book.apply(msg.get("type", ""), msg.get("data", {}), msg.get("ts"))
        elif msg.get("op") == "subscribe" and not msg.get("success", True):
            log.error("Bybit WS: підписка не вдалася: %s", msg.get("ret_msg"))
        else:
            return  # pong та службові повідомлення не рахуємо як дані
        self.last_msg_ms = now_ms()

    async def _ping(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self.network.ws_ping_interval_sec)
            await ws.send(json.dumps({"op": "ping"}))

    async def _session(self) -> None:
        async with websockets.connect(self.url, ping_interval=None, open_timeout=self.network.timeout_sec) as ws:
            self.book.clear()
            await ws.send(json.dumps({"op": "subscribe", "args": self.topics}))
            self.connected = True
            self.connects += 1
            log.info("Bybit WS підключено (%s), підписка: %s", self.url, ", ".join(self.topics))
            pinger = asyncio.create_task(self._ping(ws))
            try:
                while not self._stop.is_set():
                    # Якщо сервер замовк, з'єднання вважається мертвим.
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.network.ws_stale_sec * 2)
                    try:
                        self.handle(json.loads(raw))
                    except (ValueError, KeyError, TypeError) as exc:
                        log.warning("Bybit WS: не вдалося розібрати повідомлення: %s", exc)
            finally:
                pinger.cancel()
                self.connected = False

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — будь-яка помилка = перепідключення
                log.warning("Bybit WS розірвано: %r", exc)
            self.connected = False
            if self._stop.is_set():
                break
            if time.monotonic() - started > 60:
                backoff = 1.0  # з'єднання жило довго — починаємо паузи заново
            log.info("Bybit WS: перепідключення через %.0f с", backoff)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self._max_backoff)

    def stop(self) -> None:
        self._stop.set()
