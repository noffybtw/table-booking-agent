"""One failure in a turn, a model reply or a voice download must not end the run or drop a message."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

import run
from agent import clock
from agent.brain import BrainDown, GroqBrain
from agent.events import Event, Msg
from agent.facts import FactStore
from agent.runlog import RunLog
from agent.transport_tg import PeerFilter, TelegramTransport


class Owner:
    def __init__(self):
        self.notes = []

    async def notify(self, text):
        self.notes.append(text)


def test_an_error_in_a_turn_is_logged_and_the_run_goes_on(tmp_path):
    from agent.runner import Runner

    class Boom:
        def handle(self, ev):
            raise KeyError("slots")

    owner = Owner()
    log = RunLog(tmp_path, lambda: clock.parse_now("2026-11-19T08:00"))
    runner = Runner(Boom(), transport=None, owner=owner, runlog=log, timers=None)
    asyncio.run(run.feed_safely(runner, log, owner, Event("manager_message", text="hi")))
    assert len(owner.notes) == 1 and "KeyError" in owner.notes[0]
    assert "error in the manager_message turn (KeyError" in (tmp_path / "decisions.md").read_text()


def test_a_reply_without_choices_is_a_failed_try_not_a_crash():
    def handler(request):
        body = {"id": "x", "object": "chat.completion", "created": 0, "model": "m", "choices": []}
        return httpx.Response(200, json=body)

    b = GroqBrain("k", http_client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    with pytest.raises(BrainDown) as e:
        b.decide(FactStore.for_task(clock.parse_now("2026-11-19T08:00")), [], "open")
    assert "invalid JSON" in str(e.value)


def test_a_failed_voice_download_goes_on_as_an_unreadable_voice(tmp_path):
    class Client:
        async def send_read_acknowledge(self, peer, m):
            pass

    async def download_media(file=None):
        raise ConnectionError("file part missing")

    async def transcribe(path):
        raise AssertionError("no transcription without a file")

    got = []
    t = TelegramTransport.__new__(TelegramTransport)  # no Telethon client is built or connected
    t.client, t.peer, t.raw_dir, t.transcribe, t.texts = Client(), "peer", tmp_path, transcribe, {}
    t.filter = PeerFilter(111, datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc))
    t.debouncer = SimpleNamespace(add=got.append)
    m = SimpleNamespace(out=False, date=datetime(2026, 11, 19, 8, 1, tzinfo=timezone.utc), id=7, voice=True,
                        raw_text="", media=True, download_media=download_media)
    asyncio.run(t._on_new(SimpleNamespace(message=m, chat_id=111)))
    assert len(got) == 1 and got[0].kind == "voice" and got[0].text is None and "download failed" in got[0].error


def test_engine_answers_a_failed_download_with_the_bad_voice_line():
    from agent.brain import FakeBrain
    from agent.engine import BAD_VOICE, Engine
    from agent.events import SendText, manager_event
    now = clock.parse_now("2026-11-19T08:00")
    e = Engine(FakeBrain(), FactStore.for_task(now), now=lambda: now)
    acts = e.handle(manager_event([Msg(None, 7, kind="voice", error="download failed: ConnectionError")]))
    assert [a.text for a in acts if isinstance(a, SendText)] == [BAD_VOICE]


def test_looking_forward_goes_out():
    from agent.brain import D
    from agent.events import SendText
    from test_owner_answers import opened
    e = opened(D("Looking forward to it, thank you!", intent="off_topic"))
    acts = e.handle(Event("manager_message", text="Lovely to hear from you. Any plans for the weekend?"))
    assert [a.text for a in acts if isinstance(a, SendText)] == ["Looking forward to it, thank you!"]


def test_every_draft_blocked_sends_hold_asks_the_owner_and_arms_the_follow_up():
    from agent.brain import D
    from agent.engine import HOLD
    from agent.events import AskOwner, Schedule, SendText
    from test_owner_answers import opened
    bad = D("Bob from the front desk sends his regards!", intent="off_topic")
    e = opened(bad, bad, bad)
    acts = e.handle(Event("manager_message", text="Who did you speak to last time?"))
    assert [a.text for a in acts if isinstance(a, SendText)] == [HOLD]
    assert [a.key for a in acts if isinstance(a, AskOwner)] == ["reply_help"] and "reply_help" in e.pending
    timers = {(a.name, a.key) for a in acts if isinstance(a, Schedule)}
    assert {("owner_reping", "reply_help"), ("owner_wait", "reply_help")} <= timers
    g = e.gen["owner:reply_help"]
    later = e.handle(Event("timer", text="owner_wait", key="reply_help", gen=g))
    assert [a.text for a in later if isinstance(a, SendText)] == ["Sorry for the wait, I'm still confirming that detail."]


def test_a_slow_model_turn_leaves_the_loop_free_and_stop_works_at_once(tmp_path):
    import time
    from agent.brain import D, FakeBrain
    from agent.engine import Engine
    from agent.owner_bot import OwnerBot
    from agent.runner import Runner

    class T:
        def __init__(self):
            self.sent = []

        async def send_text(self, text):
            self.sent.append(text)
            return 1

    class O:
        async def ask(self, *a): pass
        async def notify(self, *a): pass
        async def mirror(self, *a): pass

    def slow(facts, transcript, event):
        time.sleep(0.5)  # a paced or retried Groq call
        return D("Lovely, thank you.")

    now = clock.parse_now("2026-11-19T08:00")
    log = RunLog(tmp_path, lambda: now)
    engine = Engine(FakeBrain([slow]), FactStore.for_task(now), now=lambda: now)
    t = T()
    runner = Runner(engine, t, O(), log, timers=SimpleNamespace(schedule=lambda s: None))
    bot = OwnerBot("1:x", 5550001, asyncio.Queue(),
                   http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "result": True}))))
    bot.on_stop = runner.stop_now

    async def go():
        typing = 0
        turn = asyncio.create_task(runner.feed(Event("manager_message", text="Hello?")))
        for i in range(20):  # the typing indicator loop of the transport
            await asyncio.sleep(0.02)
            typing += 1
            if i == 5:
                await bot.handle_update({"update_id": 1, "message": {"chat": {"id": 5550001}, "text": "/stop"}})
        await turn
        await bot.close()
        return typing

    ticks = asyncio.run(go())
    assert ticks == 20 and runner.stopped
    assert t.sent == []  # the slow turn finished after /stop: its reply was dropped
    log_text = (tmp_path / "decisions.md").read_text()
    assert "STOP owner /stop" in log_text and "stopped, #1 not sent" in log_text


class FlakyTransport:
    """Voice always fails; text fails the first `fails` times."""
    def __init__(self, fails):
        self.fails, self.sent = fails, []

    async def send_voice(self, path, caption):
        raise ConnectionError("upload dropped")

    async def send_text(self, text):
        if self.fails:
            self.fails -= 1
            raise ConnectionError("network drop")
        self.sent.append(text)
        return 7


class BotOwner(Owner):
    async def ask(self, *a): pass
    async def mirror(self, *a): pass


def restated_engine(tmp_path, fails):
    from agent.brain import D, FakeBrain
    from agent.engine import Engine
    from agent.runner import Runner

    now = clock.parse_now("2026-11-19T08:00")
    engine = Engine(FakeBrain([D("Good morning. I'm an assistant - could I book a table for two today at 17:00, please?",
                                 intent="greeting"),
                               D(action="final_restatement", intent="agreed", manager_agreed=True,
                                 restaurant_name="The Ivy")]), FactStore.for_task(now), now=lambda: now)
    t, owner = FlakyTransport(fails), BotOwner()

    async def tts(text):
        return str(tmp_path / "r.ogg")

    runner = Runner(engine, t, owner, RunLog(tmp_path, lambda: now), SimpleNamespace(schedule=lambda s: None),
                    synthesize=tts, send_retry=0)

    async def go():
        await runner.feed(Event("opening"))
        await runner.feed(Event("manager_message", text="Yes, 17:00 for two is fine at The Ivy."))
    asyncio.run(go())
    return engine, t, owner


def test_a_failed_text_send_is_retried_once(tmp_path):
    engine, t, owner = restated_engine(tmp_path, fails=1)
    assert len(t.sent) == 2 and "at The Ivy" in t.sent[1]
    assert engine.phase == "confirmed" and engine.n_restated == 1
    assert any(n.startswith("Бронь підтверджена") for n in owner.notes)
    assert "one retry in 0 s" in (tmp_path / "decisions.md").read_text()


def test_a_restatement_that_never_went_out_is_not_confirmed_and_the_owner_gets_the_text(tmp_path):
    from agent.brain import D

    engine, t, owner = restated_engine(tmp_path, fails=4)  # the opening's two tries and the restatement's two
    assert t.sent == []
    assert engine.phase != "confirmed" and engine.n_restated == 0 and engine.restated_sig is None
    assert not any(n.startswith("Бронь підтверджена") for n in owner.notes)
    hand = [n for n in owner.notes if "вручну" in n]
    assert len(hand) == 1 and hand[0].endswith("a table for two at 17:00 on Thursday 19 November at The Ivy.")
    assert "бронь не підтверджена" in hand[0] and "наступне повідомлення менеджера" in hand[0]
    assert not any(line.startswith("AGENT:") and "at The Ivy" in line for line in engine.transcript)
    log_text = (tmp_path / "decisions.md").read_text()
    assert "NOT SENT" in log_text and "restatement #2 not delivered" in log_text
    # the manager's next message gets the restatement again, not only a closing line
    engine.brain.push(D("Thank you!", intent="question"))
    acts = engine.handle(Event("manager_message", text="Hello? Did you get that?"))
    assert any(getattr(a, "restatement", False) for a in acts)


def test_a_failed_corrected_restatement_tells_the_owner_the_first_one_stands(tmp_path):
    from agent.brain import D
    from agent.runner import Runner

    engine, t, owner = restated_engine(tmp_path, fails=0)
    assert engine.n_restated == 1 and len(t.sent) == 2
    t.fails = 2  # the corrected one fails, its retry too
    engine.brain.push(D("Thank you!", intent="question", restaurant_name="The Wolseley"))
    runner = Runner(engine, t, owner, RunLog(tmp_path, lambda: clock.parse_now("2026-11-19T08:00")),
                    SimpleNamespace(schedule=lambda s: None), send_retry=0)
    asyncio.run(runner.feed(Event("manager_message", text="Sorry, we are The Wolseley, not The Ivy.")))
    hand = [n for n in owner.notes if "вручну" in n]
    assert len(hand) == 1 and hand[0].startswith("Виправлене підтвердження") and "залишається в силі" in hand[0]
    assert "бронь не підтверджена" not in hand[0] and "at The Wolseley" in hand[0]
    assert engine.phase == "confirmed" and engine.n_restated == 1
