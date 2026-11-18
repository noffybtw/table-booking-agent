"""London time. Everything that says "today" or "17:00" goes through here, so tests can fix the clock."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
BOOKING_TIME = time(17, 0)


def london_now(now: datetime | None = None) -> datetime:
    """now in London time. A naive `now` is read as London local time."""
    if now is None:
        return datetime.now(LONDON)
    if now.tzinfo is None:
        return now.replace(tzinfo=LONDON)
    return now.astimezone(LONDON)


def parse_now(iso: str) -> datetime:
    """--now value: ISO with or without offset; without one it is London time."""
    return london_now(datetime.fromisoformat(iso))


def booking_slot(now: datetime | None = None) -> datetime:
    """17:00 on the London calendar date of `now`."""
    d = london_now(now).date()
    return datetime.combine(d, BOOKING_TIME, tzinfo=LONDON)


def greeting(now: datetime | None = None) -> str:
    h = london_now(now).hour
    if h < 12:
        return "Good morning"
    if h < 18:
        return "Good afternoon"
    return "Good evening"


def human_date(d: date | datetime) -> str:
    """Thursday 19 November."""
    if isinstance(d, datetime):
        d = london_now(d).date()
    return f"{d:%A} {d.day} {d:%B}"


def hhmm(dt: datetime, seconds: bool = False) -> str:
    return london_now(dt).strftime("%H:%M:%S" if seconds else "%H:%M")


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
