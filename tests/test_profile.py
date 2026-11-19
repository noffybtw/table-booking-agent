"""--profile: the name and phone given before the start, and a window of other times the owner takes."""
import json

import pytest

from agent import texts
from agent.brain import D, FakeBrain
from agent.config import ROOT, SafetyError
from agent.events import AskOwner, Event
from agent.profile import from_dict, load
from sim import harness
from sim.scenarios import BY_NAME, PROFILE
from test_owner_answers import opened, sends

COUNTER = dict(action="ask_owner", intent="counter_offer", needs_owner=["alternative_time"])


def with_profile(profile=PROFILE, *script):
    e = opened(*script)
    e.profile = from_dict(profile)
    return e


def counter(manager, model_time, profile=PROFILE):
    e = with_profile(profile, D("Thank you - let me just check that time.", **COUNTER, offered_time=model_time))
    acts = e.handle(Event("manager_message", text=manager))
    return e, acts, [a for a in acts if isinstance(a, AskOwner)]


def test_name_and_phone_from_the_profile_with_no_owner_question(tmp_path):
    lines = []
    res = harness.run(BY_NAME["profile_name_phone"], FakeBrain(), tmp_path, echo=lines.append)
    assert not res.problems and res.restatements == 1
    assert not any(s.asked for s in res.steps)
    log = (tmp_path / "decisions.md").read_text()
    assert "from the owner's profile, given before the start" in log
    for text in (log, (tmp_path / "events.jsonl").read_text(), "\n".join(lines)):
        assert "7700 900123" not in text and "+44 77** ***123" in text


def test_inside_the_window_taken_with_one_restatement_at_1830(tmp_path):
    res = harness.run(BY_NAME["profile_window"], FakeBrain(), tmp_path, echo=lambda s: None)
    assert not res.problems and res.restatements == 1
    assert not any(s.asked for s in res.steps)
    assert "inside the owner's window 17:00-19:00" in (tmp_path / "decisions.md").read_text()


@pytest.mark.parametrize("manager,model_time", [
    ("Sorry, 17:00 is full. I can do 20:00", "20:00"),     # outside the window
    ("Sorry, 17:00 is full. I can do 6:30", "18:30"),      # inside, but 6:30 may be 06:30
    ("Sorry, 17:00 is full. I can do 7", "19:00"),         # a bare hour
    ("Sorry, 17:00 is full. How about half six?", "18:30"),
])
def test_outside_the_window_or_unsure_asks_the_owner(manager, model_time):
    e, acts, asked = counter(manager, model_time)
    assert [a.key for a in asked] == ["alternative_time"] and e.facts.get("time") == "17:00" and not e.agreed


def test_no_window_in_the_profile_asks_the_owner():
    e, acts, asked = counter("Sorry, 17:00 is full. I can do 18:30", "18:30", {"guest_name": "Yaroslav"})
    assert [a.key for a in asked] == ["alternative_time"]


def test_profile_without_the_phone_asks_the_owner_for_it():
    e = with_profile({"guest_name": "Yaroslav"},
                     D("Let me just check those details.", action="ask_owner", intent="asks_info",
                       needs_owner=["guest_name", "phone"], manager_agreed=True))
    acts = e.handle(Event("manager_message", text="Yes, 17:00 is fine. What name and phone number?"))
    assert [a.key for a in acts if isinstance(a, AskOwner)] == ["phone"] and e.facts.get("guest_name") == "Yaroslav"


def test_name_only_from_the_profile_goes_with_the_confirm_question():
    e = with_profile(PROFILE, D("Let me check the name.", action="ask_owner", intent="asks_info",
                                needs_owner=["guest_name"]))
    out = sends(e.handle(Event("manager_message", text="Hello. What name is it under?")))
    assert out == ["Of course - the booking can go under Yaroslav. Is the table for two at 17:00 today all "
                   "confirmed, then?"]
    assert e.facts.get("phone") is None  # not asked, not given


@pytest.mark.parametrize("manager", ["Hi, my name is Vlad. Let me check 17:00 for you.",
                                     "No problem, table number 12 is free at 17:00.", "Sure. For what number of people?",
                                     "I'll contact the chef and come back."])
def test_profile_field_not_given_when_the_manager_does_not_ask(manager):
    e = with_profile(PROFILE, D("Thank you.", intent="question", needs_owner=["guest_name", "phone"]))
    acts = e.handle(Event("manager_message", text=manager))
    out = " ".join(sends(acts))
    assert "Yaroslav" not in out and "900123" not in out and not [a for a in acts if isinstance(a, AskOwner)]
    assert e.facts.get("guest_name") is None and e.facts.get("phone") is None


@pytest.mark.parametrize("manager,keys", [
    ("What name should I put it under?", ["guest_name"]), ("Could I take a contact number?", ["phone"]),
    ("Name and phone number, please.", ["guest_name", "phone"]),
    ("And a number in case we need to reach you?", ["phone"]),
])
def test_profile_field_given_when_the_manager_asks(manager, keys):
    e = with_profile(PROFILE, D("Let me just check that.", action="ask_owner", intent="asks_info",
                                needs_owner=["guest_name", "phone"]))
    acts = e.handle(Event("manager_message", text=manager))
    out = " ".join(sends(acts))
    assert ("Yaroslav" in out) == ("guest_name" in keys) and ("+44 7700 900123" in out) == ("phone" in keys)
    assert not [a for a in acts if isinstance(a, AskOwner)]


def test_profile_field_not_given_when_the_model_does_not_want_it():
    e = with_profile(PROFILE, D("Of course.", intent="question"))
    out = " ".join(sends(e.handle(Event("manager_message", text="What name should I put it under?"))))
    assert "Yaroslav" not in out and e.facts.get("guest_name") is None


def test_restaurant_name_is_not_a_name_request():
    e = with_profile(PROFILE, D("Lovely, thank you!", intent="agreed"))
    e.handle(Event("manager_message", text="The name of the restaurant is The Ivy."))
    assert e.facts.get("guest_name") is None


@pytest.mark.parametrize("data,msg", [
    ({"guest_name": "Ярослав"}, "Latin"), ({"phone": "123"}, "7 digits"), ({"accept_from": "17:00"}, "together"),
    ({"accept_from": "19:00", "accept_to": "17:00"}, "after"), ({"accept_from": "7pm", "accept_to": "19:00"}, "HH:MM"),
    ({"name": "Yaroslav"}, "unknown"),
])
def test_bad_profile_is_refused(data, msg):
    with pytest.raises(SafetyError, match=msg):
        from_dict(data)


def test_profile_inside_the_repo_is_refused(tmp_path):
    with pytest.raises(SafetyError, match="outside the repo"):
        load(str(ROOT / "profile.json"))
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(PROFILE))
    assert load(str(p)).window == "17:00-19:00"


def test_start_message_lists_the_profile_with_the_phone_masked(monkeypatch):
    for lang in ("uk", "ru"):
        monkeypatch.setenv("OWNER_LANG", lang)
        text = from_dict(PROFILE).owner_text()
        assert "Yaroslav" in text and "+44 77** ***123" in text and "17:00" in text and "19:00" in text
        assert "900123" not in text.replace("***123", "") and text.startswith(texts.t("start_profile", items="")[:8])
