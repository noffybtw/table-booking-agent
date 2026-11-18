"""Telegram user account (Telethon). Talks to exactly one peer; every other chat on the account is left alone.

The account is shared with another tool, so the handlers are registered with chats=<peer> and checked again in
PeerFilter: other chats are never read, answered or marked as read, and history before the opening is ignored.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from agent.events import Event, Msg, manager_event
from agent.runner import voice_msg


class LockError(Exception):
    pass


class SessionLock:
    """One process per session file. flock is released by the kernel if the process dies."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise LockError(f"another process holds {self.path.name}; is an earlier run still going?")
        os.ftruncate(self.fd, 0)
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


@dataclass
class PeerFilter:
    peer_id: int
    started_at: datetime

    def accepts(self, chat_id: int | None, out: bool, date: datetime | None) -> bool:
        if chat_id != self.peer_id or out:
            return False
        if date is None:
            return True
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date >= self.started_at


class Debouncer:
    """Collects a burst of messages: flushes `quiet` seconds after the last one, at most `max_wait` after the first."""

    def __init__(self, on_batch: Callable[[list[Msg]], Awaitable[None]], quiet: float = 5.0, max_wait: float = 15.0):
        self.on_batch, self.quiet, self.max_wait = on_batch, quiet, max_wait
        self.items: list[Msg] = []
        self.first = 0.0
        self.task: asyncio.Task | None = None

    def add(self, msg: Msg) -> None:
        loop = asyncio.get_running_loop()
        if not self.items:
            self.first = loop.time()
        self.items.append(msg)
        if self.task:
            self.task.cancel()
        delay = min(self.quiet, max(0.0, self.first + self.max_wait - loop.time()))
        self.task = loop.create_task(self._later(delay))

    async def _later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.flush()

    async def flush(self) -> None:
        items, self.items, self.task = self.items, [], None
        if items:
            await self.on_batch(items)


def describe_media(msg) -> str:
    if getattr(msg, "sticker", None):
        alt = next((a.alt for a in getattr(msg.sticker, "attributes", []) if getattr(a, "alt", None)), "")
        return f"a sticker {alt}".strip()
    if getattr(msg, "photo", None):
        return "a photo"
    if getattr(msg, "gif", None):
        return "a GIF"
    if getattr(msg, "video_note", None):
        return "a round video"
    return "a file"


class TelegramTransport:
    def __init__(self, session: str, api_id: int, api_hash: str, peer_username: str, queue: asyncio.Queue,
                 raw_dir: Path, transcribe: Callable[[str], Awaitable[tuple[str, str | None]]],
                 typing_per_char: float = 0.03, typing_max: float = 4.0, quiet: float = 5.0, max_wait: float = 15.0):
        from telethon import TelegramClient  # imported here so offline tests never touch Telethon networking
        self.client = TelegramClient(session, api_id, api_hash)
        self.username = peer_username
        self.queue = queue
        self.raw_dir = Path(raw_dir)
        self.transcribe = transcribe
        self.typing_per_char, self.typing_max = typing_per_char, typing_max
        self.debouncer = Debouncer(self._batch, quiet, max_wait)
        self.peer = None
        self.filter: PeerFilter | None = None
        self.texts: dict[int, str] = {}

    async def start(self) -> str:
        """Connects with the existing session (never logs in interactively) and resolves the peer once."""
        from telethon import events
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("the session is not authorised; log in with the session tool first")
        self.peer = await self.client.get_entity(self.username)
        self.filter = PeerFilter(self.peer.id, datetime.now(timezone.utc))
        self.client.add_event_handler(self._on_new, events.NewMessage(chats=[self.peer.id], incoming=True))
        self.client.add_event_handler(self._on_edit, events.MessageEdited(chats=[self.peer.id], incoming=True))
        return getattr(self.peer, "username", None) or str(self.peer.id)

    async def stop(self) -> None:
        await self.debouncer.flush()
        await self.client.disconnect()

    async def _batch(self, items: list[Msg]) -> None:
        await self.queue.put(manager_event(items))

    async def _on_new(self, event) -> None:
        m = event.message
        if not self.filter or not self.filter.accepts(event.chat_id, m.out, m.date):
            return
        if m.voice:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            path = await m.download_media(file=str(self.raw_dir / f"in-{m.id}.ogg"))
            msg = await voice_msg(path, m.id, self.transcribe)
        elif m.raw_text:
            msg = Msg(m.raw_text, m.id)
        elif m.media:
            msg = Msg(describe_media(m), m.id, kind="media")
        else:
            return
        if msg.text and msg.kind != "media":
            self.texts[m.id] = msg.text
        await self._read(m)
        self.debouncer.add(msg)

    async def _on_edit(self, event) -> None:
        m = event.message
        if not self.filter or not self.filter.accepts(event.chat_id, m.out, m.date):
            return
        old = self.texts.get(m.id)
        if old is None or m.raw_text == old:
            return  # an edit of a message from before the run, or no change in text (reaction, preview)
        self.texts[m.id] = m.raw_text
        await self.debouncer.flush()
        await self.queue.put(Event("manager_edit", messages=[Msg(m.raw_text, m.id)], text=m.raw_text,
                                   old_text=old, msg_id=m.id))

    async def _read(self, m) -> None:
        try:
            await self.client.send_read_acknowledge(self.peer, m)
        except Exception:  # noqa: BLE001 - read receipts are cosmetic
            pass

    async def _typing(self, text: str, action: str = "typing") -> None:
        delay = min(self.typing_max, len(text) * self.typing_per_char)
        async with self.client.action(self.peer, action):
            await asyncio.sleep(delay)

    async def send_text(self, text: str) -> int:
        await self._typing(text)
        sent = await self.client.send_message(self.peer, text, link_preview=False)
        return sent.id

    async def send_voice(self, ogg_path: str, caption: str) -> int:
        await self._typing(caption, "record-audio")
        sent = await self.client.send_file(self.peer, ogg_path, caption=caption, voice_note=True)
        return sent.id
