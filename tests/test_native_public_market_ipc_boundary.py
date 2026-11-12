"""Deterministic SDK IPC boundaries; synthetic streams confer no native permission.

Only the parent executes this file in the fixed offline container. Raw call
counts are IO cost evidence, never throughput or production capacity evidence.
"""
import hashlib
import io
import json
import os
import queue
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path

import pytest

from pytradekit.ws.native_public_market import NativePublicMarket
from .test_native_public_market_failure import configuration_client, configuration_row, failure_client
from .test_native_public_market_receipts import client, completion, denial, owner
from .test_native_public_market_owner_validation import metadata as validation_metadata, negative
from .test_native_public_market_receipt_contention import RegistrationHolder


def wire(value):
    return (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()


def ordinary_client():
    value = failure_client()
    value._failure_lock = threading.Lock()
    value._request_metadata = {}
    value.session = "receipts"
    value._child.stdin = SimpleNamespace(fileno=lambda: 123)
    return value


class CountingRaw(io.RawIOBase):
    """A real unbuffered raw interface counts each readinto, including EOF."""
    def __init__(self, data):
        super().__init__()
        self.data = io.BytesIO(data)
        self.read_calls = 0
        self.returned_bytes = 0

    def readable(self):
        return True

    def readinto(self, target):
        self.read_calls += 1
        result = self.data.readinto(target)
        self.returned_bytes += result
        return result


def read_stream(source):
    value = ordinary_client()
    value._closed = True  # Finite fixture EOF, not a healthy native child.
    rows = []
    value._on_delta = rows.append
    value._child.stdout = source
    value._read()
    return value, rows


def test_b01_actual_reader_preserves_large_raw_frames_without_per_byte_reads():
    expected = [{"type": "delta", "sequence": index, "payload": "x" * 131072}
                for index in range(2)]
    packet = b"".join(wire(row) for row in expected) + wire({"type": "complete"})
    source = CountingRaw(packet)
    value, rows = read_stream(source)
    assert rows == expected, "original reader must retain complete frames and order"
    assert value._complete and value._failure is None
    assert source.returned_bytes == len(packet)
    assert source.read_calls <= 256, (source.read_calls, len(packet))


@pytest.mark.parametrize("boundary", ["exact", "over", "missing_lf"])
def test_b01_reader_exact_one_mib_and_lf_boundary(boundary):
    row = {"type": "delta", "payload": ""}
    row["payload"] = "x" * (1048576 - len(wire(row)))
    packet = wire(row)
    assert len(packet) == 1048576
    if boundary == "over":
        packet = packet[:-1] + b" \n"
    elif boundary == "missing_lf":
        packet = packet[:-1]
    value, rows = read_stream(io.BytesIO(packet))
    if boundary == "exact":
        assert rows == [row] and value._failure is None
    else:
        assert rows == [] and value._failure == "native delta frame exceeds bound"


def capture_pipe(monkeypatch):
    writes = []
    monkeypatch.setattr("pytradekit.ws.native_public_market.select.select",
                        lambda readers, writers, errors, timeout: ([], writers, []))
    def write(fd, data):
        writes.append(bytes(data))
        return len(data)
    monkeypatch.setattr("pytradekit.ws.native_public_market.os.write", write)
    return writes


@pytest.mark.parametrize("size", [17000, 64000])
def test_b02_ordinary_16_to_64k_rejected_before_write_or_request_registration(monkeypatch, size):
    writes = capture_pipe(monkeypatch)
    value = ordinary_client()
    control = {"type": "snapshot", "padding": "x" * size}
    assert 16384 < len(wire(control)) <= 65536
    with pytest.raises(ValueError, match="16KiB"):
        value.send(control)
    assert value.request(control) == {}
    assert writes == [] and value._next == 0
    assert value._requests == value._request_metadata == {} and value._failure is None


def test_b02_existing_configuration_and_over_64k_failure_contracts(monkeypatch):
    writes = capture_pipe(monkeypatch)
    value = ordinary_client()
    with pytest.raises(ValueError, match="16KiB"):
        value.send({"type": "configuration_prepare", "padding": "x" * 17000})
    assert value._failure is None
    with pytest.raises(RuntimeError):
        value.send({"type": "snapshot", "padding": "x" * 66000})
    assert value._failure == "native control frame exceeds bound" and writes == []


def test_b02_legal_control_retains_exact_serialization(monkeypatch):
    writes = capture_pipe(monkeypatch)
    value = ordinary_client()
    control = {"type": "feed", "venue": "HTX", "symbol": "ALPHAUSDT", "value": "é\n"}
    value.send(control)
    assert writes == [wire(control)] and value._failure is None


class LockHolder:
    def __init__(self, lock):
        self.lock = lock
        self.entered, self.release = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.hold, name="ipc-test-owned-lock-holder")

    def hold(self):
        with self.lock:
            self.entered.set()
            self.release.wait(.8)  # Safety release makes the unbounded baseline fail finitely.

    def start(self):
        self.thread.start()
        assert self.entered.wait(1)

    def cleanup(self):
        self.release.set()
        self.thread.join(1)
        assert not self.thread.is_alive()


@pytest.mark.parametrize("lock_name", ["_lock", "_write_lock"])
@pytest.mark.parametrize("control", [{"version": 3, "native": {"session": "receipts"}},
    {"type": "feed", "venue": "HTX"}, {"type": "feed", "venue": "OKX"},
    {"type": "configuration_status", "revision": 1}])
def test_b03_send_header_and_callbacks_have_one_200ms_lock_budget(monkeypatch, lock_name, control):
    writes = capture_pipe(monkeypatch)
    value = ordinary_client()
    holder = LockHolder(getattr(value, lock_name))
    holder.start()
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError):
            value.send(control)
        elapsed = time.monotonic() - started
        assert .15 <= elapsed < .30, elapsed
        assert holder.thread.is_alive() and value._failure is not None
        assert writes == []
    finally:
        holder.cleanup()


def test_b03_ordinary_request_registration_has_one_250ms_budget(monkeypatch):
    writes = capture_pipe(monkeypatch)
    value = ordinary_client()
    holder = RegistrationHolder(value)
    holder.start()
    try:
        started = time.monotonic()
        assert value.request({"type": "snapshot"}) == {}
        assert .20 <= time.monotonic() - started < .35
        assert holder.thread.is_alive() and value._failure is not None
        assert writes == [] and value._next == 0
    finally:
        holder.cleanup()


def test_b03_send_registry_delay_does_not_restart_write_lock_budget(monkeypatch):
    writes = capture_pipe(monkeypatch)
    value = ordinary_client()
    registry, writer = LockHolder(value._lock), LockHolder(value._write_lock)
    registry.start()
    writer.start()
    release = threading.Timer(.15, registry.release.set)
    release.start()
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError):
            value.send({"type": "feed", "venue": "OKX"})
        assert .15 <= time.monotonic() - started < .30
        assert writer.thread.is_alive() and value._failure is not None and writes == []
    finally:
        release.join(1)
        registry.cleanup()
        writer.cleanup()


def test_b03_ordinary_send_delay_and_reply_share_original_request_deadline(monkeypatch):
    value = ordinary_client()
    def delayed_send(control):
        time.sleep(.15)  # Stage evidence: original request must have <=100ms left.
    value.send = delayed_send
    started = time.monotonic()
    assert value.request({"type": "snapshot"}) == {}
    assert .20 <= time.monotonic() - started < .35
    assert value._failure is not None and value._requests == value._request_metadata == {}


@pytest.mark.parametrize("cancel", [False, True])
def test_b03_b04_ordinary_cleanup_and_failure_do_not_wait_for_registry(cancel):
    value = ordinary_client()
    holder = RegistrationHolder(value)
    original = KeyboardInterrupt("original ordinary request cancellation")
    def send_then_hold(control):
        if not cancel:
            value._requests[1].put_nowait({"accepted": False})
        holder.start()
        if cancel:
            raise original
    value.send = send_then_hold
    try:
        started = time.monotonic()
        if cancel:
            with pytest.raises(KeyboardInterrupt) as caught:
                value.request({"type": "snapshot"})
            assert caught.value is original
        else:
            assert value.request({"type": "snapshot"}) == {}
        assert time.monotonic() - started < .35
        assert holder.thread.is_alive() and value._failure is not None
        assert len(value._requests) <= 1 and len(value._request_metadata) <= 1
        assert all(item["retired"].is_set() for item in value._request_metadata.values())
    finally:
        holder.cleanup()
    first, previous = value._failure, value._next
    assert value.request({"type": "snapshot"}) == {}
    assert value._next == previous and value._failure == first
    assert value._requests == value._request_metadata == {}


@pytest.mark.parametrize("via_request", [False, True])
@pytest.mark.parametrize("prior_failure", [False, True])
def test_b04_partial_ordinary_write_cancellation_fences_half_frame(monkeypatch, via_request, prior_failure):
    value = ordinary_client()
    original = KeyboardInterrupt("partial ordinary transport cancellation")
    calls, written = [], bytearray()
    monkeypatch.setattr("pytradekit.ws.native_public_market.select.select",
                        lambda readers, writers, errors, timeout: ([], writers, []))
    def partial_then_cancel(fd, data):
        calls.append(bytes(data))
        if len(calls) == 1:
            written.extend(data[:7])
            return 7
        if prior_failure:
            value._fail("first transport diagnostic")
        raise original
    monkeypatch.setattr("pytradekit.ws.native_public_market.os.write", partial_then_cancel)
    control = {"type": "snapshot"}
    with pytest.raises(KeyboardInterrupt) as caught:
        (value.request if via_request else value.send)(control)
    assert caught.value is original
    assert bytes(written) == calls[0][:7] and b"\n" not in written
    assert value._failure is not None
    if prior_failure:
        assert value._failure == "first transport diagnostic"
    first = value._failure
    with pytest.raises(RuntimeError):
        value.send(control)
    assert len(calls) == 2 and value._failure == first
    assert value._requests == value._request_metadata == {}


def test_b07_explicit_failure_fences_original_reader_delta_callback():
    first = {"type": "failure", "reason": "first native failure"}
    delta = {"type": "delta", "observations": []}
    value, rows = read_stream(io.BytesIO(wire(first) + wire(delta) + wire({"type": "complete"})))
    assert rows == [], "business callbacks must stop once first failure is latched"
    assert value._failure == "native owner failure: first native failure"
    assert value.failure_profile == first and value._complete


def test_b08_configuration_duplicate_after_real_queue_pop_still_fails():
    row = configuration_row()
    value, target = configuration_client([row, row])
    consumed = []
    put = target.put_nowait
    def put_then_consume(item):
        put(item)
        consumed.append(target.get_nowait())  # Actual pop before second reader iteration.
    target.put_nowait = put_then_consume
    value._read()
    assert consumed[0] == row
    assert consumed.count(row) == 1, "empty queue must not admit a second correlated response"
    assert value._failure is not None and value._failure.strip()
    assert value._request_metadata[1].get("delivered") is True


@pytest.mark.parametrize("result", [{"success": True, "completed_ms": 4000000000001},
    {"success": True, "completed_ms": True}, {"success": True, "completed_ms": None},
    {"success": False, "completed_ms": 1}, {"success": 1, "completed_ms": None}])
def test_b09_invalid_completion_time_rejected_before_rpc_registration(result):
    value = client()
    value._send_owner_completion = Mock(side_effect=AssertionError("invalid input reached pipe"))
    with pytest.raises(ValueError):
        value.complete_owner(owner(), {"phase": "publication", "result": result})
    value._send_owner_completion.assert_not_called()
    assert value._next == 0 and value._requests == value._request_metadata == {}
    assert value._failure is None


def test_b09_maximum_native_completion_time_is_locally_valid():
    result = {"phase": "publication", "result": {"success": True, "completed_ms": 4000000000000}}
    assert NativePublicMarket._completion_result(result) == result
    assert NativePublicMarket._completion_result({"phase": "publication", "result": {
        "success": False, "completed_ms": None}})["result"]["completed_ms"] is None


@pytest.mark.parametrize("expansion", ["escaped_scope", "request_id"])
def test_b10_final_feed_view_wire_rejection_is_local_and_cleans_exact_slot(expansion):
    value = client()
    value._feed_views = True
    value._send_owner_completion = Mock(side_effect=AssertionError("oversized input reached pipe"))
    base = {"type": "feed_view", "session": value.session, "leg": "feed", "symbol": "", "request": 1}
    if expansion == "escaped_scope":
        symbol = "\n" * 9000
    else:
        symbol = "x" * (16384 - len(wire(base)))
        value._next = 9
        assert len(wire({**base, "symbol": symbol})) == 16384
    previous = value._next
    with pytest.raises(ValueError, match="16KiB"):
        value.feed_view(symbol)
    value._send_owner_completion.assert_not_called()
    assert value._failure is None, "prewrite size rejection is not cancellation or transport uncertainty"
    assert value._requests == value._request_metadata == {}
    assert previous <= value._next <= previous + 1


@pytest.mark.parametrize("fence", ["failure", "close", "deadline"])
def test_e02_owner_rpc_fences_response_after_final_real_cleanup(fence, monkeypatch):
    value = client()
    expected = denial()  # Well-formed negative only, never a fabricated positive ACK.
    def deliver(data, deadline):
        value._requests[1].put_nowait(expected)
    finish = value._finish_owner_completion
    def cleanup_then_fence(metadata, deadline):
        result = finish(metadata, deadline)
        if fence == "failure":
            value._fail("concurrent first failure in cleanup")
        elif fence == "close":
            value._closed = True
        else:
            monkeypatch.setattr("pytradekit.ws.native_public_market.get_monotonic_timestamp_ns",
                                lambda: deadline + 1)
        return result
    value._send_owner_completion = deliver
    value._finish_owner_completion = cleanup_then_fence
    assert value.complete_owner(owner(), completion()) == {}
    assert value._requests == value._request_metadata == {}
    if fence == "failure":
        assert value._failure == "concurrent first failure in cleanup"


@pytest.mark.parametrize("stage", ["wait", "terminate", "kill"])
@pytest.mark.parametrize("secondary", [False, True])
def test_b05_e01_close_cancellation_attempts_owned_cleanup_preserving_original(stage, secondary):
    value = ordinary_client()
    value._failure = "first transport diagnostic"
    original = KeyboardInterrupt("original close " + stage)
    calls = []
    source = io.BytesIO()
    def wait(timeout):
        calls.append(("wait", timeout))
        if stage == "wait" and len(calls) == 1:
            raise original
        if child.returncode is None:
            raise subprocess.TimeoutExpired("owned-sdk-child", timeout)
        return child.returncode
    def terminate():
        calls.append(("terminate",))
        if stage == "terminate":
            raise original
    def kill():
        calls.append(("kill",))
        child.returncode = -9
        if stage == "kill":
            raise original
    def close_stdout():
        calls.append(("stdout.close",))
        source.close()
        if secondary:
            raise OSError("secondary stdout cleanup error")
    child = SimpleNamespace(stdin=io.BytesIO(), stdout=SimpleNamespace(close=close_stdout),
                            wait=wait, terminate=terminate, kill=kill, returncode=None,
                            poll=lambda: child.returncode)
    value._child = child
    value._reader = SimpleNamespace(join=lambda timeout: calls.append(("join", timeout)))
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt) as caught:
        value.close()
    assert caught.value is original and time.monotonic() - started < .30
    assert child.stdin.closed and source.closed
    assert any(call[0] == "join" for call in calls), calls
    assert any(call[0] == "kill" for call in calls) and child.returncode == -9, calls
    assert value._closed and not value._complete and value._failure == "first transport diagnostic"
    if secondary:
        assert "secondary stdout cleanup error" in value.close_cleanup_error


def test_b11_constructor_hashes_binary_when_file_digest_api_absent(tmp_path, monkeypatch):
    binary = tmp_path / "identity-only"
    payload = b"identity-only-not-executable" * 40000
    binary.write_bytes(payload)
    original_open, reads = Path.open, []
    class BoundedSource:
        def __init__(self, source):
            self.source = source
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.source.close()
        def read(self, size=-1):
            reads.append(size)
            assert 0 < size <= 1048576, "binary identity check must use bounded reads"
            return self.source.read(size)
        def __getattr__(self, name):
            return getattr(self.source, name)
    def open_binary(path, *args, **kwargs):
        source = original_open(path, *args, **kwargs)
        return BoundedSource(source) if path == binary else source
    monkeypatch.setattr(Path, "open", open_binary)
    monkeypatch.delattr(hashlib, "file_digest", raising=False)
    popen = Mock(return_value=SimpleNamespace(stdin=Mock(), stdout=Mock()))
    monkeypatch.setattr("pytradekit.ws.native_public_market.subprocess.Popen", popen)
    monkeypatch.setattr("pytradekit.ws.native_public_market.os.set_blocking", Mock())
    monkeypatch.setattr("pytradekit.ws.native_public_market.threading.Thread", Mock())
    monkeypatch.setattr(NativePublicMarket, "send", Mock())
    NativePublicMarket(binary, hashlib.sha256(payload).hexdigest(),
                       {"native": {"session": "receipts"}}, Mock())
    assert popen.call_args.args[0] == [str(binary), "--live"]
    assert reads and len(reads) >= 2
    # API absence is a compatibility counterexample, not a complete Python3.8 run.


def test_g01_actual_reader_rejects_matching_request_kind_wrong_creation_revision():
    value = client()
    value._closed = True
    value._on_delta = Mock()
    target = queue.Queue(maxsize=1)
    value._requests = {1: target}
    value._request_metadata = {1: validation_metadata()}
    row = negative()
    row["owner"]["configuration_revision"] += 1
    value._child.stdout = io.BytesIO(wire(row))
    value._read()
    assert value._failure == "invalid native owner validation response"
    assert target.get_nowait() == {"accepted": False}
    assert target.empty() and not value._request_metadata[1]["delivered"]
    value._on_delta.assert_not_called()
