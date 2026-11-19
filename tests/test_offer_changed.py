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


# R10 finding 1: a tap on an older or closed question, or an offer changed by an edit

def tap(e, value):
    return e.handle(Event("owner_answer", key="alternative_time", text="", value=value))


def two_offers(profile=None):
    e = opened(profile)
    mgr(e, "Sorry, 17:00 is full. I can do 20:00, would that work?", offer("20:00"))
    mgr(e, "or 21:15 if you'd rather eat later", offer("21:15"))
    return e


def test_buttons_carry_their_time():
    e = opened()
    acts = mgr(e, "Sorry, 17:00 is full. I can do 20:00, would that work?", offer("20:00"))
    ask = next(a for a in acts if isinstance(a, AskOwner))
    assert [v for _, v in ask.buttons] == ["accept@20:00", "only@20:00"]


def test_tap_on_the_older_question_changes_nothing_and_asks_again():
    from agent import texts
    for old in ("accept@20:00", "only@20:00"):
        e = two_offers()
        acts = tap(e, old)
        assert sent(acts) == [] and not e.brain.queue
        again = [a for a in acts if isinstance(a, AskOwner)]
        assert len(again) == 1 and again[0].question.startswith(texts.t("time_old")) and "21:15" in again[0].question
        assert e.facts.get("time") == "17:00" and e.pending["alternative_time"]["offered"] == "21:15"
        assert not e.declined_times
        e.brain.push(D("Thank you, 21:15 works for us.", intent="counter_offer"))
        out = sent(tap(e, "accept@21:15"))
        assert out == ["Thank you, 21:15 works for us."] and e.facts.get("time") == "21:15"


def test_tap_on_a_closed_question_after_the_restatement_changes_nothing():
    for old in ("accept@20:00", "only@20:00", "accept@21:15"):
        e = two_offers()
        e.brain.push(D("Thank you, 21:15 works for us.", intent="counter_offer"))
        tap(e, "accept@21:15")
        e.brain.push(D(action="final_restatement", intent="agreed", manager_agreed=True, restaurant_name="Dishoom"))
        restated = mgr(e, "Lovely, 21:15 for two is booked. We're Dishoom.")
        assert any("21:15" in t for t in sent(restated)) and e.n_restated == 1
        acts = tap(e, old)
        assert sent(acts) == [] and not [a for a in acts if isinstance(a, AskOwner)]
        assert e.facts.get("time") == "21:15" and e.n_restated == 1


def test_window_took_the_new_time_then_the_old_tap_changes_nothing():
    e = opened(PROFILE)
    mgr(e, "Sorry, 17:00 is full. I can do 20:00, would that work?", offer("20:00"))
    acts = mgr(e, "Actually 18:30 has just come free", offer("18:30"))
    assert e.facts.get("time") == "18:30" and "alternative_time" not in e.pending
    assert any("18:30 works for us" in t for t in sent(acts))
    out = tap(e, "accept@20:00")
    assert sent(out) == [] and e.facts.get("time") == "18:30"


def test_an_edited_offer_is_a_new_question():
    from agent import texts
    e = opened()
    mgr(e, "Sorry, 17:00 is full. I can do 20:00", offer("20:00"))
    e.brain.push(offer("21:15"))
    acts = e.handle(Event("manager_edit", text="Sorry, 17:00 is full. I can do 21:15",
                          old_text="Sorry, 17:00 is full. I can do 20:00"))
    assert any("21:15" in q for q in asked_time(acts)) and e.pending["alternative_time"]["offered"] == "21:15"
    assert sent(acts) == ["Thank you - let me just check whether 21:15 is possible for us and come back to you in a "
                          "moment."]
    old = tap(e, "accept@20:00")
    assert sent(old) == [] and any(a.question.startswith(texts.t("time_old")) for a in old if isinstance(a, AskOwner))
    assert e.facts.get("time") == "17:00"
