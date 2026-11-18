import asyncio
import json
import subprocess
from pathlib import Path

import httpx

from agent import voice
from agent.events import SendText, SendVoice
from agent.runlog import RunLog
from agent import clock


def tone(path: Path, fmt_args=()):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                    *fmt_args, str(path)], check=True)


def codec(path: str) -> str:
    return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                           "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()


def groq_mock(wav_bytes: bytes, calls: list):
    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path.endswith("/audio/speech"):
            body = json.loads(req.content)
            assert body["model"] == voice.ORPHEUS_MODEL and len(body["input"]) <= 200
            return httpx.Response(200, content=wav_bytes, headers={"content-type": "audio/wav"})
        if req.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": " Yes, 17:00 for two is fine. ", "language": "english"})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


async def edge_ok(text, voice_name, path):
    tone(path)


async def edge_down(text, voice_name, path):
    raise ConnectionError("edge-tts unreachable")


def test_edge_tts_to_ogg_opus(tmp_path):
    v = voice.Voice(None, tmp_path, edge=edge_ok)
    ogg = asyncio.run(v.synthesize("Lovely, thank you."))
    assert ogg.endswith(".ogg") and codec(ogg) == "opus"
    assert v.last["tts"].startswith("edge-tts")


def test_orpheus_fallback_with_long_text(tmp_path):
    wav = tmp_path / "t.wav"
    tone(wav)
    calls = []
    v = voice.Voice("k", tmp_path / "out", http_client=groq_mock(wav.read_bytes(), calls), edge=edge_down)
    text = "Lovely, thank you. " * 15  # over 200 characters: sent in pieces and joined
    ogg = asyncio.run(v.synthesize(text))
    assert ogg and codec(ogg) == "opus"
    assert len(calls) >= 2 and "orpheus" in v.last["tts"]


def test_both_down_gives_none(tmp_path):
    def handler(req):
        return httpx.Response(503, json={"error": {"message": "down"}})
    v = voice.Voice("k", tmp_path, http_client=httpx.Client(transport=httpx.MockTransport(handler)), edge=edge_down)
    assert asyncio.run(v.synthesize("Hello.")) is None
    assert len(v.last["errors"]) == 2


def test_chunks_respect_the_limit():
    text = "A table for two at 17:00 on Thursday 19 November at The Crown & Anchor, under the name Yaroslav. " * 4
    parts = voice.chunks(text)
    assert all(len(p) <= 200 for p in parts) and " ".join(parts).split() == text.split()


def test_transcribe(tmp_path):
    ogg = tmp_path / "in.ogg"
    tone(ogg, ["-c:a", "libopus"])
    v = voice.Voice("k", tmp_path, http_client=groq_mock(b"", []))
    assert asyncio.run(v.transcribe(str(ogg))) == ("Yes, 17:00 for two is fine.", "english")


def test_runner_sends_text_when_tts_fails(tmp_path):
    from agent.runner import Runner

    class T:
        def __init__(self):
            self.sent = []

        async def send_text(self, text):
            self.sent.append(("text", text))
            return 1

        async def send_voice(self, path, caption):
            self.sent.append(("voice", caption))
            return 2

    class O:
        async def ask(self, *a): pass
        async def notify(self, *a): pass
        async def mirror(self, *a): pass

    async def no_tts(text):
        return None

    t = T()
    log = RunLog(tmp_path, lambda: clock.parse_now("2026-11-19T08:00"))
    r = Runner(engine=None, transport=t, owner=O(), runlog=log, timers=None, synthesize=no_tts)
    asyncio.run(r.execute([SendVoice("Lovely.", "restatement", 3)]))
    assert t.sent == [("text", "Lovely.")]
    assert "OUT agent #3: " in (tmp_path / "decisions.md").read_text()
