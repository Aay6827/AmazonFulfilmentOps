"""Deadlines, circuit breakers and load shedding for downstream calls."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor


class CircuitOpen(Exception):
    """The circuit breaker is open, so the call was skipped."""


class Saturated(Exception):
    """Too many calls to this dependency are already in flight."""


class DeadlineExceeded(Exception):
    """The call did not finish within its budget."""


class CircuitBreaker:
    def __init__(self, failure_threshold=3, reset_after=10.0, clock=time.monotonic):
        self._threshold = failure_threshold
        self._reset_after = reset_after
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at = None
        self._trial_started = None

    def allow(self):
        with self._lock:
            if self._opened_at is None:
                return True
            now = self._clock()
            if now - self._opened_at < self._reset_after:
                return False
            # Half-open: let one trial call through. A trial whose outcome is never
            # recorded does not wedge the breaker; another is granted after a reset period.
            if self._trial_started is None or now - self._trial_started >= self._reset_after:
                self._trial_started = now
                return True
            return False

    def abandon_trial(self):
        with self._lock:
            self._trial_started = None

    def record_success(self):
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._trial_started = None

    def record_failure(self):
        with self._lock:
            self._failures += 1
            self._trial_started = None
            if self._opened_at is not None or self._failures >= self._threshold:
                self._opened_at = self._clock()

    def state(self):
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if self._clock() - self._opened_at >= self._reset_after:
                return "half_open"
            return "open"


class Guard:
    """Runs calls in a bounded thread pool so a stalled call cannot stall the caller."""

    def __init__(self, breaker, max_inflight=16):
        self.breaker = breaker
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._pool = ThreadPoolExecutor(
            max_workers=max_inflight, thread_name_prefix="downstream"
        )

    def run(self, fn, budget, bypass_breaker=False):
        if not bypass_breaker and not self.breaker.allow():
            raise CircuitOpen
        if not self._slots.acquire(blocking=False):
            if not bypass_breaker:
                self.breaker.abandon_trial()
            raise Saturated
        future = self._pool.submit(fn)
        future.add_done_callback(lambda _done: self._slots.release())
        try:
            return future.result(timeout=budget)
        except TimeoutError:
            self.breaker.record_failure()
            raise DeadlineExceeded from None

    def close(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
