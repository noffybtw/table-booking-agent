import pytest

from agent import clock, engine, guard, restate
from agent.facts import FactStore


@pytest.fixture
def facts():
    return FactStore.for_task(clock.parse_now("2026-11-19T08:00"))


def rules(text, facts, chat=()):
    return {p.rule for p in guard.check(guard.fix(text), facts, list(chat))}


def test_fix_dashes_quotes_emoji():
    assert guard.fix("Lovely — thank you – see you") == "Lovely - thank you - see you"
    assert guard.fix("“That’s fine” \U0001F60A") == "\"That's fine\""


def test_clean_opening_passes(facts):
    assert rules("Good morning. I'm writing on behalf of Yaroslav - could I book a table for two today at 17:00, "
                 "please?", facts) == set()


def test_real_slip_watched_the_match(facts):
    assert "experience" in rules("I did catch it, quite the game.", facts)
    assert rules("Ha, I'll leave the football to the experts.", facts) == set()


@pytest.mark.parametrize("text", ["Call me on +44 7700 900456.", "The number is 07700 900456."])
def test_unknown_phone_blocked(facts, text):
    assert "phone" in rules(text, facts)


def test_known_phone_passes(facts):
    facts.set("phone", "+44 7700 900123", "owner", 1)
    assert "phone" not in rules("The number is +44 7700 900123.", facts)


def test_email_and_url(facts):
    assert "contact" in rules("You can email yaroslav@example.com.", facts)
    assert "contact" in rules("See www.example.com for details.", facts)
    assert "contact" not in rules("You can email book@crown.co.uk.", facts, ["Email us at book@crown.co.uk"])


def test_time_only_17_or_what_the_manager_said(facts):
    assert "time" in rules("Could we do 18:30 instead?", facts)
    assert "time" in rules("Would half past six work?", facts)
    assert "time" not in rules("Could 18:30 work for you?", facts, ["We have 18:30 free"])
    assert "time" not in rules("A table at 5pm, please.", facts)
    assert "time" not in rules("A table for two at 17:00.", facts)


def test_dates_and_weekdays(facts):
    assert rules("A table on Thursday 19 November.", facts) == set()
    assert "date" in rules("Could we come on Friday instead?", facts)
    assert "date" in rules("How about tomorrow?", facts)
    assert "date" not in rules("Friday works too, thank you.", facts, ["We also have Friday"])


def test_party_size(facts):
    assert "party" in rules("A table for three, please.", facts)
    assert "party" in rules("There will be 4 people.", facts)
    assert "party" not in rules("A table for two, please.", facts)


def test_unknown_names_and_venues(facts):
    assert "name" in rules("Could we sit near Hyde Park?", facts)
    assert "name" in rules("Is that The Ivy?", facts)
    assert "name" not in rules("Is that The Crown & Anchor?", facts, ["We're The Crown & Anchor"])
    assert "name" not in rules("I'm an AI assistant booking for Yaroslav.", facts)


def test_payment(facts):
    assert "payment" in rules("I can pay a deposit of 20 pounds.", facts)
    assert "payment" in rules("We'll pay when we arrive.", facts)
    assert "payment" not in rules("I'll check about the deposit with Yaroslav.", facts)


def test_restaurant_name_before_the_manager_gave_it(facts):
    facts.set("restaurant_name", "Dishoom", "owner", 1)
    assert "restaurant" in rules("See you at Dishoom!", facts)
    facts.set("restaurant_name", "Dishoom", "manager", 2)
    assert "restaurant" not in rules("See you at Dishoom!", facts, ["This is Dishoom"])


def test_restatement_without_restaurant_is_not_built(facts):
    for k in ("time", "date", "party_size"):
        facts.confirm(k)
    assert restate.build(facts) is None
    assert restate.missing(facts) == ["restaurant_name"]


def test_every_engine_template_passes(facts):
    facts.set("guest_name", "Yaroslav", "owner", 1)
    t = facts.get("time")
    templates = [
        engine.OPENING.format(greeting="Good afternoon", who="Yaroslav", time=t),
        engine.HOLD.format(who="Yaroslav"), engine.STILL_CHECKING.format(who="Yaroslav"),
        engine.SORRY_WAIT + engine.NAME_ONLY.format(name="Yaroslav"), engine.FOLLOW_UP_1.format(time=t),
        engine.FOLLOW_UP_2.format(time=t), engine.BAD_VOICE, engine.TECH_HOLD, restate.ASK_RESTAURANT,
    ]
    for text in templates:
        assert guard.check(guard.fix(text), facts, []) == [], text
        assert "—" not in text and "–" not in text
