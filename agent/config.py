"""Settings, secrets and the safety checks that decide who the agent may talk to."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

from agent import clock

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
STATE_DIR = ROOT / "state"
RUNS_DIR = ROOT / "runs"

LIVE_MANAGER = "vlad_dev_7"
ALLOWED_TEST_PEERS = {"noffybtw"}
ON_BEHALF_OF = "Yaroslav"
LIVE_LATEST = (16, 30)  # London; later than this a live run would book a slot that is about to pass

PRIMARY_MODEL = "openai/gpt-oss-120b"
FALLBACK_MODEL = "qwen/qwen3.8-27b"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


@dataclass
class Timings:
    """Seconds. Sim and tests use the same numbers on a virtual clock."""
    debounce_quiet: float = 5.0
    debounce_max: float = 15.0
    silence_first: float = 10 * 60
    silence_second: float = 30 * 60
    owner_reping: float = 3 * 60
    owner_wait: float = 10 * 60
    brain_retry: float = 30.0
    typing_per_char: float = 0.03
    typing_max: float = 4.0


@dataclass
class Secrets:
    """Values from .env. repr() shows only the key names."""
    values: dict = field(default_factory=dict, repr=False)

    def get(self, key: str) -> str | None:
        return self.values.get(key) or None

    def all_values(self) -> list[str]:
        return [v for v in self.values.values() if v and len(v) >= 6]

    def __repr__(self) -> str:
        return f"Secrets(keys={sorted(self.values)})"


def load_secrets(path: Path = ENV_PATH) -> Secrets:
    return Secrets(dict(dotenv_values(path)) if path.exists() else {})


class SafetyError(Exception):
    pass


def norm_username(name: str | None) -> str:
    return (name or "").strip().lstrip("@").lower()


def check_safety(mode: str, peer: str | None, live_confirm: str | None, now: datetime | None = None,
                 now_overridden: bool = False) -> str:
    """Returns the manager username this run may talk to, or raises SafetyError.

    test: only peers in ALLOWED_TEST_PEERS. live: only LIVE_MANAGER, the username typed again in --live-confirm,
    the real clock, and not after 16:30 London.
    """
    peer_n = norm_username(peer)
    if mode in ("sim", "dry"):
        return peer_n
    if mode == "test":
        if peer_n == LIVE_MANAGER:
            raise SafetyError("the real manager is reachable only in live mode")
        if peer_n not in ALLOWED_TEST_PEERS:
            raise SafetyError(f"test mode talks only to {sorted(ALLOWED_TEST_PEERS)}, not {peer_n!r}")
        return peer_n
    if mode == "live":
        peer_n = peer_n or LIVE_MANAGER
        if peer_n != LIVE_MANAGER:
            raise SafetyError(f"live mode talks only to {LIVE_MANAGER}")
        if norm_username(live_confirm) != LIVE_MANAGER:
            raise SafetyError(f"live mode needs --live-confirm {LIVE_MANAGER}")
        if now_overridden:
            raise SafetyError("live mode runs on the real clock, drop --now")
        ln = clock.london_now(now)
        if (ln.hour, ln.minute) > LIVE_LATEST:
            raise SafetyError(f"it is {ln:%H:%M} in London; live mode refuses after "
                              f"{LIVE_LATEST[0]}:{LIVE_LATEST[1]:02d}")
        return peer_n
    raise SafetyError(f"unknown mode {mode!r}")
