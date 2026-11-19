"""Executes engine actions against a transport, the owner channel, timers and the run log. Same code for every mode."""
from __future__ import annotations

import asyncio
import heapq
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Protocol

from agent import texts
from agent.engine import Engine
from agent.events import AskOwner, Event, Log, Msg, NotifyOwner, Schedule, SendText, SendVoice, Stop
from agent.runlog import RunLog


class Transport(Protocol):
    async def send_text(self, text: str) -> int | None: ...
    async def send_voice(self, ogg_path: str, caption: str) -> int | None: ...


class OwnerChannel(Protocol):
    async def ask(self, key: str, question: str, buttons: list, contact: bool) -> None: ...
    async def notify(self, text: str) -> None: ...
    async def mirror(self, line: str) -> None: ...


class VirtualClock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class VirtualTimers:
    """Timers on the virtual clock: due() returns the events whose time has come."""
    def __init__(self, clock: VirtualClock):
        self.clock, self.heap, self.seq = clock, [], 0

    def schedule(self, s: Schedule) -> None:
        self.seq += 1
        heapq.heappush(self.heap, (self.clock() + timedelta(seconds=s.delay), self.seq, s))

    def next_due(self, until: datetime) -> Event | None:
        if self.heap and self.heap[0][0] <= until:
            at, _, s = heapq.heappop(self.heap)
            if at > self.clock():
                self.clock.t = at
            return Event("timer", text=s.name, key=s.key, gen=s.gen)
        return None


class AsyncTimers:
    """Real timers: the event goes into the main queue after the delay."""
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self.tasks: set[asyncio.Task] = set()

    def schedule(self, s: Schedule) -> None:
        async def fire():
            await asyncio.sleep(s.delay)
            await self.queue.put(Event("timer", text=s.name, key=s.key, gen=s.gen))
        t = asyncio.get_running_loop().create_task(fire())
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    def cancel_all(self) -> None:
        for t in list(self.tasks):
            t.cancel()


async def voice_msg(path: str, msg_id: int | None, transcribe: Callable[[str], Awaitable[tuple[str, str | None]]]) -> Msg:
    """A voice note becomes a text message; a failed transcription keeps the error and no text."""
    try:
        text, lang = await transcribe(path)
        text = (text or "").strip()
        if not text:
            return Msg(None, msg_id, kind="voice", error="empty transcription")
        return Msg(text, msg_id, kind="voice", lang=lang)
    except Exception as e:  # noqa: BLE001 - any STT failure turns into a polite request to type
        return Msg(None, msg_id, kind="voice", error=f"{type(e).__name__}: {str(e)[:120]}")


class Runner:
    def __init__(self, engine: Engine, transport: Transport, owner: OwnerChannel, runlog: RunLog, timers,
                 synthesize: Callable[[str], Awaitable[str | None]] | None = None, mirror: bool = True,
                 send_retry: float = 5.0):
        self.engine, self.transport, self.owner, self.log, self.timers = engine, transport, owner, runlog, timers
        self.send_retry = send_retry  # seconds before the one retry of a failed text send
        self.synthesize = synthesize
        self.mirror_on = mirror
        self.stopped = False
        self.sent: list[tuple[str, str]] = []

    async def feed(self, ev: Event) -> list:
        # the model call blocks (pacing, retries): in a worker thread the loop keeps typing, read-acks and the bot
        # polling alive. One consumer awaits each turn, so turns stay serial.
        acts = await asyncio.to_thread(self.engine.handle, ev)
        await self.execute(acts)
        return acts

    def stop_now(self, reason: str = "owner /stop") -> None:
        """/stop from the bot takes effect at once, even in the middle of a slow turn: nothing more is sent."""
        if not self.stopped:
            self.stopped = True
            self.log.record(Stop(reason))

    async def execute(self, acts: list) -> None:
        for a in acts:
            if isinstance(a, (SendText, SendVoice)):
                if self.stopped:
                    self.log.record(Log("note", {"text": f"stopped, #{a.n} not sent"}))
                    continue
                await self._send(a)
            elif isinstance(a, AskOwner):
                self.log.record(a)
                await self._safe(self.owner.ask(a.key, a.question, a.buttons, a.contact))
            elif isinstance(a, NotifyOwner):
                self.log.record(a)
                await self._safe(self.owner.notify(a.text))
            elif isinstance(a, Schedule):
                self.log.record(a)
                self.timers.schedule(a)
            elif isinstance(a, Stop):
                self.stopped = True
                self.log.record(a)
            elif isinstance(a, Log):
                self.log.record(a)
                if a.kind == "in" and self.mirror_on:
                    what = texts.t("mirror.voice" if a.data.get("kind") == "voice" else "mirror.manager")
                    await self._safe(self.owner.mirror(f"{what}: {a.data.get('text') or texts.t('mirror.no_text')}"))

    async def _send(self, a: SendText | SendVoice) -> None:
        via, msg_id, error = "text", None, None
        try:
            if isinstance(a, SendVoice) and self.synthesize:
                ogg = await self._safe(self.synthesize(a.text))
                if ogg:
                    try:
                        msg_id = await self.transport.send_voice(ogg, a.text)
                        via = "voice"
                    except Exception as e:  # noqa: BLE001 - voice forbidden, flood wait, upload error: send it as text
                        self.log.record(Log("note", {"text": f"voice note #{a.n} not sent "
                                                             f"({type(e).__name__}: {str(e)[:120]}), sending it as text"}))
            if via == "text":
                msg_id = await self._send_text(a)
        except Exception as e:  # noqa: BLE001 - logged and told to the owner, the run goes on
            error = f"{type(e).__name__}: {str(e)[:160]}"
        self.log.sent(a, via, msg_id, error)
        if error and a.restatement:
            # not delivered: the last delivered state stands, the owner gets the exact text to send by hand
            first_stands = bool(self.engine and self.engine.restatement_not_sent(a.text))
            state = "the first restatement stands" if first_stands else "the booking is not confirmed"
            self.log.record(Log("note", {"text": f"restatement #{a.n} not delivered: {state}; the owner got the text "
                                                 "to send by hand, the agent sends it again on the manager's next "
                                                 "message"}))
            await self._safe(self.owner.notify(texts.t("send_by_hand_fix" if first_stands else "send_by_hand",
                                                       n=a.n, error=error, text=a.text)))
        elif error:
            await self._safe(self.owner.notify(texts.t("send_failed", n=a.n, error=error)))
        if not error:
            self.sent.append((via, a.text))
            if self.mirror_on:
                await self._safe(self.owner.mirror(texts.t(
                    "mirror.agent", n=a.n, voice=texts.t("mirror.voice_tag") if via == "voice" else "", text=a.text)))
            if a.restatement:
                await self._safe(self.owner.notify(texts.t("booked", text=a.text)))

    async def _send_text(self, a: SendText | SendVoice) -> int | None:
        """One retry after a few seconds: a flood wait or a network drop is often gone by then."""
        try:
            return await self.transport.send_text(a.text)
        except Exception as e:  # noqa: BLE001
            self.log.record(Log("note", {"text": f"message #{a.n} not sent ({type(e).__name__}: {str(e)[:120]}), "
                                                 f"one retry in {self.send_retry:.0f} s"}))
        await asyncio.sleep(self.send_retry)
        return await self.transport.send_text(a.text)

    async def _safe(self, coro):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001 - the owner channel must never break the conversation
            self.log.event("owner_channel_error", {"error": f"{type(e).__name__}: {str(e)[:160]}"})
            return None
