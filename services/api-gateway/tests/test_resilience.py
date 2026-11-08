import threading
import time

import pytest
from app.resilience import (
    CircuitBreaker,
    CircuitOpen,
    DeadlineExceeded,
    Guard,
    Saturated,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_breaker_opens_after_consecutive_failures():
    breaker = CircuitBreaker(failure_threshold=3, reset_after=10, clock=FakeClock())
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state() == "closed"
    breaker.record_failure()
    assert breaker.state() == "open"
    assert breaker.allow() is False


def test_success_resets_the_failure_count():
    breaker = CircuitBreaker(3, 10, FakeClock())
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state() == "closed"


def test_half_open_allows_one_trial_then_closes_on_success():
    clock = FakeClock()
    breaker = CircuitBreaker(2, 10, clock)
    breaker.record_failure()
    breaker.record_failure()
    clock.now = 10.0
    assert breaker.state() == "half_open"
    assert breaker.allow() is True
    assert breaker.allow() is False
    breaker.record_success()
    assert breaker.state() == "closed"
    assert breaker.allow() is True


def test_failed_trial_reopens_the_circuit():
    clock = FakeClock()
    breaker = CircuitBreaker(2, 10, clock)
    breaker.record_failure()
    breaker.record_failure()
    clock.now = 10.0
    assert breaker.allow() is True
    breaker.record_failure()
    clock.now = 15.0
    assert breaker.state() == "open"
    clock.now = 20.0
    assert breaker.state() == "half_open"


def test_a_lost_trial_does_not_wedge_the_breaker():
    clock = FakeClock()
    breaker = CircuitBreaker(1, 10, clock)
    breaker.record_failure()
    clock.now = 10.0
    assert breaker.allow() is True
    assert breaker.allow() is False
    clock.now = 20.0
    assert breaker.allow() is True


def test_guard_enforces_a_hard_deadline_and_counts_a_failure():
    guard = Guard(CircuitBreaker(2, 10), max_inflight=4)
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        guard.run(lambda: time.sleep(0.5), budget=0.05)
    assert time.monotonic() - started < 0.3
    with pytest.raises(DeadlineExceeded):
        guard.run(lambda: time.sleep(0.5), budget=0.05)
    assert guard.breaker.state() == "open"
    with pytest.raises(CircuitOpen):
        guard.run(lambda: "never runs", budget=0.05)
    guard.close()


def test_guard_sheds_load_when_a_dependency_has_too_many_stuck_calls():
    guard = Guard(CircuitBreaker(10, 10), max_inflight=1)
    release = threading.Event()
    with pytest.raises(DeadlineExceeded):
        guard.run(release.wait, budget=0.05)
    with pytest.raises(Saturated):
        guard.run(lambda: "x", budget=0.05)
    release.set()
    time.sleep(0.1)
    assert guard.run(lambda: "ok", budget=0.5) == "ok"
    guard.close()


def test_bypass_breaker_still_runs_when_the_circuit_is_open():
    guard = Guard(CircuitBreaker(1, 10), max_inflight=2)
    guard.breaker.record_failure()
    with pytest.raises(CircuitOpen):
        guard.run(lambda: "x", budget=0.5)
    assert guard.run(lambda: "x", budget=0.5, bypass_breaker=True) == "x"
    guard.close()


def test_exceptions_from_the_call_propagate_unchanged():
    guard = Guard(CircuitBreaker(3, 10), max_inflight=2)

    def boom():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        guard.run(boom, budget=0.5)
    guard.close()
