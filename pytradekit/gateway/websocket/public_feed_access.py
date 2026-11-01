"""Opt-in, bounded lock access for constant-time public feed snapshots."""

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from pytradekit.utils.exceptions import (
    PublicFeedReadCancelled,
    PublicFeedReadTimeout,
)
from pytradekit.utils.time_handler import get_monotonic_timestamp_ns


_MAX_DEADLINE_NS = (1 << 63) - 1
_LOCK_WAIT_SLICE_NS = 10_000_000
_NANOSECONDS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True)
class PublicFeedReadBudget:
    """An absolute PTK monotonic deadline and optional cancellation predicate.

    Args:
        deadline_ns: Integer in [0, 2**63 - 1], using
            ``time_handler.get_monotonic_timestamp_ns`` rather than wall time.
        cancelled: Nonblocking, side-effect-free callable returning exactly bool,
            such as ``threading.Event.is_set``. Called only outside the state lock.

    A blocking predicate cannot be interrupted by this budget. Reusing a budget
    preserves its original deadline; each read never renews it.
    Cancellation is sampled: keep it latched until the read completes. Only an
    observed True can reject a result; set-then-clear between checks can be missed.
    """

    deadline_ns: int
    cancelled: Optional[Callable[[], bool]] = None

    def __post_init__(self) -> None:
        if type(self.deadline_ns) is not int:
            raise TypeError('deadline_ns must be an integer, not bool or float')
        if not 0 <= self.deadline_ns <= _MAX_DEADLINE_NS:
            raise ValueError('deadline_ns must be between 0 and 2**63 - 1')
        if self.cancelled is not None and not callable(self.cancelled):
            raise TypeError('cancelled must be a nonblocking callable or None')


def _check_deadline(budget: PublicFeedReadBudget) -> int:
    remaining_ns = budget.deadline_ns - get_monotonic_timestamp_ns()
    if remaining_ns <= 0:
        raise PublicFeedReadTimeout(f'deadline_ns={budget.deadline_ns}')
    return remaining_ns


def _check_budget(budget: PublicFeedReadBudget) -> int:
    _check_deadline(budget)
    if budget.cancelled is not None:
        cancelled = budget.cancelled()
        if type(cancelled) is not bool:
            raise TypeError('cancelled predicate must return bool')
        if cancelled:
            raise PublicFeedReadCancelled(f'deadline_ns={budget.deadline_ns}')
    return _check_deadline(budget)


def _acquire_with_budget(lock: Any, budget: PublicFeedReadBudget) -> None:
    _check_budget(budget)
    if lock.acquire(blocking=False):
        return
    while True:
        remaining_ns = _check_budget(budget)
        wait_ns = min(remaining_ns, _LOCK_WAIT_SLICE_NS)
        if lock.acquire(timeout=wait_ns / _NANOSECONDS_PER_SECOND):
            return


@contextmanager
def _public_feed_read_lock(
    lock: Any, read_budget: Optional[PublicFeedReadBudget],
) -> Iterator[None]:
    """Release before final validation; keep snapshot exceptions primary.

    Only the trusted monotonic clock is consulted under the lock. In particular,
    the cancellation predicate must never run inside the critical section.
    """
    if read_budget is None:
        with lock:
            yield
        return
    if not isinstance(read_budget, PublicFeedReadBudget):
        raise TypeError('read_budget must be PublicFeedReadBudget or None')
    _acquire_with_budget(lock, read_budget)
    try:
        _check_deadline(read_budget)
        yield
    finally:
        lock.release()
    _check_budget(read_budget)
