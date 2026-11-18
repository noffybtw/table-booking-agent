"""runs/<run-id>/decisions.md (one line per message) and events.jsonl (everything). Phones masked, secrets scrubbed."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from agent import clock
from agent.events import AskOwner, Log, NotifyOwner, Schedule, SendText, SendVoice, Stop

PHONE_RE = re.compile(r"\+?\d[\d ()\-]{5,}\d")


def mask_phone(text: str) -> str:
    """+44 7700 900123 -> +44 77** ***123: first four and last three digits stay."""
    def one(m: re.Match) -> str:
        run = m.group(0)
        total = sum(c.isdigit() for c in run)
        if total < 9:
            return run
        out, i = [], 0
        for c in run:
            if c.isdigit():
                out.append(c if i < 4 or i >= total - 3 else "*")
                i += 1
            else:
                out.append(c)
        return "".join(out)
    return PHONE_RE.sub(one, text or "")


def short(text: str, n: int = 300) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 3] + "..."


class RunLog:
    def __init__(self, run_dir: Path, now: Callable[[], datetime], secrets: list[str] | None = None,
                 header: str = "", echo: Callable[[str], None] | None = None):
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.now = now
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 6]
        self.echo = echo
        self.decisions = self.dir / "decisions.md"
        self.events = self.dir / "events.jsonl"
        self.decisions.write_text(f"# Decision log\n\n{header}\n\n" if header else "# Decision log\n\n")
        self.events.write_text("")
        self._in: list[dict] = []

    def _clean(self, s: str) -> str:
        for v in self.secrets:
            s = s.replace(v, "[secret]")
        return mask_phone(s)

    def event(self, kind: str, data: dict) -> None:
        now = self.now()
        row = {"ts": clock.utc_iso(now), "london": clock.hhmm(now, seconds=True), "kind": kind}
        row |= {("msg_kind" if k == "kind" else k): v for k, v in data.items()}
        line = json.dumps(row, ensure_ascii=False, default=str)
        with self.events.open("a") as f:
            f.write(self._clean(line) + "\n")

    def line(self, text: str) -> None:
        text = self._clean(f"{clock.hhmm(self.now(), seconds=True)} {text}")
        with self.decisions.open("a") as f:
            f.write(f"- {text}\n")
        if self.echo:
            self.echo(text)

    def _flush_in(self, understood: str = "", decided: str = "") -> None:
        for i, row in enumerate(self._in):
            what = {"voice": "manager (voice)", "edit": "manager (edited)", "media": "manager (media)"}.get(
                row["kind"], "manager")
            text = f'"{short(row["text"])}"' if row["text"] else "(no text)"
            if row["kind"] == "edit" and row.get("old_text"):
                text += f' (was "{short(row["old_text"], 120)}")'
            tail = ""
            if i == len(self._in) - 1 and (understood or decided):
                tail = f" | understood: {short(understood, 200)} | decided: {short(decided, 300)}"
            self.line(f"IN {what}: {text}{tail}")
        self._in = []

    def record(self, act) -> None:
        """Engine output. Sends are logged by sent() once the transport has delivered them."""
        if isinstance(act, Log):
            self.event(act.kind, act.data)
            if act.kind == "in":
                self._in.append(act.data)
            elif act.kind == "decision":
                if self._in:
                    self._flush_in(act.data.get("understood", ""), act.data.get("decided", ""))
                else:
                    self.line(f"DECIDED: {short(act.data.get('decided', ''), 300)}")
            elif act.kind == "owner_question":
                self._flush_in()
                self.line(f'OWNER asked ({act.data["key"]}): "{short(act.data["question"], 200)}"')
            elif act.kind == "owner_answer":
                used = f', used in reply #{act.data["used_in"]}' if act.data.get("used_in") else ""
                self.line(f'OWNER answered {act.data["key"]}: "{short(act.data["text"], 200)}"{used}')
            elif act.kind == "note":
                self._flush_in()
                self.line(f"NOTE {act.data.get('text', '')}")
        elif isinstance(act, AskOwner):
            self.event("ask_owner", {"key": act.key, "question": act.question, "buttons": act.buttons,
                                     "contact": act.contact, "reminder": act.reminder})
            if act.reminder:
                self.line(f"OWNER reminded ({act.key})")
        elif isinstance(act, NotifyOwner):
            self.event("notify_owner", {"text": act.text})
        elif isinstance(act, Schedule):
            self.event("schedule", {"name": act.name, "delay": act.delay, "gen": act.gen, "key": act.key})
        elif isinstance(act, Stop):
            self._flush_in()
            self.event("stop", {"reason": act.reason})
            self.line(f"STOP {act.reason}")

    def sent(self, act: SendText | SendVoice, via: str, msg_id: int | None = None, error: str | None = None) -> None:
        self._flush_in()
        self.event("out", {"n": act.n, "text": act.text, "why": act.why, "via": via, "msg_id": msg_id,
                           "error": error})
        tag = " (voice note + caption)" if via == "voice" else ""
        fail = f" | NOT SENT: {error}" if error else ""
        self.line(f'OUT agent #{act.n}{tag}: "{short(act.text, 400)}" | why: {short(act.why, 300)}{fail}')

    def record_note(self, text: str) -> None:
        self.record(Log("note", {"text": text}))

    def close(self) -> None:
        self._flush_in()
