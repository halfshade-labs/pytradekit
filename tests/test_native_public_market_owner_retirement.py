"""Separate retirement capability fails closed when a receipt-only child ignores it."""
import io
import json

import pytest
from pytradekit.ws.native_public_market import NativePublicMarket
from .test_native_public_market_receipts import client


@pytest.mark.parametrize("capability", [None, {"version": True, "session": "receipts"},
    {"version": 1, "session": "other"}, {"version": 1, "session": "receipts", "extra": 0}])
def test_owner_retirement_missing_or_invalid_capability(capability):
    value = client()
    value._owner_retirement = True
    value._receipt_ready = False
    value._closed = True
    row = {"type": "ready", "version": 3, "production_permission": False,
           "owner_completion_receipts": {"version": 1, "session": value.session}}
    if capability is not None:
        row["owner_retirement"] = capability
    value._child = type("Child", (), {"stdout": io.BytesIO((json.dumps(row)+"\n").encode())})()
    value._read()
    assert value._failure is not None and not value._receipt_ready


@pytest.mark.parametrize("option,receipts", [(1, True), (None, True), (True, False)])
def test_owner_retirement_option_strict_and_requires_receipts(option, receipts):
    with pytest.raises(ValueError, match="owner_retirement"):
        NativePublicMarket("absent", "", {}, None, owner_completion_receipts=receipts, owner_retirement=option)
