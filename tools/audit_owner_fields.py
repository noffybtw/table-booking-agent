#!/usr/bin/env python3
"""Lists every agent message in runs/*/decisions.md that asks the manager for the name, phone or email.

Those are the owner's to give; the agent must never ask the manager for them. Usage:
  python3 tools/audit_owner_fields.py <runs dir>
Read-only. Exit 0 either way; the last line is the count.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.policy import OWNER_FIELDS, owner_field_questions  # noqa: E402

OUT_RE = re.compile(r'^- ([\d:]+) OUT agent #(\d+)[^:]*: "(.*)" \| why: ')


def audit(runs: Path) -> list[str]:
    found = []
    for log in sorted(runs.glob("*/decisions.md")):
        for line in log.read_text(errors="replace").splitlines():
            m = OUT_RE.match(line)
            if not m:
                continue
            for key, sentence in owner_field_questions(m.group(3), OWNER_FIELDS):
                found.append(f"{log.parent.name} #{m.group(2)} {m.group(1)} {key}: {sentence}")
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not Path(argv[0]).is_dir():
        print(__doc__.strip(), file=sys.stderr)
        return 2
    found = audit(Path(argv[0]))
    for f in found:
        print(f)
    print(f"{len(found)} agent message(s) ask the manager for a field the owner holds")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
