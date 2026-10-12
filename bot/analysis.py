"""Аналіз знімка ринку моделлю Claude і валідація відповіді.

Запуск з консолі (етап 4): python -m bot.analysis [--snapshot файл.json]
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from bot.config import AppConfig, Secrets

log = logging.getLogger(__name__)

ACTIONS = ("long", "short", "wait")
CONFIDENCE = ("low", "medium", "high")
MAX_ENTRY_DEVIATION_PCT = 3.0   # вхід далі за 3% від ціни — явна помилка моделі
# Моделі, для яких API підтримує серверний fallback при відмові.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_num_or_null = {"anyOf": [{"type": "number"}, {"type": "null"}]}
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "long_pct": {"type": "integer"},
        "short_pct": {"type": "integer"},
        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
        "summary": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "entry": _num_or_null,
        "stop_loss": _num_or_null,
        "take_profit": _num_or_null,
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "action", "long_pct", "short_pct", "confidence", "summary", "reasons",
        "entry", "stop_loss", "take_profit", "risk_flags",
    ],
    "additionalProperties": False,
}


class AnalysisError(Exception):
    pass


def parse_json(text: str) -> dict[str, Any]:
    """Дістає JSON-об'єкт з відповіді, навіть якщо модель обгорнула його в ```json."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    elif not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("у відповіді немає JSON-об'єкта")
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("JSON не є об'єктом")
    return data


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    raise ValueError


def validate(data: dict[str, Any], price: float, fee_pct: float, spread_pct: float | None) -> tuple[dict, list[str]]:
    """Перевіряє відповідь моделі. Повертає (нормалізовані дані, список помилок)."""
    errors: list[str] = []
    out = dict(data)

    action = out.get("action")
    if action not in ACTIONS:
        errors.append(f"action має бути одним з {ACTIONS}, отримано {action!r}")
    if out.get("confidence") not in CONFIDENCE:
        errors.append(f"confidence має бути одним з {CONFIDENCE}")

    lp, sp = out.get("long_pct"), out.get("short_pct")
    if isinstance(lp, float) and lp.is_integer():
        lp = out["long_pct"] = int(lp)
    if isinstance(sp, float) and sp.is_integer():
        sp = out["short_pct"] = int(sp)
    if not (isinstance(lp, int) and isinstance(sp, int)) or isinstance(lp, bool) or isinstance(sp, bool):
        errors.append("long_pct і short_pct мають бути цілими числами")
    elif not (0 <= lp <= 100 and 0 <= sp <= 100) or lp + sp != 100:
        errors.append(f"long_pct + short_pct має дорівнювати 100 (зараз {lp} + {sp})")
    elif action == "long" and lp <= sp:
        errors.append("action=long, але long_pct не більший за short_pct")
    elif action == "short" and sp <= lp:
        errors.append("action=short, але short_pct не більший за long_pct")

    if not isinstance(out.get("summary"), str) or not out["summary"].strip():
        errors.append("summary порожній")
    for key, limit in (("reasons", 4), ("risk_flags", 6)):
        val = out.get(key) or []
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            errors.append(f"{key} має бути списком рядків")
        else:
            out[key] = [x.strip() for x in val if x.strip()][:limit]

    try:
        entry, stop, take = (_num(out.get(k)) for k in ("entry", "stop_loss", "take_profit"))
    except ValueError:
        errors.append("entry/stop_loss/take_profit мають бути числами або null")
        return out, errors

    if action == "wait":
        out["entry"] = out["stop_loss"] = out["take_profit"] = None
    elif action in ("long", "short"):
        if None in (entry, stop, take):
            errors.append("для long/short потрібні entry, stop_loss і take_profit")
        else:
            assert entry is not None and stop is not None and take is not None
            if action == "long" and not stop < entry < take:
                errors.append(f"для лонгу потрібно stop < entry < take (зараз {stop} / {entry} / {take})")
            if action == "short" and not take < entry < stop:
                errors.append(f"для шорту потрібно take < entry < stop (зараз {take} / {entry} / {stop})")
            if abs(entry - price) / price * 100 > MAX_ENTRY_DEVIATION_PCT:
                errors.append(f"entry {entry} надто далеко від поточної ціни {price}")
            min_move = 2 * fee_pct + (spread_pct or 0)
            move = abs(take - entry) / entry * 100
            if move <= min_move:
                errors.append(
                    f"відстань до тейку {move:.3f}% не перекриває комісію і спред ({min_move:.3f}%)"
                )
    return out, errors


@dataclass
class AnalysisResult:
    data: dict[str, Any]
    model: str
    prompt_version: str
    attempts: int
    elapsed_sec: float
    warnings: list[str] = field(default_factory=list)


class Analyzer:
    def __init__(self, cfg: AppConfig, secrets: Secrets, client: Any = None):
        self.cfg = cfg
        if not secrets.anthropic_model:
            raise AnalysisError("ANTHROPIC_MODEL не задано в .env")
        self.model = secrets.anthropic_model
        if client is None:
            if secrets.anthropic_api_key is None:
                raise AnalysisError("ANTHROPIC_API_KEY не задано в .env")
            client = anthropic.AsyncAnthropic(
                api_key=secrets.anthropic_api_key.get_secret_value(),
                timeout=cfg.analysis.timeout_sec,
                max_retries=1,
            )
        self.client = client

    def load_prompt(self) -> tuple[str, str]:
        """Промпт читається з файлу щоразу — його можна правити без перезапуску."""
        text = self.cfg.prompt_path.read_text(encoding="utf-8").replace("{symbol}", self.cfg.mt5.symbol)
        version = hashlib.sha256(text.encode()).hexdigest()[:8]
        return text, version

    async def _call(self, system: str, messages: list[dict]) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.cfg.analysis.max_tokens,
            "system": system,
            "messages": messages,
        }
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}}
        if self.cfg.analysis.effort:
            output_config["effort"] = self.cfg.analysis.effort
        kwargs["output_config"] = output_config
        if self.cfg.analysis.refusal_fallback and self.model in FALLBACK_MODELS:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            resp = await asyncio.wait_for(
                self.client.beta.messages.create(**kwargs), timeout=self.cfg.analysis.timeout_sec
            )
        except asyncio.TimeoutError as exc:
            raise AnalysisError(f"модель не відповіла за {self.cfg.analysis.timeout_sec:.0f} с") from exc
        except anthropic.AuthenticationError as exc:
            raise AnalysisError("невірний ANTHROPIC_API_KEY") from exc
        except anthropic.NotFoundError as exc:
            raise AnalysisError(f"модель {self.model} не знайдена") from exc
        except anthropic.RateLimitError as exc:
            raise AnalysisError("перевищено ліміт запитів до Anthropic API") from exc
        except anthropic.APIStatusError as exc:
            raise AnalysisError(f"помилка Anthropic API {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AnalysisError("немає з'єднання з Anthropic API") from exc

        if resp.stop_reason == "refusal":
            raise AnalysisError("модель відмовилася відповідати")
        if resp.stop_reason == "max_tokens":
            log.warning("Відповідь моделі обрізана (max_tokens)")
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    async def analyze(self, snapshot: dict[str, Any], price: float) -> AnalysisResult:
        system, version = self.load_prompt()
        user = (
            f"Знімок ринку на {snapshot.get('time_utc')} UTC:\n"
            f"{json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}"
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        spread = (snapshot.get("quote") or {}).get("spread_pct")
        started = time.monotonic()
        errors: list[str] = []
        for attempt in (1, 2):
            text = await self._call(system, messages)
            try:
                data = parse_json(text)
            except ValueError as exc:
                errors = [f"невалідний JSON: {exc}"]
            else:
                data, errors = validate(data, price, 0.0, spread)
                if not errors:
                    return AnalysisResult(data, self.model, version, attempt, time.monotonic() - started)
            log.warning("Спроба %d: відповідь моделі невалідна: %s", attempt, "; ".join(errors))
            messages += [
                {"role": "assistant", "content": text or "{}"},
                {
                    "role": "user",
                    "content": "Відповідь не пройшла перевірку:\n- " + "\n- ".join(errors)
                    + "\nВиправ і поверни повний JSON заново.",
                },
            ]
        raise AnalysisError("модель двічі повернула невалідну відповідь: " + "; ".join(errors))


async def _main(snapshot_file: str | None) -> None:
    from bot.config import load_settings
    from bot.logging_setup import setup_logging
    from bot.news.calendar import EventCalendar
    from bot.news.rss import NewsService
    from bot.snapshot import MarketService, collect

    settings = load_settings()
    cfg = settings.config
    setup_logging(cfg, settings.secrets)
    analyzer = Analyzer(cfg, settings.secrets)
    if snapshot_file:
        with open(snapshot_file, encoding="utf-8") as f:
            snap_data = json.load(f)
        price = float(snap_data["price"])
    else:
        market = MarketService(cfg, secrets=settings.secrets)
        news = NewsService(cfg.news, cfg.network)
        try:
            await market.start()
            await news.poll_once()
            if not await market.wait_connected(cfg.mt5.init_timeout_ms / 1000 + 5):
                raise SystemExit(f"MT5 недоступний: {market.source.last_error}")
            snap = await collect(cfg, market, news, EventCalendar(cfg.calendar))
        finally:
            await market.stop()
            await news.close()
        snap_data, price = snap.data, snap.price
    result = await analyzer.analyze(snap_data, price)
    print(json.dumps(result.data, ensure_ascii=False, indent=2))
    print(f"\nМодель {result.model}, промпт {result.prompt_version}, спроб {result.attempts}, "
          f"{result.elapsed_sec:.1f} с")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Отримати аналіз моделі")
    p.add_argument("--snapshot", help="JSON-файл зі знімком (інакше — свіжий знімок з MT5)")
    asyncio.run(_main(p.parse_args().snapshot))
