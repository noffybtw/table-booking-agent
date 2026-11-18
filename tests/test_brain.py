import json

import httpx
import pytest

from agent import clock
from agent.brain import BrainDown, D, Decision, FakeBrain, GroqBrain, parse_decision
from agent.facts import FactStore

GOOD = json.dumps(D("Good morning, could I book a table for two today at 17:00, please?", intent="greeting"))


def facts():
    return FactStore.for_task(clock.parse_now("2026-11-19T08:00"))


def test_parse_decision_strips_think_and_text():
    d = parse_decision("<think>hmm</think>\nSure: " + GOOD + " done")
    assert d.action == "reply" and d.intent == "greeting"


def test_parse_decision_normalises_odd_values():
    d = parse_decision(json.dumps({"action": "ask_owner", "intent": "weird", "needs_owner": "phone",
                                   "slots": {"phone": "null", "time": 17}}))
    assert d.intent == "question" and d.needs_owner == ["phone"]
    assert d.slots.phone is None and d.slots.time == "17"


def test_parse_decision_rejects_garbage():
    with pytest.raises(ValueError):
        parse_decision("I would like to help")
    with pytest.raises(Exception):
        parse_decision('{"action": "dance"}')


def test_fake_brain_script_and_exhaustion():
    b = FakeBrain([GOOD, D(action="wait"), BrainDown("down")])
    assert b.decide(facts(), [], "e1").reply.startswith("Good morning")
    assert b.decide(facts(), [], "e2").action == "wait"
    with pytest.raises(BrainDown):
        b.decide(facts(), [], "e3")
    with pytest.raises(AssertionError):
        b.decide(facts(), [], "e4")


def completion(model, content, remaining="7000"):
    body = {"id": "x", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300}}
    return httpx.Response(200, json=body, headers={"x-ratelimit-remaining-tokens": remaining,
                                                    "x-ratelimit-reset-tokens": "7.5s"})


class Groq:
    """Fake Groq endpoint: per model, a list of responses (or exceptions) served in order."""

    def __init__(self, plan):
        self.plan = {m: list(v) for m, v in plan.items()}
        self.calls = []

    def __call__(self, request: httpx.Request):
        body = json.loads(request.content)
        model = body["model"]
        self.calls.append((model, body["response_format"]["type"]))
        item = self.plan[model].pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):
            return httpx.Response(item, json={"error": {"message": f"status {item}"}},
                                  headers={"retry-after": "12"})
        return completion(model, item)


def brain(plan, slept=None):
    g = Groq(plan)
    b = GroqBrain("test-key", http_client=httpx.Client(transport=httpx.MockTransport(g)),
                  sleep=(slept.append if slept is not None else (lambda s: None)))
    return b, g


def test_primary_ok():
    b, g = brain({"openai/gpt-oss-120b": [GOOD]})
    d = b.decide(facts(), [], "open")
    assert isinstance(d, Decision) and g.calls == [("openai/gpt-oss-120b", "json_schema")]
    assert b.last_meta["model"] == "openai/gpt-oss-120b" and b.last_meta["tokens"] == 1300


def test_429_switches_to_fallback_at_once():
    b, g = brain({"openai/gpt-oss-120b": [429], "qwen/qwen3.8-27b": [GOOD]})
    b.decide(facts(), [], "open")
    assert [m for m, _ in g.calls] == ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    assert b.last_meta["model"] == "qwen/qwen3.8-27b"
    # the primary is skipped while its retry-after runs
    b2_calls = len(g.calls)
    g.plan["qwen/qwen3.8-27b"].append(GOOD)
    b.decide(facts(), [], "next")
    assert [m for m, _ in g.calls[b2_calls:]] == ["qwen/qwen3.8-27b"]


def test_timeout_retry_then_fallback():
    t = httpx.ReadTimeout("slow")
    b, g = brain({"openai/gpt-oss-120b": [t, t], "qwen/qwen3.8-27b": [GOOD]})
    b.decide(facts(), [], "open")
    assert [m for m, _ in g.calls] == ["openai/gpt-oss-120b"] * 2 + ["qwen/qwen3.8-27b"]


def test_5xx_retry_once_on_same_model():
    b, g = brain({"openai/gpt-oss-120b": [503, GOOD]})
    b.decide(facts(), [], "open")
    assert [m for m, _ in g.calls] == ["openai/gpt-oss-120b"] * 2


def test_invalid_json_retries():
    b, g = brain({"openai/gpt-oss-120b": ["not json at all", GOOD]})
    assert b.decide(facts(), [], "open").intent == "greeting"
    assert len(g.calls) == 2


def test_400_on_schema_falls_back_to_json_object():
    b, g = brain({"openai/gpt-oss-120b": [400, GOOD]})
    b.decide(facts(), [], "open")
    assert [mode for _, mode in g.calls] == ["json_schema", "json_object"]


def test_both_down_raises():
    b, _ = brain({"openai/gpt-oss-120b": [503, 503], "qwen/qwen3.8-27b": [429]})
    with pytest.raises(BrainDown):
        b.decide(facts(), [], "open")


def test_pacing_on_remaining_tokens():
    slept = []
    g = Groq({})
    g.plan["openai/gpt-oss-120b"] = []

    def handler(request):
        g.calls.append(1)
        return completion("openai/gpt-oss-120b", GOOD, remaining="1200")

    clock_t = [100.0]
    b = GroqBrain("k", http_client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=slept.append,
                  monotonic=lambda: clock_t[0])
    b.decide(facts(), [], "one")
    b.decide(facts(), [], "two")
    assert slept and 7.5 <= slept[0] <= 8.5


def test_prompt_carries_facts_and_note():
    b, g = brain({"openai/gpt-oss-120b": [GOOD]})
    seen = {}
    orig = g.__call__

    def spy(request):
        seen.update(json.loads(request.content))
        return orig(request)

    b.client._client._transport = httpx.MockTransport(spy)
    b.decide(facts(), ["MANAGER: hi"], "evt", note="name: 'Ivy' is not in the facts")
    user = seen["messages"][1]["content"]
    assert "Thursday 19 November 2026" in user and "MANAGER: hi" in user and "BLOCKED" in user
    assert seen["temperature"] == 0.3
