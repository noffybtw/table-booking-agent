"""Transcript PNG of a run, drawn from its events.jsonl in a Telegram-like layout (only what the manager saw).

  python -m agent.render runs/<run-id> [--title "Restaurant manager"]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from jinja2 import Environment

from agent import clock

TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><style>
body { margin: 0; background: #c8d7b9; font: 15px/1.35 -apple-system, "Segoe UI", Roboto, "Noto Sans", sans-serif; }
.top { background: #517da2; color: #fff; padding: 12px 16px; }
.top b { font-size: 16px; } .top small { display: block; opacity: .8; font-size: 12px; }
.chat { padding: 10px 10px 16px; }
.day { text-align: center; margin: 8px 0; } .day span { background: rgba(0,0,0,.25); color: #fff; border-radius: 10px;
  padding: 2px 10px; font-size: 13px; }
.row { display: flex; margin: 4px 0; } .row.out { justify-content: flex-end; }
.b { max-width: 78%; padding: 6px 9px 4px; border-radius: 12px; box-shadow: 0 1px 1px rgba(0,0,0,.15);
  word-wrap: break-word; }
.in .b { background: #fff; border-bottom-left-radius: 4px; } .out .b { background: #effdde; border-bottom-right-radius: 4px; }
.who { font-size: 13px; font-weight: 600; color: #3a8bc6; margin-bottom: 2px; } .out .who { color: #4fae4e; }
.meta { font-size: 11px; color: #8a9aa8; text-align: right; margin-top: 2px; } .out .meta { color: #5fae5c; }
.voice { display: flex; align-items: center; gap: 8px; margin: 2px 0 4px; }
.play { width: 34px; height: 34px; border-radius: 50%; background: #4fae4e; color: #fff; display: flex;
  align-items: center; justify-content: center; font-size: 14px; } .in .play { background: #3a8bc6; }
.wave { height: 18px; flex: 1; min-width: 120px; background: repeating-linear-gradient(90deg, #9fcf9a 0 2px,
  transparent 2px 5px); border-radius: 2px; } .in .wave { background: repeating-linear-gradient(90deg, #a9c8e2 0 2px,
  transparent 2px 5px); }
.cap { color: #222; white-space: pre-wrap; } .media { color: #6b7b88; font-style: italic; } .fail { color: #c0392b; font-size: 12px; }
</style></head><body>
<div class="top"><b>{{ title }}</b><small>{{ subtitle }}</small></div>
<div class="chat">
{% for m in msgs %}
{% if m.day %}<div class="day"><span>{{ m.day }}</span></div>{% endif %}
<div class="row {{ m.side }}"><div class="b">
<div class="who">{{ m.who }}</div>
{% if m.voice %}<div class="voice"><div class="play">&#9654;</div><div class="wave"></div></div>{% endif %}
{% if m.media %}<div class="media">{{ m.text }}</div>{% else %}<div class="cap">{{ m.text }}</div>{% endif %}
{% if m.error %}<div class="fail">not delivered: {{ m.error }}</div>{% endif %}
<div class="meta">{% if m.edited %}edited {% endif %}{{ m.time }}</div>
</div></div>
{% endfor %}
</div></body></html>"""


def messages(events_path: Path, agent_name: str, manager_name: str) -> list[dict]:
    out, last_day = [], None
    for line in events_path.read_text().splitlines():
        r = json.loads(line)
        if r["kind"] not in ("in", "out"):
            continue
        when = clock.london_now(datetime.fromisoformat(r["ts"]))
        day = clock.human_date(when)
        m = {"time": when.strftime("%H:%M"), "day": day if day != last_day else None}
        last_day = day
        if r["kind"] == "in":
            kind = r.get("msg_kind", "text")
            text = r.get("text") or ("(voice note, not transcribed)" if kind == "voice" else "")
            m |= {"side": "in", "who": manager_name, "text": text, "voice": kind == "voice",
                  "media": kind == "media", "edited": kind == "edit"}
        else:
            if r.get("error"):
                m |= {"error": r["error"]}
            m |= {"side": "out", "who": agent_name, "text": r.get("text", ""), "voice": r.get("via") == "voice"}
        out.append(m)
    return out


def render(run_dir: Path, title: str = "Restaurant manager", subtitle: str = "Telegram, times in London",
           agent_name: str = "Booking agent", manager_name: str = "Manager", width: int = 460) -> Path:
    run_dir = Path(run_dir)
    msgs = messages(run_dir / "events.jsonl", agent_name, manager_name)
    html = Environment(autoescape=True).from_string(TEMPLATE).render(msgs=msgs, title=title, subtitle=subtitle)
    (run_dir / "transcript.html").write_text(html)
    png = run_dir / "transcript.png"
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 800}, device_scale_factor=2)
        page.set_content(html)
        page.screenshot(path=str(png), full_page=True)
        browser.close()
    return png


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--title", default="Restaurant manager")
    ap.add_argument("--manager", default="Manager")
    ap.add_argument("--agent", default="Booking agent")
    a = ap.parse_args(argv)
    print(render(Path(a.run_dir), title=a.title, manager_name=a.manager, agent_name=a.agent))


if __name__ == "__main__":
    main()
