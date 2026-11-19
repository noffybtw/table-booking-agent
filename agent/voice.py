"""Voice notes. In: Groq whisper-large-v3. Out: edge-tts en-US-BrianMultilingualNeural, Groq Orpheus as the fallback, then text only.

Telegram plays a voice note when it is OGG/Opus, so every synthesis ends in `ffmpeg -c:a libopus`.
Orpheus (console.groq.com/docs/text-to-speech/orpheus): WAV only, at most 200 characters per request,
English voices autumn, diana, hannah, austin, daniel, troy.
"""
from __future__ import annotations

import asyncio
import itertools
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

from openai import OpenAI

from agent.config import GROQ_BASE_URL

STT_MODEL = "whisper-large-v3"
EDGE_VOICE = "en-US-BrianMultilingualNeural"
ORPHEUS_MODEL = "canopylabs/orpheus-v1-english"
ORPHEUS_VOICE = "daniel"
ORPHEUS_FIRST = True   # Orpheus first, edge-tts as the fallback
ORPHEUS_MAX = 200


async def run_ffmpeg(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode(errors='ignore')[-200:]}")


async def to_ogg(src: Path, dst: Path) -> Path:
    await run_ffmpeg("-i", str(src), "-vn", "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "32k", str(dst))
    return dst


def chunks(text: str, limit: int = ORPHEUS_MAX) -> list[str]:
    """Sentences packed into pieces of at most `limit` characters."""
    out, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        while len(sent) > limit:
            cut = sent.rfind(" ", 0, limit) if " " in sent[:limit] else limit
            out.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if cur and len(cur) + 1 + len(sent) > limit:
            out.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        out.append(cur)
    return out


async def edge_save(text: str, voice: str, path: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text, voice).save(str(path))


class Voice:
    def __init__(self, api_key: str | None, out_dir: Path, http_client=None,
                 edge: Callable[[str, str, Path], Awaitable[None]] = edge_save, edge_voice: str = EDGE_VOICE,
                 orpheus_voice: str = ORPHEUS_VOICE, orpheus_first: bool = ORPHEUS_FIRST):
        self.client = (OpenAI(api_key=api_key, base_url=GROQ_BASE_URL, timeout=30, max_retries=1,
                              http_client=http_client) if api_key else None)
        self.dir = Path(out_dir)
        self.edge, self.edge_voice, self.orpheus_voice = edge, edge_voice, orpheus_voice
        self.orpheus_first = orpheus_first
        self.seq = itertools.count(1)
        self.last: dict = {}

    async def transcribe(self, ogg_path: str) -> tuple[str, str | None]:
        if not self.client:
            raise RuntimeError("no GROQ_API_KEY for speech-to-text")

        def call():
            with open(ogg_path, "rb") as f:
                return self.client.audio.transcriptions.create(model=STT_MODEL, file=f,
                                                               response_format="verbose_json")
        t0 = time.monotonic()
        r = await asyncio.to_thread(call)
        self.last = {"stt": STT_MODEL, "latency_s": round(time.monotonic() - t0, 2)}
        return (getattr(r, "text", "") or "").strip(), getattr(r, "language", None)

    async def synthesize(self, text: str) -> str | None:
        """OGG/Opus path, or None when both engines fail (the caller then sends text). Orpheus goes first when
        ORPHEUS_FIRST (it sounds more like a person; it needs the model's terms accepted in the Groq console, and
        until then it fails at once and edge-tts speaks)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        n = next(self.seq)
        errors = []
        for engine in (("orpheus", "edge") if self.orpheus_first else ("edge", "orpheus")):
            try:
                if engine == "edge":
                    mp3 = self.dir / f"out-{n}.mp3"
                    await self.edge(text, self.edge_voice, mp3)
                    ogg = await to_ogg(mp3, self.dir / f"out-{n}.ogg")
                    self.last = {"tts": f"edge-tts {self.edge_voice}", **({"errors": errors} if errors else {})}
                else:
                    ogg = await self._orpheus(text, n)
                    self.last = {"tts": f"{ORPHEUS_MODEL} {self.orpheus_voice}", "errors": errors}
                self.orpheus_first = engine == "orpheus"  # one voice for the whole run: the engine that spoke stays first
                return str(ogg)
            except Exception as e:  # noqa: BLE001 - any failure moves on to the other engine
                errors.append(f"{'edge-tts' if engine == 'edge' else 'orpheus'}: {type(e).__name__}: {str(e)[:100]}")
        self.last = {"tts": None, "errors": errors}
        return None

    async def _orpheus(self, text: str, n: int) -> Path:
        if not self.client:
            raise RuntimeError("no GROQ_API_KEY")
        parts = []
        for i, piece in enumerate(chunks(text)):
            wav = self.dir / f"out-{n}-{i}.wav"

            def call(piece=piece):
                return self.client.audio.speech.create(model=ORPHEUS_MODEL, voice=self.orpheus_voice, input=piece,
                                                       response_format="wav")
            r = await asyncio.to_thread(call)
            wav.write_bytes(r.read())
            parts.append(wav)
        if len(parts) == 1:
            return await to_ogg(parts[0], self.dir / f"out-{n}.ogg")
        lst = self.dir / f"out-{n}.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
        joined = self.dir / f"out-{n}-all.wav"
        await run_ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined))
        return await to_ogg(joined, self.dir / f"out-{n}.ogg")
