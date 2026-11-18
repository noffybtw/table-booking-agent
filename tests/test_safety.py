import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import run
from agent import clock
from agent.config import SafetyError, check_safety
from agent.transport_tg import Debouncer, LockError, PeerFilter, SessionLock
from agent.events import Msg


def test_test_mode_refuses_a_peer_outside_the_list(capsys):
    assert run.main(["--mode", "test", "--peer", "someone_else"]) == 2
    assert "refused" in capsys.readouterr().err


def test_test_mode_never_reaches_the_real_manager():
    with pytest.raises(SafetyError):
        check_safety("test", "@vlad_dev_7", None)
    assert check_safety("test", "@NoffyBTW", None) == "noffybtw"


def test_live_needs_matching_confirm(capsys):
    assert run.main(["--mode", "live"]) == 2
    assert run.main(["--mode", "live", "--live-confirm", "vlad_dev"]) == 2
    with pytest.raises(SafetyError):
        check_safety("live", "noffybtw", "noffybtw")


def test_live_refuses_clock_override_and_late_start():
    morning = clock.parse_now("2026-11-19T08:00")
    assert check_safety("live", None, "vlad_dev_7", morning) == "vlad_dev_7"
    with pytest.raises(SafetyError):
        check_safety("live", None, "vlad_dev_7", morning, now_overridden=True)
    with pytest.raises(SafetyError):
        check_safety("live", None, "vlad_dev_7", clock.parse_now("2026-11-19T16:31"))


def test_other_chats_and_old_history_are_ignored():
    start = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)
    f = PeerFilter(peer_id=111, started_at=start)
    later = start + timedelta(seconds=5)
    assert f.accepts(111, False, later)
    assert not f.accepts(222, False, later)          # another chat on the shared account
    assert not f.accepts(111, True, later)           # our own outgoing message
    assert not f.accepts(111, False, start - timedelta(minutes=1))  # history before the opening


def test_debounce_merges_a_burst():
    async def go():
        batches = []

        async def on_batch(items):
            batches.append([m.text for m in items])
        d = Debouncer(on_batch, quiet=0.05, max_wait=0.5)
        for t in ("Hi", "yes 17:00", "for two"):
            d.add(Msg(t))
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.15)
        d.add(Msg("later"))
        await asyncio.sleep(0.15)
        return batches
    assert asyncio.run(go()) == [["Hi", "yes 17:00", "for two"], ["later"]]


def test_debounce_max_wait():
    async def go():
        batches = []

        async def on_batch(items):
            batches.append(len(items))
        d = Debouncer(on_batch, quiet=0.08, max_wait=0.2)
        for _ in range(8):
            d.add(Msg("x"))
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        return batches
    b = asyncio.run(go())
    assert len(b) >= 2 and sum(b) == 8


def test_session_lock_is_exclusive(tmp_path):
    with SessionLock(tmp_path / "session.lock"):
        with pytest.raises(LockError):
            with SessionLock(tmp_path / "session.lock"):
                pass
    with SessionLock(tmp_path / "session.lock"):
        pass


def test_dry_line_parsing():
    assert run.dry_event("Yes, 17:00 is fine", "").kind == "manager_message"
    assert run.dry_event("voice: hello there", "").kind == "manager_voice"
    ev = run.dry_event("edit: 17:30 then", "17:00 then")
    assert ev.kind == "manager_edit" and ev.old_text == "17:00 then"
    ev = run.dry_event("owner phone: +44 7700 900123", "")
    assert ev.kind == "owner_answer" and ev.key == "phone"
    assert run.dry_event("owner: only", "").value == "only"
    assert run.dry_event("/stop", "").kind == "owner_command"
