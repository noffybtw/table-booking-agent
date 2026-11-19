"""The gate: what the employer reads (decisions.md, README) has no Cyrillic, whatever OWNER_LANG the owner uses."""
import json
import re
from pathlib import Path

import pytest

from agent.brain import FakeBrain
from sim import harness
from sim.scenarios import SCENARIOS

ROOT = Path(__file__).resolve().parent.parent
CYRILLIC = re.compile(r"[Ѐ-ԯ]")


def cyrillic_lines(path: Path) -> list[str]:
    return [f"{path.name}:{i}: {line}" for i, line in enumerate(path.read_text().splitlines(), 1)
            if CYRILLIC.search(line)]


@pytest.mark.parametrize("lang", ["uk", "ru"])
def test_no_cyrillic_in_any_decision_log(lang, tmp_path, monkeypatch):
    monkeypatch.setenv("OWNER_LANG", lang)
    asked = []
    for scn in SCENARIOS:
        rd = tmp_path / scn.name
        harness.run(scn, FakeBrain(), rd, echo=lambda s: None)
        assert cyrillic_lines(rd / "decisions.md") == [], scn.name
        asked += [json.loads(r) for r in (rd / "events.jsonl").read_text().splitlines()
                  if json.loads(r)["kind"] == "ask_owner"]
    # the owner still gets the questions in the chosen language, kept in events.jsonl
    name_q = next(r["question"] for r in asked if r["key"] == "guest_name")
    assert name_q.startswith("На яке ім'я" if lang == "uk" else "На какое имя")


def test_no_cyrillic_in_readme():
    assert cyrillic_lines(ROOT / "README.md") == []


def test_the_gate_fails_on_a_planted_russian_line(tmp_path):
    bad = tmp_path / "decisions.md"
    bad.write_text("# Decision log\n\n- 08:00:30 OWNER asked (guest_name): \"На какое имя записать бронь?\"\n")
    assert len(cyrillic_lines(bad)) == 1


def test_owner_answers_parse_in_every_language_whatever_owner_lang(monkeypatch):
    from agent import policy
    for lang in ("uk", "ru"):
        monkeypatch.setenv("OWNER_LANG", lang)
        for yes in ("так", "да", "yes", "підходить", "хорошо"):
            assert policy.time_answer(yes, "18:30", "17:00") == "accept", (lang, yes)
        for no in ("ні", "нет", "no", "тільки 17:00", "только 17:00"):
            assert policy.time_answer(no, "18:30", "17:00") == "only", (lang, no)
