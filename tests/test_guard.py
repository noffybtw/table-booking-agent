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
    text = engine.OPENING.format(greeting="Good morning", time=facts.get("time"))
    assert text == "Good morning. I'm an assistant - could I book a table for two today at 17:00, please?"
    assert "AI" not in text.split()
    assert rules(text, facts) == set()


def test_real_slip_watched_the_match(facts):
    assert "experience" in rules("I did catch it, quite the game.", facts)
    assert "experience" in rules("It was quite a match indeed.", facts)  # the Groq sim slip
    assert "experience" in rules("Quite the game, wasn't it?", facts)
    assert "experience" in rules("I missed it, but I'm sure it was quite something.", facts)
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
    assert rules("I'm an AI assistant making this booking.", facts) == set()


def test_payment(facts):
    assert "payment" in rules("I can pay a deposit of 20 pounds.", facts)
    assert "payment" in rules("We'll pay when we arrive.", facts)
    assert "payment" not in rules("I'll check about the deposit and come back to you.", facts)


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
    # no guest_name: the templates name nobody
    t = facts.get("time")
    templates = [
        engine.OPENING.format(greeting="Good afternoon", time=t), engine.HOLD, engine.STILL_CHECKING,
        engine.SORRY_WAIT, engine.FOLLOW_UP_1.format(time=t), engine.FOLLOW_UP_2.format(time=t), engine.BAD_VOICE,
        engine.TECH_HOLD, engine.CLOSING, restate.ASK_RESTAURANT,
    ]
    for text in templates:
        assert guard.check(guard.fix(text), facts, []) == [], text
        assert "—" not in text and "–" not in text
        assert "yaroslav" not in text.lower()
    facts.set("guest_name", "Yaroslav", "owner", 1)
    text = engine.SORRY_WAIT + engine.NAME_ONLY.format(name="Yaroslav")
    assert guard.check(text, facts, []) == []


@pytest.mark.parametrize("variant", [0, 1, 2])
@pytest.mark.parametrize("correction", [False, True])
@pytest.mark.parametrize("condition", [None, "table back by 18:30"])
def test_every_restatement_passes_without_a_guest_name(facts, variant, correction, condition):
    for k in ("time", "date", "party_size"):
        facts.confirm(k)
    facts.set("restaurant_name", "The Crown & Anchor", "manager", 1, confirmed=True)
    chat = ["We're The Crown & Anchor"]
    if condition:
        facts.set("condition", condition, "manager", 2)
        chat.append("We need the table back by 18:30")
    text = restate.build(facts, variant=variant, correction=correction)
    assert "yaroslav" not in text.lower()
    assert guard.check(guard.fix(text), facts, chat) == [], text


@pytest.mark.parametrize("text", ["I'm afraid 17:00 is the time Yaroslav needs.", "I'm Yaroslav's assistant.",
                                  "I'll check with yaroslav.", "It's for YAROSLAV, thank you."])
def test_owner_name_blocked_without_a_guest_name(facts, text):
    assert "owner_name" in rules(text, facts)
    assert "owner_name" in rules(text, facts, ["Is the table for Yaroslav?"])  # the chat does not make it a booking name


def test_owner_name_passes_once_it_is_the_guest_name(facts):
    assert "owner_name" not in rules("I'm an assistant.", facts)
    assert "owner_name" not in rules("Thank you for asking: Y-A-R-O-S-L-A-V.", facts)  # not the name as a word
    facts.set("guest_name", "Yaroslav Kostyk", "owner", 3)
    assert rules("The booking can go under Yaroslav.", facts) == set()
    other = FactStore.for_task(clock.parse_now("2026-11-19T08:00"))
    other.set("guest_name", "Anna", "owner", 3)
    assert "owner_name" in rules("It's under Anna, for Yaroslav.", other)
