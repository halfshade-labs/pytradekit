"""Real registration-lock contention; no fabricated positive native receipts.

Only the parent runs these in the fixed offline halfshade container. The owned
holder's .8s safety release lets the old unbounded facade fail without hanging
the suite. Correct completion returns while that same lock is still held.
"""
import threading
import time

import pytest

from .test_native_public_market_receipts import client, completion, denial, owner


class RegistrationHolder:
    def __init__(self, value):
        self.value = value
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self.hold, name='receipt-test-registration-holder')

    def hold(self):
        with self.value._lock:
            self.entered.set()
            self.release.wait(.8)

    def start(self):
        self.thread.start()
        assert self.entered.wait(1), 'holder did not acquire real registration RLock'

    def cleanup(self):
        self.release.set()
        self.thread.join(1)
        assert not self.thread.is_alive(), 'test-owned registration holder leaked'


def assert_bounded_fenced(value, holder, elapsed):
    assert elapsed < .40, f'receipt total budget extended by registration lock: {elapsed:.6f}s'
    assert holder.thread.is_alive(), 'completion only returned after registration lock release'
    assert value._failure is not None
    # Retention until the registry owner can drain is allowed, never unbounded.
    assert len(value._requests) <= 1 and len(value._request_metadata) <= 1


def assert_drained_after_release(value):
    # An ordinary public operation owns registry draining after contention ends.
    # No retry, new request ID, or pipe write is allowed once failed.
    previous = value._next
    assert value.complete_owner(owner(), completion()) == {}
    assert value._next == previous
    assert value._requests == value._request_metadata == {}


def test_receipt_registration_timeout_latches_failure_without_waiting_for_holder():
    value = client()
    holder = RegistrationHolder(value)
    holder.start()
    try:
        started = time.monotonic()
        assert value.complete_owner(owner(), completion()) == {}
        elapsed = time.monotonic() - started
        assert .20 <= elapsed
        assert_bounded_fenced(value, holder, elapsed)
        assert 'registration deadline' in value._failure
        assert value._next == 0 and value._requests == value._request_metadata == {}
    finally:
        holder.cleanup()
    assert_drained_after_release(value)


def test_receipt_reply_finally_cleanup_uses_remaining_total_budget():
    value = client()
    holder = RegistrationHolder(value)

    def deliver_negative_then_hold(data, deadline):
        # A well-formed negative is sufficient to exercise the return/finally
        # path. This does not stand in for a real child's terminal effect.
        value._requests[1].put_nowait(denial())
        holder.start()

    value._send_owner_completion = deliver_negative_then_hold
    try:
        started = time.monotonic()
        result = value.complete_owner(owner(), completion())
        elapsed = time.monotonic() - started
        assert_bounded_fenced(value, holder, elapsed)
        assert result == {}, 'cleanup uncertainty must not return a healthy receipt'
    finally:
        holder.cleanup()
    assert_drained_after_release(value)


@pytest.mark.parametrize('first_failure', [None, 'original transport error'])
def test_receipt_cancellation_during_registration_contention_preserves_identity_and_first(first_failure):
    value = client()
    holder = RegistrationHolder(value)
    cancellation = KeyboardInterrupt('original receipt cancellation')

    def cancel_with_registration_held(data, deadline):
        if first_failure is not None:
            value._fail(first_failure)
        holder.start()
        raise cancellation

    value._send_owner_completion = cancel_with_registration_held
    try:
        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt) as caught:
            value.complete_owner(owner(), completion())
        elapsed = time.monotonic() - started
        assert caught.value is cancellation
        assert_bounded_fenced(value, holder, elapsed)
        assert value._failure == (first_failure or 'native owner completion cancelled')
    finally:
        holder.cleanup()
    assert_drained_after_release(value)
