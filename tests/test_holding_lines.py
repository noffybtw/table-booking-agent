"""One holding line per owner question; a line the agent can answer itself gets that answer while questions are open;
the manager's goodbye gets one warm closing line."""
from agent import clock
from agent.brain import D, FakeBrain
from agent.engine import SORRY_WAIT, Engine
from agent.events import Event, NotifyOwner, SendText, SendVoice
from agent.facts import FactStore

HOLD = "Let me just check and I'll come back to you shortly."


def opened(at="2026-11-19T15:59"):
    now = clock.parse_now(at)
    e = Engine(FakeBrain([D("Good afternoon. Could I book a table for two today at 17:00, please?", intent="greeting")]),
               FactStore.for_task(now), now=lambda: now)
    e.handle(Event("opening"))
    return e


def say(e, text, *script):
    e.brain.push(*script)
    return [a.text for a in e.handle(Event("manager_message", text=text)) if isinstance(a, (SendText, SendVoice))]


def test_bot_question_while_the_owner_is_asked_gets_the_answer_without_a_second_holding_line():
    e = opened()
    assert say(e, "Any dietary requirements or accessibility needs for the guests?",
               D(HOLD, action="ask_owner", intent="asks_info", needs_owner=["allergies"])) == [HOLD]
    out = say(e, "Are you a human or a bot?",
              D("I'm an AI assistant making this booking. Let me just check and I'll get back to you shortly.",
                intent="bot_question"))
    assert out == ["I'm an AI assistant making this booking."]


def test_while_the_time_is_with_the_owner_a_reply_about_something_else_goes_as_written():
    e = opened()
    say(e, "Sorry, 17:00 is full. I can do 20:00", D("Let me check.", action="ask_owner", intent="counter_offer",
                                                    needs_owner=["alternative_time"], offered_time="20:00"))
    out = say(e, "Are you a bot?", D("I'm an AI assistant making this booking.", intent="bot_question"))
    assert out == ["I'm an AI assistant making this booking."]
    # a reply that would decide the time is still held by code, and the holding line went already: nothing
    assert say(e, "So is 20:00 ok?", D("Yes, 20:00 is fine.", intent="counter_offer", offered_time="20:00")) == []


def test_the_ten_minute_apology_still_goes():
    e = opened()
    say(e, "Could I take a name?", D(HOLD, action="ask_owner", intent="asks_info", needs_owner=["guest_name"]))
    acts = e.handle(Event("timer", text="owner_wait", key="guest_name", gen=e.gen["owner:guest_name"]))
    assert [a.text for a in acts if isinstance(a, SendText)] == [SORRY_WAIT]


def test_goodbye_gets_one_warm_line():
    e = opened()
    out = say(e, "Thanks, that's all from me.")
    assert out == ["Thank you, have a lovely evening."] and not e.brain.queue
    assert say(e, "Bye!") == []
    morning = opened("2026-11-19T09:10")
    assert say(morning, "That's all, bye.") == ["Thank you, have a lovely day."]


def test_goodbye_with_a_yes_in_it_before_the_restatement_goes_to_the_model():
    e = opened()
    out = say(e, "Fine, booked for you. That's all from me.",
              D("", action="final_restatement", intent="agreed", manager_agreed=True))
    assert e.agreed and out and "restaurant" in out[0]


def test_goodbye_while_the_owner_is_asked_still_gets_the_closing():
    e = opened()
    say(e, "Could I take a name?", D(HOLD, action="ask_owner", intent="asks_info", needs_owner=["guest_name"]))
    acts = e.handle(Event("manager_message", text="Thanks, that's all from me."))
    assert [a.text for a in acts if isinstance(a, SendText)] == ["Thank you, have a lovely evening."]
    assert not any(isinstance(a, NotifyOwner) for a in acts)
