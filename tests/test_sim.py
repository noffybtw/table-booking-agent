import pytest

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
    # before the owner answered (steps 0-3) no name other than Yaroslav and no number went out
    early = " ".join(t for s in res.steps[:4] for _, t in s.sent)
    assert "+44" not in early and "under the name" not in early


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
