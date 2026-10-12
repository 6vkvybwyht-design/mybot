"""Тексти повідомлень (Telegram HTML)."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from bot.tracking.stats import Stats, Tally

DIRECTION_EMOJI = {"long": "🟢", "short": "🔴", "wait": "⚪"}
ACTION_UA = {"long": "ЛОНГ", "short": "ШОРТ", "wait": "ЧЕКАТИ"}
CONFIDENCE_UA = {"low": "низька", "medium": "середня", "high": "висока"}
MISSING_UA = {
    "oi": "open interest",
    "ls": "long/short ratio",
    "ticker": "тікер",
    "news": "новини",
    "trades_ws": "стрічка угод",
    "orderbook_ws": "стакан",
    "kline_1": "1-хв свічки",
    "kline_15": "15-хв свічки",
    "kline_60": "60-хв свічки",
}


def fmt_time(ts_ms: int, tz: str, with_seconds: bool = True) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).astimezone(ZoneInfo(tz))
    label = "UTC" if tz == "UTC" else dt.strftime("%Z") or tz
    return dt.strftime("%H:%M:%S" if with_seconds else "%H:%M") + f" {label}"


def fmt_dt(dt: datetime, tz: str) -> str:
    local = dt.astimezone(ZoneInfo(tz))
    label = "UTC" if tz == "UTC" else local.strftime("%Z") or tz
    return local.strftime("%d.%m %H:%M") + f" {label}"


def decimals(tick: float | None) -> int:
    if not tick:
        return 4
    s = f"{tick:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def fmt_price(x: float | None, tick: float | None) -> str:
    return "—" if x is None else f"{x:.{decimals(tick)}f}"


def fmt_usdt(x: float) -> str:
    sign = "+" if x > 0 else "−" if x < 0 else ""
    a = abs(x)
    if a >= 1e6:
        return f"{sign}{a / 1e6:.1f} млн"
    if a >= 1e3:
        return f"{sign}{a / 1e3:.0f} тис"
    return f"{sign}{a:.0f}"


def _missing_line(missing: list[str]) -> str | None:
    if not missing:
        return None
    names = sorted({MISSING_UA.get(m, m) for m in missing})
    return "ℹ️ Без даних: " + ", ".join(escape(n) for n in names)


def format_analysis(
    data: dict,
    *,
    symbol: str,
    ts_ms: int,
    tz: str,
    tick: float | None,
    kind: str = "manual",
    triggers: list[str] | None = None,
    missing: list[str] | None = None,
) -> str:
    action = data["action"]
    label = "АВТОСИГНАЛ" if kind == "alert" else "АНАЛІЗ"
    lines = [
        f"{DIRECTION_EMOJI[action]} <b>{escape(symbol)} · {label}</b> · дані на {fmt_time(ts_ms, tz)}",
        "",
        f"Лонг {data['long_pct']}% · Шорт {data['short_pct']}% · впевненість: "
        f"{CONFIDENCE_UA.get(data['confidence'], escape(str(data['confidence'])))}",
        f"Дія: <b>{ACTION_UA[action]}</b>",
    ]
    if data.get("summary"):
        lines.append(f"<i>{escape(data['summary'])}</i>")
    if action != "wait":
        lines += [
            "",
            f"Вхід {fmt_price(data['entry'], tick)} · Стоп {fmt_price(data['stop_loss'], tick)} · "
            f"Тейк {fmt_price(data['take_profit'], tick)}",
        ]
    if data.get("reasons"):
        lines += ["", "Чому:"] + [f"• {escape(r)}" for r in data["reasons"]]
    if data.get("risk_flags"):
        lines += ["", "⚠️ Ризики: " + " · ".join(escape(r) for r in data["risk_flags"])]
    if kind == "alert" and triggers:
        lines += ["", "Тригери: " + ", ".join(escape(t) for t in triggers)]
    m = _missing_line(missing or [])
    if m:
        lines += ["", m]
    return "\n".join(lines)


def format_failure(error: str, snapshot: dict | None, *, symbol: str, tz: str, tick: float | None,
                   ts_ms: int | None = None) -> str:
    """Аналіз не вдався: помилка + сирі ключові цифри."""
    lines = [f"⚠️ <b>{escape(symbol)}</b>: аналіз недоступний", escape(error)]
    if snapshot:
        tf5 = snapshot.get("tf_5m", {})
        flow = (snapshot.get("order_flow") or {}).get("windows", {}).get("5m", {})
        book = snapshot.get("orderbook") or {}
        oi = snapshot.get("open_interest") or {}
        t = snapshot.get("ticker") or {}
        lines += ["", f"Сирі дані{' на ' + fmt_time(ts_ms, tz) if ts_ms else ''}:"]
        lines.append(f"Ціна {fmt_price(snapshot.get('price'), tick)} · RSI {tf5.get('rsi', '—')} · "
                     f"ATR {tf5.get('atr_pct', '—')}%")
        lines.append(f"Підтримка {fmt_price(tf5.get('support'), tick)} · Опір {fmt_price(tf5.get('resistance'), tick)}")
        if flow:
            lines.append(f"Кити 5 хв: {fmt_usdt(flow.get('whale_delta_usdt', 0))} USDT · "
                         f"CVD 5 хв: {fmt_usdt(flow.get('cvd_usdt', 0))}")
        if book:
            lines.append(f"Стакан bid/ask: {book['bid_share'] * 100:.0f}/{(1 - book['bid_share']) * 100:.0f}")
        if oi.get("change_15m_pct") is not None:
            lines.append(f"OI за 15 хв: {oi['change_15m_pct']:+.2f}%")
        if t.get("funding_rate_pct") is not None:
            lines.append(f"Funding: {t['funding_rate_pct']:+.4f}%")
    return "\n".join(lines)


def format_status(
    *, symbol: str, price: float | None, tick: float | None, ws_connected: bool, ws_age: float | None,
    alerts_on: bool, sensitivity: str, need: int, dry_run: bool, next_event: str | None, tz: str,
    news_ok: bool, predictions: int, model: str,
) -> str:
    age = "немає даних" if ws_age is None else f"{ws_age:.0f} с тому"
    lines = [
        f"<b>{escape(symbol)}</b> · ціна {fmt_price(price, tick)}",
        f"WebSocket: {'🟢 підключено' if ws_connected else '🔴 відключено'}, останні дані {age}",
        f"Новини: {'🟢' if news_ok else '🔴 недоступні'}",
        f"Сповіщення: {'увімкнені' if alerts_on else 'вимкнені'}{' (dry-run)' if dry_run else ''}, "
        f"чутливість {sensitivity} (тригерів: {need})",
        f"Модель: {escape(model)}",
        f"Прогнозів у базі: {predictions}",
        f"Найближча подія: {next_event or 'немає'}",
    ]
    return "\n".join(lines)


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0f}%"


def _tally(t: Tally) -> str:
    if not t.n:
        return "немає даних"
    return f"{t.n} шт · вдалих {_pct(t.win_rate)} · невдалих {t.loss} · нейтр. {t.neutral}"


def format_stats(periods: list[tuple[str, Stats]], round_trip_pct: float) -> str:
    from bot.triggers import NAMES_UA

    parts = [f"<b>Статистика</b> (вдалий = рух у прогнозованому напрямку &gt; {round_trip_pct:.2f}%)"]
    for title, st in periods:
        h = st.main_horizon
        src = st.by_source_count
        lines = [
            "",
            f"<b>{escape(title)}</b>: прогнозів {st.total} "
            f"(ручних {src.get('manual', 0)}, авто {src.get('alert', 0)}, відфільтр. {src.get('alert_filtered', 0)})",
        ]
        if not st.total:
            parts += lines
            continue
        for hz, t in st.horizons.items():
            lines.append(f"{hz} хв: {_tally(t)}")
        w = st.wait_ok.get(h)
        if w and w.n:
            lines.append(f"«Чекати» без сильного руху ({h} хв): {_pct(w.rate)} з {w.n}")
        cal = [f"{name}: {_pct(c.rate)} ({c.n})" for name, c in st.calibration.items() if c.n]
        if cal:
            lines.append(f"Калібрування ({h} хв): " + " · ".join(cal))
        srcs = {"manual": "ручні", "alert": "авто", "alert_filtered": "відфільтр. (за тригерами)"}
        s_lines = [f"{srcs[k]} {_pct(t.win_rate)} ({t.n})" for k, t in st.sources.items() if t.n]
        if s_lines:
            lines.append("Джерела, вдалих: " + " · ".join(s_lines))
        trig = [f"{NAMES_UA.get(k, k)} {_pct(v.rate)} ({v.n})" for k, v in sorted(st.triggers.items())]
        if trig:
            lines.append("Тригери, напрямок вгадано: " + " · ".join(trig))
        m, b = st.model_vs_baseline
        if m.n:
            verdict = "краще" if (m.win_rate or 0) > (b.win_rate or 0) else "не краще"
            lines.append(f"Бот vs «як остання свічка» ({h} хв): {_pct(m.win_rate)} vs {_pct(b.win_rate)} "
                         f"на {m.n} прогнозах — бот {verdict}")
        parts += lines
    return "\n".join(parts)
