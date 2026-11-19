from agent.brain import FakeBrain
from agent.render import messages, render
from sim import harness
from sim.scenarios import BY_NAME


def test_render_shows_only_the_chat(tmp_path):
    harness.run(BY_NAME["name_phone"], FakeBrain(), tmp_path, echo=lambda s: None)
    msgs = messages(tmp_path / "events.jsonl", "Agent", "Manager")
    assert [m["side"] for m in msgs][:3] == ["out", "in", "out"]
    assert not any("Напоминаю" in m["text"] or "На какое имя" in m["text"] for m in msgs)  # owner side stays out
    assert msgs[-1]["voice"] and msgs[0]["day"] == "Thursday 19 November" and msgs[0]["time"] == "08:00"
    png = render(tmp_path, title="<b>sim</b>")
    assert png.stat().st_size > 10_000
    assert "&lt;b&gt;sim" in (tmp_path / "transcript.html").read_text()


def test_render_works_from_inside_the_running_loop(tmp_path):
    """cmd_telegram renders while asyncio runs; a direct call there fails with "Sync API inside the asyncio loop"."""
    import asyncio
    harness.run(BY_NAME["happy"], FakeBrain(), tmp_path, echo=lambda s: None)

    async def go():
        return await asyncio.to_thread(render, tmp_path, title="@noffybtw")
    assert asyncio.run(go()).stat().st_size > 10_000
