import json

from agent import clock
from agent.events import Log, SendText
from agent.runlog import RunLog, mask_phone


def test_mask_phone():
    assert mask_phone("+44 7700 900123") == "+44 77** ***123"
    assert mask_phone("at 17:00 for two") == "at 17:00 for two"


def test_secrets_never_written(tmp_path):
    secret = "gsk_live_abcdef123456"
    log = RunLog(tmp_path, lambda: clock.parse_now("2026-11-19T08:00"), secrets=[secret])
    log.record(Log("model", {"error": f"401 bad key {secret}"}))
    log.record(Log("in", {"n": 1, "kind": "text", "text": f"my key is {secret}"}))
    log.record(Log("decision", {"understood": "x", "decided": "y"}))
    log.sent(SendText("Thank you.", "why"), "text")
    log.close()
    for f in ("decisions.md", "events.jsonl"):
        assert secret not in (tmp_path / f).read_text()
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert {r["kind"] for r in rows} >= {"model", "in", "decision", "out"}
    assert all(r["london"] == "08:00:00" for r in rows)


def test_one_line_per_message(tmp_path):
    log = RunLog(tmp_path, lambda: clock.parse_now("2026-11-19T08:00"))
    log.record(Log("in", {"n": 1, "kind": "text", "text": "Hello"}))
    log.record(Log("in", {"n": 2, "kind": "voice", "text": "Yes 17:00 is fine"}))
    log.record(Log("decision", {"understood": "agreed", "decided": "restate"}))
    log.sent(SendText("Lovely.", "agreed", 1), "voice")
    lines = [l for l in (tmp_path / "decisions.md").read_text().splitlines() if l.startswith("- ")]
    assert len(lines) == 3
    assert "understood: agreed" in lines[1] and "manager (voice)" in lines[1]
    assert "(voice note + caption)" in lines[2]


def test_braces_are_doubled_only_when_the_managers_words_go_in(tmp_path):
    log = RunLog(tmp_path, lambda: clock.parse_now("2026-11-19T08:00"))
    log.record(Log("in", {"n": 1, "kind": "media", "text": ""}))
    log.record(Log("decision", {"understood": "a photo {menu}", "decided": "ask what it means"}))
    log.record(Log("in", {"n": 2, "kind": "text", "text": "Is {this} fine?"}))
    log.record(Log("decision", {"understood": "asks about {this}", "decided": "answer"}))
    lines = [l for l in (tmp_path / "decisions.md").read_text().splitlines() if l.startswith("- ")]
    assert "(no text) | understood: a photo {menu} |" in lines[0] and "{{" not in lines[0]
    assert '"Is {this} fine?" | understood: asks about {this} |' in lines[1] and "{{" not in lines[1]
