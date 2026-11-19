"""The manager changes the offered time while the owner is asked about the first one: the owner is asked again about the
new time, and a yes is applied to the time the open question names (R9 finding 1)."""
from agent import clock
from agent.brain import D, FakeBrain
from agent.engine import Engine
from agent.events import AskOwner, Event, SendText, SendVoice
from agent.facts import FactStore
from agent.profile import from_dict

PROFILE = {"guest_name": "Yaroslav", "phone": "+44 7700 900123", "accept_from": "16:00", "accept_to": "19:00"}


def opened(profile=None):
    now = clock.parse_now("2026-11-19T11:34")
    e = Engine(FakeBrain([D("Good morning. Could I book a table for two today at 17:00, please?", intent="greeting")],
                         default=D("Thank you.", intent="other")),
               FactStore.for_task(now), now=lambda: now, profile=from_dict(profile) if profile else None)
    e.handle(Event("opening"))
    return e


def mgr(e, text, *script):
    e.brain.push(*script)
    return e.handle(Event("manager_message", text=text))


def offer(t):
    return D("Let me check.", action="ask_owner", intent="counter_offer", needs_owner=["alternative_time"], offered_time=t)


def asked_time(acts):
    return [a.question for a in acts if isinstance(a, AskOwner) and a.key == "alternative_time"]


def sent(acts):
    return [a.text for a in acts if isinstance(a, (SendText, SendVoice))]


def test_changed_offer_asks_the_owner_again_and_the_yes_takes_the_new_time():
    for profile in (None, PROFILE):
        e = opened(profile)
        first = mgr(e, "Sorry, 17:00 is full. I can do 20:00, would that work?", offer("20:00"))
        assert any("20:00" in q for q in asked_time(first))
        second = mgr(e, "or 21:15 if you'd rather eat later", offer("21:15"))
        assert any("21:15" in q for q in asked_time(second)), "the owner must be asked about the new time"
        assert e.pending["alternative_time"]["offered"] == "21:15"
        assert not any("works for us" in t for t in sent(first) + sent(second)), "nothing accepted before the owner"
        e.brain.push(D("", action="final_restatement", intent="agreed", manager_agreed=True))
        e.handle(Event("owner_answer", key="alternative_time", text="", value="accept"))
        assert e.facts.get("time") == "21:15"


def test_same_offer_again_is_not_a_new_question():
    e = opened()
    mgr(e, "Sorry, 17:00 is full. I can do 20:00, would that work?", offer("20:00"))
    again = mgr(e, "Hello? Is 20:00 ok?", offer("20:00"))
    assert asked_time(again) == []
    assert e.pending["alternative_time"]["offered"] == "20:00"
