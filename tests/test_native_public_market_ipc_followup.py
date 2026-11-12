"""SDK lifecycle counterexamples; only the parent runs the isolated unit suite.

Synthetic replies are negative or snapshots and grant no native permission.
Raw read counts describe these fragments only, never CPU or capacity results.
"""
import hashlib
import io
import json
import os
import select
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pytradekit.utils.time_handler import get_monotonic_timestamp_ns
from pytradekit.ws.native_public_market import NativePublicMarket
from .test_native_public_market_failure import failure_client
from .test_native_public_market_receipts import client, completion, denial, owner
from .test_native_public_market_receipt_contention import RegistrationHolder


def wire(value):
    return (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()


def ordinary_client():
    value = failure_client()
    value._failure_lock = threading.Lock()
    value._request_metadata = {}
    value.session = "receipts"
    return value


def invoke(value, route, control=None, expected=None):
    if route == "ordinary":
        return value.request({"type": "snapshot"} if control is None else control)
    control = {"type": "owner_complete", **owner(), **completion()} if control is None else control
    expected = {"kind": "owner_completion", "identity": owner(),
                "completion": completion()} if expected is None else expected
    return value._owner_rpc(control, expected, get_monotonic_timestamp_ns() + 250_000_000)


@pytest.mark.parametrize("route", ["ordinary", "owner"])
@pytest.mark.parametrize("invalid", ["unserializable", "nonfinite"])
def test_f01_local_encoding_error_never_registers_writes_or_poison_client(monkeypatch, route, invalid):
    value = ordinary_client() if route == "ordinary" else client()
    control = {"type": "snapshot" if route == "ordinary" else "owner_complete",
               "padding": object() if invalid == "unserializable" else float("nan")}
    original_dumps, errors = json.dumps, []
    writes = []

    def encode(*args, **kwargs):
        try:
            return original_dumps(*args, **kwargs)
        except (TypeError, ValueError) as error:
            errors.append(error)
            raise

    def deliver(data, deadline, *args):
        writes.append(bytes(data))
        request = json.loads(data)["request"]
        response = {"type": "snapshot", "request": request, "state": {}}
        if route == "owner":
            response = {**denial(), "request": request}
        value._requests[request].put_nowait(response)

    monkeypatch.setattr("pytradekit.ws.native_public_market.json.dumps", encode)
    value._write_control = deliver
    if invalid == "nonfinite" and route == "ordinary":
        assert invoke(value, route, control) == {}
    else:
        expected_error = TypeError if invalid == "unserializable" else ValueError
        with pytest.raises(expected_error) as caught:
            invoke(value, route, control)
        assert errors and caught.value is errors[0], "preserve the original local encoder exception"
    assert errors and writes == [] and value._next == 0
    assert value._requests == value._request_metadata == {} and value._failure is None
    response = invoke(value, route)
    assert response["type"] == ("snapshot" if route == "ordinary" else "owner_completion")
    assert len(writes) == 1 and value._next == 1 and value._failure is None
    assert value._requests == value._request_metadata == {}


@pytest.mark.parametrize("fault", ["select", "fileno"])
@pytest.mark.parametrize("first_failure", [None, "first partial-write diagnostic"])
def test_f02_real_partial_owner_pipe_valueerror_fences_all_later_rpcs(monkeypatch, fault, first_failure):
    value = client()
    read_fd, write_fd = os.pipe()
    source = os.fdopen(write_fd, "wb", buffering=0)
    os.set_blocking(write_fd, False)
    original_select, original_write = select.select, os.write
    error = ValueError("controlled transport " + fault)
    written, calls = bytearray(), []

    def transport_error():
        if first_failure is not None:
            value._fail(first_failure)
        raise error

    class PipeTap:
        def fileno(self):
            if fault == "fileno" and written:
                transport_error()
            return source.fileno()

    value._child.stdin = PipeTap()

    def ready(readers, writers, errors, timeout):
        calls.append("select")
        if fault == "select" and written:
            transport_error()
        result = original_select(readers, [source], errors, timeout)
        return result[0], writers if result[1] else [], result[2]

    def partial_write(fd, data):
        calls.append("write")
        assert not written, "no transport write may follow the uncertain partial frame"
        size = original_write(fd, data[:7])
        written.extend(data[:size])
        return size

    monkeypatch.setattr("pytradekit.ws.native_public_market.select.select", ready)
    monkeypatch.setattr("pytradekit.ws.native_public_market.os.write", partial_write)
    try:
        try:
            result = value.complete_owner(owner(), completion())
        except ValueError as caught:
            assert caught is error
        else:
            assert result == {}
        assert len(written) == 7
        os.set_blocking(read_fd, False)
        assert os.read(read_fd, 7) == bytes(written) and len(written) == 7
        assert b"\n" not in written
        assert value._failure is not None, "transport ValueError must fence a partial owner frame"
        if first_failure is not None:
            assert value._failure == first_failure
        first, previous, previous_calls = value._failure, value._next, list(calls)
        assert value.complete_owner(owner(), completion()) == {}
        assert value._next == previous and value._failure == first and calls == previous_calls
        assert value._requests == value._request_metadata == {}
    finally:
        source.close()
        os.close(read_fd)


class CancelExpected:
    def __init__(self, cancel):
        self.expected = {"kind": "owner_completion", "identity": owner(), "completion": completion()}
        self.cancel = cancel

    def __getitem__(self, key):
        return self.expected[key]

    def keys(self):
        self.cancel()


@pytest.mark.parametrize("route", ["ordinary", "owner"])
@pytest.mark.parametrize("preparation", ["event", "metadata"])
def test_f03_cancellation_during_preparation_publishes_no_orphan(monkeypatch, route, preparation):
    value = ordinary_client() if route == "ordinary" else client()
    cancellation = KeyboardInterrupt("original " + route + " " + preparation + " cancellation")
    value.send = Mock(side_effect=AssertionError("preparation cancellation reached ordinary send"))
    value._send_owner_completion = Mock(side_effect=AssertionError("preparation cancellation reached owner send"))

    def cancel():
        value._fail("first preparation diagnostic")
        raise cancellation

    class CancelControl(dict):
        def get(self, key, default=None):
            if key == "type":
                cancel()
            return super().get(key, default)

    control, expected = None, None
    if preparation == "event":
        monkeypatch.setattr("pytradekit.ws.native_public_market.threading.Event", cancel)
    elif route == "ordinary":
        control = CancelControl(type="snapshot")
    else:
        expected = CancelExpected(cancel)
    with pytest.raises(KeyboardInterrupt) as caught:
        invoke(value, route, control, expected)
    assert caught.value is cancellation and value._failure == "first preparation diagnostic"
    assert value._requests == value._request_metadata == {}, "no queue without its retirement metadata"
    assert 0 <= value._next <= 1
    value.send.assert_not_called()
    value._send_owner_completion.assert_not_called()


class CancelPublication(dict):
    def __init__(self, cancel, moment):
        super().__init__()
        self.cancel, self.moment, self.called = cancel, moment, False

    def __setitem__(self, key, value):
        if self.called:
            return super().__setitem__(key, value)
        self.called = True
        if self.moment == "before_store":
            self.cancel()
        super().__setitem__(key, value)
        self.cancel()


@pytest.mark.parametrize("route", ["ordinary", "owner"])
@pytest.mark.parametrize("registry", ["_requests", "_request_metadata"])
@pytest.mark.parametrize("moment", ["before_store", "after_store"])
def test_f03_cancellation_between_registry_publications_retires_exact_shared_slot(route, registry, moment):
    value = ordinary_client() if route == "ordinary" else client()
    cancellation = KeyboardInterrupt("original " + registry + " " + moment + " cancellation")

    def cancel():
        value._fail("first publication diagnostic")
        raise cancellation

    target = CancelPublication(cancel, moment)
    setattr(value, registry, target)
    value.send = Mock(side_effect=AssertionError("cancelled registration reached ordinary send"))
    value._send_owner_completion = Mock(side_effect=AssertionError("cancelled registration reached owner send"))
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt) as caught:
        invoke(value, route)
    assert caught.value is cancellation and time.monotonic() - started < .35
    assert target.called and value._failure == "first publication diagnostic"
    assert value._requests == value._request_metadata == {}, "cancelled publication left a shared slot"
    previous = value._next
    assert previous == 1 and invoke(value, route) == {} and value._next == previous
    assert value._failure == "first publication diagnostic"
    value.send.assert_not_called()
    value._send_owner_completion.assert_not_called()


def test_f04_close_reaches_exact_child_cleanup_while_registry_remains_owned():
    value = ordinary_client()
    value._failure = "first close diagnostic"
    calls = []
    child = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(),
                            wait=lambda timeout: calls.append(("wait", timeout)) or 0)
    value._child = child
    value._reader = SimpleNamespace(join=lambda timeout: calls.append(("join", timeout)))
    holder = RegistrationHolder(value)
    holder.start()
    try:
        started = time.monotonic()
        value.close()
        elapsed = time.monotonic() - started
        assert elapsed < .40 and holder.thread.is_alive(), "registry entry delayed all owned child cleanup"
        assert calls == [("wait", 7), ("join", 1)]
        assert child.stdin.closed and child.stdout.closed and value._child is child
        assert value._closed and not value._complete and value._failure == "first close diagnostic"
    finally:
        holder.cleanup()
        child.stdin.close()
        child.stdout.close()


@pytest.mark.parametrize("fallback_available", [False, True])
def test_f05_kill_cancellation_before_signal_uses_bounded_exact_child_fallback(fallback_available):
    value = ordinary_client()
    value._failure = "first transport diagnostic"
    cancellation = KeyboardInterrupt("original kill before signal cancellation")
    calls, kill_calls = [], []

    def wait(timeout):
        calls.append(("wait", timeout))
        if child.returncode is None:
            raise subprocess.TimeoutExpired("exact-unit-child", timeout)
        return child.returncode

    def kill():
        kill_calls.append(child)
        calls.append(("kill",))
        if len(kill_calls) == 1:
            raise cancellation  # No signal or returncode side effect happened.
        if not child.fallback_available:
            raise OSError("exact child fallback signal unavailable")
        child.returncode = -9

    child = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), returncode=None,
                            fallback_available=fallback_available, wait=wait, kill=kill,
                            terminate=lambda: calls.append(("terminate",)))
    child.poll = lambda: child.returncode
    value._child = child
    value._reader = SimpleNamespace(join=lambda timeout: calls.append(("join", timeout)))
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt) as caught:
        value.close()
    assert caught.value is cancellation and time.monotonic() - started < .35
    assert 2 <= len(kill_calls) <= 3 and all(target is child for target in kill_calls)
    assert [entry[1] for entry in calls if entry[0] == "wait"] == [7, 2, 2]
    assert ("join", 1) in calls and child.stdin.closed and child.stdout.closed
    assert not value._complete and value._failure == "first transport diagnostic"
    if fallback_available:
        assert child.returncode == -9
    else:
        assert child.returncode is None, "failed signalling cannot be reported as a reaped child"
        diagnostic = getattr(value, "close_cleanup_error", None)
        assert isinstance(diagnostic, str) and diagnostic.strip() and len(diagnostic) <= 256
        previous = len(kill_calls)
        child.fallback_available = True
        value.close()
        assert len(kill_calls) > previous and child.returncode == -9, "closed no-op concealed the live owned child"
    assert value._child is child and value._failure == "first transport diagnostic"


class FragmentedRaw(io.RawIOBase):
    def __init__(self, fragments):
        super().__init__()
        self.fragments = list(fragments)
        self.index = self.offset = self.read_calls = self.returned_bytes = 0

    def readable(self):
        return True

    def readline(self, size=-1):
        raise AssertionError("RawIOBase boundary must use the raw frame helper")

    def readinto(self, target):
        self.read_calls += 1
        if self.index == len(self.fragments):
            return 0
        fragment = self.fragments[self.index]
        size = min(len(target), len(fragment) - self.offset)
        target[:size] = fragment[self.offset:self.offset + size]
        self.offset += size
        self.returned_bytes += size
        if self.offset == len(fragment):
            self.index += 1
            self.offset = 0
        return size


def read_raw(fragments):
    source = FragmentedRaw(fragments)
    value = ordinary_client()
    value._closed = True  # Finite fixture EOF is not a healthy real native child.
    value._child.stdout = source
    rows = []
    value._on_delta = rows.append
    value._read()
    assert isinstance(source, io.RawIOBase) and source.read_calls > 1
    return value, rows, source


@pytest.mark.parametrize("boundary", ["exact", "over", "missing_lf"])
def test_f06_raw_short_reads_retain_exact_one_mib_and_enforce_lf(boundary):
    row = {"type": "delta", "payload": ""}
    row["payload"] = "x" * (1048576 - len(wire(row)))
    packet = wire(row)
    assert len(packet) == 1048576
    if boundary == "over":
        packet = packet[:-1] + b" \n"
    elif boundary == "missing_lf":
        packet = packet[:-1]
    fragments = [packet[:1], packet[1:7], packet[7:65539], packet[65539:-1], packet[-1:]]
    value, rows, source = read_raw(fragments)
    assert source.returned_bytes == len(packet)
    if boundary == "exact":
        assert rows == [row] and value._failure is None
    else:
        assert rows == [] and value._failure == "native delta frame exceeds bound"


def test_f06_raw_helper_preserves_multiple_frames_and_order_across_fragmented_lf():
    expected = [{"type": "delta", "sequence": index, "payload": "x" * (65536 + index)}
                for index in range(3)]
    frames = [wire(row) for row in expected] + [wire({"type": "complete"})]
    fragments = [frames[0][:3], frames[0][3:-1], frames[0][-1:] + frames[1][:5],
                 frames[1][5:-1], frames[1][-1:], frames[2][:-1],
                 frames[2][-1:] + frames[3][:-1], frames[3][-1:]]
    value, rows, source = read_raw(fragments)
    assert rows == expected and value._complete and value._failure is None
    assert source.returned_bytes == sum(map(len, frames))


@pytest.mark.parametrize("startup", ["reader_start", "header_send"])
@pytest.mark.parametrize("cancel", [False, True])
def test_f07_constructor_preserves_original_when_started_child_cleanup_also_fails(tmp_path, monkeypatch,
                                                                                startup, cancel):
    binary = tmp_path / "synthetic-child-identity"
    payload = b"identity-only-never-executed"
    binary.write_bytes(payload)
    original = KeyboardInterrupt("original startup cancellation") if cancel else RuntimeError("original startup failure")
    secondary = OSError("secondary bounded constructor cleanup diagnostic " + "x" * 400)
    calls = []

    class Stdin:
        def fileno(self):
            return 123

        def close(self):
            calls.append("stdin.close")
            raise secondary

    class Reader:
        def start(self):
            calls.append("reader.start")
            if startup == "reader_start":
                raise original

        def join(self, timeout):
            calls.append(("reader.join", timeout))

    source = io.BytesIO()
    child = SimpleNamespace(stdin=Stdin(), stdout=source,
                            wait=lambda timeout: calls.append(("wait", timeout)) or 0)
    popen = Mock(return_value=child)

    def send(value, control):
        calls.append("header.send")
        assert value._child is child
        raise original

    monkeypatch.setattr("pytradekit.ws.native_public_market.subprocess.Popen", popen)
    monkeypatch.setattr("pytradekit.ws.native_public_market.os.set_blocking", Mock())
    monkeypatch.setattr("pytradekit.ws.native_public_market.threading.Thread", Mock(return_value=Reader()))
    monkeypatch.setattr(NativePublicMarket, "send", send)
    value = NativePublicMarket.__new__(NativePublicMarket)
    with pytest.raises(type(original)) as caught:
        value.__init__(binary, hashlib.sha256(payload).hexdigest(), {"native": {"session": "receipts"}}, Mock())
    assert caught.value is original and popen.call_count == 1 and value._child is child
    assert "reader.start" in calls and ("header.send" in calls) == (startup == "header_send")
    assert calls[-3:] == ["stdin.close", ("wait", 7), ("reader.join", 1)] and source.closed
    diagnostic = getattr(value, "close_cleanup_error", None)
    assert isinstance(diagnostic, str) and 0 < len(diagnostic) <= 256
    assert diagnostic.startswith("secondary bounded constructor cleanup diagnostic")


@pytest.mark.parametrize("reply", ["negative_validation", "snapshot"])
@pytest.mark.parametrize("fence", ["failure", "close", "deadline"])
def test_f08_ordinary_request_fences_reply_after_its_actual_final_cleanup(monkeypatch, reply, fence):
    value = ordinary_client()
    control = {"type": "validate" if reply == "negative_validation" else "snapshot"}
    expected = {"type": "validation", "request": 1, "accepted": False,
                "owner": owner(), "market_snapshot": None}
    if reply == "snapshot":
        expected = {"type": "snapshot", "request": 1, "state": {"sequence": 2}}
    observed = []

    def deliver(data, deadline, label):
        request = json.loads(data)["request"]
        assert request == 1 and value._request_metadata[request]["kind"] == expected["type"]
        observed.append(expected)
        value._requests[request].put_nowait(expected)

    finish = value._finish_owner_completion

    def cleanup_then_fence(metadata, deadline):
        result = finish(metadata, deadline)
        assert result and metadata["retired"].is_set()
        assert value._requests == value._request_metadata == {}
        if fence == "failure":
            value._fail("first ordinary cleanup failure")
        elif fence == "close":
            value._closed = True
        else:
            monkeypatch.setattr("pytradekit.ws.native_public_market.get_monotonic_timestamp_ns", lambda: deadline + 1)
        return result

    value._write_control = deliver  # Actual ordinary request -> send -> cleanup routing.
    value._finish_owner_completion = cleanup_then_fence
    assert value.request(control) == {} and observed == [expected]
    assert value._next == 1 and value._requests == value._request_metadata == {}
    if fence == "failure":
        assert value._failure == "first ordinary cleanup failure"
    elif fence == "close":
        assert value._closed and value._failure is None
    else:
        assert value._failure is not None and "deadline" in value._failure
