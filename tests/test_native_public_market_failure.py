"""Terminal scalar diagnostics cannot grant ownership or replace first failure."""
import io
import json
import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pytradekit.ws.native_public_market import NativePublicMarket


def test_native_failure_row_latches_reason_and_wakes_owner_waiter():
    row = {"type": "failure", "complete": False, "reason": "native ingress full",
           "runtime": {"wire_raw_received": 4100, "supplied": 3, "retired_frames": 0},
           "profile": {"actions_read": 8}, "instrumented": True}
    client = NativePublicMarket.__new__(NativePublicMarket)
    client._child = SimpleNamespace(stdout=io.BytesIO((json.dumps(row) + "\n").encode()))
    client._lock = threading.RLock()
    client._failure, client._closed, client._complete = None, False, False
    waiter = queue.Queue(maxsize=1)
    client._requests = {1: waiter}
    client._read()
    assert client._failure == "native owner failure: native ingress full"
    assert client.failure_profile == row
    assert waiter.get_nowait() == {"accepted": False}
    assert client._complete is False


class UnprintableError(Exception):
    def __str__(self):
        raise ValueError("cannot format")


def failure_client():
    client = NativePublicMarket.__new__(NativePublicMarket)
    client._lock, client._write_lock = threading.RLock(), threading.Lock()
    client._failure, client._closed, client._complete = None, False, False
    client._next, client._requests = 0, {}
    client._child = SimpleNamespace(poll=MagicMock(return_value=None))
    return client


@pytest.mark.parametrize("reason", ["", " \t\r\n", queue.Full(), UnprintableError(), "x" * 1000, " " * 1000 + "meaningful"])
def test_failure_diagnostic_is_bounded_nonempty_and_preserves_first(reason):
    client = failure_client()
    empty, full = queue.Queue(maxsize=1), queue.Queue(maxsize=1)
    completed = {"accepted": True, "request": 2}
    full.put_nowait(completed)
    client._requests = {1: empty, 2: full}
    client._fail(reason)
    first = client._failure
    assert isinstance(first, str) and first.strip() and len(first) <= 256
    assert empty.get_nowait() == {"accepted": False}
    assert full.get_nowait() == completed
    client._fail("a later failure")
    assert client._failure == first


@pytest.mark.parametrize("failure,closed", [("", False), (" \t", False), ("original reason", False), (None, True)])
def test_latched_empty_or_meaningful_failure_and_closed_state_reject_new_io(failure, closed):
    client = failure_client()
    client._failure, client._closed = failure, closed
    sent = MagicMock()
    original_send = client.send
    client.send = sent
    assert client.request({"type": "snapshot"}) == {}
    sent.assert_not_called()
    with pytest.raises(RuntimeError):
        original_send({"type": "snapshot"})
    client._child.poll.assert_not_called()
    if failure is not None:
        client._fail("later")
        assert client._failure == failure


def test_meaningful_first_reason_is_preserved_exactly():
    client = failure_client()
    client._fail("native control pipe deadline")
    client._fail(UnprintableError())
    assert client._failure == "native control pipe deadline"


def configuration_client(rows):
    client = failure_client()
    client.session = "configuration"
    client._closed = True  # Bound fixture EOF without fabricating a live child.
    client._request_metadata = {1: {"kind": "configuration", "identity": {
        "operation": 7, "revision": 2, "expected_revision": 1, "plan_digest": "a" * 64}}}
    target = queue.Queue(maxsize=1)
    client._requests = {1: target}
    client._child.stdout = io.BytesIO(b"".join((json.dumps(row) + "\n").encode() for row in rows))
    client._on_delta = MagicMock()
    return client, target


def configuration_row():
    return {"type": "configuration", "request": 1, "session": "configuration", "operation": 7,
            "revision": 2, "plan_digest": "a" * 64, "phase": "PREPARED", "accepted": True, "error": None}


@pytest.mark.parametrize("mutation", ["session", "operation", "revision", "digest", "kind", "late", "extra", "boolean_request"])
def test_configuration_reader_rejects_mismatched_bounded_identity(mutation):
    row = configuration_row()
    if mutation == "session": row["session"] = "other"
    elif mutation == "operation": row["operation"] += 1
    elif mutation == "revision": row["revision"] += 1
    elif mutation == "digest": row["plan_digest"] = "b" * 64
    elif mutation == "kind": row["type"] = "snapshot"
    elif mutation == "late": row["request"] = 2
    elif mutation == "extra": row["observations"] = []
    else: row["request"] = True
    client, target = configuration_client([row])
    client._read()
    assert client._failure is not None and client._failure.strip()
    assert target.get_nowait() == {"accepted": False}
    client._on_delta.assert_not_called()


def test_configuration_reader_preserves_matching_response_and_first_failure_on_duplicate():
    row = configuration_row()
    client, target = configuration_client([row, row])
    client._read()
    assert target.get_nowait() == row
    assert client._failure is not None
    first = client._failure
    client._fail("later EOF")
    assert client._failure == first
    client._on_delta.assert_not_called()


def test_configuration_reader_routes_pure_rejection_without_latching_transport_failure():
    row = configuration_row()
    row.update(accepted=False, phase="REJECTED", operation=None, plan_digest=None, revision=1,
               error="invalid plan")
    client, target = configuration_client([row])
    client._read()
    assert target.get_nowait() == row and client._failure is None


def test_configuration_input_uses_native_16k_bound_before_any_pipe_write():
    client = failure_client()
    with pytest.raises(ValueError, match="16KiB"):
        client.send({"type": "configuration_prepare", "plan": {"padding": "x" * 16384}})
    assert client._failure is None
    client._child.poll.assert_not_called()


@pytest.mark.parametrize("field,value", [("operation", True), ("operation", 2**64), ("revision", 2**64),
                                         ("plan_digest", "z"*64), ("plan_digest", 1), ("accepted", False)])
def test_actual_reader_rejects_configuration_scalar_domains(field, value):
    row = configuration_row(); row[field] = value
    client, target = configuration_client([row])
    if field == "operation" and value is True:
        client._request_metadata[1]["identity"]["operation"] = 1  # True == 1 must still fail.
    client._read()
    assert client._failure is not None
    assert target.get_nowait() == {"accepted": False}
    client._on_delta.assert_not_called()


class ClosePipeCase:
    """Synthetic owned pipe exercises original reader/close, not a real-child proof."""
    def __init__(self, mode="normal", first_failure=None):
        import os
        self.os = os
        self.release, self.entered = threading.Event(), threading.Event()
        self.timer, self.wait_calls = None, []
        self.writer_closed = False
        read_fd, self.write_fd = os.pipe()
        self.client = failure_client()
        self.client._failure = first_failure
        self.client._on_delta = self.callback
        self.client._child = SimpleNamespace(
            stdin=io.BytesIO(), stdout=os.fdopen(read_fd, "rb", buffering=0), wait=self.wait)
        self.mode = mode
        if mode in ("callback", "self_close"):
            os.write(self.write_fd, b'{"type":"delta","observations":[]}\n')
        self.client._reader = threading.Thread(target=self.read, daemon=True)
        self.client._reader.start()
        assert self.entered.wait(1)

    def read(self):
        if self.mode in ("normal", "before_read"):
            self.entered.set()
            assert self.release.wait(3), "owned reader scheduling barrier not released"
        self.client._read()

    def callback(self, row):
        self.entered.set()
        if self.mode == "self_close":
            self.client.close()
        else:
            assert self.release.wait(3), "owned callback scheduling barrier not released"

    def wait(self, timeout):
        self.wait_calls.append(timeout)
        if not self.writer_closed:
            self.os.write(self.write_fd, b'{"type":"complete"}\n')
            self.os.close(self.write_fd)
            self.writer_closed = True
        if self.mode == "normal":
            # Finite reader scheduling delay independent of stdout.close.
            self.timer = threading.Timer(.02, self.release.set)
            self.timer.start()
        return 0

    def cleanup(self):
        self.release.set()
        try:
            self.client.close()
        finally:
            self.client._reader.join(2)
            if self.timer is not None: self.timer.join(1)
            if not self.writer_closed:
                self.os.close(self.write_fd)
                self.writer_closed = True
            self.client._child.stdout.close()
        assert not self.client._reader.is_alive()


@pytest.mark.parametrize("first_failure", [None, "original transport failure"])
def test_close_preserves_original_reader_terminal_and_existing_first_failure(first_failure):
    case = ClosePipeCase(first_failure=first_failure)
    try:
        case.client.close()
        assert case.client._complete is True
        assert case.client._failure == first_failure
        assert case.wait_calls == [7]
        assert not case.client._reader.is_alive()
        assert case.client._child.stdin.closed and case.client._child.stdout.closed
    finally:
        case.cleanup()


@pytest.mark.parametrize("mode", ["before_read", "callback"])
def test_close_blocked_reader_or_callback_is_bounded_and_remains_incomplete(mode):
    from pytradekit.utils.time_handler import get_monotonic_timestamp_ns
    case = ClosePipeCase(mode=mode)
    try:
        started = get_monotonic_timestamp_ns()
        case.client.close()
        elapsed = get_monotonic_timestamp_ns() - started
        assert 800000000 <= elapsed < 2000000000
        assert case.client._reader.is_alive()  # Still owned, never claimed reaped.
        assert case.client._complete is False and case.client._closed is True
        assert case.client._child.stdout.closed
        assert case.wait_calls == [7]
    finally:
        case.cleanup()
    assert case.client._complete is False
    assert case.client._failure == "I/O operation on closed file"


def test_close_called_by_original_reader_callback_never_joins_itself():
    case = ClosePipeCase(mode="self_close")
    try:
        case.client._reader.join(2)
        assert not case.client._reader.is_alive()
        assert case.client._closed and case.client._complete is False
        assert case.client._failure == "I/O operation on closed file"
        assert case.wait_calls == [7]
        assert case.client._child.stdout.closed
    finally:
        case.cleanup()


def test_close_without_constructed_child_owner_is_idempotent():
    client = failure_client()
    client._child = None
    client.close()
    client.close()
    assert client._closed and client._complete is False and client._failure is None


@pytest.mark.parametrize("secondary_close_error", [False, True])
def test_close_join_cancellation_closes_fd_preserves_identity_and_audits_secondary(secondary_close_error):
    case = ClosePipeCase(mode="before_read", first_failure="preexisting transport diagnostic")
    client = case.client
    source, original_join = client._child.stdout, client._reader.join
    cancellation = KeyboardInterrupt("controlled original join cancellation")
    close_calls, join_calls = [], []

    class CloseTap:
        def close(self):
            close_calls.append("original stdout close")
            result = source.close()
            if secondary_close_error:
                raise OSError("controlled stdout close failure")
            return result

        def __getattr__(self, key):
            return getattr(source, key)

    def canceled_join(timeout):
        join_calls.append(timeout)
        raise cancellation

    client._child.stdout = CloseTap()
    client._reader.join = canceled_join
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            client.close()
        assert caught.value is cancellation
        assert source.closed and close_calls == ["original stdout close"]
        assert join_calls == [1] and client._failure == "preexisting transport diagnostic"
        if secondary_close_error:
            diagnostic = getattr(client, "close_cleanup_error", None)
            assert isinstance(diagnostic, str) and 0 < len(diagnostic) <= 256
            assert "controlled stdout close failure" in diagnostic
        else:
            assert getattr(client, "close_cleanup_error", None) is None
        assert client._complete is False
    finally:
        # Restore the pass-throughs before test-owned final release/reap. Never
        # retry the production close operation that reported the secondary error.
        client._reader.join, client._child.stdout = original_join, source
        case.cleanup()
