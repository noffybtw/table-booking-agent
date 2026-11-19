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


ASK_NAME = dict(action="ask_owner", intent="asks_info", needs_owner=["guest_name"])  # agreement not recorded


def test_name_with_no_recorded_agreement_goes_with_a_confirm_question_and_ok_restates_once():
    e = opened(D("Let me just check the name and come back to you.", **ASK_NAME))
    e.handle(Event("manager_message", text="Yes, 17:00 is fine. What name?"))
    assert not e.agreed
    e.brain.push(D("Thank you for waiting - it's under Kostyk.", intent="asks_info", guest_name="Kostyk"))
    out = texts(e.handle(Event("owner_answer", key="guest_name", text="Kostyk")))
    assert len(out) == 1 and "Kostyk" in out[0] and "Is the table for two at 17:00 today all confirmed, then?" in out[0]
    assert "restaurant's name" in out[0] and e.n_restated == 0
    e.brain.push(D("Thank you!", intent="closing"))  # the model still does not say "agreed"
    acts = e.handle(Event("manager_message", text="ok"))
    said = restatements(acts)
    assert len(said) == 1 and "under the name Kostyk" in said[0] and "at your restaurant" in said[0]
    assert e.n_restated == 1 and texts(acts) == said


def test_confirm_question_keeps_the_name_when_the_model_leaves_it_out():
    e = opened(D("Let me just check the name.", **ASK_NAME, restaurant_name=REST))
    e.handle(Event("manager_message", text=f"Yes, 17:00 is fine at {REST}. What name?"))
    e.brain.push(D(action="wait", intent="asks_info"))
    out = texts(e.handle(Event("owner_answer", key="guest_name", text="Kostyk")))
    assert out == ["Thank you for waiting - the booking can go under Kostyk. "
                   "Is the table for two at 17:00 today all confirmed, then?"]
    e.brain.push(D("Thank you!", intent="closing"))
    said = restatements(e.handle(Event("manager_message", text="Yes, all confirmed.")))
    assert len(said) == 1 and f"at {REST}" in said[0]


def test_a_reply_to_the_confirm_question_that_is_not_a_plain_yes_does_not_restate():
    e = opened(D("Let me just check the name.", **ASK_NAME, restaurant_name=REST))
    e.handle(Event("manager_message", text=f"Yes, 17:00 is fine at {REST}. What name?"))
    e.brain.push(D("Thank you for waiting - it's under Kostyk.", guest_name="Kostyk"))
    e.handle(Event("owner_answer", key="guest_name", text="Kostyk"))
    e.brain.push(D("Of course, I'll check.", intent="question"))
    acts = e.handle(Event("manager_message", text="Ok, but could you do 18:00 instead?"))
    assert restatements(acts) == [] and not e.agreed


def test_manager_plain_yes():
    from agent import policy
    for t in ("ok", "Ok.", "Yes, all confirmed.", "yep", "\U0001F44D", "Yes, 17:00 confirmed", "Perfect, see you at 5"):
        assert policy.manager_yes(t, "17:00"), t
    for t in ("ok?", "Yes but only until 19:00", "Yes, 18:00 then", "Sorry, not yet", "Hmm", "I don't think so", ""):
        assert not policy.manager_yes(t, "17:00"), t


def test_a_stalling_answer_to_the_confirm_question_is_not_a_yes():
    from agent import policy
    for t in ("Ok, let me check", "Okay, one moment please", "Yes, one moment", "Great, let me have a look",
              "Sure, give me a minute", "Right, bear with me", "Perfect, I'll check the diary",
              "Fine, we will let you know", "Yes, hang on", "Ok, I\u2019ll get back to you"):
        assert not policy.manager_yes(t, "17:00"), t
    for t in ("Yes", "Yes, that's fine", "Yes, all confirmed", "Confirmed", "That's fine, see you then",
              "Great, thanks, see you at 17:00 then!"):
        assert policy.manager_yes(t, "17:00"), t
    # words outside the short list are left to the model
    assert not policy.manager_yes("Yes, the chef says so", "17:00")


def test_ok_let_me_check_after_the_confirm_question_sends_no_restatement():
    e = opened(D("Let me just check the name.", **ASK_NAME, restaurant_name=REST))
    e.handle(Event("manager_message", text="Hello. What name is it under?"))
    e.brain.push(D("Thank you for waiting - it's under Kostyk.", guest_name="Kostyk"))
    e.handle(Event("owner_answer", key="guest_name", text="Kostyk"))
    e.brain.push(D("Of course, thank you.", intent="question"))
    acts = e.handle(Event("manager_message", text="Ok, let me check"))
    assert restatements(acts) == [] and not e.agreed and e.phase == "open"
    e.brain.push(D("Thank you!", intent="closing"))
    said = restatements(e.handle(Event("manager_message", text="Yes, all booked")))
    assert len(said) == 1 and "under the name Kostyk" in said[0] and e.n_restated == 1


def silence(e):
    return e.handle(Event("timer", text="silence", gen=e.gen["silence"]))


def test_agreed_then_silent_gets_the_restatement_not_a_follow_up():
    e = opened(D(action="final_restatement", intent="agreed", manager_agreed=True))
    assert texts(e.handle(Event("manager_message", text="Yes, 17:00 for two is fine."))) == [restate.ASK_RESTAURANT]
    acts = silence(e)
    said = restatements(acts)
    assert len(said) == 1 and "at your restaurant" in said[0] and "might be possible" not in " ".join(texts(acts))
    assert any(isinstance(a, NotifyOwner) and "at your restaurant" in a.text for a in acts)
    assert e.phase == "confirmed" and silence(e) == []


def test_silence_after_the_confirm_question_asks_to_confirm():
    e = opened(D("Let me just check the name.", **ASK_NAME, restaurant_name=REST))
    e.handle(Event("manager_message", text=f"Yes, 17:00 is fine at {REST}. What name?"))
    e.brain.push(D("Thank you for waiting - it's under Kostyk.", guest_name="Kostyk"))
    e.handle(Event("owner_answer", key="guest_name", text="Kostyk"))
    out = texts(silence(e))
    assert out == ["Sorry to chase - just checking that the table for two at 17:00 today is all confirmed?"]
    out = texts(silence(e))
    assert len(out) == 1 and "might be possible" not in out[0]
    e.brain.push(D("Thank you!", intent="closing"))
    # the confirm question still stands after the follow-ups: a late plain yes is the agreement
    said = restatements(e.handle(Event("manager_message", text="Yes, all confirmed.")))
    assert len(said) == 1 and "under the name Kostyk" in said[0]


def test_silence_before_any_agreement_still_asks_whether_a_table_is_possible():
    e = opened()
    assert texts(silence(e)) == ["Sorry to chase - just checking whether a table for two at 17:00 today might be possible?"]


def test_a_name_left_out_of_the_reply_still_reaches_the_manager_while_the_phone_is_open():
    e = opened(D("Let me check the name.", **AGREE_ASK, restaurant_name=REST))
    e.handle(Event("manager_message", text=f"Yes, 17:00 is fine at {REST}. What name?"))
    e.brain.push(D("Of course, I'll check the number too.", action="ask_owner", intent="asks_info",
                   manager_agreed=True, needs_owner=["phone"]))
    e.handle(Event("manager_message", text="And a contact number, please."))
    e.brain.push(D("I'll check on the contact number and get back to you shortly.", intent="asks_info",
                   manager_agreed=True))
    out = texts(e.handle(Event("owner_answer", key="guest_name", text="Kostyk")))
    assert out == ["Thank you for waiting - the booking can go under Kostyk. "
                   "I'll check on the contact number and get back to you shortly."]
    e.brain.push(D("And the number is 07700 900123.", intent="asks_info", manager_agreed=True))
    said = restatements(e.handle(Event("owner_answer", key="phone", text="+44 7700 900123")))
    assert len(said) == 1  # the phone in another format is not added twice: the restatement carries it
