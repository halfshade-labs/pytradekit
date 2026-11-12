"""Frozen ordinary frames and retryable cleanup; parent-only isolated execution.

Real test-owned pipes carry synthetic controls and snapshots, never native owner
acceptance or permission. These checks make no runtime capacity claim.
"""
import io
import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from .test_native_public_market_ipc_followup import invoke, ordinary_client, wire
from .test_native_public_market_receipt_contention import RegistrationHolder
from .test_native_public_market_receipts import client, denial


class OrdinaryPipe:
    """Observe original send/_write_control through a bounded actual pipe."""

    def __init__(self, value, monkeypatch):
        self.value = value
        self.read_fd, write_fd = os.pipe()
        self.source = os.fdopen(write_fd, "wb", buffering=0)
        os.set_blocking(self.read_fd, False)
        os.set_blocking(write_fd, False)
        self.frames, self.replies = [], []
        self.pending = bytearray()
        self.original_write = os.write
        value._child.stdin = self.source
        monkeypatch.setattr("pytradekit.ws.native_public_market.os.write", self.write)

    def write(self, fd, data):
        assert fd == self.source.fileno(), "only the exact test-owned pipe is writable"
        size = self.original_write(fd, data)
        self.pending.extend(data[:size])
        while b"\n" in self.pending:
            frame, separator, remainder = self.pending.partition(b"\n")
            self.pending[:] = remainder
            self.frames.append(bytes(frame + separator))
            self.deliver(json.loads(frame))
        return size

    def deliver(self, control):
        if control["type"] == "snapshot":
            request = control["request"]
            reply = {"type": "snapshot", "request": request, "state": {"sequence": 1}}
            assert request in self.value._requests
            self.replies.append(reply)
            self.value._requests[request].put_nowait(reply)

    def assert_emitted(self, expected):
        assert self.frames == expected and not self.pending
        assert sum(map(len, expected)) < 4096, "keep test-owned real pipe writes bounded"
        assert os.read(self.read_fd, 4096) == b"".join(expected)

    def close(self):
        self.source.close()
        os.close(self.read_fd)


@pytest.mark.parametrize("mutation", ["serializable", "unserializable"])
def test_z01_ordinary_request_sends_exact_final_validated_bytes_after_nested_mutation(monkeypatch, mutation):
    value = ordinary_client()
    control = {"type": "snapshot", "plan": {"limits": [{"value": "validated"}]}}
    encode, validated = value._encode_control, []

    def encode_then_mutate(outgoing):
        frame = encode(outgoing)
        validated.append(frame)
        if len(validated) == 2:
            assert outgoing["request"] == 1 and outgoing["plan"] is control["plan"]
            control["plan"]["limits"][0]["value"] = "changed" if mutation == "serializable" else object()
        return frame

    value._encode_control = encode_then_mutate
    pipe = OrdinaryPipe(value, monkeypatch)
    try:
        result = value.request(control)
        assert len(validated) >= 2 and json.loads(validated[1])["plan"]["limits"][0]["value"] == "validated"
        pipe.assert_emitted([validated[1]])
        assert result == pipe.replies[0] and result["type"] == "snapshot"
        assert value._next == 1 and value._failure is None and not value._closed
        assert value._requests == value._request_metadata == {}
    finally:
        pipe.close()


def test_z01_distinct_send_object_with_same_request_id_cannot_borrow_registered_bytes(monkeypatch):
    value = ordinary_client()
    original_send, outgoing_frames = value.send, []

    def observe_send(outgoing):
        request = outgoing["request"]
        assert request in value._requests and request in value._request_metadata
        distinct = {"type": "feed", "venue": "HTX", "request": request}
        assert distinct is not outgoing
        outgoing_frames.extend([wire(distinct), wire(outgoing)])
        original_send(distinct)
        original_send(outgoing)

    value.send = observe_send
    pipe = OrdinaryPipe(value, monkeypatch)
    try:
        result = value.request({"type": "snapshot"})
        pipe.assert_emitted(outgoing_frames)
        assert len(outgoing_frames) == 2 and json.loads(outgoing_frames[0])["type"] == "feed"
        assert result == pipe.replies[0] and value._failure is None
        assert value._next == 1 and value._requests == value._request_metadata == {}
    finally:
        pipe.close()


def reaped_child(calls):
    child = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), returncode=0)
    child.wait = lambda timeout: calls.append(("wait", timeout)) or child.returncode
    child.poll = lambda: child.returncode
    child.terminate = Mock(side_effect=AssertionError("already reaped child was terminated"))
    child.kill = Mock(side_effect=AssertionError("already reaped child was killed"))
    return child


@pytest.mark.parametrize("route", ["ordinary", "owner"])
def test_z02_second_close_drains_original_retired_rpc_after_registry_release(route):
    value = ordinary_client() if route == "ordinary" else client()
    holder, calls, registered = RegistrationHolder(value), [], []
    child = reaped_child(calls)
    value._child = child
    value._reader = SimpleNamespace(join=lambda timeout: calls.append(("join", timeout)), is_alive=lambda: False)

    def deliver_then_hold(data, deadline, *label):
        request = json.loads(data)["request"]
        registered.append(value._request_metadata[request])
        reply = {"type": "snapshot", "request": request, "state": {}}
        if route == "owner":
            reply = {**denial(), "request": request}
        value._requests[request].put_nowait(reply)
        holder.start()
        value._fail("first retired transport diagnostic")

    value._write_control = deliver_then_hold
    try:
        started = time.monotonic()
        assert invoke(value, route) == {}
        assert time.monotonic() - started < .40 and holder.thread.is_alive()
        assert len(registered) == 1 and registered[0]["retired"].is_set()
        assert value._request_metadata[1] is registered[0] and 1 in value._requests
        started = time.monotonic()
        value.close()
        assert time.monotonic() - started < .35 and holder.thread.is_alive()
        assert calls == [("wait", 7), ("join", 1)]
        assert child.stdin.closed and child.stdout.closed and value._child is child
        assert value._failure == "first retired transport diagnostic" and value._next == 1
        holder.cleanup()
        value.close()
        assert value._requests == value._request_metadata == {}, "completed child cleanup concealed retired RPC slots"
        assert value._close_cleanup_complete and value._child is child and not value._complete
        assert value._failure == "first retired transport diagnostic" and value._next == 1
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
    finally:
        holder.cleanup()
        child.stdin.close()
        child.stdout.close()


def test_z03_actual_two_digit_ordinary_request_id_size_rejection_is_local_then_recovers(monkeypatch):
    value = ordinary_client()
    control = {"type": "snapshot", "padding": ""}
    control["padding"] = "x" * (16384 - len(wire({**control, "request": 1})))
    assert len(wire({**control, "request": 1})) == 16384
    assert len(wire({**control, "request": 10})) == 16385
    value._next = 9
    pipe = OrdinaryPipe(value, monkeypatch)
    try:
        assert value.request(control) == {}
        assert pipe.frames == [] and pipe.pending == b"" and value._next == 10
        assert value._failure is None and value._requests == value._request_metadata == {}
        result = value.request({"type": "snapshot"})
        pipe.assert_emitted([wire({"type": "snapshot", "request": 11})])
        assert result == pipe.replies[0] and result["request"] == 11
        assert value._next == 11 and value._failure is None
        assert value._requests == value._request_metadata == {}
    finally:
        pipe.close()


class CloseTap:
    def __init__(self, source, name, calls):
        self.source, self.name, self.calls = source, name, calls
        self.failure = None

    def close(self):
        self.calls.append((self.name, "close"))
        if self.failure is not None:
            error, self.failure = self.failure, None
            raise error  # Deliberately before closing the actual test-owned FD.
        self.source.close()

    def __getattr__(self, name):
        return getattr(self.source, name)


def assert_close_noop(value, calls):
    before = list(calls)
    value.close()
    assert calls == before, "completed cleanup must not repeat child/FD/join operations"


@pytest.mark.parametrize("descriptor", ["stdin", "stdout"])
def test_z04_reaped_child_retries_close_that_failed_before_fd_close_then_becomes_noop(descriptor):
    value, calls = ordinary_client(), []
    value._failure = "first close transport diagnostic"
    child = reaped_child(calls)
    read_fd, write_fd = os.pipe()
    sources = {"stdin": os.fdopen(write_fd, "wb", buffering=0),
               "stdout": os.fdopen(read_fd, "rb", buffering=0)}
    child.stdin, child.stdout = [CloseTap(sources[name], name, calls) for name in ("stdin", "stdout")]
    original = KeyboardInterrupt("original " + descriptor + " close cancellation before FD close")
    getattr(child, descriptor).failure = original
    value._child = child
    value._reader = SimpleNamespace(join=lambda timeout: calls.append(("join", timeout)), is_alive=lambda: False)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            value.close()
        assert caught.value is original and not sources[descriptor].closed
        assert sources["stdout" if descriptor == "stdin" else "stdin"].closed
        assert not value._close_cleanup_complete and value._failure == "first close transport diagnostic"
        assert child.returncode == 0 and calls.count(("wait", 7)) == calls.count(("join", 1)) == 1
        value.close()
        assert all(source.closed for source in sources.values()) and value._close_cleanup_complete
        assert calls.count((descriptor, "close")) == 2
        assert calls.count(("wait", 7)) == calls.count(("join", 1)) == 2
        assert_close_noop(value, calls)
        assert value._child is child and value._failure == "first close transport diagnostic" and not value._complete
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
    finally:
        for source in sources.values():
            source.close()


def test_z05_reaped_child_with_live_reader_retries_original_bounded_join():
    value, calls = ordinary_client(), []
    child = reaped_child(calls)
    value._child = child
    entered, release = threading.Event(), threading.Event()

    def read_until_release():
        entered.set()
        release.wait()

    reader = threading.Thread(target=read_until_release, name="finalization-test-owned-reader", daemon=True)
    value._reader = reader
    reader.start()
    original_join = reader.join

    def join(timeout):
        calls.append(("join", timeout))
        if calls.count(("join", 1)) == 2:
            release.set()
        return original_join(timeout)

    reader.join = join
    try:
        assert entered.wait(1)
        started = time.monotonic()
        value.close()
        assert .8 <= time.monotonic() - started < 2
        assert reader.is_alive() and not value._close_cleanup_complete
        assert child.returncode == 0 and child.stdin.closed and child.stdout.closed
        started = time.monotonic()
        value.close()
        assert time.monotonic() - started < .40
        assert not reader.is_alive() and value._close_cleanup_complete
        assert calls == [("wait", 7), ("join", 1), ("wait", 7), ("join", 1)]
        assert_close_noop(value, calls)
        assert value._child is child and value._failure is None and not value._complete
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
    finally:
        release.set()
        original_join(1)
        child.stdin.close()
        child.stdout.close()
        assert not reader.is_alive(), "test-owned reader leaked"


def test_z06_ordinary_preparation_cancellation_without_prior_failure_latches_original_fence():
    value = ordinary_client()
    original = KeyboardInterrupt("original ordinary preparation cancellation")

    class CancelControl(dict):
        def get(self, key, default=None):
            if key == "type":
                raise original
            return super().get(key, default)

    value.send = Mock(side_effect=AssertionError("preparation cancellation reached public send"))
    value._write_control = Mock(side_effect=AssertionError("preparation cancellation reached pipe"))
    assert value._failure is None
    with pytest.raises(KeyboardInterrupt) as caught:
        value.request(CancelControl(type="snapshot"))
    assert caught.value is original and value._failure == "native owner validation cancelled"
    assert value._next == 0 and value._requests == value._request_metadata == {}
    assert value.request({"type": "snapshot"}) == {}
    assert value._failure == "native owner validation cancelled" and value._next == 0
    value.send.assert_not_called()
    value._write_control.assert_not_called()


def test_z07_retired_rpc_does_not_retain_mutated_caller_graph(monkeypatch):
    import gc
    import weakref

    class CallerGraph(dict):
        pass

    value = ordinary_client()
    control = {"type": "snapshot", "plan": {"limits": [{"value": "validated"}]}}
    holder = RegistrationHolder(value)
    encode, validated, references = value._encode_control, [], []

    def encode_then_mutate(outgoing):
        frame = encode(outgoing)
        validated.append(frame)
        if len(validated) == 2:
            graph = CallerGraph(payload="x" * 65536)
            references.append(weakref.ref(graph))
            control["plan"]["limits"][0]["value"] = graph
        return frame

    value._encode_control = encode_then_mutate
    pipe = OrdinaryPipe(value, monkeypatch)
    deliver = pipe.deliver

    def snapshot_then_hold(outgoing):
        deliver(outgoing)
        holder.start()

    pipe.deliver = snapshot_then_hold
    try:
        assert value.request(control) == {}
        assert len(validated) == 2 and len(references) == 1
        pipe.assert_emitted([validated[1]])
        assert holder.thread.is_alive() and not holder.release.is_set()
        assert value._next == 1 and len(value._requests) == len(value._request_metadata) == 1
        assert value._request_metadata[1]["retired"].is_set()
        assert value._failure == "native owner completion cleanup deadline"
        del control  # Empty the encoder closure's caller cell as well.
        gc.collect()
        assert holder.thread.is_alive() and not holder.release.is_set()
        assert value._request_metadata[1]["retired"].is_set() and 1 in value._requests
        assert references[0]() is None, "retired metadata retained the caller graph after request returned"
    finally:
        holder.cleanup()
        pipe.close()
