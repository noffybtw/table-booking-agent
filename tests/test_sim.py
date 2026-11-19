import pytest

from agent import policy
from agent.brain import FakeBrain
from agent.events import SendText, SendVoice
from sim import harness
from sim.scenarios import SCENARIOS


@pytest.mark.parametrize("scn", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_scenario(scn, tmp_path):
    brain = FakeBrain()
    res = harness.run(scn, brain, tmp_path, echo=lambda s: None)
    assert res.problems == []
    assert not brain.queue, "scripted decisions left unused"
    # every text that went out passed the guard on its way
    log = (tmp_path / "decisions.md").read_text()
    assert log.count(" OUT agent #") == len(res.all_sent)
    assert "—" not in log and "–" not in log


def test_blocked_text_is_never_sent(tmp_path):
    res = harness.run(next(s for s in SCENARIOS if s.name == "name_phone"), FakeBrain(), tmp_path, echo=lambda s: None)
    sent = " ".join(t for _, t in res.all_sent)
    assert "Petrenko" not in sent and "900456" not in sent


def test_phone_masked_in_logs(tmp_path):
    harness.run(next(s for s in SCENARIOS if s.name == "name_phone"), FakeBrain(), tmp_path, echo=lambda s: None)
    for name in ("decisions.md", "events.jsonl"):
        text = (tmp_path / name).read_text()
        assert "7700 900123" not in text
    assert "+44 77** ***123" in (tmp_path / "decisions.md").read_text()


def test_nothing_invented_while_waiting(tmp_path):
    res = harness.run(next(s for s in SCENARIOS if s.name == "name_phone"), FakeBrain(), tmp_path, echo=lambda s: None)
    # before the owner answered (steps 0-3) no name and no number went out
    early = " ".join(t for s in res.steps[:4] for _, t in s.sent)
    assert "+44" not in early and "under the name" not in early and "Yaroslav" not in early


@pytest.mark.parametrize("scn", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_owner_never_named_before_the_owner_gives_the_name(scn, tmp_path):
    res = harness.run(scn, FakeBrain(), tmp_path, echo=lambda s: None)
    for s in res.steps:
        if s.step.kind == "owner" and s.step.key == "guest_name":
            break
        if scn.profile and s.step.kind == "manager" and policy.manager_asked_now("guest_name", [s.step.text]):
            break  # the profile gave the name before the start: it goes once the manager asks
        assert not any("yaroslav" in t.lower() for _, t in s.sent), s.sent


def test_reply_naming_the_owner_is_blocked_and_never_sent(tmp_path):
    from agent.events import Log
    res = harness.run(next(s for s in SCENARIOS if s.name == "counter_offer"), FakeBrain(), tmp_path,
                      echo=lambda s: None)
    assert res.problems == []
    sent = " ".join(t for _, t in res.all_sent)
    assert "the time Yaroslav needs" not in sent and "only 17:00 works for us" in sent
    blocked = [a for s in res.steps for a in s.acts if isinstance(a, Log) and a.kind == "guard"
               and any(p.startswith("owner_name") for p in a.data["problems"])]
    assert len(blocked) == 1 and "the time Yaroslav needs" in blocked[0].data["text"]


def test_name_passes_after_the_manager_asked_and_the_owner_answered(tmp_path):
    res = harness.run(next(s for s in SCENARIOS if s.name == "name_phone"), FakeBrain(), tmp_path, echo=lambda s: None)
    assert res.problems == []
    answered = next(s for s in res.steps if s.step.kind == "owner" and s.step.key == "guest_name")
    assert answered.sent == [("text", "Thank you for waiting - the booking can go under Yaroslav.")]


def test_restatement_is_voice_and_code_built(tmp_path):
    res = harness.run(next(s for s in SCENARIOS if s.name == "happy"), FakeBrain(), tmp_path, echo=lambda s: None)
    voice = [a for s in res.steps for a in s.acts if isinstance(a, SendVoice)]
    assert len(voice) == 1 and "restatement built by code" in voice[0].why
    assert res.engine.phase == "confirmed"


def test_stop_command_silences_the_agent(tmp_path):
    from sim.scenarios import M, Scenario, Step, opening
    from agent.brain import D
    scn = Scenario("stop", "owner stops", [opening(), Step("command", "stop", expect={"sends": 0}),
                                            M("Hello? 17:00 for two is fine.", sends=0)])
    res = harness.run(scn, FakeBrain(), tmp_path, echo=lambda s: None)
    assert res.problems == [] and res.engine.stopped


def test_pause_holds_then_resume_answers(tmp_path):
    from sim.scenarios import M, REST, Scenario, Step, opening, AGREE
    from agent.brain import D
    scn = Scenario("pause", "owner pauses", [
        opening(), Step("command", "pause", expect={"sends": 0}),
        M(f"Yes, 17:00 for two is fine. {REST}.", sends=0),
        Step("command", "resume", [D(**AGREE, restaurant_name=REST)], {"sends": 1, "voice": True}),
    ])
    res = harness.run(scn, FakeBrain(), tmp_path, echo=lambda s: None)
    assert res.problems == []


def test_model_down_schedules_one_retry_at_a_time(tmp_path):
    from agent.brain import BrainDown, D
    from sim.scenarios import M, Scenario, opening, silence
    down = BrainDown("429 on both")
    scn = Scenario("down", "long outage", [
        opening(),
        M("Yes, 17:00 for two is fine.", down, sends=1),
        M("Hello?", down, down, sends=0),  # the 30 s retry fires first, then this message: both fail
        silence(0.5, down, sends=0),
        silence(0.5, D("Thank you, could I take the restaurant's name for my note?"), sends=1),
    ])
    brain = FakeBrain()
    res = harness.run(scn, brain, tmp_path, echo=lambda s: None)
    assert res.problems == []
    # a stale retry would make an extra model call, and the strict FakeBrain would fail on it
    assert len(brain.calls) == 6


def test_opening_has_no_self_introduction(tmp_path):
    from agent.brain import D
    from sim.scenarios import Scenario, Step
    draft = "Good morning. I'm an AI assistant - could I book a table for two today at 17:00, please?"
    scn = Scenario("open_ai", "model says AI in the opening", [Step("open", brain=[D(draft)], expect={"sends": 1})])
    res = harness.run(scn, FakeBrain(), tmp_path, echo=lambda s: None)
    assert res.all_sent == [("text", "Good morning. Could I book a table for two today at 17:00, please?")]


def test_pleasantries_after_the_closing_get_no_reply(tmp_path):
    from agent.events import Log
    res = harness.run(next(s for s in SCENARIOS if s.name == "pleasantries"), FakeBrain(), tmp_path,
                      echo=lambda s: None)
    assert res.problems == []
    quiet = [s for s in res.steps if s.step.text in ("You're welcome!", "\U0001F44D", "Bye!")]
    assert len(quiet) == 3
    for s in quiet:
        assert s.sent == []
        decisions = [a for a in s.acts if isinstance(a, Log) and a.kind == "decision"]
        assert len(decisions) == 1 and "no reply: closing already sent" in decisions[0].data["decided"]
    answered = next(s for s in res.steps if s.step.text == "Thanks! Could you spell the name?")
    assert len(answered.sent) == 1
    log = (tmp_path / "decisions.md").read_text()
    assert log.count("no reply: closing already sent") == 3
    # one line per message: the reason sits on the IN line, no separate decision line for the quiet ones
    assert log.count(" IN manager:") == sum(s.step.kind == "manager" for s in res.steps)


def test_closing_template_counts_as_the_closing_reply(tmp_path):
    from agent.brain import D
    from sim.scenarios import AGREE, M, REST, RESTATED, Scenario, opening
    scn = Scenario("template_close", "the CLOSING template is the one closing reply", [
        opening(),
        M(f"Yes, 17:00 for two is fine at {REST}.", D(**AGREE, restaurant_name=REST), **RESTATED),
        M("Thanks!", D(**AGREE), sends=1, says=["Thank you very much"]),
        M("Cheers!", D(**AGREE), sends=0),
        M("You too, bye!", D("Goodbye!", intent="closing"), sends=0),
        M("Sorry, is that under any name?", D("Let me just check the name and come back to you.", action="ask_owner",
                                               intent="asks_info", needs_owner=["guest_name"]),
          sends=1, asks=["guest_name"]),
    ])
    res = harness.run(scn, FakeBrain(), tmp_path, echo=lambda s: None)
    assert res.problems == []


def test_quiet_rule_needs_nothing_pending(tmp_path):
    from agent.brain import D
    from sim.scenarios import AGREE, M, REST, RESTATED, Scenario, opening
    scn = Scenario("pending_close", "a pleasantry while an owner question is open still gets an answer", [
        opening(),
        M(f"Yes, 17:00 for two is fine at {REST}.", D(**AGREE, restaurant_name=REST), **RESTATED),
        M("Lovely, see you then!", D("Thank you, see you at 17:00.", intent="closing"), sends=1),
        M("Oh, and a contact number please.", D("Of course, let me just check that.",
                                                 action="ask_owner", intent="asks_info", needs_owner=["phone"]),
          sends=1, asks=["phone"]),
        M("Thanks!", D("Thank you for bearing with me, I'm still checking.", intent="closing"), sends=1),
    ])
    res = harness.run(scn, FakeBrain(), tmp_path, echo=lambda s: None)
    assert res.problems == []


def test_one_closing_is_owed_when_the_model_stays_quiet(tmp_path):
    from agent.brain import D
    from sim.scenarios import AGREE, M, REST, RESTATED, Scenario, opening
    scn = Scenario("owed_close", "the model returns wait on the first thank-you after the restatement", [
        opening(),
        M(f"Yes, 17:00 for two is fine at {REST}.", D(**AGREE, restaurant_name=REST), **RESTATED),
        M("Great, thanks!", D(action="wait", intent="closing"), sends=1, says=["Thank you very much"]),
        M("Bye!", D(action="wait", intent="closing"), sends=0),
    ])
    res = harness.run(scn, FakeBrain(), tmp_path, echo=lambda s: None)
    assert res.problems == []


def test_voice_reply_naming_the_owner_is_blocked(tmp_path):
    from agent.brain import D
    from sim.scenarios import Scenario, V, opening
    scn = Scenario("voice_name", "a voice reply that names the owner", [
        opening(),
        V("Hi, five o'clock might be tight. Is it a special occasion?",
          D("Yaroslav would prefer 17:00 if at all possible.", intent="question"),
          D("Ideally 17:00, if at all possible. Is there any chance?", intent="question"),
          sends=1, voice=True, blocked=True, never=["Yaroslav"]),
    ])
    res = harness.run(scn, FakeBrain(), tmp_path, echo=lambda s: None)
    assert res.problems == []
    assert res.all_sent[-1] == ("voice", "Ideally 17:00, if at all possible. Is there any chance?")
