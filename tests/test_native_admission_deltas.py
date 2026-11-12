"""Stub-only output negotiation tests; no actual child or owner acceptance claims."""
import hashlib
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pytradekit.ws.native_public_market import NativePublicMarket
from .test_native_public_market_receipts import client


PROFILE = dict(owner_completion_receipts=True, owner_retirement=True, live_accounting=True)


def header():
    return {"version": 3, "configuration_transactions": True, "diagnostics": False,
            "native": {"session": "receipts"}}


@pytest.mark.parametrize("invalid", [None, 0, 1, "v1", [], {}])
def test_admission_deltas_strict_bool_before_io(invalid):
    with pytest.raises(ValueError, match="admission_deltas"):
        NativePublicMarket(None, None, {}, None, admission_deltas=invalid)


@pytest.mark.parametrize("missing", list(PROFILE))
def test_admission_deltas_requires_each_explicit_profile_option(missing):
    options = {**PROFILE, missing: False}
    with pytest.raises(ValueError):
        NativePublicMarket(None, None, header(), None, **options, admission_deltas=True)


@pytest.mark.parametrize("field,value", [("version", True), ("version", 2),
    ("configuration_transactions", False), ("native", None), ("diagnostics", True),
    ("diagnostics", None), ("native_event_diagnostics", True), ("native_event_diagnostics", 0)])
def test_admission_deltas_rejects_incompatible_header_before_io(field, value):
    with pytest.raises(ValueError, match="admission_deltas"):
        NativePublicMarket(None, None, {**header(), field: value}, None,
                           **PROFILE, admission_deltas=True)


@pytest.mark.parametrize("enabled", [None, False, True])
def test_admission_deltas_argv_stub_keeps_original_prefix(tmp_path, monkeypatch, enabled):
    # This file is hashed only; Popen and the reader are stubs, never executed.
    binary = tmp_path / "identity-only"
    binary.write_bytes(b"stub identity only")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    process = SimpleNamespace(stdin=Mock(), stdout=Mock())
    popen = Mock(return_value=process)
    monkeypatch.setattr("pytradekit.ws.native_public_market.subprocess.Popen", popen)
    monkeypatch.setattr("pytradekit.ws.native_public_market.os.set_blocking", Mock())
    monkeypatch.setattr("pytradekit.ws.native_public_market.threading.Thread", Mock())
    monkeypatch.setattr(NativePublicMarket, "send", Mock())
    options = {} if enabled is None else {"admission_deltas": enabled}
    NativePublicMarket(binary, digest, header(), Mock(), **PROFILE, **options)
    expected = [str(binary), "--live", "--owner-completion-receipts=v1",
                "--owner-retirement=v1", "--live-accounting=v1"]
    assert popen.call_args.args[0] == expected + (["--admission-deltas=v1"] if enabled else [])
    assert popen.call_args.args[0][2] == "--owner-completion-receipts=v1"


def readiness(capability):
    row = {"type": "ready", "version": 3, "production_permission": False}
    for field in ("owner_completion_receipts", "owner_retirement", "live_accounting"):
        row[field] = {"version": 1, "session": "receipts"}
    if capability is not None:
        row["admission_deltas"] = capability
    return row


def read_stub(rows, enabled=True):
    value = client()
    value._owner_retirement = value._live_accounting = True
    value._admission_deltas = enabled
    value._receipt_ready = False
    value._ready = threading.Event()
    value._closed = True  # Finite stub EOF is intentional, not active transport.
    value._on_delta = Mock()
    value._child.stdout = io.BytesIO("".join(json.dumps(row) + "\n" for row in rows).encode())
    value._read()
    return value


@pytest.mark.parametrize("capability", [None, {}, {"version": True, "session": "receipts"},
    {"version": 1.0, "session": "receipts"}, {"version": 2, "session": "receipts"},
    {"version": 1, "session": "other"}, {"version": 1, "session": True},
    {"version": 1, "session": "receipts", "extra": 0}])
def test_admission_deltas_old_binary_or_bad_capability_never_ready(capability):
    value = read_stub([readiness(capability)])
    assert not value._ready.is_set() and not value._receipt_ready
    assert "admission deltas capability" in value._failure
    value._on_delta.assert_not_called()


def test_admission_deltas_capability_is_output_support_only_stub():
    value = read_stub([readiness({"version": 1, "session": "receipts"})])
    assert value._ready.is_set() and value._receipt_ready and value._failure is None
    assert not value._requests and not value._request_metadata
    value._on_delta.assert_not_called()


def test_admission_deltas_default_readiness_needs_no_new_capability():
    value = read_stub([readiness(None)], enabled=False)
    assert value._ready.is_set() and value._receipt_ready and value._failure is None


@pytest.mark.parametrize("observations", [[{"states": [], "admissions": [{}]}],
    [{"admissions": []}], [{"admissions": None}], [None], [], {}, None])
def test_admission_deltas_rejects_wrong_optin_shape_stub(observations):
    value = read_stub([readiness({"version": 1, "session": "receipts"}),
                       {"type": "delta", "observations": observations}])
    assert "admission delta observations" in value._failure
    value._on_delta.assert_not_called()


def test_admission_deltas_empty_observations_default_path_unchanged_stub():
    delta = {"type": "delta", "observations": []}
    value = read_stub([readiness(None), delta], enabled=False)
    assert value._failure is None
    value._on_delta.assert_called_once_with(delta)


def test_admission_deltas_reader_forwards_original_batch_and_seal_stub():
    # Opaque values test transport shape only; they confer no owner permission.
    delta = {"type": "delta", "version": 3, "action": 9, "admission_seal_through": 12,
             "observations": [{"admissions": [{"opaque": "first"}, {"opaque": "second"}]}]}
    for enabled in (False, True):
        value = read_stub([readiness({"version": 1, "session": "receipts"}), delta], enabled)
        assert value._failure is None
        value._on_delta.assert_called_once_with(delta)


def test_admission_deltas_cannot_arrive_before_capability_stub():
    value = read_stub([{"type": "delta", "observations": [{"admissions": [{}]}]}])
    assert not value._ready.is_set() and value._failure is not None
    value._on_delta.assert_not_called()
