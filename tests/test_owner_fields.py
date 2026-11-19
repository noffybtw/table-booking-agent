"""The name, phone and email are the owner's: never asked from the manager, and given back in one message."""
import pytest

from agent import policy
from agent.brain import D
from agent.events import Event
from test_owner_answers import opened, sends
from test_restatement_once import REST

ASK_BOTH = dict(action="ask_owner", intent="asks_info", needs_owner=["guest_name", "phone"])


@pytest.mark.parametrize("text", [
    "The phone number is +3809*****864; could you let me know the name to use for the booking?",
    "What name would you like it under?",
    "Could I have your phone number?",
    "May I take a contact number, please?",
    "Which email should we use for the confirmation?",
    "Could you tell me the name for the booking, please?",
    "Please let me know the phone number to use.",
    "What's the best number to reach you on?",
    "Thank you - could you confirm the name and phone number you'd like?",
    "Under which name should I hold it?",
])
def test_question_for_an_owner_field_is_found(text):
    assert policy.owner_field_questions(text, policy.OWNER_FIELDS)


@pytest.mark.parametrize("text", [
    "Could I also take the restaurant's name for my note?",
    "What is the name of the restaurant?",
    "The booking can go under Yaroslav, contact number +44 7700 900123.",
    "The booking can be under the name Yaroslav and the phone number +3809*****864. Could you let me know the "
    "restaurant's name, please?",
    "Sorry for the wait, I'm still confirming that detail. Would it be all right to hold the table under the name "
    "Yaroslav for now?",
    "Let me just check the name and come back to you.",
    "Is the table for two at 17:00 today all confirmed, then?",
    "Could you confirm the number of guests?",
])
def test_giving_or_other_questions_are_not_found(text):
    assert policy.owner_field_questions(text, policy.OWNER_FIELDS) == []


def test_an_open_owner_question_key_counts_too():
    assert policy.owner_field_questions("Is there an occasion we should know about?", ["occasion"])
    assert policy.owner_field_questions("Is there an occasion we should know about?", policy.OWNER_FIELDS) == []


def both_asked():
    e = opened(D("Let me just check those details.", **ASK_BOTH, manager_agreed=True))
    e.handle(Event("manager_message", text="Yes, 17:00 is fine. What name and phone number should I put it under?"))
    return e


def test_draft_asking_the_manager_for_the_name_goes_as_a_template():
    e = opened(D("Let me just check the name.", action="ask_owner", intent="asks_info", needs_owner=["guest_name"]))
    e.handle(Event("manager_message", text="Hello. What name is it under?"))
    e.brain.push(D("Of course - could you let me know the name to use for the booking?", intent="question"))
    acts = e.handle(Event("manager_message", text="Any news?"))
    out = sends(acts)
    assert out == ["I'm just checking the name and will come back to you shortly."]
    why = [a.why for a in acts if hasattr(a, "why")]
    assert "guard: owner-held field asked from the manager, template used" in why[0]


def test_the_template_carries_the_phone_the_owner_gave():
    e = both_asked()
    assert sends(e.handle(Event("owner_answer", key="phone", text="+44 7700 900123"))) == []
    e.brain.push(D("The phone number is +44 7700 900123; could you let me know the name to use?", intent="question"))
    out = sends(e.handle(Event("manager_message", text="Any luck?")))
    assert out == ["Thank you for waiting - the contact number is +44 7700 900123. I'm just checking the name and "
                   "will come back to you shortly."]


def test_held_answer_goes_with_the_restatement_after_ten_minutes():
    e = both_asked()
    e.handle(Event("owner_answer", key="phone", text="+44 7700 900123"))
    e.facts.set("restaurant_name", REST, "manager", 2, confirmed=True)
    acts = e.handle(Event("timer", text="owner_wait", key="guest_name", gen=e.gen["owner:guest_name"]))
    out = sends(acts)
    assert len(out) == 1 and "+44 7700 900123" in out[0] and REST in out[0] and e.n_restated == 1
    assert not e.batch and "guest_name" in e.pending


def test_held_answer_goes_with_the_apology_when_not_agreed():
    e = opened(D("Let me just check those details.", **ASK_BOTH))
    e.handle(Event("manager_message", text="Hello. What name and phone number should I put it under?"))
    e.handle(Event("owner_answer", key="phone", text="+44 7700 900123"))
    out = sends(e.handle(Event("timer", text="owner_wait", key="guest_name", gen=e.gen["owner:guest_name"])))
    assert out == ["Sorry for the wait, I'm still confirming that detail. In the meantime, the contact number is "
                   "+44 7700 900123."]
    # the name after the apology goes on its own, at once
    e.brain.push(D("Thank you for waiting - it's under Yaroslav.", intent="asks_info"))
    out = sends(e.handle(Event("owner_answer", key="guest_name", text="Yaroslav")))
    assert len(out) == 1 and "Yaroslav" in out[0]


def test_audit_lists_a_question_for_the_name(tmp_path):
    from tools.audit_owner_fields import audit
    run = tmp_path / "test-x"
    run.mkdir()
    (run / "decisions.md").write_text(
        '- 11:49:52 OUT agent #6: "The phone number is +3809*****864; could you let me know the name to use for '
        'the booking?" | why: holding line\n'
        '- 11:50:35 OUT agent #7: "The booking can be under the name Yaroslav and the phone number +3809*****864. '
        'Could you let me know the restaurant\'s name, please?" | why: agreed\n')
    found = audit(tmp_path)
    assert len(found) == 1 and found[0].startswith("test-x #6 11:49:52 guest_name")


def test_holding_line_is_never_sent_twice_word_for_word():
    same = "I'll check with the guest and get back to you shortly."
    e = opened(D(same, action="ask_owner", intent="counter_offer", needs_owner=["alternative_time"],
                 offered_time="18:30"))
    first = sends(e.handle(Event("manager_message", text="Sorry, 17:00 is fully booked, but I can do 18:30.")))
    e.brain.push(D("Thank you, 18:30 works for us.", **dict(action="final_restatement", intent="agreed",
                                                             manager_agreed=True)))
    e.handle(Event("owner_answer", key="alternative_time", value="accept"))
    e.brain.push(D(same, **ASK_BOTH))
    second = sends(e.handle(Event("manager_message", text="Lovely. What name and phone number should I put it under?")))
    e.brain.push(D(same, intent="question"))
    third = sends(e.handle(Event("manager_message", text="Any luck?")))
    assert first == [same] and len(second) == 1 and len(third) == 1
    assert len({first[0], second[0], third[0]}) == 3
