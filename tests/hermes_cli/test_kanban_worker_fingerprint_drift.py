"""A same-host drifted worker fingerprint still identifies our live worker (#118326).

``gateway.status.get_process_start_time`` readings drift by ~1 s between the claim-time and a later
liveness read — on macOS after a sleep/wake cycle (``kern.boottime`` adjustment, #117505). Every
sibling reconciler (delivery ledger, API-server durable runs, async delegation, ``cron.executions``)
routes that comparison through ``gateway.status.start_time_fingerprints_match`` with the shared
drift tolerance. ``kanban_db_dispatch._pid_recycled`` did NOT: it compared the composite
``"<epoch>|<start time>"`` fingerprint with exact string equality, so a drifted-but-live worker read
as *recycled*. ``_terminate_reclaimed_worker`` then reported ``terminated=True`` with
``pid_recycled=True`` and deliberately sent **no signal**, ``_worker_survived_termination`` saw a
terminated worker and released the claim — spawning a duplicate worker beside the live one.
"""

from __future__ import annotations

import os
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from gateway.status import get_process_start_time

# psutil centiseconds; the shared tolerance is START_TIME_DRIFT_TOLERANCE = 200 (2 s).
RECORDED_START = 178864182760


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def drifted(monkeypatch):
    """Our own live PID whose fingerprint reads back ~1 s later than it was recorded (sleep/wake)."""
    import gateway.status as status
    from gateway import drain_control

    epoch = drain_control.current_instantiation_epoch()
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: RECORDED_START + 100)
    monkeypatch.setattr(
        kbd, "_process_fingerprint", lambda pid: f"{epoch}|{RECORDED_START + 100}"
    )
    return f"{epoch}|{RECORDED_START}"


def _claimed_running(conn, *, pid: int, started_at) -> str:
    tid = kb.create_task(conn, title="job", assignee="worker")
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid)
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_started_at = ?, started_at = ?, claim_expires = ? WHERE id = ?",
            (started_at, old, old, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (old, tid),
        )
    return tid


# --- the comparator itself -------------------------------------------------------------------


def test_drifted_fingerprint_is_not_a_recycled_pid(drifted):
    """The live worker whose start time drifted ~1 s is still our worker."""
    assert kbd._worker_alive(os.getpid(), drifted) is True
    assert kbd._pid_recycled(os.getpid(), drifted) is False


def test_drifted_worker_is_not_reported_as_terminated_without_a_signal(drifted):
    """The bug's exact signature: ``terminated: True`` with nothing signalled.

    ``pid_recycled=True`` must never be reported for a drifted live worker — that is what let the
    stale-claim reaper release a live worker's claim.
    """
    termination = kbd._terminate_reclaimed_worker(
        os.getpid(), kb._claimer_id(), signal_fn=lambda pid, sig: None, started_at=drifted
    )
    assert not termination.get("pid_recycled")
    assert not termination.get("signal_refused")


def test_released_claim_is_held_beside_a_drifted_live_worker(board, drifted):
    """End-to-end: the stale-claim sweep must NOT release a live (drifted) worker's claim."""
    conn = board
    tid = _claimed_running(conn, pid=os.getpid(), started_at=drifted)

    assert kb.release_stale_claims(conn) == 0
    assert kb.get_task(conn, tid).status == "running"
    assert "claim_extended" in [e.kind for e in kb.list_events(conn, tid)]


# --- the tolerance must not swallow a genuine identity change ---------------------------------


def test_fingerprint_from_another_boot_is_still_recycled(drifted, monkeypatch):
    """Same drifted start time, different instantiation epoch (a reboot): still a stranger."""
    foreign = "deadbeef-boot:1|" + str(RECORDED_START + 100)
    assert kbd._pid_recycled(os.getpid(), foreign) is True


def test_fingerprint_beyond_the_tolerance_is_still_recycled(drifted):
    """A start time far outside the 2 s tolerance (real PID reuse) stays recycled."""
    stale = drifted.rsplit("|", 1)[0] + "|" + str(RECORDED_START + 900_000)
    assert kbd._pid_recycled(os.getpid(), stale) is True


def test_unreadable_fingerprint_still_reads_as_foreign(monkeypatch):
    """A live PID whose fingerprint can no longer be read stays un-signallable (fail closed)."""
    import gateway.status as status

    from gateway import drain_control

    epoch = drain_control.current_instantiation_epoch()
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: None)
    monkeypatch.setattr(kbd, "_process_fingerprint", lambda pid: None)
    assert kbd._pid_recycled(os.getpid(), f"{epoch}|{RECORDED_START}") is True


def test_unverified_marker_is_still_foreign():
    assert kbd._pid_recycled(os.getpid(), kbd.UNVERIFIED_WORKER_FINGERPRINT) is True


def test_legacy_null_fingerprint_is_never_recycled():
    assert kbd._pid_recycled(os.getpid(), None) is False


def test_live_fingerprint_on_this_host_matches_exactly():
    """Sanity: an un-drifted reading of a real live process still matches itself."""
    recorded = get_process_start_time(os.getpid())
    assert recorded is not None
    fingerprint = kbd._process_fingerprint(os.getpid())
    assert fingerprint is not None
    assert kbd._pid_recycled(os.getpid(), fingerprint) is False