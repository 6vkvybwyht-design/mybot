"""Наскрізний запуск bot.main.run: фейкові Telegram API, Bybit і модель; зупинка через SIGTERM."""

import asyncio
import os
import signal
from datetime import datetime, timezone
from types import SimpleNamespace

import aiogram
import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, GetMe, GetUpdates, SendMessage
from aiogram.types import Chat, Message, Update, User
from pydantic import SecretStr

import bot.analysis
import bot.snapshot
from bot.config import Secrets, Settings
from bot.main import run
from bot.market.bybit_ws import BybitWS
from bot.telegram.keyboard import BTN_ANALYZE
from tests.conftest import FakeModel
from tests.fake_bybit import FakeBybit, rest_client
from tests.helpers import CFG

OWNER = 777


class FakeTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list = []
        self.updates = [
            Update(update_id=1, message=Message(message_id=1, date=datetime.now(timezone.utc),
                   chat=Chat(id=OWNER, type="private"), from_user=User(id=OWNER, is_bot=False, first_name="O"),
                   text=BTN_ANALYZE)),
            Update(update_id=2, message=Message(message_id=2, date=datetime.now(timezone.utc),
                   chat=Chat(id=5, type="private"), from_user=User(id=5, is_bot=False, first_name="X"),
                   text="/start")),
        ]

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="B", username="test_bot")
        if isinstance(method, GetUpdates):
            ups = [u for u in self.updates if u.update_id >= (method.offset or 0)]
            if not ups:
                await asyncio.sleep(0.2)
            return ups
        if isinstance(method, (SendMessage, EditMessageText)):
            self.sent.append(method)
            return Message(message_id=len(self.sent) + 10, date=datetime.now(timezone.utc),
                           chat=Chat(id=OWNER, type="private"), text=method.text).as_(bot)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""


async def test_full_run(tmp_path, monkeypatch):
    tg = FakeTelegram()
    real_bot = aiogram.Bot
    monkeypatch.setattr(aiogram, "Bot", lambda token, **kw: real_bot(token, session=tg, **kw))

    fake = FakeBybit()
    real_ms = bot.snapshot.MarketService
    monkeypatch.setattr(bot.snapshot, "MarketService", lambda cfg: real_ms(
        cfg, rest=rest_client(fake, cfg),
        ws=BybitWS(cfg.market.model_copy(update={"ws_public_url": "ws://127.0.0.1:9"}), cfg.network)))
    model = FakeModel()
    monkeypatch.setattr(bot.analysis.anthropic, "AsyncAnthropic",
                        lambda **kw: SimpleNamespace(beta=SimpleNamespace(messages=model)))

    cfg = CFG.model_copy(update={"storage": CFG.storage.model_copy(update={"db_path": str(tmp_path / "b.db")})})
    # швидко перевіряємо, що RSS не потрібен для запуску: стрічки на недоступний хост
    cfg = cfg.model_copy(update={"news": cfg.news.model_copy(update={"feeds": []})})
    settings = Settings(config=cfg, secrets=Secrets(
        telegram_bot_token=SecretStr("123456:" + "A" * 35), telegram_owner_id=OWNER,
        anthropic_api_key=SecretStr("sk-ant-x"), anthropic_model="claude-opus-5-5"))

    async def stopper():
        for _ in range(100):
            await asyncio.sleep(0.1)
            if any(isinstance(m, EditMessageText) for m in tg.sent):
                break
        os.kill(os.getpid(), signal.SIGTERM)

    task = asyncio.create_task(stopper())
    await asyncio.wait_for(run(settings, dry_run=True), timeout=30)
    await task

    texts = [m.text for m in tg.sent]
    assert texts[0].startswith("✅ Бот запущено (dry-run)")
    assert any("Збираю дані" in t for t in texts)
    edited = [m.text for m in tg.sent if isinstance(m, EditMessageText)]
    assert edited and "АНАЛІЗ" in edited[0]
    assert all(m.chat_id == OWNER for m in tg.sent if isinstance(m, SendMessage))  # чужому — нічого
    assert model.calls == 1
    assert (tmp_path / "b.db").exists()
