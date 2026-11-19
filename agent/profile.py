"""The owner's profile (--profile <path>, off by default): what the owner gives before the start.

A JSON file outside the repo with any of: guest_name (Latin letters), phone, accept_from and accept_to (HH:MM,
London, today). With it the agent answers the manager's request for the name or phone at once, and takes another
time inside [accept_from, accept_to] without asking. Anything the profile does not hold is asked as without it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from agent import texts
from agent.config import ROOT, SafetyError
from agent.runlog import mask_phone

KEYS = ("guest_name", "phone", "accept_from", "accept_to")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z '.-]{1,40}$")


@dataclass(frozen=True)
class Profile:
    guest_name: str | None = None
    phone: str | None = None
    accept_from: str | None = None
    accept_to: str | None = None

    def get(self, key: str) -> str | None:
        return getattr(self, key, None) if key in ("guest_name", "phone") else None

    @property
    def window(self) -> str | None:
        return f"{self.accept_from}-{self.accept_to}" if self.accept_from else None

    def inside(self, hhmm: str) -> bool:
        return bool(self.accept_from) and self.accept_from <= hhmm <= self.accept_to

    def summary(self) -> str:
        """English, phone masked: for decisions.md and stdout."""
        parts = [f"name {self.guest_name}" if self.guest_name else "",
                 f"phone {mask_phone(self.phone)}" if self.phone else "",
                 f"other time accepted {self.window}" if self.window else ""]
        return ", ".join(p for p in parts if p) or "empty"

    def owner_text(self) -> str:
        """For the owner bot start message, in OWNER_LANG, phone masked."""
        parts = [texts.t("profile.name", name=self.guest_name) if self.guest_name else "",
                 texts.t("profile.phone", phone=mask_phone(self.phone)) if self.phone else "",
                 texts.t("profile.window", start=self.accept_from, end=self.accept_to) if self.window else ""]
        return texts.t("start_profile", items="; ".join(p for p in parts if p))


def from_dict(data: dict) -> Profile:
    if not isinstance(data, dict):
        raise SafetyError("profile: the file must hold one JSON object")
    unknown = sorted(set(data) - set(KEYS))
    if unknown:
        raise SafetyError(f"profile: unknown key(s) {', '.join(unknown)}; known: {', '.join(KEYS)}")
    v = {k: str(data[k]).strip() for k in KEYS if data.get(k) not in (None, "")}
    if "guest_name" in v and not NAME_RE.match(v["guest_name"]):
        raise SafetyError("profile: guest_name must be in Latin letters, as the manager should hear it")
    if "phone" in v and len(re.sub(r"\D", "", v["phone"])) < 7:
        raise SafetyError("profile: phone must have at least 7 digits")
    if ("accept_from" in v) != ("accept_to" in v):
        raise SafetyError("profile: accept_from and accept_to go together")
    for k in ("accept_from", "accept_to"):
        if k in v and not HHMM_RE.match(v[k]):
            raise SafetyError(f"profile: {k} must be HH:MM (London, today)")
    if "accept_from" in v and v["accept_from"] > v["accept_to"]:
        raise SafetyError("profile: accept_from is after accept_to")
    return Profile(**v)


def load(path: str) -> Profile:
    p = Path(path).expanduser().resolve()
    if p.is_relative_to(ROOT):
        raise SafetyError("profile: keep the file outside the repo, so it is never committed")
    try:
        data = json.loads(p.read_text())
    except OSError as e:
        raise SafetyError(f"profile: cannot read the file ({type(e).__name__})") from None
    except ValueError:
        raise SafetyError("profile: the file is not valid JSON") from None
    return from_dict(data)
