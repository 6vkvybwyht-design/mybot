"""Тексти повідомлень (Telegram HTML)."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from bot.tracking.stats import HitRate, HorizonStats, PnL, Stats, Tally

DIRECTION_EMOJI = {"long": "🟢", "short": "🔴", "wait": "⚪"}
ACTION_UA = {"long": "ЛОНГ", "short": "ШОРТ", "wait": "ЧЕКАТИ"}
CONFIDENCE_UA = {"low": "низька", "medium": "середня", "high": "висока"}
MISSING_UA = {
    "tick": "поточний тік",
    "tick_activity": "тікова активність",
    "news": "новини",
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


def plural_triggers(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} тригер"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} тригери"
    return f"{n} тригерів"


def plural_trades(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} угода"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} угоди"
    return f"{n} угод"


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


def format_market_closed(symbol: str, reason: str | None) -> str:
    return (f"🌙 <b>{escape(symbol)}</b>: ринок закритий" + (f" — {escape(reason)}" if reason else "")
            + ".\nАналіз не виконується, автосповіщення призупинені до відкриття.")


def format_failure(error: str, snapshot: dict | None, *, symbol: str, tz: str, tick: float | None,
                   ts_ms: int | None = None) -> str:
    """Аналіз не вдався: помилка + сирі ключові цифри."""
    lines = [f"⚠️ <b>{escape(symbol)}</b>: аналіз недоступний", escape(error)]
    if snapshot:
        tf5 = snapshot.get("tf_5m", {})
        q = snapshot.get("quote") or {}
        act = snapshot.get("tick_activity") or {}
        lines += ["", f"Сирі дані{' на ' + fmt_time(ts_ms, tz) if ts_ms else ''}:"]
        lines.append(f"Ціна {fmt_price(snapshot.get('price'), tick)} · RSI {tf5.get('rsi', '—')} · "
                     f"ATR {tf5.get('atr_pct', '—')}%")
        lines.append(f"Підтримка {fmt_price(tf5.get('support'), tick)} · Опір {fmt_price(tf5.get('resistance'), tick)}")
        if q.get("spread_points") is not None:
            lines.append(f"Спред {q['spread_points']:.0f} п. ({q.get('spread_pct')}%)")
        if act.get("ticks_5m") is not None:
            lines.append(f"Тіків за 5 хв: {act['ticks_5m']} (×{act.get('ratio_5m') or '—'} від звичайного)")
    return "\n".join(lines)


def format_status(
    *, symbol: str, price: float | None, tick: float | None, mt5_connected: bool, tick_age: float | None,
    mt5_error: str | None = None, market: str | None = None, alerts_on: bool, sensitivity: str, need: int, dry_run: bool, next_event: str | None, tz: str,
    news_ok: bool, predictions: int, model: str,
) -> str:
    age = "немає даних" if tick_age is None else f"{tick_age:.0f} с тому"
    mt5 = f"🟢 підключено, останній тік {age}" if mt5_connected else (
        "🔴 немає зв'язку" + (f" ({escape(mt5_error)})" if mt5_error else ""))
    lines = [
        f"<b>{escape(symbol)}</b> · ціна {fmt_price(price, tick)}",
        f"MT5: {mt5}",
        *([f"Ринок: {market}"] if market else []),
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


def _rate(rate: float | None, ci: tuple[float, float] | None, n: int) -> str:
    """«58% [44–71] з 48» — частка, 95% довірчий інтервал і розмір вибірки."""
    if not n:
        return "— з 0"
    lo, hi = ci or (0.0, 100.0)
    small = " ⚠️" if n < 30 else ""
    return f"{rate:.0f}% [{lo:.0f}–{hi:.0f}] з {n}{small}"


def _hr(h) -> str:
    return _rate(h.rate, h.ci, h.n)


def _signals(t: Tally) -> str:
    if not t.n:
        return "немає даних"
    return f"вдалих {_rate(t.win_rate, t.ci, t.n)} · невдалих {t.loss} · нейтр. {t.neutral}"


def _pnl(p: PnL) -> str:
    if not p.n:
        return "немає угод"
    return (f"{plural_trades(p.n)} · сума {p.total:+.2f}% · макс. просадка {p.max_drawdown:.2f}% · "
            f"в середньому {p.mean:+.3f}% · прибуткових {_hr(p.wins)}")


def _verdict(model: HitRate, base: HitRate) -> str:
    if not model.n:
        return ""
    m_lo, m_hi = model.ci
    b_lo, b_hi = base.ci
    if m_lo > b_hi:
        return "бот краще"
    if m_hi < b_lo:
        return "бот гірше"
    return "різниця в межах похибки"


def _horizon_main(hs: HorizonStats) -> list[str]:
    from bot.triggers import NAMES_UA

    h = hs.horizon
    lines = [f"<b>Основне — {h} хв</b>", f"Сигнали лонг/шорт: {_signals(hs.signals)}"]
    lines.append(f"«Чекати» (руху більшого за витрати не було): {_hr(hs.wait_ok)}")
    lines.append("Калібрування (прогноз → напрямок вгадано):")
    lines += [f"  {name}: {_hr(c)}" for name, c in hs.calibration.items()]
    lines.append("Бот vs «як остання свічка» (усі прогнози з напрямком &gt; 50%):")
    if hs.model_dir.n:
        lines.append(f"  бот {_hr(hs.model_dir)}")
        lines.append(f"  свічка {_hr(hs.baseline_dir)}")
        lines.append(f"  → {_verdict(hs.model_dir, hs.baseline_dir)}")
    else:
        lines.append("  немає даних")
    if hs.triggers:
        lines.append("Тригери (напрямок вгадано):")
        lines += [f"  {escape(NAMES_UA.get(k, k))}: {_hr(v)}" for k, v in sorted(hs.triggers.items())]
    srcs = {"manual": "ручні", "alert": "авто", "alert_filtered": "відфільтр. (за тригерами)"}
    s_lines = [f"  {srcs[k]}: {_rate(t.win_rate, t.ci, t.n)}" for k, t in hs.sources.items() if t.n]
    if s_lines:
        lines += ["Джерела (вдалих):"] + s_lines
    lines.append(f"Гіпотетичний P&amp;L (угода на {h} хв, мінус спред і комісія):")
    lines.append(f"  кожен прогноз з напрямком &gt; 50%: {_pnl(hs.pnl_all)}")
    lines.append(f"  лише сигнали лонг/шорт: {_pnl(hs.pnl_signals)}")
    return lines


def _horizon_extra(hs: HorizonStats) -> list[str]:
    cal = " · ".join(f"{name} {_pct(c.rate)} ({c.n})" for name, c in hs.calibration.items() if c.n)
    lines = [
        f"<b>{hs.horizon} хв</b>: сигнали — {_signals(hs.signals)}",
        f"  «чекати» {_hr(hs.wait_ok)} · бот {_hr(hs.model_dir)} vs свічка {_hr(hs.baseline_dir)}",
    ]
    if cal:
        lines.append(f"  калібрування: {cal}")
    lines.append(f"  P&amp;L усіх з напрямком: {_pnl(hs.pnl_all)}")
    return lines


def format_stats(periods: list[tuple[str, Stats]], round_trip_pct: float) -> list[str]:
    """Звіт статистики: одне повідомлення на період (щоб не впертися в ліміт Telegram 4096 символів)."""
    header = (
        "<b>Статистика</b>. Вдалий — рух у бік прогнозу більший за витрати (спред + комісія на момент "
        f"прогнозу, зараз ≈{round_trip_pct:.3f}%). У дужках — 95% довірчий інтервал; ⚠️ — менше 30 прогнозів, "
        "висновків ще не робіть."
    )
    messages = []
    for i, (title, st) in enumerate(periods):
        src = st.by_source_count
        lines = [header, ""] if i == 0 else []
        lines.append(
            f"<b>{escape(title)}</b>: прогнозів {st.total} "
            f"(ручних {src.get('manual', 0)}, авто {src.get('alert', 0)}, відфільтр. {src.get('alert_filtered', 0)})"
        )
        if st.total:
            lines += [""] + _horizon_main(st.main)
            extra = [hs for h, hs in st.horizons.items() if h != st.main_horizon]
            if extra:
                lines += ["", "<b>Додатково</b>"]
                for hs in extra:
                    lines += _horizon_extra(hs)
        messages.append("\n".join(lines))
    return messages
