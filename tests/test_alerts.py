import time
from datetime import datetime, timedelta, timezone

import pytest

from bot.alerts import AlertManager
from bot.news.calendar import Event
from bot.triggers import DOWN, UP, TriggerHit
from tests.conftest import LONG, SHORT, WAIT

MIN = 60_000


def hits(direction, n=3):
    names = ["whales", "volume", "breakout", "impulse"]
    return [TriggerHit(names[i], direction, "test") for i in range(n)]


@pytest.fixture
def sent():
    return []


@pytest.fixture
def mgr(app, sent):
    async def notify(text):
        sent.append(text)
    app.alerts_enabled = True
    return AlertManager(app, notify, dry_run=False)


async def test_signal_sent_and_saved(mgr, app, sent):
    now = int(time.time() * 1000)
    assert await mgr.process(hits(UP), now) == "sent"
    assert "АВТОСИГНАЛ" in sent[0] and "Тригери: кити, обсяг, пробій рівня" in sent[0]
    rows = await app.db.predictions_with_outcomes()
    assert rows[0]["source"] == "alert" and rows[0]["trigger_direction"] == "up"
    assert [t["name"] for t in rows[0]["triggers"]] == ["whales", "volume", "breakout"]


async def test_not_enough_triggers(mgr, app):
    now = int(time.time() * 1000)
    assert await mgr.process(hits(UP, 2), now) == "no_signal"
    await app.set_sensitivity("high")
    assert await mgr.process(hits(UP, 2), now) == "sent"


async def test_cooldowns(mgr, app, sent):
    now = int(time.time() * 1000)
    assert await mgr.process(hits(UP), now) == "sent"
    assert await mgr.process(hits(UP), now + 11 * MIN) == "cooldown"
    assert await mgr.process(hits(DOWN), now + 4 * MIN) == "cooldown"
    app.fake_model.default = SHORT
    assert await mgr.process(hits(DOWN), now + 5 * MIN) == "sent"       # протилежний — через 5 хв
    app.fake_model.default = LONG
    # останній надісланий — шорт о +5 хв; лонг для нього протилежний -> можна з +10 хв
    assert await mgr.process(hits(UP), now + 9 * MIN) == "cooldown"
    assert await mgr.process(hits(UP), now + 10 * MIN) == "sent"
    assert len(sent) == 3


async def test_model_filter(mgr, app, sent):
    now = int(time.time() * 1000)
    app.fake_model.default = WAIT
    assert await mgr.process(hits(UP), now) == "filtered"
    app.fake_model.default = {**LONG, "long_pct": 58, "short_pct": 42}
    assert await mgr.process(hits(UP), now + 1 * MIN) == "cooldown"     # пауза після фільтра
    assert await mgr.process(hits(UP), now + 3 * MIN) == "filtered"     # 58% < 60%
    app.fake_model.default = SHORT
    assert await mgr.process(hits(UP), now + 6 * MIN) == "filtered"     # модель проти тригерів
    assert sent == []
    rows = await app.db.predictions_with_outcomes()
    assert [r["source"] for r in rows] == ["alert_filtered"] * 3


async def test_rate_limit(mgr, app):
    now = int(time.time() * 1000)
    app.fake_model.default = WAIT
    results = [await mgr.process(hits(UP), now + i * 4 * MIN) for i in range(8)]
    assert results.count("filtered") == 6 and results[-1] == "rate_limited"
    assert app.fake_model.calls == 6
    assert await mgr.process(hits(UP), now + 61 * MIN) == "filtered"    # година минула


async def test_quiet_mode_single_warning(mgr, app, sent):
    now = datetime.now(timezone.utc)
    app.calendar.add(Event("Звіт EIA", now + timedelta(minutes=10), "user"))
    now_ms = int(now.timestamp() * 1000)
    assert await mgr.process(hits(UP), now_ms) == "quiet"
    assert await mgr.process(hits(UP), now_ms + MIN) == "quiet"
    assert await mgr.process([], now_ms + 20 * MIN) == "quiet"          # 10 хв після події
    assert len(sent) == 1 and "Звіт EIA" in sent[0] and "за 10 хв" in sent[0]
    assert app.fake_model.calls == 0
    assert await mgr.process(hits(UP), now_ms + 26 * MIN) == "sent"


async def test_disabled_and_dry_run(mgr, app, sent):
    now = int(time.time() * 1000)
    app.alerts_enabled = False
    assert await mgr.process(hits(UP), now) == "disabled"
    app.alerts_enabled = True
    mgr.dry_run = True
    assert await mgr.process(hits(UP), now) == "dry_run"
    assert sent == [] and (await app.db.count_predictions()) == 1


async def test_analysis_failure_does_not_send(mgr, app, sent):
    now = int(time.time() * 1000)
    app.fake_model.queue = ["bad", "bad"]
    assert await mgr.process(hits(UP), now) == "analysis_failed"
    assert sent == []


async def test_stale_data_and_connection_alarm(mgr, app, sent):
    now = int(time.time() * 1000)
    app.market.ws.last_msg_ms = now - 40_000
    assert await mgr.tick(now) == "stale"
    assert sent == []
    app.market.ws.last_msg_ms = now - 130_000
    assert await mgr.tick(now) == "stale"
    assert await mgr.tick(now + 1000) == "stale"
    assert len(sent) == 1 and "Немає даних" in sent[0]
    app.market.ws.last_msg_ms = now + 2000
    await mgr.check_connection(now + 2000)
    assert "відновлено" in sent[-1]


async def test_tick_end_to_end(mgr, app):
    now = int(time.time() * 1000)
    app.market.ws.last_msg_ms = now
    result = await mgr.tick(now)
    assert result in {"no_signal", "sent", "filtered", "cooldown"}
    assert "/v5/market/kline" in app.fake_bybit.calls
