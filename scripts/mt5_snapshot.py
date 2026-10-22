"""Друкує в консоль реальний знімок символу з MT5 (за замовчуванням USOUSD.s).

Запуск на Windows із запущеним і залогіненим терміналом MT5:
    python scripts/mt5_snapshot.py            # таблиці
    python scripts/mt5_snapshot.py --json     # сирий JSON
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import ConfigError, load_settings  # noqa: E402
from bot.logging_setup import setup_logging  # noqa: E402
from bot.market import indicators as ind  # noqa: E402
from bot.market.mt5_source import Credentials, MT5Error, MT5Source  # noqa: E402

TF_NAMES = {1: "M1", 5: "M5", 15: "M15", 60: "H1"}


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _main(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Помилка конфігурації: {exc}", file=sys.stderr)
        return 2
    cfg = settings.config
    if args.symbol:
        cfg.mt5.symbol = args.symbol
    setup_logging(cfg, settings.secrets)

    src = MT5Source(cfg.mt5, Credentials.from_secrets(settings.secrets))
    try:
        await src.connect()
        snap = await src.snapshot()
        term, acc = await src.info()
    except MT5Error as exc:
        print(f"\nПОМИЛКА MT5: {exc}", file=sys.stderr)
        return 1
    finally:
        await src.stop()

    now_ms = snap["ts_ms"]
    k5 = snap["klines"][5]
    tf5 = None
    try:
        tf5 = ind.compute_5m(k5, cfg.indicators, now_ms, snap["tick"]["mid"])
    except ValueError as exc:
        tf5 = {"error": str(exc)}

    if args.json:
        out = {
            "symbol": src.symbol,
            "spec": asdict(src.spec),
            "server_offset_hours": src.offset / 3600,
            "offset_source": src.offset_source,
            "time_utc": _utc(now_ms),
            "tick": snap["tick"],
            "tick_activity": snap["tick_activity"],
            "tf_5m_indicators": tf5,
            "klines_tail": {TF_NAMES[i]: df.tail(args.bars).to_dict("records") for i, df in snap["klines"].items()},
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    print("\n=== Термінал / рахунок ===")
    print(f"Термінал: {getattr(term, 'name', '?')} build {getattr(term, 'build', '?')}, "
          f"з'єднаний з брокером: {getattr(term, 'connected', '?')}")
    print(f"Рахунок: {getattr(acc, 'login', '?')} @ {getattr(acc, 'server', '?')} ({getattr(acc, 'company', '?')})")
    print(f"Час сервера: UTC{src.offset / 3600:+.1f} год (джерело: {src.offset_source})")

    s = src.spec
    print(f"\n=== {s.symbol} — {s.description} ===")
    print(f"digits {s.digits}, point {s.point}, tick {s.tick_size}, контракт {s.contract_size}, "
          f"мін. лот {s.volume_min}, крок {s.volume_step}, валюта {s.currency_profit}, trade_mode {s.trade_mode}")

    t = snap["tick"]
    print(f"\nЗараз (UTC): {_utc(now_ms)}")
    print(f"Bid {t['bid']}  Ask {t['ask']}  Mid {t['mid']}")
    print(f"Спред {t['spread']} = {t['spread_points']} п. = {t['spread_pct']}%")
    print(f"Останній тік: {_utc(t['time_ms'])} UTC ({t['age_sec']} с тому)")

    a = snap["tick_activity"]
    print(f"\n=== Тікова активність (вікно {a['window_min']} хв) ===")
    for k, v in a.items():
        if k != "window_min":
            print(f"  {k}: {v}")

    for i, df in snap["klines"].items():
        print(f"\n=== {TF_NAMES[i]}: {len(df)} свічок, останні {args.bars} (час UTC) ===")
        print(f"{'start':<20}{'open':>10}{'high':>10}{'low':>10}{'close':>10}{'ticks':>8}{'spread':>8}  closed")
        for r in df.tail(args.bars).itertuples():
            print(f"{_utc(int(r.start)):<20}{r.open:>10}{r.high:>10}{r.low:>10}{r.close:>10}"
                  f"{int(r.volume):>8}{int(r.spread):>8}  {r.closed}")

    print("\n=== Індикатори 5 хв (ті самі, що бачить модель) ===")
    print(json.dumps(tf5, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Знімок символу з MetaTrader 5")
    p.add_argument("--symbol", help="інший символ замість mt5.symbol з config.yaml")
    p.add_argument("--bars", type=int, default=5, help="скільки останніх свічок друкувати")
    p.add_argument("--json", action="store_true", help="вивести JSON")
    sys.exit(asyncio.run(_main(p.parse_args())))
