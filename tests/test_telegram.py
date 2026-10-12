import asyncio
from datetime import datetime, timezone

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from aiogram.types import Chat, Message, Update, User

from bot.telegram.format import format_analysis, format_failure, format_stats
from bot.telegram.handlers import build_router
from bot.telegram.keyboard import BTN_ALERTS_OFF, BTN_ALERTS_ON, BTN_ANALYZE, BTN_STATS
from bot.telegram.middleware import OwnerOnlyMiddleware
from bot.tracking.stats import compute
from tests.conftest import LONG, WAIT

OWNER, STRANGER = 111, 222


class MockSession(BaseSession):
    """Записує всі виклики Telegram API замість мережі."""

    def __init__(self):
        super().__init__()
        self.requests = []
        self._id = 100

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, (SendMessage, EditMessageText, SendDocument)):
            self._id += 1
            return Message(message_id=self._id, date=datetime.now(timezone.utc),
                           chat=Chat(id=OWNER, type="private"), text=getattr(method, "text", None)).as_(bot)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""


@pytest.fixture
def tg(app):
    session = MockSession()
    bot = Bot("123456:" + "A" * 35, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.update.outer_middleware(OwnerOnlyMiddleware(OWNER))
    dp.include_router(build_router(app))
    counter = {"n": 0}

    async def send(text, user=OWNER):
        counter["n"] += 1
        session.requests.clear()
        upd = Update(update_id=counter["n"], message=Message(
            message_id=counter["n"], date=datetime.now(timezone.utc), chat=Chat(id=user, type="private"),
            from_user=User(id=user, is_bot=False, first_name="U"), text=text))
        await dp.feed_update(bot, upd)
        return list(session.requests)

    return send


def texts(reqs):
    return [r.text for r in reqs if isinstance(r, (SendMessage, EditMessageText))]


async def test_stranger_ignored(tg, app):
    assert await tg("/start", user=STRANGER) == []
    assert await tg(BTN_ANALYZE, user=STRANGER) == []
    assert app.fake_model.calls == 0


async def test_start_shows_keyboard(tg):
    reqs = await tg("/start")
    assert len(reqs) == 1 and "USOUSD.s" in reqs[0].text and "Bybit" not in reqs[0].text
    kb = [b.text for row in reqs[0].reply_markup.keyboard for b in row]
    assert BTN_ANALYZE in kb and BTN_STATS in kb


async def test_analyze_button_flow(tg, app):
    reqs = await tg(BTN_ANALYZE)
    assert isinstance(reqs[0], SendMessage) and "Збираю дані" in reqs[0].text
    assert isinstance(reqs[1], EditMessageText)
    text = reqs[1].text
    assert "🟢" in text and "АНАЛІЗ" in text and "Лонг 68% · Шорт 32%" in text and "Дія: <b>ЛОНГ</b>" in text
    assert "&lt;сильні&gt;" in text and "Тригери" not in text   # HTML екранується
    rows = await app.db.predictions_with_outcomes()
    assert len(rows) == 1 and rows[0]["source"] == "manual" and rows[0]["model"] == "claude-opus-5-5"
    # /analyze — те саме
    reqs = await tg("/analyze")
    assert isinstance(reqs[1], EditMessageText)


async def test_analyze_not_parallel(tg, app):
    gate = asyncio.Event()
    orig = app.fake_model.create

    async def slow(**kw):
        await gate.wait()
        return await orig(**kw)

    app.fake_model.create = slow
    first = asyncio.create_task(tg(BTN_ANALYZE))
    await asyncio.sleep(0.05)
    second = await tg(BTN_ANALYZE)
    assert "уже виконується" in second[0].text
    gate.set()
    await first
    assert app.fake_model.calls == 1


async def test_analyze_failure_shows_raw_numbers(tg, app):
    app.fake_model.queue = ["not json", "still not json"]
    reqs = await tg(BTN_ANALYZE)
    text = reqs[-1].text
    assert "аналіз недоступний" in text and "Ціна" in text and "RSI" in text and "Спред 30 п." in text
    assert "Кити" not in text and "Стакан" not in text
    assert await app.db.count_predictions() == 0


async def test_event_flag_is_mandatory(tg, app):
    from datetime import timedelta
    from bot.news.calendar import Event
    app.calendar.add(Event("Звіт EIA", datetime.now(timezone.utc) + timedelta(minutes=9), "user"))
    text = (await tg(BTN_ANALYZE))[-1].text
    assert "Звіт EIA через" in text and "Ризики" in text


async def test_wait_message_has_no_levels(tg, app):
    app.fake_model.default = WAIT
    text = (await tg(BTN_ANALYZE))[-1].text
    assert "⚪" in text and "ЧЕКАТИ" in text and "Вхід" not in text


async def test_alerts_and_sensitivity_persist(tg, app):
    app.alerts_enabled = True
    assert "вимкнено" in texts(await tg("/alerts off"))[0]
    assert app.alerts_enabled is False and await app.db.get_setting("alerts_enabled") is False
    reqs = await tg(BTN_ALERTS_OFF)          # кнопка перемикає
    assert app.alerts_enabled is True and "увімкнено" in reqs[0].text
    kb = [b.text for row in reqs[0].reply_markup.keyboard for b in row]
    assert BTN_ALERTS_ON in kb
    assert "Використання" in texts(await tg("/alerts maybe"))[0]
    assert "1 тригер в одному" in texts(await tg("/sensitivity high"))[0]
    assert await app.db.get_setting("sensitivity") == "high" and app.need_triggers == 1
    assert "Використання" in texts(await tg("/sensitivity extreme"))[0]


async def test_events_commands(tg, app):
    assert "Не вдалося" in texts(await tg("/event завтра Рішення"))[0]
    assert "минула" in texts(await tg("/event 2020-01-01 10:00 Старе"))[0]
    assert "Додано" in texts(await tg("/event 2099-01-01 10:00 Засідання ОПЕК+"))[0]
    assert len(await app.db.load_events()) == 1
    await app.add_event(__import__("bot.news.calendar", fromlist=["Event"]).Event(
        "Скоро", datetime.now(timezone.utc).replace(microsecond=0) + __import__("datetime").timedelta(hours=2), "user"))
    out = texts(await tg("/events"))[0]
    assert "Скоро" in out


async def test_status_stats_export(tg, app):
    status = texts(await tg("/status"))[0]
    assert "USOUSD.s" in status and "MT5: 🟢 підключено" in status and "Модель: claude-opus-5-5" in status
    assert "ціна 61.000" in status and "Ринок: 🟢 відкритий" in status
    stats = texts(await tg(BTN_STATS))[0]
    assert "7 днів" in stats and "Весь час" in stats
    reqs = await tg("/export")
    assert "ще немає" in reqs[0].text
    await tg(BTN_ANALYZE)
    reqs = await tg("/export")
    assert isinstance(reqs[0], SendDocument) and reqs[0].caption == "Прогнозів: 1"


async def test_closed_market_replies(tg, app):
    app.fake_mt5.tick_age_sec = 900
    text = texts(await tg(BTN_ANALYZE))[-1]
    assert "🌙" in text and "ринок закритий" in text and app.fake_model.calls == 0
    assert "Ринок: 🌙 закритий" in texts(await tg("/status"))[0]


async def test_unknown_text_shows_help(tg):
    assert "/analyze" in texts(await tg("привіт"))[0]


# ---------- форматування ----------

def test_format_alert_example():
    t = format_analysis(LONG, symbol="USOUSD.s", ts_ms=1_760_279_530_000, tz="UTC", tick=0.01, kind="alert",
                        triggers=["тіковий обсяг", "імпульс"], missing=["news", "tick_activity"])
    assert t.startswith("🟢 <b>USOUSD.s · АВТОСИГНАЛ</b> · дані на ")
    assert "Вхід 61.00 · Стоп 60.80 · Тейк 61.40" in t
    assert "• Тіків ×2.8" in t and "⚠️ Ризики: EIA &amp; ОПЕК+" in t
    assert "Тригери: тіковий обсяг, імпульс" in t and "Без даних: news" not in t
    assert "новини" in t and "тікова активність" in t


def test_format_time_zone():
    t = format_analysis(WAIT, symbol="X", ts_ms=1_760_279_530_000, tz="Europe/Kyiv", tick=0.01)
    assert "EEST" in t


def test_format_failure_without_snapshot():
    assert "аналіз недоступний" in format_failure("помилка <x>", None, symbol="X", tz="UTC", tick=None)


def test_format_stats_empty_and_full():
    from tests.test_tracking import row
    empty = format_stats([("7 днів", compute([], [5, 10, 15], 0.11))], 0.11)
    assert "прогнозів 0" in empty
    full = format_stats([("Весь час", compute([row(1, "manual", "long", 65, 0.5),
                                                row(2, "alert", "short", 30, -0.3, "down", ["tick_volume"])],
                                               [5, 10, 15], 0.11))], 0.11)
    assert "Калібрування" in full and "тіковий обсяг" in full and "Бот vs" in full
