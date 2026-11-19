"""Another time written "18.30", "6:30" or "half six": the owner is asked about 18:30 and one restatement follows."""
import pytest

from agent import guard
from agent.brain import D
from agent.events import AskOwner, Event
from test_owner_answers import opened
from test_restatement_once import REST, restatements


@pytest.mark.parametrize("text,want", [
    ("Sorry, 17:00 is full. I can do 18.30", {"18:30"}), ("6.30", {"06:30", "18:30"}), ("17.30", {"17:30"}),
    ("I can do 6:30", {"06:30", "18:30"}), ("10:30", {"10:30", "22:30"}), ("06:30", {"06:30"}),
    ("6:30am", {"06:30"}), ("How about half six?", {"06:30", "18:30"}), ("half past six", {"06:30", "18:30"}),
    ("quarter past six", {"06:15", "18:15"}), ("quarter to seven", {"06:45", "18:45"}), ("Ok 1830", {"18:30"}),
])
def test_time_spellings(text, want):
    assert guard.find_times(text)[-1][1] == want


@pytest.mark.parametrize("text", ["Call us on 020 7946 1830", "+44 7700 900123", "07700 900456", "in 2026",
                                  "It's 2026 already", "£18.30 a head", "20.11.2026", "Yes, 20/11", "1830-1900"])
def test_numbers_that_are_not_times(text):
    assert guard.find_times(text) == []


COUNTER = dict(action="ask_owner", intent="counter_offer", needs_owner=["alternative_time"])


def offered(manager: str, model_time: str | None):
    e = opened(D("Thank you - let me just check that time and come back to you.", **COUNTER, offered_time=model_time))
    acts = e.handle(Event("manager_message", text=manager))
    return e, [a for a in acts if isinstance(a, AskOwner) and a.key == "alternative_time"]


CASES = [("Sorry, 17:00 is full. I can do 18.30", "18:30"), ("Sorry, 17:00 is full. I can do 18.30", "18.30"),
         ("Sorry, we're full at 17:00. I can do 6:30", "6:30"), ("Sorry, we're full at 17:00. I can do 6:30", "18:30"),
         ("We're full at 17:00, I'm afraid. How about half six?", "18:30"),
         ("We're full at 17:00, I'm afraid. How about half six?", "half six")]


@pytest.mark.parametrize("manager,model_time", CASES)
@pytest.mark.parametrize("answer", [dict(value="accept"), dict(text="18:30"), dict(text="так")])
def test_owner_accepts_the_other_time_one_restatement(manager, model_time, answer):
    e, asks = offered(manager, model_time)
    assert e.facts.get("offered_time") == "18:30"
    assert len(asks) == 1 and "18:30" in asks[0].question and "06:30" not in asks[0].question
    e.brain.push(D("Thank you, 18:30 works for us - could you book it for 18:30?", intent="counter_offer"))
    acts = e.handle(Event("owner_answer", key="alternative_time", **answer))
    assert not [a for a in acts if isinstance(a, AskOwner)] and e.facts.get("time") == "18:30"
    e.brain.push(D(action="final_restatement", intent="agreed", manager_agreed=True, restaurant_name=REST))
    acts = e.handle(Event("manager_message", text=f"Great, 18:30 for two is booked. We're {REST}."))
    said = restatements(acts)
    assert len(said) == 1 and "18:30" in said[0] and "06:30" not in said[0] and e.n_restated == 1


@pytest.mark.parametrize("manager,model_time", [("Sorry, 17:00 is full. I can do 6:30am", "6:30am"),
                                                ("Sorry, 17:00 is full. We only have 6:30 in the morning", "6:30"),
                                                ("Sorry, 17:00 is full. I can do 06:30", "06:30")])
def test_morning_time_stays_morning(manager, model_time):
    e, asks = offered(manager, model_time)
    assert e.facts.get("offered_time") == "06:30" and "06:30" in asks[0].question


def no_offer_stored():
    e, asks = offered("Sorry, 17:00 is full. Could you do a bit later?", None)
    assert e.facts.get("offered_time") is None and "alternative_time" in e.pending
    return e


def test_typed_time_taken_when_no_offer_was_stored():
    e = no_offer_stored()
    e.brain.push(D("Thank you, 18:30 works for us - could you book it for 18:30?", intent="counter_offer"))
    acts = e.handle(Event("owner_answer", key="alternative_time", text="18:30"))
    assert not [a for a in acts if isinstance(a, AskOwner)] and e.facts.get("time") == "18:30"
    assert "alternative_time" not in e.pending


@pytest.mark.parametrize("typed", ["так", "18:30?", "не 18:30", "зачекай, 18:30", "17:00 або 18:30"])
def test_typed_answer_with_no_offer_and_no_clear_time_is_asked_again(typed):
    e = no_offer_stored()
    acts = e.handle(Event("owner_answer", key="alternative_time", text=typed))
    assert [a for a in acts if isinstance(a, AskOwner)] and e.facts.get("time") == "17:00"


@pytest.mark.parametrize("manager,model_time,want", [("I can do 6:30", "06:30", "18:30"),
                                                     ("I can do 6:30am", "18:30", "06:30"),
                                                     ("I can do 18:30", "6:30", "18:30")])
def test_model_digits_take_the_managers_reading(manager, model_time, want):
    e, _ = offered(f"Sorry, 17:00 is full. {manager}", model_time)
    assert e.facts.get("offered_time") == want


def test_model_time_the_manager_did_not_write_is_rejected():
    e, _ = offered("Sorry, 17:00 is full. I can do 18:30", "19:00")
    assert e.facts.get("offered_time") is None


def test_confirm_question_and_restatement_after_six_thirty_say_18_30():
    e, _ = offered("Sorry, we're full at 17:00. I can do 6:30", "6:30")
    e.brain.push(D("Thank you, 6:30 works for us.", intent="counter_offer"))
    e.handle(Event("owner_answer", key="alternative_time", value="accept"))
    e.brain.push(D("Let me just check the name and come back to you.", action="ask_owner", intent="asks_info",
                   needs_owner=["guest_name"]))
    e.handle(Event("manager_message", text="Lovely. What name is it under?"))
    e.brain.push(D("Thank you for waiting - it's under Yaroslav.", intent="asks_info", guest_name="Yaroslav"))
    acts = e.handle(Event("owner_answer", key="guest_name", text="Yaroslav"))
    sent = " ".join(a.text for a in acts if hasattr(a, "text") and a.__class__.__name__.startswith("Send"))
    assert "at 18:30 today all confirmed" in sent and "06:30" not in sent
    e.brain.push(D("Thank you!", intent="closing"))
    e.handle(Event("manager_message", text="Yes, all booked"))
    e.brain.push(D("Thank you!", intent="question", restaurant_name=REST))
    acts = e.handle(Event("manager_message", text=f"It's {REST}."))
    said = restatements(acts)
    assert len(said) == 1 and "18:30" in said[0] and e.n_restated == 1


def test_accepting_the_other_time_tells_the_manager_it_works_when_the_restaurant_is_unknown():
    # the real model often takes the manager's offer plus the owner's yes as the agreement on the owner turn
    e, _ = offered("Sorry, 17:00 is full. I can do 18.30", "18.30")
    e.brain.push(D("Thank you. Could I just take the restaurant's name for my note?", action="final_restatement",
                   intent="agreed", manager_agreed=True))
    acts = e.handle(Event("owner_answer", key="alternative_time", text="так"))
    sent = [a.text for a in acts if a.__class__.__name__.startswith("Send")]
    assert len(sent) == 1 and "18:30 works for us" in sent[0] and "restaurant's name" in sent[0]
    e.brain.push(D(action="final_restatement", intent="agreed", manager_agreed=True, restaurant_name=REST))
    said = restatements(e.handle(Event("manager_message", text=f"It's {REST}.")))
    assert len(said) == 1 and "18:30" in said[0] and e.n_restated == 1


@pytest.mark.parametrize("manager,model_time,want", [
    ("We have 11:30 free, 17:00 is full", "11:30", "11:30"), ("We have 11:30 free, 17:00 is full", "23:30", "11:30"),
    ("17:00 is full, 10:30 is free", "22:30", "10:30"), ("17:00 is full, I can do 9:30", "9:30", "21:30"),
    ("17:00 is full, 11:30pm is free", "23:30", "23:30"),
])
def test_ten_and_eleven_are_read_as_written(manager, model_time, want):
    e, asked = offered(manager, model_time)
    assert e.facts.get("offered_time") == want and len(asked) == 1 and want in asked[0].question


def test_only_the_booking_time_and_the_model_says_agreed_is_not_an_agreement():
    from test_owner_answers import sends
    e, _ = offered("Sorry, 17:00 is full. I can do 18:30", "18:30")
    # R5: the model returns final_restatement with no text; the code line "17:00 works for us" must not follow
    e.brain.push(D(action="final_restatement", intent="agreed", manager_agreed=True))
    out = sends(e.handle(Event("owner_answer", key="alternative_time", value="only")))
    assert not e.agreed and e.n_restated == 0 and not e.asked_restaurant
    assert not any("works for us" in t or "restaurant" in t for t in out), out


def test_owner_takes_the_offer_and_the_model_says_agreed_still_restates():
    from test_owner_answers import sends
    e, _ = offered(f"Sorry, 17:00 is full. I can do 18:30 at {REST}.", "18:30")
    e.facts.set("restaurant_name", REST, "manager", 1, confirmed=True)
    e.brain.push(D("Thank you, 18:30 works for us.", action="final_restatement", intent="agreed", manager_agreed=True))
    out = sends(e.handle(Event("owner_answer", key="alternative_time", value="accept")))
    assert e.agreed and e.n_restated == 1 and "18:30" in out[0]


@pytest.mark.parametrize("manager,model_time,want", [
    ("Sorry, 17:00 is full. I can do 7", "19:00", "19:00"), ("Sorry, 17:00 is full. 7 instead?", "7pm", "19:00"),
    ("We're full at 17:00. 6ish?", "18:00", "18:00"), ("We're full at 17:00. Six thirty any good?", "18:30", "18:30"),
    ("17:00 is gone, 18h30 is free", "18:30", "18:30"),
])
def test_a_bare_hour_backs_the_model_time(manager, model_time, want):
    e, asked = offered(manager, model_time)
    assert e.facts.get("offered_time") == want and want in asked[0].question
    assert [v for _, v in asked[0].buttons] == ["accept", "only"]


@pytest.mark.parametrize("manager,model_time", [
    ("We're full at 17:00. Six thirty any good?", "18:00"), ("Sorry, 17:00 is full. I can do 7", "20:00"),
])
def test_a_bare_hour_backs_only_its_own_hour(manager, model_time):
    e, asked = offered(manager, model_time)
    assert e.facts.get("offered_time") is None


def test_no_stored_offer_drops_the_accept_button_and_asks_for_the_time():
    from agent import texts
    e, asked = offered("Sorry, 17:00 is full. We have something later", "19:00")
    assert e.facts.get("offered_time") is None and len(asked) == 1
    assert [v for _, v in asked[0].buttons] == ["only"] and texts.t("time_type") in asked[0].question
    again = [a for a in e.handle(Event("owner_answer", key="alternative_time", text="так")) if isinstance(a, AskOwner)]
    assert len(again) == 1 and again[0].question.startswith(texts.t("time_type"))
    e.brain.push(D("Thank you, 19:00 works for us.", intent="counter_offer"))
    e.handle(Event("owner_answer", key="alternative_time", text="19:00"))
    assert e.facts.get("time") == "19:00" and "alternative_time" not in e.pending
