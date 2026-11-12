"""Stub transport/type/deadline tests only; no actual owner or child claims."""
import hashlib
import io
import json
import os
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pytradekit.utils.time_handler import get_monotonic_timestamp_ns as ns
from pytradekit.ws.native_public_market import NativePublicMarket
from .test_native_public_market_receipts import client
from .test_native_public_market_receipt_contention import RegistrationHolder, assert_bounded_fenced

PROFILE = dict(owner_completion_receipts=True, owner_retirement=True,
               live_accounting=True, admission_deltas=True)


def header():
    return {"version": 3, "configuration_transactions": True, "diagnostics": False,
            "native": {"session": "receipts"}}


@pytest.mark.parametrize("invalid", [None, 0, 1, "v1", [], {}])
def test_feed_views_strict_bool_before_io(invalid):
    with pytest.raises(ValueError, match="feed_views"):
        NativePublicMarket(None, None, {}, None, feed_views=invalid)


@pytest.mark.parametrize("missing", list(PROFILE))
def test_feed_views_requires_each_explicit_profile_option(missing):
    with pytest.raises(ValueError):
        NativePublicMarket(None, None, header(), None,
                           **{**PROFILE, missing: False}, feed_views=True)


@pytest.mark.parametrize("field,value", [("version", True), ("configuration_transactions", False),
    ("native", None), ("diagnostics", True), ("native_event_diagnostics", True)])
def test_feed_views_configured_native_preflight_before_io(field, value):
    with pytest.raises(ValueError):
        NativePublicMarket(None, None, {**header(), field: value}, None, **PROFILE, feed_views=True)


@pytest.mark.parametrize("enabled", [None, False, True])
def test_feed_views_argv_preserves_original_prefix_stub(tmp_path, monkeypatch, enabled):
    binary = tmp_path / "identity-only"
    binary.write_bytes(b"stub identity only")
    popen = Mock(return_value=SimpleNamespace(stdin=Mock(), stdout=Mock()))
    monkeypatch.setattr("pytradekit.ws.native_public_market.subprocess.Popen", popen)
    monkeypatch.setattr("pytradekit.ws.native_public_market.os.set_blocking", Mock())
    monkeypatch.setattr("pytradekit.ws.native_public_market.threading.Thread", Mock())
    monkeypatch.setattr(NativePublicMarket, "send", Mock())
    NativePublicMarket(binary, hashlib.sha256(binary.read_bytes()).hexdigest(), header(), Mock(),
                       **PROFILE, **({} if enabled is None else {"feed_views": enabled}))
    assert popen.call_args.args[0] == [str(binary), "--live", "--owner-completion-receipts=v1",
        "--owner-retirement=v1", "--live-accounting=v1", "--admission-deltas=v1"] + (
        ["--feed-views=v1"] if enabled else [])


def view_client():
    value = client()
    value._owner_retirement = value._live_accounting = value._admission_deltas = value._feed_views = True
    return value


def ready(capability):
    row = {"type": "ready", "version": 3, "production_permission": False}
    for name in PROFILE:
        row[name] = {"version": 1, "session": "receipts"}
    if capability is not None:
        row["feed_views"] = capability
    return row


def read_rows(value, rows):
    value._closed = True  # Intentional finite stub EOF.
    value._child.stdout = io.BytesIO(b"".join((json.dumps(row) + "\n").encode() for row in rows))
    value._read()
    return value


@pytest.mark.parametrize("capability", [None, {}, {"version": True, "session": "receipts"},
    {"version": 1.0, "session": "receipts"}, {"version": 2, "session": "receipts"},
    {"version": 1, "session": True}, {"version": 1, "session": "other"},
    {"version": 1, "session": "receipts", "extra": 0}])
def test_feed_views_bad_capability_never_ready_stub(capability):
    value = view_client()
    value._receipt_ready = False
    value._ready = threading.Event()
    read_rows(value, [ready(capability)])
    assert value._failure == "invalid native feed views capability"
    assert not value._ready.is_set() and not value._receipt_ready


@pytest.mark.parametrize("enabled", [False, True])
def test_feed_views_capability_only_required_when_requested_stub(enabled):
    value = view_client()
    value._feed_views = enabled
    value._receipt_ready = False
    value._ready = threading.Event()
    read_rows(value, [ready({"version": 1, "session": "receipts"} if enabled else None)])
    assert value._ready.is_set() and value._receipt_ready and value._failure is None
    assert value._requests == value._request_metadata == {}


def response(symbol=None):
    return {"type": "feed_view", "request": 1, "session": "receipts", "leg": "feed",
            "symbol": symbol, "state": {"connected": False, "symbols": {}}}


def metadata(symbol=None):
    return {"kind": "feed_view", "identity": {"session": "receipts", "leg": "feed", "symbol": symbol},
            "deadline": ns() + 250_000_000, "delivered": False}


@pytest.mark.parametrize("mutation", ["omitted", "request_bool", "request_zero", "request_float",
    "session", "session_type", "leg", "leg_type", "symbol_type", "symbol_scope", "state", "extra", "kind"])
def test_feed_views_strict_response_required_nullable_and_identity_stub(mutation):
    row = response()
    if mutation == "omitted": del row["symbol"]
    elif mutation == "request_bool": row["request"] = True
    elif mutation == "request_zero": row["request"] = 0
    elif mutation == "request_float": row["request"] = 1.0
    elif mutation == "session": row["session"] = "other"
    elif mutation == "session_type": row["session"] = True
    elif mutation == "leg": row["leg"] = "spot"
    elif mutation == "leg_type": row["leg"] = True
    elif mutation == "symbol_type": row["symbol"] = False
    elif mutation == "symbol_scope": row["symbol"] = "UNKNOWN"
    elif mutation == "state": row["state"] = []
    elif mutation == "extra": row["accepted"] = True
    elif mutation == "kind": row["type"] = "snapshot"
    with pytest.raises(RuntimeError, match="invalid native feed view"):
        view_client()._check_feed_view(row, metadata(), 1)


@pytest.mark.parametrize("symbol", [None, "ETHUSDT", "UNKNOWN", "", "unknown-scope"])
def test_feed_views_preserves_string_scope_and_sends_required_null_stub(symbol):
    value = view_client()
    capture = Mock(return_value={})
    value._owner_rpc = capture
    assert value.feed_view(symbol, spot=True) == {}
    control, expected, deadline = capture.call_args.args
    assert control == {"type": "feed_view", "session": "receipts", "leg": "spot", "symbol": symbol}
    assert expected == {"kind": "feed_view", "identity": {k: control[k] for k in ("session", "leg", "symbol")}}
    assert 0 < deadline - ns() <= 250_000_000


@pytest.mark.parametrize("mutation", ["late", "duplicate", "wrong_kind", "bool_id", "expired"])
def test_feed_views_reader_delivered_once_and_late_fail_closed_stub(mutation):
    value = view_client()
    target = queue.Queue(maxsize=1)
    value._requests = {1: target}
    value._request_metadata = {1: metadata()}
    row = response()
    if mutation == "late": row["request"] = 2
    if mutation == "wrong_kind": row["type"] = "snapshot"
    if mutation == "bool_id": row["request"] = True
    if mutation == "expired": value._request_metadata[1]["deadline"] = ns() - 1
    read_rows(value, [row, row] if mutation == "duplicate" else [row])
    assert value._failure is not None
    assert target.get_nowait() == (response() if mutation == "duplicate" else {"accepted": False})
    first = value._failure
    value._fail("later")
    assert value._failure == first


def test_feed_views_disabled_output_retains_unknown_behavior_stub():
    value = view_client()
    value._feed_views = False
    read_rows(value, [response()])
    assert value._failure == "unknown native output"
    with pytest.raises(RuntimeError, match="capability unavailable"):
        value.feed_view()


@pytest.mark.parametrize("scope", [{"spot": 1}, {"symbol": False}, {"symbol": []}])
def test_feed_views_scope_types_before_registration_stub(scope):
    value = view_client()
    with pytest.raises(ValueError):
        value.feed_view(**scope)
    assert value._next == 0 and not value._requests


def test_feed_views_held_registration_uses_shared_total_deadline():
    value = view_client()
    holder = RegistrationHolder(value)
    holder.start()
    try:
        start = time.monotonic()
        assert value.feed_view() == {}
        elapsed = time.monotonic() - start
        assert .20 <= elapsed
        assert_bounded_fenced(value, holder, elapsed)
        assert "registration deadline" in value._failure and value._next == 0
    finally:
        holder.cleanup()
    assert value.feed_view() == {} and value._requests == value._request_metadata == {}


def test_feed_views_write_lock_uses_shared_total_deadline():
    value = view_client()
    value._write_lock.acquire()
    try:
        start = time.monotonic()
        assert value.feed_view() == {}
        assert .20 <= time.monotonic() - start < .40
    finally:
        value._write_lock.release()
    assert "write-lock deadline" in value._failure
    assert value._requests == value._request_metadata == {}


def test_feed_views_real_pipe_lost_reply_fences_no_retry():
    value = view_client()
    read_fd, write_fd = os.pipe()
    writer = os.fdopen(write_fd, "wb", buffering=0)
    os.set_blocking(write_fd, False)
    value._child.stdin = writer
    try:
        start = time.monotonic()
        assert value.feed_view() == {}
        assert .20 <= time.monotonic() - start < .40
        command = json.loads(os.read(read_fd, 16384))
        assert command == {"type": "feed_view", "request": 1, "session": "receipts", "leg": "feed", "symbol": None}
        assert value._failure == "native feed view deadline"
        assert value.feed_view() == {} and value._next == 1
        assert value._requests == value._request_metadata == {}
    finally:
        writer.close()
        os.close(read_fd)


@pytest.mark.parametrize("prior_failure", [None, "first transport failure"])
def test_feed_views_cancellation_preserves_object_and_first_failure(prior_failure):
    value = view_client()
    holder = RegistrationHolder(value)
    original = KeyboardInterrupt("owned query cancellation")
    def cancel(data, deadline):
        if prior_failure:
            value._fail(prior_failure)
        holder.start()
        raise original
    value._send_owner_completion = cancel
    try:
        start = time.monotonic()
        with pytest.raises(KeyboardInterrupt) as caught:
            value.feed_view()
        assert caught.value is original
        assert_bounded_fenced(value, holder, time.monotonic() - start)
        assert value._failure == (prior_failure or "native feed view cancelled")
    finally:
        holder.cleanup()
    assert value.feed_view() == {} and value._requests == value._request_metadata == {}


def test_feed_views_impossible_symbol_scale_rejected_before_serialization_stub(monkeypatch):
    value = view_client()
    original = json.dumps
    serialized = []
    def bounded(value, **kwargs):
        assert not isinstance(value, dict) or len(value.get("symbol") or "") <= 16384
        serialized.append(value)
        return original(value, **kwargs)
    monkeypatch.setattr("pytradekit.ws.native_public_market.json.dumps", bounded)
    value._owner_rpc = Mock(side_effect=AssertionError("impossible scope entered shared RPC"))
    with pytest.raises(ValueError, match="scope exceeds 16KiB"):
        value.feed_view("x" * 1_000_000)
    value._owner_rpc.assert_not_called()
    assert value._next == 0 and value._requests == value._request_metadata == {} and value._failure is None
    assert serialized and all(row["symbol"] == "" for row in serialized)


@pytest.mark.parametrize("unit", ["x", "é", "🙂", "\n", "\0", "\\", '"'])
def test_feed_views_wire_boundary_preserves_utf8_and_escaped_scopes_stub(unit):
    value = view_client()
    base = {"type": "feed_view", "session": value.session, "leg": "feed", "symbol": "", "request": 1}
    encode = lambda row: (json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n").encode()
    width = len(json.dumps(unit).encode()) - 2
    count = (16384 - len(encode(base))) // width
    symbol, packets = unit * count, []
    def wire(data, deadline):
        packets.append(data)
        # Stub transport only: no fabricated positive response or owner.
        value._requests[1].put_nowait({})
    value._send_owner_completion = wire
    assert value.feed_view(symbol) == {}
    assert packets == [encode({**base, "symbol": symbol})]
    assert len(packets[0]) <= 16384 and json.loads(packets[0])["symbol"] == symbol
    assert value._failure is None and value._requests == value._request_metadata == {}
    invalid = view_client()
    invalid._send_owner_completion = Mock(side_effect=AssertionError("oversized wire sent"))
    with pytest.raises(ValueError, match="16KiB"):
        invalid.feed_view(unit * (count + 1))
    invalid._send_owner_completion.assert_not_called()
    assert invalid._requests == invalid._request_metadata == {}


@pytest.mark.parametrize("partial", [False, True], ids=["full-pipe", "partial-write"])
def test_feed_views_real_pipe_send_backpressure_uses_shared_total_deadline(partial):
    """Real pipe only; no child, positive reply or owner is invented."""
    value = view_client()
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    os.set_blocking(read_fd, False)
    writer = os.fdopen(write_fd, "wb", buffering=0)
    value._child.stdin = writer
    filled = 0
    try:
        while True:
            try:
                filled += os.write(write_fd, b"!" * 16384)
                assert filled <= 8 * 1024 * 1024, "fixture pipe fill bound exceeded"
            except BlockingIOError:
                break
        freed = len(os.read(read_fd, 4096)) if partial else 0
        assert not partial or freed == 4096
        symbol = "x" * 8000
        packet = (json.dumps({"type": "feed_view", "session": value.session, "leg": "feed",
                              "symbol": symbol, "request": 1}, separators=(",", ":")) + "\n").encode()
        started = time.monotonic()
        assert value.feed_view(symbol) == {}
        elapsed = time.monotonic() - started
        assert .20 <= elapsed < .30  # Original250ms plus50ms scheduler tolerance.
        assert value._failure == "native owner completion pipe deadline"
        drained = bytearray()
        while True:
            try:
                drained.extend(os.read(read_fd, 16384))
            except BlockingIOError:
                break
        retained_fill = filled - freed
        assert drained[:retained_fill] == b"!" * retained_fill
        written = bytes(drained[retained_fill:])
        if partial:
            assert 0 < len(written) < len(packet) and written == packet[:len(written)]
        else:
            assert written == b""
        first = value._failure
        assert value.feed_view(symbol) == {} and value._next == 1
        assert value._failure == first and value._requests == value._request_metadata == {}
    finally:
        writer.close()
        os.close(read_fd)
