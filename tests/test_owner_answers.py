"""What the owner types in the bot and what the agent does with it."""
from agent import clock
from agent.brain import D, FakeBrain
from agent.engine import Engine
from agent.events import AskOwner, Event, Log, NotifyOwner, SendText, SendVoice
from agent.facts import FactStore


def engine(*script):
    now = clock.parse_now("2026-11-19T08:00")
    e = Engine(FakeBrain(list(script)), FactStore.for_task(now), now=lambda: now)
    e.handle(Event("opening"))
    return e


def opened(*script):
    return engine(D("Good morning. I'm an assistant - could I book a table for two today at 17:00, please?",
                    intent="greeting"), *script)


def asked(e, text, *keys):
    e.brain.push(D("Let me just check that, I'll be back in a moment.", action="ask_owner", intent="asks_info",
                   needs_owner=list(keys)))
    return e.handle(Event("manager_message", text=text))


def sends(acts):
    return [a.text for a in acts if isinstance(a, (SendText, SendVoice))]


def test_cyrillic_name_is_not_stored_and_asked_again():
    e = opened()
    asked(e, "Lovely. What name shall I put it under?", "guest_name")
    for typed in ("Ярослав", "на Ярослава, на двоих"):
        acts = e.handle(Event("owner_answer", key="guest_name", text=typed))
        assert e.facts.get("guest_name") is None and "guest_name" in e.pending
        assert sends(acts) == [] and not e.brain.queue  # no model turn, nothing to the manager
        again = [a for a in acts if isinstance(a, AskOwner)]
        assert len(again) == 1 and again[0].key == "guest_name" and "латиниц" in again[0].question
        assert any(isinstance(a, Log) and a.data.get("result") == "not in Latin letters, asked again" for a in acts)


def test_latin_name_is_accepted():
    e = opened()
    asked(e, "Lovely. What name shall I put it under?", "guest_name")
    e.brain.push(D("Thank you for waiting - it's under Kostyk.", guest_name="Kostyk"))
    acts = e.handle(Event("owner_answer", key="guest_name", text="Kostyk"))
    assert e.facts.get("guest_name") == "Kostyk" and "guest_name" not in e.pending
    assert sends(acts) == ["Thank you for waiting - it's under Kostyk."]


def test_phone_typed_as_plain_text_goes_to_the_phone_question():
    e = opened()
    asked(e, "What name, and a contact number please?", "guest_name", "phone")
    e.brain.push(D("Thank you, the number is +44 7700 900123.", phone="+44 7700 900123"))
    e.handle(Event("owner_answer", text="+44 7700 900123"))
    assert e.facts.get("phone") == "+44 7700 900123" and e.facts.get("guest_name") is None
    assert list(e.pending) == ["guest_name"]


def test_one_open_question_takes_the_text():
    e = opened()
    asked(e, "Under what name?", "guest_name")
    e.brain.push(D("Thank you, it's under Yaroslav.", guest_name="Yaroslav"))
    e.handle(Event("owner_answer", text="Yaroslav"))
    assert e.facts.get("guest_name") == "Yaroslav"


def test_several_open_and_unclear_text_asks_which():
    e = opened()
    asked(e, "What name, and a contact number please?", "guest_name", "phone")
    acts = e.handle(Event("owner_answer", text="Yaroslav"))
    assert e.facts.get("guest_name") is None and set(e.pending) == {"guest_name", "phone"}
    note = [a.text for a in acts if isinstance(a, NotifyOwner)]
    assert len(note) == 1 and "имя для брони" in note[0] and "телефон" in note[0]
    assert sends(acts) == [] and not e.brain.queue
