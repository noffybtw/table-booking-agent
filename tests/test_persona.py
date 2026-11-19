"""The agent writes as the one booking: no "assistant", no "the guest", unless the manager asks if it is a bot."""
import pytest

from agent import engine as eng, policy
from agent.brain import D
from agent.events import Event, SendText
from test_owner_answers import opened, sends


@pytest.mark.parametrize("draft,sent", [
    ("Good morning. I'm an assistant - could I book a table for two today at 17:00, please?",
     "Good morning. Could I book a table for two today at 17:00, please?"),
    ("Thank you. I'm booking on behalf of my client. Is 17:00 free?", "Thank you. Is 17:00 free?"),
    ("I'll check with the guest and get back to you shortly.", eng.HOLD),
    ("Lovely - let me check with my client. Is 17:00 still free?", "Lovely. Is 17:00 still free?"),
    ("Let me ask the guests and come back to you.", eng.HOLD),
    ("As an assistant I can't say. Could I book for 17:00?", "Could I book for 17:00?"),
])
def test_persona_wording_leaves_the_text(draft, sent):
    e = opened(D(draft, intent="question"))
    acts = e.handle(Event("manager_message", text="Hello, how can I help?"))
    assert sends(acts) == [sent]
    why = next(a.why for a in acts if isinstance(a, SendText))
    assert eng.PERSONA_GUARD in why


@pytest.mark.parametrize("question", ["Are you a bot?", "Is this an AI?", "Am I talking to a real person?",
                                      "Are you human?"])
def test_bot_question_gets_the_honest_answer(question):
    answer = "I'm an AI assistant making this booking. Could I have a table for two today at 17:00?"
    e = opened(D(answer, intent="bot_question"))
    acts = e.handle(Event("manager_message", text=question))
    assert sends(acts) == [answer]
    assert eng.PERSONA_GUARD not in next(a.why for a in acts if isinstance(a, SendText))


def test_templates_have_no_persona_wording():
    for t in (eng.OPENING, eng.HOLD, eng.STILL_CHECKING, eng.SORRY_WAIT, eng.TECH_HOLD, eng.CLOSING, *eng.HOLD_AGAIN,
              eng.FOLLOW_UP_1, eng.FOLLOW_UP_2, eng.FOLLOW_UP_CONFIRM, eng.ASK_RESTAURANT_ALSO):
        assert not policy.PERSONA_RE.search(t), t


def test_not_a_bot_question():
    assert not policy.asks_if_bot("What time would you like?")
    assert not policy.asks_if_bot("We have a table at 17:00.")
