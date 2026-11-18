"""Real Groq model on one scenario. Skipped by default; run with: python -m pytest -m groq (about 6k tokens)."""
import pytest

from agent.brain import GroqBrain
from agent.config import load_secrets
from sim import harness
from sim.scenarios import BY_NAME

pytestmark = pytest.mark.groq


def test_happy_with_the_real_model(tmp_path):
    key = load_secrets().get("GROQ_API_KEY")
    if not key:
        pytest.skip("no GROQ_API_KEY")
    res = harness.run(BY_NAME["happy"], GroqBrain(key, max_wait=65), tmp_path, echo=lambda s: None, scripted=False)
    assert res.problems == []
    assert res.engine.phase == "confirmed"
