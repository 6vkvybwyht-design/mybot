import asyncio

import pytest

from bot.market import indicators as ind
from bot.market.mt5_source import Credentials, MT5Error, MT5Source, MT5Unavailable, offset_from_tick
from tests.fake_mt5 import FakeMT5
from tests.helpers import CFG

CREDS = Credentials(login=123, password="investor-pass", server="Bybit-Live-6")


def make(fake=None, creds=CREDS, notify=None, **cfg_upd):
    fake = fake or FakeMT5()
    cfg = CFG.mt5.model_copy(update={"health_check_sec": 1, **cfg_upd})
    return MT5Source(cfg, creds, mt5_module=fake, notify=notify, clock=lambda: fake.now), fake


def test_default_config_symbol():
    assert CFG.mt5.symbol == "USOUSD.s"
    assert CFG.mt5.timeframes == [1, 5, 15, 60]


def test_offset_from_tick():
    assert offset_from_tick(1000 + 3 * 3600 - 2, 1000) == 3 * 3600
    assert offset_from_tick(1000 - 2, 1000) == 0
    assert offset_from_tick(1000 + 3 * 3600 - 900, 1000) is None   # тік 15 хв тому — не визначити


async def test_connect_selects_symbol_and_detects_offset():
    src, fake = make()
    await src.connect()
    assert src.connected
    assert "USOUSD.s" in fake.selected
    assert src.spec.tick_size == 0.001
    assert (src.server_offset_sec, src.offset_source) == (3 * 3600, "tick")
    args, kwargs = fake.init_calls[0]
    assert args == () and kwargs["login"] == 123 and kwargs["server"] == "Bybit-Live-6"


async def test_path_passed_and_partial_creds_attach_to_terminal():
    src, fake = make(creds=Credentials(path=r"C:\MT5\terminal64.exe"))
    await src.connect()
    args, kwargs = fake.init_calls[0]
    assert args == (r"C:\MT5\terminal64.exe",)
    assert "login" not in kwargs


async def test_symbol_not_found_logged(caplog):
    src, _ = make(FakeMT5(symbols=("USOIL",)))
    with pytest.raises(MT5Error, match="USOUSD.s не знайдено"):
        await src.connect()
    assert "НЕ ЗНАЙДЕНО" in caplog.text and "USOIL" in caplog.text


async def test_auth_failure_hint():
    fake = FakeMT5()
    fake.auth_fail = True
    src, _ = make(fake)
    with pytest.raises(MT5Unavailable, match="MT5_PASSWORD"):
        await src.connect()


async def test_closed_market_offset_falls_back_to_config():
    src, _ = make(FakeMT5(tick_age_sec=3 * 3600), server_utc_offset_hours=2)
    await src.connect()
    assert (src.offset, src.offset_source) == (7200, "config")


async def test_klines_converted_to_utc_and_indicators_work():
    src, fake = make()
    await src.connect()
    snap = await src.snapshot()
    now_ms = int(fake.now * 1000)
    for tf, df in snap["klines"].items():
        assert len(df) == CFG.mt5.bars
        assert list(df.columns[:6]) == ["start", "open", "high", "low", "close", "volume"]
        assert df["start"].is_monotonic_increasing
        # Остання свічка — поточна (незакрита) і стартувала в UTC не пізніше «зараз».
        last = int(df["start"].iloc[-1])
        assert now_ms - tf * 60_000 < last <= now_ms
        assert not df["closed"].iloc[-1] and df["closed"].iloc[:-1].all()
    tf5 = ind.compute_5m(snap["klines"][5], CFG.indicators, now_ms, snap["tick"]["mid"])
    assert tf5["atr"] > 0


async def test_klines_range_for_outcomes():
    src, fake = make()
    await src.connect()
    now_ms = int(fake.now * 1000)
    start = now_ms - 20 * 60_000
    df = await src.klines(1, limit=50, start_ms=start, end_ms=now_ms)
    assert df["start"].iloc[0] >= start and len(df) in (20, 21)


async def test_tick_and_activity():
    src, _ = make(FakeMT5(ticks_per_min=10, burst_last_min=40))
    await src.connect()
    t = await src.tick()
    assert t["spread"] == pytest.approx(0.03) and t["spread_points"] == pytest.approx(30)
    assert t["age_sec"] == pytest.approx(1.0, abs=0.01)
    a = await src.tick_activity()
    assert a["base_per_min"] == pytest.approx(10, rel=0.1)
    assert a["ratio_1m"] > 4          # 10 звичайних + 40 сплеску за останню хвилину
    assert a["spread_median_points"] == pytest.approx(30)


async def test_ipc_failure_marks_disconnected():
    src, fake = make()
    await src.connect()
    fake.down = True
    with pytest.raises(MT5Unavailable):
        await src.tick()
    assert not src.connected
    with pytest.raises(MT5Unavailable):
        await src.snapshot()


async def test_run_reconnects_and_notifies_once():
    sent = []

    async def notify(text):
        sent.append(text)

    fake = FakeMT5()
    fake.down = True
    src, _ = make(fake, notify=notify)
    task = asyncio.create_task(src.run())
    await asyncio.sleep(0.05)
    assert not src.connected and len(sent) == 1 and "MT5" in sent[0]
    fake.down = False
    src._stop.set(); src._stop.clear()   # розбудити паузу перепідключення
    for _ in range(100):
        if src.connected:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert src.connected and len(sent) == 2 and sent[1].startswith("✅")
    # Обрив під час роботи: health-check бачить, що термінал закрито.
    fake.down = True
    await asyncio.sleep(1.2)
    assert not src.connected and len(sent) == 3
    await src.stop()
    task.cancel()


async def test_without_package_clear_error():
    src = MT5Source(CFG.mt5, CREDS, mt5_module=None)
    src.mt5 = None
    with pytest.raises(MT5Unavailable, match="MetaTrader5"):
        await src.connect()


def test_password_not_in_repr():
    assert "investor-pass" not in repr(CREDS)
