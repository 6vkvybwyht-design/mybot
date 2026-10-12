import asyncio
import json
from types import SimpleNamespace

import pytest

from bot.analysis import RESPONSE_SCHEMA, AnalysisError, Analyzer, parse_json, validate
from bot.config import Secrets
from tests.helpers import CFG

FEE = 0.055
GOOD_LONG = {
    "action": "long", "long_pct": 64, "short_pct": 36, "confidence": "medium",
    "summary": "Покупці домінують.", "reasons": ["a", "b", "c", "d", "e"],
    "entry": 61.42, "stop_loss": 61.21, "take_profit": 61.78, "risk_flags": [],
}


def test_parse_json_variants():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Ось відповідь: {"a": {"b": 2}} кінець') == {"a": {"b": 2}}
    for bad in ("немає json", "[1, 2]", "{bad json}"):
        with pytest.raises(ValueError):
            parse_json(bad)


def test_valid_long_and_truncates_reasons():
    out, errors = validate(GOOD_LONG, 61.42, FEE, 0.02)
    assert errors == []
    assert len(out["reasons"]) == 4


def test_valid_short():
    d = {**GOOD_LONG, "action": "short", "long_pct": 30, "short_pct": 70,
         "entry": 61.40, "stop_loss": 61.60, "take_profit": 61.00}
    assert validate(d, 61.4, FEE, 0.02)[1] == []


def test_wait_clears_levels():
    d = {**GOOD_LONG, "action": "wait", "long_pct": 50, "short_pct": 50}
    out, errors = validate(d, 61.4, FEE, None)
    assert errors == [] and out["entry"] is None and out["take_profit"] is None


@pytest.mark.parametrize(
    "patch, fragment",
    [
        ({"long_pct": 60, "short_pct": 30}, "100"),
        ({"long_pct": 40, "short_pct": 60}, "long_pct не більший"),
        ({"long_pct": "64"}, "цілими"),
        ({"stop_loss": 61.5}, "stop < entry < take"),
        ({"take_profit": 61.43}, "не перекриває комісію"),
        ({"entry": 70.0, "take_profit": 71.0}, "надто далеко"),
        ({"take_profit": None}, "потрібні entry"),
        ({"action": "buy"}, "action"),
        ({"confidence": "very"}, "confidence"),
        ({"summary": " "}, "summary"),
        ({"entry": "61.4"}, "числами"),
    ],
)
def test_invalid_cases(patch, fragment):
    _, errors = validate({**GOOD_LONG, **patch}, 61.42, FEE, 0.02)
    assert any(fragment in e for e in errors), errors


def test_short_level_order():
    d = {**GOOD_LONG, "action": "short", "long_pct": 30, "short_pct": 70}
    _, errors = validate(d, 61.42, FEE, 0.02)
    assert any("take < entry < stop" in e for e in errors)


def test_fee_threshold_includes_spread():
    # 0.2% рух: проходить при спреді 0.02%, не проходить при спреді 0.1%
    d = {**GOOD_LONG, "entry": 100.0, "stop_loss": 99.8, "take_profit": 100.2}
    assert validate(d, 100.0, FEE, 0.02)[1] == []
    assert validate(d, 100.0, FEE, 0.1)[1] != []


def test_schema_is_strict():
    assert RESPONSE_SCHEMA["additionalProperties"] is False
    assert set(RESPONSE_SCHEMA["required"]) == set(RESPONSE_SCHEMA["properties"])


# ---------- виклик моделі (фейковий клієнт) ----------

class FakeMessages:
    def __init__(self, replies, delay=0.0, stop_reason="end_turn"):
        self.replies = list(replies)
        self.calls = []
        self.delay = delay
        self.stop_reason = stop_reason

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        text = self.replies.pop(0)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="thinking", thinking=""),
                                        SimpleNamespace(type="text", text=text)])


def _analyzer(msgs, model="claude-opus-5-5", **cfg_update):
    cfg = CFG
    if cfg_update:
        cfg = CFG.model_copy(update={"analysis": CFG.analysis.model_copy(update=cfg_update)})
    secrets = Secrets(telegram_bot_token=None, telegram_owner_id=None, anthropic_api_key=None,
                      anthropic_model=model)
    client = SimpleNamespace(beta=SimpleNamespace(messages=msgs))
    return Analyzer(cfg, secrets, client)


SNAP = {"time_utc": "2026-10-12 14:32:10", "price": 61.42, "quote": {"spread_pct": 0.02}}


async def test_analyze_success_and_request_shape():
    msgs = FakeMessages([json.dumps(GOOD_LONG)])
    res = await _analyzer(msgs).analyze(SNAP, 61.42)
    assert res.data["action"] == "long" and res.attempts == 1 and len(res.prompt_version) == 8
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["schema"] is RESPONSE_SCHEMA
    assert call["output_config"]["effort"] == "medium"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "USOUSD.s" in call["system"] and "{symbol}" not in call["system"]
    assert '"price":61.42' in call["messages"][0]["content"]


async def test_no_fallback_for_other_models():
    msgs = FakeMessages([json.dumps(GOOD_LONG)])
    await _analyzer(msgs, model="claude-haiku-4-5", effort=None).analyze(SNAP, 61.42)
    assert "fallbacks" not in msgs.calls[0] and "effort" not in msgs.calls[0]["output_config"]


async def test_retry_once_with_errors_then_ok():
    bad = {**GOOD_LONG, "long_pct": 70}
    msgs = FakeMessages([json.dumps(bad), json.dumps(GOOD_LONG)])
    res = await _analyzer(msgs).analyze(SNAP, 61.42)
    assert res.attempts == 2
    second = msgs.calls[1]["messages"]
    assert second[1]["role"] == "assistant" and "100" in second[2]["content"]


async def test_two_invalid_answers_raise():
    msgs = FakeMessages(["not json", "still not json"])
    with pytest.raises(AnalysisError):
        await _analyzer(msgs).analyze(SNAP, 61.42)
    assert len(msgs.calls) == 2


async def test_timeout():
    msgs = FakeMessages([json.dumps(GOOD_LONG)], delay=1.0)
    with pytest.raises(AnalysisError, match="не відповіла"):
        await _analyzer(msgs, timeout_sec=0.1).analyze(SNAP, 61.42)


async def test_refusal():
    msgs = FakeMessages([""], stop_reason="refusal")
    with pytest.raises(AnalysisError, match="відмовилася"):
        await _analyzer(msgs).analyze(SNAP, 61.42)


def test_missing_key_or_model():
    s = Secrets(telegram_bot_token=None, telegram_owner_id=None, anthropic_api_key=None, anthropic_model="m")
    with pytest.raises(AnalysisError):
        Analyzer(CFG, s)
    with pytest.raises(AnalysisError):
        Analyzer(CFG, s.model_copy(update={"anthropic_model": None}), client=object())
