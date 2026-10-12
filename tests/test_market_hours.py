from datetime import datetime, timedelta, timezone

import pytest

from bot.alerts import AlertManager
from bot.market.hours import CLOSED, NO_CONNECTION, OPEN, session_closed_reason, stale_ticks_reason
from bot.news.calendar import Event
from tests.conftest import mt5_market
from tests.fake_mt5 import FakeMT5
from tests.helpers import CFG

S = CFG.market.session          # справжній розклад з config.yaml (Нью-Йорк, як CME)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize("now, closed", [
    (utc(2026, 10, 14, 15, 0), None),                 # ср 11:00 NY — торги
    (utc(2026, 10, 12, 20, 22), None),                # пн 16:22 NY — як у реальному знімку
    (utc(2026, 10, 13, 21, 30), "перерва"),           # вт 17:30 NY (EDT) — щоденна перерва
    (utc(2026, 10, 13, 22, 1), None),                 # вт 18:01 NY — знову торги
    (utc(2027, 1, 12, 21, 30), None),                 # взимку 21:30 UTC = 16:30 EST — торги
    (utc(2027, 1, 12, 22, 30), "перерва"),            # 17:30 EST — перерва
    (utc(2026, 10, 16, 21, 30), "вихідні"),           # пт 17:30 NY — тиждень закрито
    (utc(2026, 10, 17, 12, 0), "вихідні"),            # субота
    (utc(2026, 10, 18, 21, 59), "вихідні"),           # нд 17:59 NY
    (utc(2026, 10, 18, 22, 0), None),                 # нд 18:00 NY — відкриття
])
def test_session_schedule(now, closed):
    reason = session_closed_reason(S, now)
    assert (reason is None) if closed is None else (closed in reason)


def test_same_open_and_close_means_no_weekend():
    always = S.model_copy(update={"week_close": S.week_open, "daily_break": None})
    assert session_closed_reason(always, utc(2026, 10, 17, 12, 0)) is None


def test_stale_ticks():
    assert stale_ticks_reason(5, 120) is None
    assert "90 с" in stale_ticks_reason(90, 60)
    assert "5 хв" in stale_ticks_reason(300, 120)
    assert stale_ticks_reason(None, 120) == "немає жодного тіка"


async def test_state_open_stale_and_disconnected():
    market, fake = mt5_market()
    assert (await market.state()).status == NO_CONNECTION
    await market.source.connect()
    st = await market.state()
    assert st.status == OPEN and st.is_open and st.tick["bid"] == 61.0
    fake.tick_age_sec = 600                       # тіків немає 10 хв
    st = await market.state()
    assert st.status == CLOSED and "10 хв" in st.reason


async def test_state_by_schedule():
    fake = FakeMT5(now=utc(2026, 10, 17, 12, 0).timestamp())   # субота
    market, _ = mt5_market(fake, cfg=CFG)
    await market.source.connect()
    st = await market.state()
    assert st.status == CLOSED and "вихідні" in st.reason


async def test_closed_market_answer_and_no_model_call(app):
    app.fake_mt5.tick_age_sec = 3600
    outcome = await app.analyze("manual")
    assert outcome.market_closed and app.fake_model.calls == 0
    text = app.render(outcome)
    assert "ринок закритий" in text and "60 хв" in text
    assert await app.db.count_predictions() == 0


async def test_alerts_silent_when_closed(app):
    sent = []

    async def notify(text):
        sent.append(text)

    app.alerts_enabled = True
    mgr = AlertManager(app, notify, dry_run=False)
    app.calendar.add(Event("Звіт EIA", datetime.now(timezone.utc) + timedelta(minutes=5), "user"))
    app.fake_mt5.tick_age_sec = 3600
    app.fake_mt5.spread = 0.5                     # навіть розширений спред — без повідомлень
    assert await mgr.tick() == "market_closed"
    assert sent == [] and app.fake_model.calls == 0
    app.fake_mt5.tick_age_sec = 1
    assert await mgr.tick() != "market_closed"
