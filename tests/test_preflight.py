"""Preflight with fakes: it sends to the agent's own Saved Messages and to nobody else. No Telethon, no network."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import run
from agent import config, preflight
from agent.config import SafetyError


class FakeClient:
    def __init__(self, me_id=config.AGENT_ACCOUNT_ID):
        self.me_id, self.sent, self.calls = me_id, [], []

    async def connect(self):
        self.calls.append("connect")

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=self.me_id, username="alexithymia0x")

    async def get_entity(self, username):
        self.calls.append(f"get_entity {username}")
        return SimpleNamespace(id=hash(username) % 1000, username=username)

    async def send_file(self, target, path, **kw):
        self.sent.append(target)
        return SimpleNamespace(id=55, path=path)

    async def send_message(self, *a, **kw):  # never reachable through the wrapper
        raise AssertionError("send_message must never be called")

    async def download_media(self, message, file=None):
        Path(file).write_bytes(Path(message.path).read_bytes())
        return file

    async def disconnect(self):
        self.calls.append("disconnect")


class FakeVoice:
    def __init__(self, tmp, heard=preflight.VOICE_TEXT):
        self.tmp, self.heard, self.last = tmp, heard, {}

    async def synthesize(self, text):
        p = self.tmp / "out-1.ogg"
        p.write_bytes(b"OggS fake")
        self.last = {"tts": "edge-tts en-GB-RyanNeural"}
        return str(p)

    async def transcribe(self, path):
        return self.heard, "english"


class FakeBot:
    def __init__(self):
        self.methods = []

    async def call(self, method, **params):
        self.methods.append(method)
        return {"is_bot": True, "username": "yk_booking_console_bot"}


def test_the_wrapper_refuses_every_target_but_me():
    c = FakeClient()
    w = preflight.SavedMessagesOnly(c)
    for target in ("noffybtw", "@vlad_dev_7", "vlad_dev_7", "Me", SimpleNamespace(id=1), 7357586975, None):
        with pytest.raises(SafetyError):
            asyncio.run(w.send_file(target, "x.ogg", voice_note=True))
    assert c.sent == []
    assert not hasattr(w, "send_message")
    asyncio.run(w.send_file("me", __file__))
    assert c.sent == ["me"]


def test_preflight_passes_and_sends_only_to_saved_messages(tmp_path):
    c, bot = FakeClient(), FakeBot()
    ok, lines = asyncio.run(preflight.run(preflight.SavedMessagesOnly(c), FakeVoice(tmp_path), bot, tmp_path / "pf"))
    assert ok, lines
    assert c.sent == ["me"] and bot.methods == ["getMe"]
    assert "get_entity noffybtw" in c.calls and "get_entity vlad_dev_7" in c.calls
    report = (tmp_path / "pf" / "report.txt").read_text()
    assert report.rstrip().endswith("PREFLIGHT OK") and "100% word overlap" in report


def test_wrong_account_sends_nothing_and_fails(tmp_path):
    c = FakeClient(me_id=42)
    ok, lines = asyncio.run(preflight.run(preflight.SavedMessagesOnly(c), FakeVoice(tmp_path), FakeBot(), tmp_path / "pf"))
    assert not ok and c.sent == [] and not any(x.startswith("get_entity") for x in c.calls)
    assert (tmp_path / "pf" / "report.txt").read_text().rstrip().endswith("PREFLIGHT FAILED")


def test_bad_transcription_fails(tmp_path):
    ok, lines = asyncio.run(preflight.run(preflight.SavedMessagesOnly(FakeClient()),
                                          FakeVoice(tmp_path, heard="Thank you for watching."), FakeBot(), tmp_path / "pf"))
    assert not ok and any(line.startswith("FAIL    stt") for line in lines)


def test_preflight_is_a_mode():
    assert run.parse_args(["--mode", "preflight"]).mode == "preflight"
