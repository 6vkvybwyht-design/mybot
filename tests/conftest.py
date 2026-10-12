import json
from types import SimpleNamespace

import pytest

from bot.analysis import Analyzer
from bot.app import App
from bot.config import Secrets
from bot.market.mt5_source import Credentials, MT5Source
from bot.news.calendar import EventCalendar
from bot.snapshot import MarketService
from bot.tracking.db import Database
from tests.fake_mt5 import FakeMT5
from tests.helpers import CFG, CFG_24_7

LONG = {"action": "long", "long_pct": 68, "short_pct": 32, "confidence": "medium", "summary": "Покупці <сильні>.",
        "reasons": ["Тіків ×2.8", "Пробій 61.35"], "entry": 61.0, "stop_loss": 60.8, "take_profit": 61.4,
        "risk_flags": ["EIA & ОПЕК+"]}
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


def mt5_market(fake=None, cfg=CFG_24_7):
    """MarketService на фейковому MetaTrader5 (без терміналу)."""
    fake = fake or FakeMT5()
    src = MT5Source(cfg.mt5, Credentials(), mt5_module=fake, clock=lambda: fake.now)
    return MarketService(cfg, source=src), fake


@pytest.fixture
async def app(tmp_path):
    market, fake = mt5_market()
    await market.source.connect()
    db = Database(tmp_path / "t.db")
    await db.connect()
    model = FakeModel()
    secrets = Secrets(telegram_bot_token=None, telegram_owner_id=1, anthropic_api_key=None,
                      anthropic_model="claude-opus-5-5")
    analyzer = Analyzer(CFG_24_7, secrets, SimpleNamespace(beta=SimpleNamespace(messages=model)))
    a = App(CFG_24_7, db=db, market=market, news=None, calendar=EventCalendar(CFG.calendar.model_copy(
        update={"recurring": []})), analyzer=analyzer)
    a.fake_model = model
    a.fake_mt5 = fake
    yield a
    await db.close()
