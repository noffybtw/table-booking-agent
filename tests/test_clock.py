from datetime import datetime
from zoneinfo import ZoneInfo

from agent import clock

PRAGUE = ZoneInfo("Europe/Prague")


def test_17_london_is_18_prague_on_19_november():
    slot = clock.booking_slot(datetime(2026, 11, 19, 9, 0, tzinfo=PRAGUE))
    assert slot.astimezone(PRAGUE).strftime("%Y-%m-%d %H:%M") == "2026-11-19 18:00"
    assert clock.hhmm(slot) == "17:00"


def test_london_date_when_prague_is_past_midnight():
    # 00:30 in Prague on the 20th is still 23:30 on the 19th in London
    now = datetime(2026, 11, 20, 0, 30, tzinfo=PRAGUE)
    assert clock.london_now(now).date().isoformat() == "2026-11-19"
    assert clock.booking_slot(now).date().isoformat() == "2026-11-19"


def test_human_date_and_greeting():
    assert clock.human_date(datetime(2026, 11, 19, 8, tzinfo=clock.LONDON)) == "Thursday 19 November"
    assert clock.greeting(clock.parse_now("2026-11-19T08:00")) == "Good morning"
    assert clock.greeting(clock.parse_now("2026-11-19T13:00")) == "Good afternoon"
    assert clock.greeting(clock.parse_now("2026-11-19T19:00")) == "Good evening"


def test_naive_now_is_london():
    assert clock.parse_now("2026-11-19T08:00").utcoffset().total_seconds() == 0
