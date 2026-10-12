import json
import time
from types import SimpleNamespace

import pytest

from bot.analysis import Analyzer
from bot.app import App
from bot.config import Secrets
from bot.market.bybit_ws import BybitWS
from bot.market.trades import Trade
from bot.news.calendar import EventCalendar
from bot.snapshot import MarketService
from bot.tracking.db import Database
from tests.fake_bybit import FakeBybit, rest_client
from tests.helpers import CFG

LONG = {"action": "long", "long_pct": 68, "short_pct": 32, "confidence": "medium", "summary": "Покупці <сильні>.",
        "reasons": ["Кити +1.9 млн", "Пробій 61.35"], "entry": 61.0, "stop_loss": 60.8, "take_profit": 61.4,
        "risk_flags": ["funding & OI"]}
SHORT = {**LONG, "action": "short", "long_pct": 30, "short_pct": 70, "entry": 61.0, "stop_loss": 61.2,
         "take_profit": 60.6}
WAIT = {**LONG, "action": "wait", "long_pct": 52, "short_pct": 48, "entry": None, "stop_loss": None,
        "take_profit": None}


class FakeModel:
    """Фейковий client.beta.messages: повертає відповіді з черги (або default)."""

    def __init__(self, default=None):
        self.queue = []
        self.default = default or LONG
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        data = self.queue.pop(0) if self.queue else self.default
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def feed_ws(market, price=61.0):
    now = int(time.time() * 1000)
    for i in range(300):
        market.ws.trades.add(Trade(now - 600_000 + i * 2000, price, 3.0, 1 if i % 3 else -1))
    market.ws.book.apply("snapshot", {"b": [[str(price - 0.01), "500"]], "a": [[str(price + 0.01), "300"]], "u": 1})
    market.ws.last_msg_ms = now


@pytest.fixture
async def app(tmp_path):
    fake = FakeBybit()
    market = MarketService(CFG, rest=rest_client(fake, CFG), ws=BybitWS(CFG.market, CFG.network))
    await market.start(with_ws=False)
    feed_ws(market)
    db = Database(tmp_path / "t.db")
    await db.connect()
    model = FakeModel()
    secrets = Secrets(telegram_bot_token=None, telegram_owner_id=1, anthropic_api_key=None,
                      anthropic_model="claude-opus-5-5")
    analyzer = Analyzer(CFG, secrets, SimpleNamespace(beta=SimpleNamespace(messages=model)))
    a = App(CFG, db=db, market=market, news=None, calendar=EventCalendar(CFG.calendar.model_copy(
        update={"recurring": []})), analyzer=analyzer)
    a.fake_model = model
    a.fake_bybit = fake
    yield a
    await db.close()
