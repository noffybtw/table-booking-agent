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
