"""The model call. One stateless request per turn; JSON in a fixed schema; primary and fallback models."""
from __future__ import annotations

import json
import re
import time
from collections import deque
from typing import Callable, Literal

import openai
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent import prompt
from agent.config import FALLBACK_MODEL, GROQ_BASE_URL, PRIMARY_MODEL
from agent.facts import FactStore


class Slots(BaseModel):
    model_config = ConfigDict(extra="ignore")
    time: str | None = None
    date: str | None = None
    party_size: str | None = None
    restaurant_name: str | None = None
    guest_name: str | None = None
    phone: str | None = None
    offered_time: str | None = None
    condition: str | None = None
    manager_agreed: bool = False

    @field_validator("time", "date", "party_size", "restaurant_name", "guest_name", "phone", "offered_time",
                     "condition", mode="before")
    @classmethod
    def _str(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return None if v.lower() in ("", "null", "none", "unknown", "n/a") else v


class Decision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    understood: str = ""
    intent: str = "question"
    slots: Slots = Field(default_factory=Slots)
    needs_owner: list[str] = Field(default_factory=list)
    action: Literal["reply", "ask_owner", "final_restatement", "wait", "end"]
    reply: str = ""
    decision: str = ""
    claim: bool = False  # the manager's line says our side said, called, wants, cancelled or changed something

    @field_validator("intent", mode="before")
    @classmethod
    def _intent(cls, v):
        return v if v in prompt.INTENTS else "question"

    @field_validator("needs_owner", mode="before")
    @classmethod
    def _needs(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        return [str(x) for x in v if str(x).strip()]


class BrainDown(Exception):
    """No model produced a valid decision."""


def parse_decision(content: str) -> Decision:
    text = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in the reply")
    return Decision.model_validate(json.loads(text[a:b + 1]))


def build_messages(facts: FactStore, transcript: list[str], event: str, state_text: str = "",
                   note: str | None = None) -> list[dict]:
    who = facts.get("on_behalf_of", "the client")
    return [{"role": "system", "content": prompt.system(who)},
            {"role": "user", "content": prompt.user_message(facts.as_prompt(), state_text, transcript, event, note)}]


def _secs(v: str | None, default: float = 5.0) -> float:
    m = re.match(r"(?:(\d+)m)?([\d.]+)(ms|s)", v or "")
    if not m:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default
    s = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1)
    return int(m.group(1) or 0) * 60 + s


class GroqBrain:
    """Primary model first, the fallback on 429 at once, on 5xx/timeout after one retry, on bad JSON after one retry."""

    def __init__(self, api_key: str, primary: str = PRIMARY_MODEL, fallback: str = FALLBACK_MODEL,
                 http_client=None, timeout: float = 20.0, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic, low_tokens: int = 2500, max_wait: float = 30.0,
                 pace_wait: float = 10.0):
        self.client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL, timeout=timeout, max_retries=0,
                             http_client=http_client)
        self.primary, self.fallback = primary, fallback
        self.sleep, self.mono = sleep, monotonic
        self.low_tokens, self.max_wait, self.pace_wait = low_tokens, max_wait, pace_wait
        self.ready_at: dict[str, float] = {}     # pacing: low remaining tokens, worth a short wait
        self.blocked_until: dict[str, float] = {}  # 429: use the other model meanwhile
        self.no_schema: set[str] = set()
        self.last_meta: dict = {}
        self.name = f"groq:{primary}"

    def _call(self, model: str, messages: list[dict]) -> tuple[str, dict]:
        mode = "json_object" if model in self.no_schema else "json_schema"
        rf = ({"type": "json_schema", "json_schema": {"name": "turn", "schema": prompt.SCHEMA, "strict": True}}
              if mode == "json_schema" else {"type": "json_object"})
        t0 = self.mono()
        raw = self.client.chat.completions.with_raw_response.create(
            model=model, messages=messages, response_format=rf, temperature=0.3, max_completion_tokens=1500)
        comp = raw.parse()
        hdr = {k: v for k, v in raw.headers.items() if k.startswith("x-ratelimit")}
        try:
            if int(hdr.get("x-ratelimit-remaining-tokens", 99999)) < self.low_tokens:
                self.ready_at[model] = self.mono() + _secs(hdr.get("x-ratelimit-reset-tokens")) + 0.5
        except ValueError:
            pass
        usage = comp.usage.total_tokens if comp.usage else None
        meta = {"model": model, "mode": mode, "latency_s": round(self.mono() - t0, 2), "tokens": usage,
                "ratelimit": hdr}
        msg = comp.choices[0].message if comp.choices else None
        return (getattr(msg, "content", None) or ""), meta  # no choice: empty text, retried as invalid JSON

    def _wait(self, model: str) -> tuple[float, float]:
        now = self.mono()
        return max(0.0, self.blocked_until.get(model, 0) - now), max(0.0, self.ready_at.get(model, 0) - now)

    def _pick(self, live: list[str]) -> tuple[str, float]:
        """The preferred model unless it is cooling down after a 429 or would need more than pace_wait of pacing;
        otherwise whichever model is ready first."""
        for m in live:
            blocked, pace = self._wait(m)
            if blocked == 0 and pace <= self.pace_wait:
                return m, pace
        m = min(live, key=lambda x: max(self._wait(x)))
        return m, max(self._wait(m))

    def decide(self, facts: FactStore, transcript: list[str], event: str, state_text: str = "",
               note: str | None = None, prefer_fallback: bool = False) -> Decision:
        messages = build_messages(facts, transcript, event, state_text, note)
        models = [self.fallback] if prefer_fallback else [self.primary, self.fallback]
        attempts = {m: 0 for m in models}
        errors: list[str] = []
        for _ in range(2 * len(models) + 2):
            live = [m for m in models if attempts[m] < 2]
            if not live:
                break
            model, wait = self._pick(live)
            if wait > self.max_wait:
                errors.append(f"{model}: rate limit, ready in {wait:.0f}s")
                break
            if wait > 0:
                self.sleep(wait)
            attempts[model] += 1
            try:
                content, meta = self._call(model, messages)
            except openai.RateLimitError as e:
                ra = _secs(e.response.headers.get("retry-after") if e.response is not None else None, 10)
                self.blocked_until[model] = self.mono() + max(ra, 1.0)
                errors.append(f"{model}: 429, retry after {ra:.0f}s")
                continue
            except openai.InternalServerError as e:
                errors.append(f"{model}: {e.status_code}")
                continue
            except openai.APITimeoutError:
                errors.append(f"{model}: timeout")
                continue
            except openai.APIConnectionError as e:
                errors.append(f"{model}: connection error {type(e.__cause__).__name__}")
                continue
            except openai.BadRequestError as e:
                if model not in self.no_schema:
                    self.no_schema.add(model)
                    attempts[model] -= 1  # the switch to json_object is not a failed try
                    errors.append(f"{model}: 400 on json_schema, switching to json_object")
                else:
                    errors.append(f"{model}: 400 {str(e)[:120]}")
                continue
            except openai.APIStatusError as e:
                errors.append(f"{model}: {e.status_code}")
                attempts[model] = 2
                continue
            except Exception as e:  # noqa: BLE001 - anything else from one call counts as a failed try
                errors.append(f"{model}: {type(e).__name__}")
                continue
            try:
                d = parse_decision(content)
            except ValueError as e:
                errors.append(f"{model}: invalid JSON ({str(e)[:80]})")
                continue
            self.last_meta = meta | {"errors": errors}
            return d
        self.last_meta = {"errors": errors}
        raise BrainDown("; ".join(errors) or "no model available")


Script = Decision | dict | str | Exception | Callable


class FakeBrain:
    """Returns scripted decisions in order. Exhausting the script fails the test unless a default is given."""

    name = "fake"

    def __init__(self, script: list[Script] | None = None, default: Callable | None = None):
        self.queue: deque = deque(script or [])
        self.default = default
        self.calls: list[dict] = []
        self.last_meta: dict = {}

    def push(self, *items: Script) -> None:
        self.queue.extend(items)

    def decide(self, facts: FactStore, transcript: list[str], event: str, state_text: str = "",
               note: str | None = None, prefer_fallback: bool = False) -> Decision:
        self.calls.append({"event": event, "note": note, "fallback": prefer_fallback,
                           "transcript": list(transcript), "facts": facts.known()})
        self.last_meta = {"model": "fake-fallback" if prefer_fallback else "fake", "latency_s": 0.0, "tokens": 0}
        if self.queue:
            item = self.queue.popleft()
        elif self.default:
            item = self.default
        else:
            raise AssertionError(f"FakeBrain has no scripted decision for: {event}")
        if isinstance(item, Exception):
            raise item
        if callable(item) and not isinstance(item, (Decision, dict, str)):
            item = item(facts, transcript, event)
        if isinstance(item, str):
            return parse_decision(item)
        return item if isinstance(item, Decision) else Decision.model_validate(item)


def D(reply: str = "", action: str = "reply", intent: str = "question", understood: str = "", decision: str = "",
      needs_owner: tuple | list = (), claim: bool = False, **slots) -> dict:
    """A scripted decision for FakeBrain."""
    return {"understood": understood or intent.replace("_", " "), "intent": intent, "slots": slots,
            "needs_owner": list(needs_owner), "action": action, "reply": reply,
            "decision": decision or f"{action}: {intent}", "claim": claim}


def simple_rules(facts: FactStore, transcript: list[str], event: str) -> Decision:
    """Tiny offline stand-in for dry mode with --llm fake. Not used by the real agent."""
    last = next((t for t in reversed(transcript) if t.startswith("MANAGER")), "").lower()
    if "open the conversation" in event.lower():
        return Decision.model_validate(D("Hello. Could I book a table for two today at 17:00, please?",
                                         intent="greeting"))
    if re.search(r"\b(yes|booked|confirmed|fine|done|sure)\b", last):
        return Decision.model_validate(D(action="final_restatement", intent="agreed", manager_agreed=True))
    return Decision.model_validate(D("Thank you. Could I book a table for two today at 17:00, please?"))
