#!/usr/bin/env python3
"""Runs the booking agent.

  sim   scripted managers from sim/scenarios.py, offline (--llm fake) or with the real model (--llm groq)
  dry   you play the manager in the console, the real model answers, nothing is sent anywhere
  test  Telegram, manager = a test account from ALLOWED_TEST_PEERS (default @noffybtw, Yaroslav's main account)
  live  Telegram, manager = @vlad_dev_7; needs --live-confirm vlad_dev_7, the real clock, before 16:30 London
  preflight  Telegram checks that write to nobody: session, account, peers resolved, a voice note to the
        agent's own Saved Messages and back through STT, the owner bot by getMe; report in runs/preflight-<id>/
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from agent import clock, config, texts
from agent.config import RUNS_DIR, STATE_DIR, SafetyError, check_safety, load_secrets, norm_username
from agent.events import Event
from agent.facts import FactStore

DRY_HELP = """You are the manager. One line = one message. Also:
  voice: <text>        a voice note (already transcribed)
  edit: <text>         edit your last message
  media: <what>        a sticker or photo, e.g. "media: a sticker"
  owner: <text>        the owner answers the oldest open question; "owner phone: +44..." names the question
  owner: accept|only   the owner's button for another time
  /pause /resume /stop /status
Ctrl-D ends the run."""


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["sim", "dry", "test", "live", "preflight"], required=True)
    p.add_argument("--scenario", default="all", help="sim: a scenario name or 'all'")
    p.add_argument("--llm", choices=["fake", "groq"], help="default: fake for sim, groq otherwise")
    p.add_argument("--now", help="clock override, ISO (London if no offset), e.g. 2026-11-19T08:00; not in live")
    p.add_argument("--peer", help=f"test: manager username (default {sorted(config.ALLOWED_TEST_PEERS)[0]})")
    p.add_argument("--live-confirm", help=f"live: type {config.LIVE_MANAGER} again")
    p.add_argument("--until", default="17:00", help="test/live: stop at this London time if not done (HH:MM); "
                                                     "a run does not start after it")
    p.add_argument("--linger", type=float, default=60,
                   help="test/live: minutes to stay after the restatement, counted from the last manager or owner event")
    p.add_argument("--tts", action="store_true", help="dry: really synthesise voice notes into runs/<id>/raw/")
    p.add_argument("--widgets", action="store_true", help="owner booking card (optional, see README)")
    p.add_argument("--quiet", action="store_true", help="sim: do not print transcripts")
    p.add_argument("--profile", help="dry/test/live: the owner's profile, a JSON file outside the repo (see README)")
    return p.parse_args(argv)


def stamp(now: datetime) -> str:
    return clock.london_now(now).strftime("%Y%m%d-%H%M%S")


def make_brain(llm: str, secrets, max_wait: float = 30.0):
    from agent.brain import FakeBrain, GroqBrain, simple_rules
    if llm == "fake":
        return FakeBrain(default=simple_rules)
    key = secrets.get("GROQ_API_KEY")
    if not key:
        raise SafetyError("GROQ_API_KEY is missing in .env")
    return GroqBrain(key, max_wait=max_wait)


class OffsetClock:
    """--now: starts at the given moment and runs at real speed."""

    def __init__(self, start: datetime | None):
        self.start, self.t0 = start, clock.london_now()

    def __call__(self) -> datetime:
        if self.start is None:
            return clock.london_now()
        return self.start + (clock.london_now() - self.t0)


async def feed_safely(runner, log, owner, ev) -> None:
    """One turn. An unexpected error in it is logged and told to the owner; the run goes on with the next event."""
    try:
        await runner.feed(ev)
    except Exception as e:  # noqa: BLE001 - one bad turn must not end a live chat
        error = f"{type(e).__name__}: {str(e)[:160]}"
        log.record_note(f"error in the {ev.kind} turn ({error}), the run goes on")
        await runner._safe(owner.notify(texts.t("turn_error", kind=ev.kind, error=error)))


class RunWindow:
    """When a Telegram run ends: at --until (London) without a confirmed booking, or --linger minutes after the last
    manager or owner event once the restatement is out."""

    def __init__(self, until: str, linger_min: float):
        self.until = parse_until(until)
        self.linger = linger_min * 60
        self.last: datetime | None = None

    def seen(self, ev, t: datetime) -> None:
        if ev is not None and ev.kind.startswith(("manager", "owner")):
            self.last = t

    def end_reason(self, t: datetime, confirmed: bool) -> str | None:
        if confirmed:
            self.last = self.last or t
            if (t - self.last).total_seconds() > self.linger:
                return "linger"
            return None
        if past(t, self.until):
            return "until"
        return None


def parse_until(value: str) -> tuple[int, int]:
    try:
        hh, mm = (int(x) for x in value.split(":"))
    except ValueError:
        raise SafetyError(f"--until {value!r} is not HH:MM") from None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise SafetyError(f"--until {value!r} is not HH:MM")
    return hh, mm


def past(t: datetime, until: tuple[int, int]) -> bool:
    t = clock.london_now(t)
    return (t.hour, t.minute) >= until


def check_until(until: str, now: datetime) -> None:
    """A run that would stop at its first loop pass must not send the opening."""
    if past(now, parse_until(until)):
        raise SafetyError(f"it is {clock.hhmm(now)} in London, past --until {until}; nothing was sent")


def check_start(now: datetime) -> None:
    """No opening to a restaurant before its working day."""
    if clock.london_now(now).hour < 8:
        raise SafetyError(f"it is {clock.hhmm(now)} in London, before 08:00; nothing was sent")


# ---------- sim ----------

def cmd_sim(args, secrets) -> int:
    from agent.brain import FakeBrain
    from sim import harness
    from sim.scenarios import BY_NAME, SCENARIOS
    names = [s.name for s in SCENARIOS] if args.scenario == "all" else args.scenario.split(",")
    unknown = [n for n in names if n not in BY_NAME]
    if unknown:
        print(f"unknown scenario(s) {unknown}; known: {', '.join(BY_NAME)}", file=sys.stderr)
        return 2
    llm = args.llm or "fake"
    st = stamp(clock.london_now())
    failed = 0
    echo = (lambda s: None) if args.quiet else print
    for name in names:
        scn = BY_NAME[name]
        # scripted steps arrive back to back, far faster than a manager types: let the brain wait for its budget
        brain = FakeBrain() if llm == "fake" else make_brain("groq", secrets, max_wait=65)
        rd = RUNS_DIR / f"sim-{st}-{name}" if len(names) > 1 or llm == "fake" else RUNS_DIR / f"sim-{st}-{name}-groq"
        res = harness.run(scn, brain, rd, echo=echo, scripted=(llm == "fake"))
        problems = res.problems
        if llm == "fake" and brain.queue:
            problems.append(f"{len(brain.queue)} scripted decision(s) not used")
        status = "ok" if not problems else f"{len(problems)} problem(s)"
        print(f"-> {name}: {status}; restatements {res.restatements}, correction wording "
              f"{'yes' if res.correction_wording else 'no'}; log {rd.relative_to(RUNS_DIR.parent)}/decisions.md")
        for p in problems:
            print(f"   ! {p}")
        print()
        failed += bool(problems)
    print(f"{len(names) - failed}/{len(names)} scenarios without problems")
    return 1 if failed and llm == "fake" else 0


# ---------- dry ----------

class ConsoleTransport:
    def __init__(self, now):
        self.now, self.n = now, 0

    async def send_text(self, text):
        from agent.runlog import mask_phone
        self.n += 1
        print(f"{clock.hhmm(self.now(), True)} agent: {mask_phone(text)}", flush=True)
        return self.n

    async def send_voice(self, ogg_path, caption):
        from agent.runlog import mask_phone
        self.n += 1
        where = f" [{ogg_path}]" if ogg_path and Path(ogg_path).exists() else ""
        print(f"{clock.hhmm(self.now(), True)} agent (voice note){where}: {mask_phone(caption)}", flush=True)
        return self.n


class ConsoleOwner:
    async def ask(self, key, question, buttons, contact):
        extra = f"  buttons: {', '.join(f'{lbl} -> owner: {v}' for lbl, v in buttons)}" if buttons else ""
        print(f"   [owner asked: {key}] {question}{extra}", flush=True)

    async def notify(self, text):
        print(f"   [owner told] {text}", flush=True)

    async def mirror(self, line):
        pass


def dry_event(line: str, last_text: str) -> Event | None:
    from agent.events import Msg, manager_event
    s = line.strip()
    if not s:
        return None
    if s.startswith("/"):
        return Event("owner_command", text=s[1:])
    head, sep, rest = s.partition(":")
    head_l = head.strip().lower()
    if sep and head_l == "voice":
        return manager_event([Msg(rest.strip(), kind="voice", lang="en")])
    if sep and head_l == "edit":
        return Event("manager_edit", messages=[Msg(rest.strip())], text=rest.strip(), old_text=last_text)
    if sep and head_l == "media":
        return manager_event([Msg(rest.strip() or "a sticker", kind="media")])
    if sep and head_l.startswith("owner"):
        key = head_l[5:].strip() or None
        val = rest.strip()
        value = val.lower() if val.lower() in ("accept", "only") else None
        return Event("owner_answer", key=key, text=val, value=value)
    return manager_event([Msg(s)])


def load_profile(args):
    """--profile, or None. Only a masked summary is printed."""
    if not args.profile:
        return None
    from agent.profile import load
    profile = load(args.profile)
    print(f"owner profile: {profile.summary()}", file=sys.stderr)
    return profile


def profile_header(profile) -> str:
    return f" Owner profile: {profile.summary()}." if profile else ""


async def cmd_dry(args, secrets) -> int:
    from agent.engine import Engine
    from agent.runlog import RunLog
    from agent.runner import AsyncTimers, Runner
    now = OffsetClock(clock.parse_now(args.now) if args.now else None)
    brain = make_brain(args.llm or "groq", secrets)
    rd = RUNS_DIR / f"dry-{stamp(now())}"
    profile = load_profile(args)
    log = RunLog(rd, now, secrets.all_values(),
                 header=f"Dry run (console manager). Model: {brain.name}.{profile_header(profile)}")
    queue: asyncio.Queue = asyncio.Queue()
    synth = None
    if args.tts:
        from agent.voice import Voice
        synth = Voice(secrets.get("GROQ_API_KEY"), rd / "raw").synthesize
    else:
        async def synth(text):
            return "(not synthesised, use --tts)"
    engine = Engine(brain, FactStore.for_task(now()), now=now, profile=profile)
    timers = AsyncTimers(queue)
    runner = Runner(engine, ConsoleTransport(now), ConsoleOwner(), log, timers, synthesize=synth, mirror=False)
    print(DRY_HELP, flush=True)
    await runner.feed(Event("opening"))
    last_text = ""
    loop = asyncio.get_running_loop()

    async def read_stdin():
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                await queue.put(None)
                return
            await queue.put(line)

    reader = loop.create_task(read_stdin())
    try:
        while not runner.stopped:
            item = await queue.get()
            if item is None:
                break
            ev = dry_event(item, last_text) if isinstance(item, str) else item
            if ev is None:
                continue
            if ev.kind.startswith("manager") and ev.messages and ev.messages[0].text:
                last_text = ev.messages[-1].text
            await feed_safely(runner, log, runner.owner, ev)
    finally:
        timers.cancel_all()
        reader.cancel()
        log.close()
    print(f"log: {rd / 'decisions.md'}")
    return 0


# ---------- preflight ----------

async def cmd_preflight(args, secrets) -> int:
    from agent import preflight
    from agent.owner_bot import OwnerBot
    from agent.transport_tg import SessionLock
    from agent.voice import Voice
    need = ["TG_SESSION", "TG_API_ID", "TG_API_HASH", "OWNER_BOT_TOKEN", "GROQ_API_KEY"]
    missing = [k for k in need if not secrets.get(k)]
    if missing:
        print(f"missing in .env: {', '.join(missing)}", file=sys.stderr)
        return 2
    rd = RUNS_DIR / f"preflight-{stamp(clock.london_now())}"
    values = secrets.all_values()

    def scrub(s: str) -> str:
        for v in values:
            s = s.replace(v, "[secret]")
        return s
    with SessionLock(STATE_DIR / "session.lock"):
        from telethon import TelegramClient
        client = TelegramClient(session_arg(secrets.get("TG_SESSION")), int(secrets.get("TG_API_ID")),
                                secrets.get("TG_API_HASH"))
        bot = OwnerBot(secrets.get("OWNER_BOT_TOKEN"), int(secrets.get("OWNER_CHAT_ID") or 0), asyncio.Queue())
        try:
            ok, _ = await preflight.run(preflight.SavedMessagesOnly(client), Voice(secrets.get("GROQ_API_KEY"), rd / "raw"),
                                        bot, rd, scrub)
        finally:
            await bot.close()
    print(f"report: {rd / 'report.txt'}")
    return 0 if ok else 1


# ---------- test / live ----------

def session_arg(value: str):
    """TG_SESSION is a path to a .session file or a StringSession string."""
    if "/" in value or value.endswith(".session") or Path(value).exists() or Path(value + ".session").exists():
        return value[:-8] if value.endswith(".session") else value
    from telethon.sessions import StringSession
    return StringSession(value)


async def cmd_telegram(args, secrets, peer: str) -> int:
    from agent.engine import CONFIRMED, Engine
    from agent.owner_bot import OwnerBot
    from agent.runlog import RunLog
    from agent.runner import AsyncTimers, Runner
    from agent.transport_tg import SessionLock, TelegramTransport
    from agent.voice import Voice

    missing = [k for k in config.TELEGRAM_KEYS if not secrets.get(k)]
    if missing:
        print(f"missing in .env: {', '.join(missing)}", file=sys.stderr)
        return 2
    now = OffsetClock(clock.parse_now(args.now) if args.now else None)
    timings = config.Timings()
    profile = load_profile(args)  # a bad profile is refused before Telegram is touched
    with SessionLock(STATE_DIR / "session.lock"):
        rd = RUNS_DIR / f"{args.mode}-{stamp(now())}"
        queue: asyncio.Queue = asyncio.Queue()
        voice = Voice(secrets.get("GROQ_API_KEY"), rd / "raw")
        brain = make_brain(args.llm or "groq", secrets)
        log = RunLog(rd, now, secrets.all_values(),
                     header=f"Mode {args.mode}, manager @{peer}, model {brain.name}.{profile_header(profile)}",
                     echo=print)
        owner = OwnerBot(secrets.get("OWNER_BOT_TOKEN"), int(secrets.get("OWNER_CHAT_ID")), queue)
        transport = TelegramTransport(session_arg(secrets.get("TG_SESSION")), int(secrets.get("TG_API_ID")),
                                      secrets.get("TG_API_HASH"), peer, queue, rd / "raw", voice.transcribe,
                                      timings.typing_per_char, timings.typing_max, timings.debounce_quiet,
                                      timings.debounce_max)
        timers = AsyncTimers(queue)
        poller, started, ended = None, False, "other"
        try:
            await owner.skip_old()
            resolved = await transport.start()
            if norm_username(resolved) != peer:
                raise SafetyError(f"{peer!r} resolved to {resolved!r}; refusing")
            engine = Engine(brain, FactStore.for_task(now()), now=now, timings=timings, profile=profile)
            runner = Runner(engine, transport, owner, log, timers, synthesize=voice.synthesize)
            owner.on_stop = runner.stop_now
            owner.on_note = log.record_note
            start = texts.t("start", mode=args.mode, peer=peer)
            await owner.send(f"{start}\n{profile.owner_text()}" if profile else start)
            started = True
            poller = asyncio.get_running_loop().create_task(owner.poll_forever())
            await queue.put(Event("opening"))
            window = RunWindow(args.until, args.linger)
            while not runner.stopped:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    ev = None
                if ev is not None:
                    await feed_safely(runner, log, owner, ev)
                t = now()
                window.seen(ev, t)
                reason = window.end_reason(t, engine.phase == CONFIRMED)
                if reason == "linger":
                    log.record_note(f"restatement sent and no message for {args.linger:.0f} minutes, ending the run")
                    ended = "linger"
                    break
                if reason == "until":
                    log.record_note(f"{args.until} London reached without a confirmed booking, ending the run")
                    await runner._safe(owner.notify(texts.t("time_up", until=args.until)))
                    ended = "until"
                    break
            if runner.stopped:
                ended = "stop"
        finally:
            timers.cancel_all()
            if poller:
                poller.cancel()
            log.close()
            if started:
                why = texts.t(f"end.{ended}", minutes=f"{args.linger:.0f}")
                try:
                    await owner.send(texts.t("end", reason=why, log=f"runs/{rd.name}/decisions.md"))
                except Exception:  # noqa: BLE001 - the run is over either way
                    pass
            await owner.close()
            await transport.stop()
        try:
            from agent.render import render
            # the Playwright sync API refuses to run inside the event loop, so it gets a worker thread
            png = await asyncio.to_thread(render, rd, title=f"@{peer}", manager_name="Manager",
                                          agent_name="Booking agent")
            print(f"transcript: {png}")
        except Exception as e:  # noqa: BLE001 - the picture is a bonus, the logs are already written
            print(f"transcript not rendered: {type(e).__name__}: {e}", file=sys.stderr)
        print(f"log: {rd / 'decisions.md'}")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    secrets = load_secrets()
    texts.use(secrets.get("OWNER_LANG"))  # the environment variable, when set, wins
    try:
        now = clock.parse_now(args.now) if args.now else None
        peer = args.peer or (sorted(config.ALLOWED_TEST_PEERS)[0] if args.mode == "test" else None)
        peer = check_safety(args.mode, peer, args.live_confirm, now, now_overridden=bool(args.now))
        if args.mode in ("test", "live"):
            check_start(now or clock.london_now())
            check_until(args.until, now or clock.london_now())
        if args.mode == "test" and not args.now and clock.london_now().hour >= 17:
            print("warning: it is past 17:00 in London; pass --now 2026-11-19T08:00 so 'today at 17:00' makes sense",
                  file=sys.stderr)
        if args.profile and args.mode in ("sim", "preflight"):
            raise SafetyError("--profile is for dry, test and live; sim scenarios carry their own profile")
        if args.widgets:
            print("note: --widgets is not built in this version; the owner gets plain messages", file=sys.stderr)
        if args.mode == "sim":
            return cmd_sim(args, secrets)
        if args.mode == "dry":
            return asyncio.run(cmd_dry(args, secrets))
        if args.mode == "preflight":
            return asyncio.run(cmd_preflight(args, secrets))
        return asyncio.run(cmd_telegram(args, secrets, peer))
    except SafetyError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
