"""What the owner types in the bot and what the agent does with it."""
from agent import clock, texts
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
    return engine(D("Good morning. Could I book a table for two today at 17:00, please?",
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
        assert len(again) == 1 and again[0].key == "guest_name" and again[0].question == texts.t("name_again")
        assert any(isinstance(a, Log) and a.data.get("result") == "not a name in Latin letters, asked again" for a in acts)


def test_latin_name_is_accepted():
    e = opened()
    asked(e, "Lovely. What name shall I put it under?", "guest_name")
    e.brain.push(D("Thank you for waiting - it's under Kostyk.", guest_name="Kostyk"))
    acts = e.handle(Event("owner_answer", key="guest_name", text="Kostyk"))
    assert e.facts.get("guest_name") == "Kostyk" and "guest_name" not in e.pending
    # the manager's yes is not recorded here, so the name goes with a confirm question
    out = sends(acts)
    assert len(out) == 1 and out[0].startswith("Thank you for waiting - it's under Kostyk. Is the table for two")


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
    assert len(note) == 1 and texts.t("key.guest_name") in note[0] and texts.t("key.phone") in note[0]
    assert sends(acts) == [] and not e.brain.queue


YES_WORDS = ["хорошо", "ладно", "конечно", "пойдёт", "пойдет", "можно", "берём", "берем", "так", "добре", "гаразд",
             "звісно", "підходить", "згоден", "годиться", "+", "\U0001F44D", "да", "ok", "Yes, fine",
             "так, давай", "так!", "давай 18:30"]
NO_WORDS = ["нет", "не подходит", "только 17:00", "ні", "не підходить", "тільки 17:00", "лише 17", "no", "only 17:00"]


def countered():
    e = opened()
    e.brain.push(D("Let me just check whether 18:30 would work.", action="ask_owner", intent="counter_offer",
                   needs_owner=["alternative_time"], offered_time="18:30"))
    e.handle(Event("manager_message", text="We're full at 17:00. Could you do 18:30 instead?"))
    assert "alternative_time" in e.pending
    return e


import pytest  # noqa: E402


@pytest.mark.parametrize("word", YES_WORDS)
def test_yes_in_words_takes_the_offered_time(word):
    e = countered()
    e.brain.push(D("Thank you, 18:30 works for us.", intent="agreed"))
    e.handle(Event("owner_answer", key="alternative_time", text=word))
    assert e.facts.get("time") == "18:30" and e.facts.is_confirmed("time") and not e.declined_times


@pytest.mark.parametrize("word", NO_WORDS)
def test_no_in_words_keeps_17_and_declines_the_offer(word):
    e = countered()
    e.brain.push(D("I'm afraid only 17:00 works for us - might anything open up?", intent="fully_booked"))
    e.handle(Event("owner_answer", key="alternative_time", text=word))
    assert e.facts.get("time") == "17:00" and e.declined_times == {"18:30"}


@pytest.mark.parametrize("word", ["хм", "подумаю", "ок, но не знаю", "а 19:00?", "ну как там?", "давай 19:00",
                                  "так, але краще 18:00", "можно ли 18:00?", "ладно, но лучше 17", "так, подожди",
                                  "так собі", "ні, краще 19:00", "добре, зачекай", "ok, hold on", "стоп"])
def test_neither_yes_nor_no_sends_the_question_again(word):
    e = countered()
    acts = e.handle(Event("owner_answer", key="alternative_time", text=word))
    again = [a for a in acts if isinstance(a, AskOwner)]
    assert len(again) == 1 and again[0].key == "alternative_time" and [v for _, v in again[0].buttons] == ["accept", "only"]
    assert sends(acts) == [] and not e.brain.queue  # no model call
    assert e.facts.get("time") == "17:00" and e.facts.get("offered_time") == "18:30" and not e.declined_times
    assert "alternative_time" in e.pending


def test_stray_owner_message_gets_the_status_and_no_model_turn():
    e = opened()
    acts = e.handle(Event("owner_answer", text="ну як там?"))
    assert sends(acts) == [] and not e.brain.queue and len(e.brain.calls) == 1  # only the opening called the model
    note = [a.text for a in acts if isinstance(a, NotifyOwner)]
    assert len(note) == 1 and "Етап: розмова з менеджером. Менеджер погодився: ні. Чекаю від тебе: нічого." in note[0]
    assert "STATE" not in note[0] and "phase" not in note[0]
    assert e.facts.get("owner_note") is None  # a question is answered with the status, not kept as a note
    acts = e.handle(Event("owner_answer", text="якщо що, буду вчасно"))
    assert sends(acts) == [] and len(e.brain.calls) == 1
    assert e.facts.get("owner_note") == "якщо що, буду вчасно"
    # the note is a known fact on the next model turn
    e.brain.push(D("Thank you, could I book a table for two today at 17:00?"))
    e.handle(Event("manager_message", text="Hello, sorry, who is this?"))
    assert e.brain.calls[-1]["facts"]["owner_note"] == "якщо що, буду вчасно"


def test_a_chase_while_the_owner_is_asked_never_gets_silence():
    from agent.engine import STILL_CHECKING
    e = opened()
    asked(e, "What name shall I put it under?", "guest_name")
    e.brain.push(D(action="wait", intent="question"))
    acts = e.handle(Event("manager_message", text="Any luck?"))
    assert sends(acts) == [STILL_CHECKING]


def test_a_short_stray_owner_text_is_not_kept_as_a_note():
    e = opened()
    for short_text in ("дякую", "ок", "ну що?"):
        acts = e.handle(Event("owner_answer", text=short_text))
        assert sends(acts) == [] and e.facts.get("owner_note") is None
        assert not any(short_text in line for line in e.transcript)
        note = [a.text for a in acts if isinstance(a, NotifyOwner)]
        assert len(note) == 1 and note[0].startswith("Зараз питань немає") and "нотатку" not in note[0]


def test_status_is_in_the_console_language(monkeypatch):
    for lang, want in (("uk", "Етап: розмова з менеджером. Менеджер погодився: ні. Чекаю від тебе: ім'я для броні."),
                       ("ru", "Этап: разговор с менеджером. Менеджер согласился: нет. Жду от тебя: имя для брони.")):
        monkeypatch.setenv("OWNER_LANG", lang)
        e = opened()
        asked(e, "What name shall I put it under?", "guest_name")
        status = [a.text for a in e.handle(Event("owner_command", text="status")) if isinstance(a, NotifyOwner)]
        assert len(status) == 1 and status[0].startswith(want), status
        assert "STATE" not in status[0] and "pending" not in status[0]


def test_not_a_name_is_said_so_in_both_tables(monkeypatch):
    e = opened()
    asked(e, "Lovely. What name shall I put it under?", "guest_name")
    for lang, want in (("uk", "Це не схоже на ім'я. Напиши тільки ім'я латиницею, як його сказати менеджеру "
                              "(наприклад, Yaroslav)."),
                       ("ru", "Это не похоже на имя. Напиши только имя латиницей, как его сказать менеджеру "
                              "(например, Yaroslav).")):
        monkeypatch.setenv("OWNER_LANG", lang)
        acts = e.handle(Event("owner_answer", key="guest_name", text="Yaroslav +44 7700 900123"))
        assert [a.question for a in acts if isinstance(a, AskOwner)] == [want]
