"""One restatement: it waits for a name or phone the manager asked for, and "correction" only when a fact changed."""
from agent import restate
from agent.brain import D
from agent.events import Event, NotifyOwner, SendText, SendVoice
from test_owner_answers import opened

REST = "The Ivy"
AGREE_ASK = dict(action="final_restatement", intent="agreed", manager_agreed=True, needs_owner=["guest_name"])


def restatements(acts):
    return [a.text for a in acts if isinstance(a, SendVoice) and a.why.startswith("manager agreed; restatement")]


def texts(acts):
    return [a.text for a in acts if isinstance(a, (SendText, SendVoice))]


def test_agree_and_what_name_gives_one_restatement_after_the_answer():
    e = opened(D("Let me just check the name and come back to you.", **AGREE_ASK, restaurant_name=REST))
    acts = e.handle(Event("manager_message", text=f"Yes, 17:00 is fine at {REST}. What name?"))
    assert restatements(acts) == [] and texts(acts) == ["Let me just check the name and come back to you."]
    e.brain.push(D("Thank you, it's under Yaroslav.", guest_name="Yaroslav"))
    acts = e.handle(Event("owner_answer", key="guest_name", text="Yaroslav"))
    said = restatements(acts)
    assert len(said) == 1 and "under the name Yaroslav" in said[0] and "correction" not in said[0].lower()
    assert e.n_restated == 1


def test_empty_reply_gets_the_holding_template():
    e = opened(D(**AGREE_ASK, restaurant_name=REST))
    acts = e.handle(Event("manager_message", text=f"Yes, 17:00 is fine at {REST}. What name?"))
    assert texts(acts) == ["Let me just check that and come back to you in a moment."]


def test_owner_silent_restates_without_the_name_and_tells_the_owner():
    e = opened(D("Let me just check the name and come back to you.", **AGREE_ASK, restaurant_name=REST))
    e.handle(Event("manager_message", text=f"Yes, 17:00 is fine at {REST}. What name?"))
    g = e.gen["owner:guest_name"]
    acts = e.handle(Event("timer", text="owner_wait", key="guest_name", gen=g))
    said = restatements(acts)
    assert len(said) == 1 and "under the name" not in said[0] and REST in said[0]
    assert any(isinstance(a, NotifyOwner) and "без" in a.text for a in acts)
    assert "guest_name" in e.pending  # a late answer is still used
    # the late name adds itself with a normal opener, not "Thank you for the correction"
    e.brain.push(D("Thank you, it's under Yaroslav.", guest_name="Yaroslav"))
    acts = e.handle(Event("owner_answer", key="guest_name", text="Yaroslav"))
    late = restatements(acts)
    assert len(late) == 1 and "under the name Yaroslav" in late[0] and not late[0].startswith(restate.CORRECTION_OPENER)


def test_correction_opener_only_when_a_booking_fact_changed():
    sig = ("17:00", "2026-11-19", 2, REST, None, None)
    assert not restate.is_correction(None, sig)
    assert not restate.is_correction(sig, sig[:4] + ("Yaroslav", None))
    assert not restate.is_correction(sig, sig[:5] + ("table back by 19:00",))
    assert not restate.is_correction(("17:00", "2026-11-19", 2, None, None, None), sig)
    assert restate.is_correction(sig, ("17:30",) + sig[1:])
    assert restate.is_correction(sig, sig[:3] + ("The Wolseley", None, None))


def test_no_restaurant_name_after_one_ask_restates_at_your_restaurant():
    e = opened(D(action="final_restatement", intent="agreed", manager_agreed=True))
    acts = e.handle(Event("manager_message", text="Good morning, yes - 17:00 for two today is booked for you."))
    assert texts(acts) == [restate.ASK_RESTAURANT] and restatements(acts) == []
    e.brain.push(D("Of course, thank you!", intent="question"))
    acts = e.handle(Event("manager_message", text="It's our place, you wrote to us!"))
    said = restatements(acts)
    assert len(said) == 1 and "at your restaurant" in said[0] and "17:00" in said[0]
    assert any(isinstance(a, NotifyOwner) and "at your restaurant" in a.text for a in acts)
    assert e.phase == "confirmed"
    # the name given later goes into one more restatement, with a normal opener
    e.brain.push(D("Thank you!", intent="question", restaurant_name="The Ivy"))
    acts = e.handle(Event("manager_message", text="Ha, sorry - it's The Ivy."))
    late = restatements(acts)
    assert len(late) == 1 and "at The Ivy" in late[0] and not late[0].startswith(restate.CORRECTION_OPENER)


def test_owner_phone_with_no_model_reply_still_reaches_the_manager():
    e = opened(D("Let me check both and come back to you.", action="final_restatement", intent="agreed",
                 manager_agreed=True, needs_owner=["phone"]))
    e.handle(Event("manager_message", text="Yes, 17:00 for two is fine. A contact number, please?"))
    e.brain.push(D(action="final_restatement", intent="agreed", manager_agreed=True))
    acts = e.handle(Event("owner_answer", key="phone", text="+44 7700 900123"))
    out = texts(acts)
    assert len(out) == 1 and "+44 7700 900123" in out[0] and "restaurant's name" in out[0]
    e.brain.push(D("Thank you!", intent="question", restaurant_name=REST))
    acts = e.handle(Event("manager_message", text=f"It's {REST}."))
    said = restatements(acts)
    assert len(said) == 1 and "contact number +44 7700 900123" in said[0]
