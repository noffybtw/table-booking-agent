from agent import clock, policy, prompt
from agent.facts import FactStore


def test_other_key_question_does_not_crash():
    f = FactStore.for_task(clock.parse_now("2026-11-19T08:00"))
    q, buttons, contact = policy.owner_question("cancellation", f, "Your client rang us and cancelled.")
    assert "cancellation" in q and buttons == [] and contact is False
    q, _, _ = policy.owner_question("high_chair", f, "Do you need a high chair?")
    assert "high chair" in q


def test_prompt_has_the_cancellation_rule():
    src = open(prompt.__file__, encoding="utf-8").read()
    assert 'needs_owner [\\"cancellation\\"]' in src or 'needs_owner ["cancellation"]' in src
