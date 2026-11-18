"""The final restatement. Built from confirmed facts only; the model never writes it."""
from __future__ import annotations

import re

from agent import clock
from agent.facts import FactStore

REQUIRED = ("time", "date", "party_size", "restaurant_name")
NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}

OPENERS = (
    "Lovely, thank you. Just to confirm:",
    "Wonderful, thank you very much. So that's",
    "Perfect, thank you. To confirm, that's",
)
CORRECTION_OPENER = "Thank you for the correction. So that's now"
ASK_RESTAURANT = "Thank you. Could I just take the restaurant's name for my note?"


def missing(facts: FactStore) -> list[str]:
    return [k for k in REQUIRED if not facts.is_confirmed(k)]


def booking_line(facts: FactStore, with_restaurant: bool = True) -> str:
    n = int(facts.get("party_size"))
    people = NUMBER_WORDS.get(n, str(n))
    line = f"a table for {people} at {facts.get('time')} on {clock.human_date(facts.booking_date())}"
    if with_restaurant and facts.get("restaurant_name"):
        line += f" at {facts.get('restaurant_name')}"
    return line


def build(facts: FactStore, variant: int = 0, correction: bool = False) -> str | None:
    """None while any of time, date, party size or restaurant is not confirmed."""
    if missing(facts):
        return None
    opener = CORRECTION_OPENER if correction else OPENERS[variant % len(OPENERS)]
    text = f"{opener} {booking_line(facts)}"
    if facts.get("guest_name"):
        text += f", under the name {facts.get('guest_name')}"
    text += "."
    if facts.get("condition"):
        text += f" Noted: {str(facts.get('condition')).rstrip('.')}."
    return text


def signature(facts: FactStore) -> tuple:
    """Values a restatement carries; a change after CONFIRMED means a corrected restatement is due."""
    return tuple(facts.get(k) for k in REQUIRED + ("guest_name", "condition"))


def verify(text: str, facts: FactStore) -> list[str]:
    """Every required value must appear in the text exactly as it is in the facts."""
    problems = []
    n = int(facts.get("party_size") or 0)
    checks = {
        "time": str(facts.get("time")),
        "date": clock.human_date(facts.booking_date()),
        "party_size": f"for {NUMBER_WORDS.get(n, str(n))}",
        "restaurant_name": str(facts.get("restaurant_name")),
    }
    for k, v in checks.items():
        if not facts.is_confirmed(k):
            problems.append(f"{k} not confirmed")
        elif v not in text:
            problems.append(f"{k} {v!r} missing from the restatement")
    for t in re.findall(r"\b\d{1,2}:\d{2}\b", text):
        if t != facts.get("time"):
            problems.append(f"time {t} differs from the facts")
    return problems
