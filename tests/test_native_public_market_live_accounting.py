"""New profile negotiates a separate exact capability before becoming ready."""
import io
import json

import pytest
from .test_native_public_market_receipts import client


@pytest.mark.parametrize("capability", [None, {"version": True, "session": "receipts"},
    {"version": 1, "session": "other"}, {"version": 1, "session": "receipts", "extra": 0}])
def test_live_accounting_missing_or_invalid_capability(capability):
    value = client()
    value._owner_retirement = True
    value._live_accounting = True
    value._receipt_ready = False
    value._closed = True
    row = {"type": "ready", "version": 3, "production_permission": False,
           "owner_completion_receipts": {"version": 1, "session": value.session},
           "owner_retirement": {"version": 1, "session": value.session}}
    if capability is not None:
        row["live_accounting"] = capability
    value._child = type("Child", (), {"stdout": io.BytesIO((json.dumps(row)+"\n").encode())})()
    value._read()
    assert value._failure is not None and not value._receipt_ready
    assert "live accounting capability" in value._failure


def test_live_accounting_request_identity_exhaustion_has_no_send_or_registration():
    from unittest.mock import MagicMock
    value = client()
    value._live_accounting = True
    value._next = 2**64-1
    value.send = MagicMock(side_effect=AssertionError("exhausted identity must not send"))
    with pytest.raises(RuntimeError, match="request identity exhausted"):
        value.request({"type": "snapshot"})
    assert value._next == 2**64-1
    assert not value._requests and not value._request_metadata
    assert value._failure == "native request identity exhausted"
    value.send.assert_not_called()
