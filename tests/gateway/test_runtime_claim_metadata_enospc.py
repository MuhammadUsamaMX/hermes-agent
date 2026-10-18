"""Startup runtime-claim metadata must survive ENOSPC instead of killing the gateway (#122720).

The flock / ``O_EXCL`` inode is the mutual-exclusion mechanism; its JSON body is diagnostic
metadata only, and every strict reader already fails closed on an empty or torn record
(``status._read_json_file``). So when the filesystem holding ``HERMES_HOME`` is full, losing
the *metadata* must not lose the *claim*: a supervisor with ``Restart=always`` turned the
resulting exit 1 into an unbounded restart loop (73 restarts in 12 minutes) instead of leaving
one live gateway running degraded.

Covers both startup metadata-write paths:
  - ``runtime_ownership.ProfileOwnership.reserve`` (the ``gateway.lock`` record), and
  - ``status._write_json_excl`` (the ``gateway.pid`` record).

Non-ENOSPC write errors must keep their existing behaviour: still raised, still unwound.
"""

import errno
import os as _os
import pytest

from gateway import runtime_ownership, status

# Captured before any monkeypatching: ``os`` is a shared module, so patching ``fdopen`` on it makes
# a later ``os.fdopen`` lookup recurse into the patch itself.
_REAL_FDOPEN = _os.fdopen


class _FlushRaisesEnospc:
    """Delegates a file handle exactly, except that ``flush()`` reports a full disk.

    ``flush()`` is where the reported crash surfaced: the bytes were already buffered, so this
    reproduces "write accepted, durability rejected" rather than a rejected write.
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def flush(self):
        raise OSError(errno.ENOSPC, "No space left on device")


def _enospc_fdopen(monkeypatch, module) -> None:
    """Make ``module``'s ``os.fdopen`` yield handles whose ``flush()`` reports a full disk."""
    monkeypatch.setattr(
        module.os, "fdopen",
        lambda fd, *a, **k: _FlushRaisesEnospc(_REAL_FDOPEN(fd, *a, **k)),
    )


def _enospc(*_args, **_kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


def _eacces(*_args, **_kwargs):
    raise OSError(errno.EACCES, "Permission denied")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An isolated HERMES_HOME with a fresh ownership singleton (module-level state)."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(runtime_ownership, "process_ownership", runtime_ownership.ProfileOwnership())
    yield hermes_home


# --- gateway.lock metadata (runtime_ownership.reserve) -------------------------------


def test_reserve_keeps_claim_when_lock_metadata_write_hits_enospc(home, monkeypatch):
    """ENOSPC while publishing the lock record must NOT unwrap the flock we already hold."""
    monkeypatch.setattr(runtime_ownership.json, "dump", _enospc)

    runtime_ownership.process_ownership.reserve([home])

    assert runtime_ownership.process_ownership.owns(runtime_ownership.canonical_home(home)), (
        "the runtime claim was released even though only its metadata failed to write"
    )
    # The exclusive claim is still real: the OS lock survives, not just our bookkeeping.
    assert status.is_gateway_runtime_lock_active(home / "gateway.lock")


def test_reserve_keeps_claim_when_lock_record_flush_hits_enospc(home, monkeypatch):
    """The reported failure was at ``flush()``, after the bytes were buffered."""
    _enospc_fdopen(monkeypatch, runtime_ownership)

    runtime_ownership.process_ownership.reserve([home])

    assert runtime_ownership.process_ownership.owns(runtime_ownership.canonical_home(home))
    assert status.is_gateway_runtime_lock_active(home / "gateway.lock")


def test_reserve_still_raises_non_enospc_metadata_errors(home, monkeypatch):
    """A permissions failure is not a degraded-but-alive case: it must still propagate."""
    monkeypatch.setattr(runtime_ownership.json, "dump", _eacces)

    with pytest.raises(OSError) as excinfo:
        runtime_ownership.process_ownership.reserve([home])

    assert excinfo.value.errno == errno.EACCES
    assert not runtime_ownership.process_ownership.owns(runtime_ownership.canonical_home(home))


def test_reserve_still_refuses_a_lock_already_held_elsewhere(home, monkeypatch):
    """Mutual exclusion is the point of the claim: a real conflict still unwinds."""
    rival = runtime_ownership.ProfileOwnership()
    try:
        rival.reserve([home])
        monkeypatch.setattr(runtime_ownership.json, "dump", _enospc)

        with pytest.raises(runtime_ownership.OwnershipConflict):
            runtime_ownership.process_ownership.reserve([home])

        assert not runtime_ownership.process_ownership.owns(runtime_ownership.canonical_home(home))
    finally:
        rival.close()


# --- gateway.pid metadata (status._write_json_excl) ----------------------------------


def test_write_pid_file_survives_enospc_on_close_flush(home, monkeypatch):
    """``_write_json_excl`` writes through ``os.fdopen``; the close-time flush can raise ENOSPC."""
    _enospc_fdopen(monkeypatch, status)

    status.write_pid_file()  # must not raise: startup continues in a degraded state


def test_write_pid_file_failure_leaves_readers_failing_closed(home, monkeypatch):
    """The unwritten record must make strict readers fail closed, never raise or misreport.

    An empty/torn ``gateway.pid`` is the accepted degraded state; what must not happen is a reader
    raising on it, or resolving a *different* live gateway from it.
    """
    monkeypatch.setattr(status.json, "dump", _enospc)

    status.write_pid_file()

    # Fails closed rather than raising, and never invents a foreign live PID.
    assert status._read_pid_record() is None
    assert status.get_running_pid() is None


def test_write_pid_file_still_raises_non_enospc_errors(home, monkeypatch):
    """Non-ENOSPC keeps the old contract: raise, and unlink the partial file."""
    monkeypatch.setattr(status.json, "dump", _eacces)

    with pytest.raises(OSError) as excinfo:
        status.write_pid_file()

    assert excinfo.value.errno == errno.EACCES
    assert not status._get_pid_path().exists()


def test_write_pid_file_still_propagates_file_exists_race(home):
    """``O_EXCL`` is the second mutual-exclusion mechanism; FileExistsError must survive."""
    pid_path = status._get_pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text('{"pid": 1}', encoding="utf-8")

    with pytest.raises(FileExistsError):
        status.write_pid_file()


# --- the full startup seam -----------------------------------------------------------


def test_claim_pid_file_startup_completes_under_enospc(home, monkeypatch):
    """``_start_gateway_claim_pid_file`` is the real call site: it must not crash-loop.

    This is the regression the issue reported: ENOSPC escaped the claim and exited 1, which a
    ``Restart=always`` supervisor turned into an unbounded restart loop.
    """
    from gateway import run_bootstrap

    monkeypatch.setattr(status.json, "dump", _enospc)
    monkeypatch.setattr(runtime_ownership.json, "dump", _enospc)

    assert run_bootstrap._start_gateway_claim_pid_file() is True
    assert runtime_ownership.process_ownership.owns(runtime_ownership.canonical_home(home))


def test_startup_still_loses_to_a_real_running_gateway(home, monkeypatch):
    """Degrading on ENOSPC must not degrade a genuine conflict into a false success."""
    from gateway import run_bootstrap

    rival = runtime_ownership.ProfileOwnership()
    try:
        rival.reserve([home])
        assert run_bootstrap._start_gateway_claim_pid_file() is False
    finally:
        rival.close()