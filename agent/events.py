"""What goes into the engine (events) and what comes out (actions)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Msg:
    """One manager message. Voice notes arrive already transcribed; media carries a short description."""
    text: str | None
    msg_id: int | None = None
    kind: str = "text"  # text | voice | media
    lang: str | None = None
    error: str | None = None


@dataclass
class Event:
    kind: str  # opening | manager_message | manager_voice | manager_media | manager_edit | owner_answer | owner_command | timer
    messages: list[Msg] = field(default_factory=list)
    text: str = ""
    old_text: str = ""
    msg_id: int | None = None
    key: str | None = None
    value: str | None = None
    gen: int | None = None
    contact: bool = False


def manager_event(messages: list[Msg]) -> Event:
    kinds = {m.kind for m in messages}
    kind = "manager_voice" if "voice" in kinds else "manager_media" if kinds == {"media"} else "manager_message"
    return Event(kind, messages=list(messages))


@dataclass
class SendText:
    text: str
    why: str
    n: int = 0


@dataclass
class SendVoice:
    """Voice note with the same text as the caption; the runner falls back to text if TTS fails."""
    text: str
    why: str
    n: int = 0


@dataclass
class AskOwner:
    key: str
    question: str
    buttons: list[tuple[str, str]] = field(default_factory=list)
    contact: bool = False
    reminder: bool = False


@dataclass
class NotifyOwner:
    text: str


@dataclass
class Schedule:
    """Fire Event(kind="timer", text=name, key=key, gen=gen) after `delay` seconds; a stale gen is ignored."""
    name: str
    delay: float
    gen: int
    key: str | None = None


@dataclass
class Log:
    kind: str  # in | owner_answer | guard | note | event
    data: dict


@dataclass
class Stop:
    reason: str


Action = SendText | SendVoice | AskOwner | NotifyOwner | Schedule | Log | Stop
