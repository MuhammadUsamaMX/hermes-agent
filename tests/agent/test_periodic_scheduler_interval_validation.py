"""Regression tests for #119219: ``PeriodicScheduler`` must refuse intervals that
cannot represent a valid periodic delay.

A zero or negative interval is already due when it is queued, so the handle is
re-dispatched the moment its body returns — pinning the shared scheduler thread
to one callback.  ``NaN``/``+-inf`` corrupt the heap ordering (``time.monotonic()
+ handle._interval``), so they are rejected on the same rule: the stored interval
must be finite and strictly positive.
"""

import math
import time

import pytest

from agent.periodic_scheduler import PeriodicScheduler, schedule

_INVALID = [0, 0.0, -0.0, -1, -0.5, math.nan, math.inf, -math.inf]


@pytest.mark.parametrize("interval", _INVALID)
def test_schedule_rejects_invalid_interval(interval):
    scheduler = PeriodicScheduler()
    with pytest.raises(ValueError):
        scheduler.schedule(lambda: True, interval)


@pytest.mark.parametrize("interval", _INVALID)
def test_module_level_schedule_rejects_invalid_interval(interval):
    with pytest.raises(ValueError):
        schedule(lambda: True, interval)


@pytest.mark.parametrize("interval", _INVALID)
def test_rejection_queues_nothing_and_starts_no_thread(interval):
    """A refused interval must not reach the heap or boot the timer thread."""
    scheduler = PeriodicScheduler()
    with pytest.raises(ValueError):
        scheduler.schedule(lambda: True, interval)
    assert scheduler._heap == []
    assert scheduler._thread is None


def test_rejection_leaves_scheduler_usable():
    """Rejecting one bad call must not poison later, valid scheduling."""
    scheduler = PeriodicScheduler()
    with pytest.raises(ValueError):
        scheduler.schedule(lambda: True, 0)
    fired = []
    handle = scheduler.schedule(lambda: fired.append(1) or False, 0.01)
    try:
        stop_at = time.monotonic() + 3.0
        while not fired and time.monotonic() < stop_at:
            time.sleep(0.005)
        assert fired, "scheduler stayed dead after a rejected schedule() call"
    finally:
        handle.cancel(wait=1.0)


def test_schedule_accepts_valid_positive_interval():
    scheduler = PeriodicScheduler()
    handle = scheduler.schedule(lambda: False, 3600.0)
    try:
        assert not handle.cancelled
        assert handle._interval == 3600.0
    finally:
        handle.cancel(wait=1.0)


def test_tiny_but_positive_interval_is_allowed():
    """The lower bound is > 0, not some minimum: 0.01 stays legal."""
    scheduler = PeriodicScheduler()
    handle = scheduler.schedule(lambda: False, 0.01)
    handle.cancel(wait=1.0)
