"""What the agent knows, where each value came from, and whether the manager has confirmed it."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime

from agent import clock

SOURCES = ("task", "owner", "manager")

# shown to the model next to each value
DESCRIPTIONS = {
    "party_size": "people in the booking",
    "time": "booking time, London",
    "date": "booking date",
    "timezone": "timezone",
    "on_behalf_of": "the person you book for; never say this name to the manager",
    "guest_name": "name for the booking",
    "phone": "contact phone for the booking",
    "restaurant_name": "restaurant name, as the manager gave it",
    "offered_time": "time the manager offered instead",
    "condition": "condition the manager set",
    "owner_note": "private instruction from the person you book for",
    "reference": "booking reference the manager gave",
}


@dataclass
class Fact:
    key: str
    value: object
    source: str
    turn: int
    confirmed: bool = False


class FactStore:
    def __init__(self) -> None:
        self._facts: dict[str, Fact] = {}
        self.history: list[Fact] = []

    @classmethod
    def for_task(cls, now: datetime | None = None, on_behalf_of: str = "Yaroslav", party_size: int = 2) -> "FactStore":
        fs = cls()
        slot = clock.booking_slot(now)
        fs.set("party_size", party_size, "task", 0)
        fs.set("time", "17:00", "task", 0)
        fs.set("date", slot.date().isoformat(), "task", 0)
        fs.set("timezone", "Europe/London", "task", 0)
        fs.set("on_behalf_of", on_behalf_of, "task", 0)
        return fs

    def set(self, key: str, value, source: str, turn: int, confirmed: bool = False) -> Fact:
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        f = Fact(key, value, source, turn, confirmed)
        self._facts[key] = f
        self.history.append(copy.copy(f))
        return f

    def drop(self, key: str) -> None:
        self._facts.pop(key, None)

    def get(self, key: str, default=None):
        f = self._facts.get(key)
        return default if f is None or f.value in (None, "") else f.value

    def fact(self, key: str) -> Fact | None:
        return self._facts.get(key)

    def source(self, key: str) -> str | None:
        f = self._facts.get(key)
        return f.source if f else None

    def known(self) -> dict:
        return {k: f.value for k, f in self._facts.items() if f.value not in (None, "")}

    def confirm(self, key: str) -> None:
        if key in self._facts:
            self._facts[key].confirmed = True

    def unconfirm(self, key: str) -> None:
        if key in self._facts:
            self._facts[key].confirmed = False

    def is_confirmed(self, key: str) -> bool:
        f = self._facts.get(key)
        return bool(f and f.confirmed and f.value not in (None, ""))

    def booking_date(self) -> date:
        return date.fromisoformat(self.get("date"))

    def copy(self) -> "FactStore":
        return copy.deepcopy(self)

    def as_prompt(self) -> str:
        lines = []
        for k, f in self._facts.items():
            if f.value in (None, ""):
                continue
            v = f.value
            if k == "date":
                d = self.booking_date()
                v = f"{clock.human_date(d)} {d.year} (today in London)"
            note = DESCRIPTIONS.get(k, k.replace("_", " "))
            conf = ", confirmed by the manager" if f.confirmed else ""
            lines.append(f"- {k}: {v}  ({note}; from {f.source}{conf})")
        missing = [k for k in ("guest_name", "phone") if k not in self.known()]
        if missing:
            lines.append(f"- {' and '.join(missing)}: not known; ask the owner only if the manager asks for it")
        if "restaurant_name" not in self.known():
            lines.append("- restaurant_name: not known yet (the manager may say it)")
        return "\n".join(lines)
