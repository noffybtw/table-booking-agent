"""--mode preflight: the Telegram path end to end, without writing to anyone.

The agent's own session (under the same lock), get_me must be the agent account, the test peer and the live
manager are resolved by username only, a short English voice note goes through the production TTS, is sent to
the agent's own Saved Messages ("me") and nowhere else, comes back by download and goes through the production
STT. The owner bot is checked with getMe only. Exit 0 only when every check passes.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from agent import config
from agent.config import SafetyError

SAVED = "me"
VOICE_TEXT = "Good morning. This is a short check of the booking agent's voice, sent to its own saved messages."
STEP_TIMEOUT = 60.0


class SavedMessagesOnly:
    """The only Telegram client preflight gets. It can send to "me" and to nobody else; it has no other send."""

    def __init__(self, client):
        self._client = client

    async def connect(self) -> None:
        await self._client.connect()

    async def is_user_authorized(self) -> bool:
        return await self._client.is_user_authorized()

    async def get_me(self):
        return await self._client.get_me()

    async def resolve(self, username: str):
        return await self._client.get_entity(username)

    async def send_file(self, target, path: str, **kw):
        if not isinstance(target, str) or target != SAVED:
            raise SafetyError(f"preflight sends only to {SAVED!r} (Saved Messages), not {target!r}")
        return await self._client.send_file(SAVED, path, **kw)

    async def download(self, message, path: str):
        return await self._client.download_media(message, file=path)

    async def disconnect(self) -> None:
        await self._client.disconnect()


def overlap(said: str, heard: str) -> float:
    want = set(re.findall(r"[a-z']+", said.lower()))
    got = set(re.findall(r"[a-z']+", (heard or "").lower()))
    return len(want & got) / len(want) if want else 0.0


async def run(tg: SavedMessagesOnly, voice, bot, run_dir: Path, scrub=lambda s: s) -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok_all = True

    def put(name: str, ok: bool | None, detail: str = "") -> None:
        nonlocal ok_all
        mark = "ok" if ok else ("skipped" if ok is None else "FAIL")
        ok_all = ok_all and bool(ok)
        line = scrub(f"{mark:7} {name}{': ' + detail if detail else ''}")
        lines.append(line)
        print(line, flush=True)

    async def step(coro):
        return await asyncio.wait_for(coro, STEP_TIMEOUT)

    def err(e: Exception) -> str:
        return f"{type(e).__name__}: {str(e)[:160]}"

    account_ok = False
    try:
        await step(tg.connect())
        authorised = await step(tg.is_user_authorized())
        put("telethon session", authorised, "authorised" if authorised else "not authorised, nothing else tried")
        if authorised:
            me = await step(tg.get_me())
            account_ok = getattr(me, "id", None) == config.AGENT_ACCOUNT_ID
            put("agent account", account_ok, f"get_me id {getattr(me, 'id', None)} @{getattr(me, 'username', None)}"
                + ("" if account_ok else f", expected {config.AGENT_ACCOUNT_ID}"))
    except Exception as e:  # noqa: BLE001 - every check reports, none ends the preflight
        put("telethon session", False, err(e))

    for username in sorted(config.ALLOWED_TEST_PEERS) + [config.LIVE_MANAGER]:
        if not account_ok:
            put(f"resolve @{username}", None, "the account check did not pass")
            continue
        try:
            ent = await step(tg.resolve(username))
            got = config.norm_username(getattr(ent, "username", None))
            put(f"resolve @{username}", got == username, f"id {getattr(ent, 'id', None)}, @{got}, nothing sent")
        except Exception as e:  # noqa: BLE001
            put(f"resolve @{username}", False, err(e))

    ogg = None
    try:
        ogg = await step(voice.synthesize(VOICE_TEXT))
        made = bool(ogg) and Path(ogg).exists() and Path(ogg).stat().st_size > 0
        put("tts", made, f"{(voice.last or {}).get('tts')} -> {ogg}" if made else f"{(voice.last or {}).get('errors')}")
        ogg = ogg if made else None
    except Exception as e:  # noqa: BLE001
        put("tts", False, err(e))

    back = None
    if ogg and account_ok:
        try:
            sent = await step(tg.send_file(SAVED, ogg, voice_note=True))
            put("voice note to Saved Messages", True, f"message id {getattr(sent, 'id', None)}")
            back = str(Path(run_dir) / "raw" / "preflight-back.ogg")
            Path(back).parent.mkdir(parents=True, exist_ok=True)
            got = await step(tg.download(sent, back))
            back = str(got or back)
            downloaded = Path(back).exists() and Path(back).stat().st_size > 0
            put("download back", downloaded, back)
            back = back if downloaded else None
        except Exception as e:  # noqa: BLE001
            put("voice note to Saved Messages", False, err(e))
            back = None
    else:
        put("voice note to Saved Messages", None, "no voice file or the account check did not pass")

    if back:
        try:
            text, lang = await step(voice.transcribe(back))
            score = overlap(VOICE_TEXT, text)
            put("stt", score >= 0.6, f"{score:.0%} word overlap, lang {lang}, heard: {text!r}")
        except Exception as e:  # noqa: BLE001
            put("stt", False, err(e))
    else:
        put("stt", None, "nothing came back to transcribe")

    try:
        me = await step(bot.call("getMe"))
        is_bot = bool(me.get("is_bot")) if isinstance(me, dict) else False
        put("owner bot getMe (IPv4)", is_bot, f"@{me.get('username') if isinstance(me, dict) else '?'}, no message sent")
    except Exception as e:  # noqa: BLE001
        put("owner bot getMe (IPv4)", False, err(e))

    try:
        await tg.disconnect()
    except Exception:  # noqa: BLE001
        pass
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    verdict = "PREFLIGHT OK" if ok_all else "PREFLIGHT FAILED"
    (Path(run_dir) / "report.txt").write_text("\n".join(lines + [verdict]) + "\n")
    print(verdict, flush=True)
    return ok_all, lines
