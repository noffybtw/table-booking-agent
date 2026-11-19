"""Plays a scripted manager (and owner) against the real engine on a virtual clock. Nothing leaves the machine."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable

from agent import clock
from agent import texts as texts_
from agent.brain import FakeBrain
from agent.engine import Engine
from agent.events import Event, Log, Msg, NotifyOwner, SendVoice, manager_event
from agent.facts import FactStore
from agent.profile import from_dict
from agent.runlog import RunLog, mask_phone
from agent.runner import Runner, VirtualClock, VirtualTimers, voice_msg
from sim.scenarios import Scenario, Step


class SimTransport:
    def __init__(self, clock_: VirtualClock, echo: Callable[[str], None]):
        self.clock, self.echo, self.out, self.n = clock_, echo, [], 1000

    async def send_text(self, text: str) -> int:
        self.n += 1
        self.out.append(("text", text))
        self.echo(f"  {clock.hhmm(self.clock(), True)} agent: {text}")
        return self.n

    async def send_voice(self, ogg_path: str, caption: str) -> int:
        self.n += 1
        self.out.append(("voice", caption))
        self.echo(f"  {clock.hhmm(self.clock(), True)} agent (voice note): {caption}")
        return self.n


class SimOwner:
    def __init__(self, clock_: VirtualClock, echo: Callable[[str], None]):
        self.clock, self.echo = clock_, echo
        self.asked: list[tuple[str, str, list, bool]] = []
        self.notes: list[str] = []
        self.mirrored: list[str] = []

    async def ask(self, key, question, buttons, contact):
        self.asked.append((key, question, buttons, contact))
        self.echo(f"  {clock.hhmm(self.clock(), True)}   [owner asked: {key}] {question.splitlines()[0]}")

    async def notify(self, text):
        self.notes.append(text)
        self.echo(f"  {clock.hhmm(self.clock(), True)}   [owner told] {text}")

    async def mirror(self, line):
        self.mirrored.append(line)


async def fake_tts(text: str) -> str:
    return "sim-voice.ogg"


@dataclass
class StepResult:
    step: Step
    sent: list[tuple[str, str]]
    asked: list[str]
    notes: list[str]
    acts: list
    problems: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


@dataclass
class SimResult:
    scenario: Scenario
    run_dir: Path
    steps: list[StepResult]
    engine: Engine
    brain: object

    @property
    def problems(self) -> list[str]:
        return ([f"step {i} ({s.step.kind}): {p}" for i, s in enumerate(self.steps) for p in s.problems]
                + self.run_problems)

    run_problems: list[str] = field(default_factory=list)

    @property
    def all_sent(self) -> list[tuple[str, str]]:
        return [x for s in self.steps for x in s.sent]

    @property
    def restatements(self) -> int:
        return self.engine.n_restated

    @property
    def correction_wording(self) -> bool:
        return any("correction" in t.lower() for _, t in self.all_sent)


def check(step: Step, res: StepResult, scripted: bool = True) -> list[str]:
    e, probs = step.expect, []
    texts = " || ".join(t for _, t in res.sent)
    low = texts.lower()
    if "sends" in e and len(res.sent) != e["sends"]:
        probs.append(f"expected {e['sends']} message(s), sent {len(res.sent)}: {texts!r}")
    for s in e.get("says", []):
        if s.lower() not in low:
            probs.append(f"expected {s!r} in {texts!r}")
    for s in e.get("never", []):
        if s.lower() in low:
            probs.append(f"{s!r} must not be sent: {texts!r}")
    for k in e.get("asks", []):
        if k not in res.asked:
            probs.append(f"owner should be asked {k!r}, asked {res.asked}")
    for s in e.get("asks_says", []):
        if not any(s in q for q in res.questions):
            probs.append(f"expected {s!r} in the owner question: {res.questions!r}")
    if "asks_none" in e and res.asked:
        probs.append(f"owner should not be asked, asked {res.asked}")
    if e.get("voice") and not any(v == "voice" for v, _ in res.sent):
        probs.append("expected a voice note")
    if e.get("voice") is False and any(v == "voice" for v, _ in res.sent):
        probs.append("expected text only")
    if e.get("blocked") and scripted and not any(isinstance(a, Log) and a.kind == "guard" and a.data["problems"]
                                    for a in res.acts):
        probs.append("expected the guard to block a draft")
    if e.get("booked") is False and any(n.startswith(texts_.t("booked", text="")) for n in res.notes):
        probs.append(f"the owner was told the booking is confirmed: {res.notes!r}")
    if e.get("notify") and not res.notes:
        probs.append("expected a note to the owner")
    if e.get("english") and any("Ѐ" <= ch <= "ӿ" for ch in texts):
        probs.append("reply is not in English")
    return probs


async def run_async(scn: Scenario, brain, run_dir: Path, echo: Callable[[str], None] = print,
                    scripted: bool = True) -> SimResult:
    out = echo
    echo = lambda s: out(mask_phone(s))  # noqa: E731 - the phone is masked on stdout as in the logs
    vclock = VirtualClock(clock.parse_now(scn.now))
    facts = FactStore.for_task(vclock())
    profile = from_dict(scn.profile) if scn.profile else None
    engine = Engine(brain, facts, now=vclock, profile=profile)
    transport = SimTransport(vclock, echo)
    owner = SimOwner(vclock, echo)
    header = f"Sim scenario `{scn.name}`: {scn.title}. Model: {getattr(brain, 'name', '?')}."
    if profile:
        header += f" Owner profile: {profile.summary()}."
    log = RunLog(run_dir, vclock, header=header)
    timers = VirtualTimers(vclock)
    runner = Runner(engine, transport, owner, log, timers, synthesize=fake_tts)
    echo(f"== {scn.name}: {scn.title}")
    results: list[StepResult] = []
    stt_texts: dict[str, str | None] = {}

    async def stt(path: str):
        text = stt_texts[path]
        if text is None:
            raise RuntimeError("fake STT failure")
        return text, "en"

    for i, step in enumerate(scn.steps):
        if scripted and isinstance(brain, FakeBrain):
            brain.push(*step.brain)
        n_out, n_ask, n_note = len(transport.out), len(owner.asked), len(owner.notes)
        acts: list = []
        target = vclock() + timedelta(seconds=step.wait)
        while (ev := timers.next_due(target)) is not None:
            acts += await runner.feed(ev)
        vclock.t = target
        ev = None
        if step.kind == "open":
            ev = Event("opening")
        elif step.kind == "manager":
            echo(f"  {clock.hhmm(vclock(), True)} manager: {step.text}")
            ev = manager_event([Msg(step.text, 100 + i)])
        elif step.kind == "voice":
            path = f"sim-{i}.ogg"
            stt_texts[path] = step.text
            echo(f"  {clock.hhmm(vclock(), True)} manager (voice note): {step.text or '(unreadable)'}")
            ev = manager_event([await voice_msg(path, 100 + i, stt)])
        elif step.kind == "media":
            echo(f"  {clock.hhmm(vclock(), True)} manager: [{step.text}]")
            ev = manager_event([Msg(step.text, 100 + i, kind="media")])
        elif step.kind == "edit":
            echo(f"  {clock.hhmm(vclock(), True)} manager edited: {step.old!r} -> {step.text!r}")
            ev = Event("manager_edit", messages=[Msg(step.text, step.msg_id)], text=step.text, old_text=step.old,
                       msg_id=step.msg_id)
        elif step.kind == "owner":
            echo(f"  {clock.hhmm(vclock(), True)}   [owner answers{' ' + step.key if step.key else ''}] "
                 f"{step.text or step.value}")
            ev = Event("owner_answer", key=step.key, text=step.text, value=step.value, contact=step.contact)
        elif step.kind == "command":
            echo(f"  {clock.hhmm(vclock(), True)}   [owner command] /{step.text}")
            ev = Event("owner_command", text=step.text)
        elif step.kind == "silence":
            echo(f"  {clock.hhmm(vclock(), True)} ... ({step.wait / 60:.0f} min of silence)")
        if ev is not None:
            acts += await runner.feed(ev)
        res = StepResult(step, transport.out[n_out:], [a[0] for a in owner.asked[n_ask:]], owner.notes[n_note:], acts)
        res.questions = [a[1] for a in owner.asked[n_ask:]]
        res.problems = check(step, res, scripted)
        results.append(res)
    log.close()
    res = SimResult(scn, Path(run_dir), results, engine, brain)
    if scn.restatements is not None and res.restatements != scn.restatements:
        res.run_problems.append(f"expected {scn.restatements} restatement(s), got {res.restatements}")
    return res


def run(scn: Scenario, brain, run_dir: Path, echo: Callable[[str], None] = print, scripted: bool = True) -> SimResult:
    return asyncio.run(run_async(scn, brain, run_dir, echo, scripted))
