"""Receipt-only facade denials/deadlines; positive effects require real-child tests."""
import copy
import io
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from pytradekit.utils.time_handler import get_monotonic_timestamp_ns
from pytradekit.ws.native_public_market import NativePublicMarket
from .test_native_public_market_failure import failure_client


def owner():
    return {'session': 'receipts', 'candidate': 6, 'symbol': 'ALPHAUSDT', 'venue': 'BN',
            'configuration_revision': 1}


def completion():
    return {'phase': 'worker', 'result': {'outcome': 'error'}}


def client():
    value = failure_client()
    value._failure_lock = threading.Lock()
    value.session = 'receipts'
    value._owner_completion_receipts = True
    value._receipt_ready = True
    value._request_metadata = {}
    return value


def denial():
    return {'type': 'owner_completion', 'request': 1, **owner(), **completion(),
            'accepted': False, 'recorded': False, 'terminal': False, 'error': 'unknown_owner'}


def metadata():
    return {'kind': 'owner_completion', 'identity': owner(), 'completion': completion(),
            'deadline': get_monotonic_timestamp_ns() + 250_000_000, 'delivered': False}


@pytest.mark.parametrize('field,value', [('candidate', True), ('candidate', 0), ('candidate', 2**64),
    ('configuration_revision', True), ('configuration_revision', -1), ('session', 'x'*41),
    ('symbol', 'lower'), ('venue', 'other')])
def test_owner_receipt_strict_identity(field, value):
    bad = owner(); bad[field] = value
    with pytest.raises(ValueError):
        NativePublicMarket._completion_identity(bad)


@pytest.mark.parametrize('value', [None, 1, 'true'])
def test_owner_receipt_option_requires_actual_bool_before_construction(value):
    with pytest.raises(ValueError, match='must be bool'):
        NativePublicMarket('absent', '', {}, None, owner_completion_receipts=value)


@pytest.mark.parametrize('value', [
    {'phase': 'publication', 'result': {'success': False}},
    {'phase': 'publication', 'result': {'success': 1, 'completed_ms': 1}},
    {'phase': 'publication', 'result': {'success': True, 'completed_ms': True}},
    {'phase': 'publication', 'result': {'success': False, 'completed_ms': 1}},
    {'phase': 'worker', 'result': {'outcome': 'success'}},
    {'phase': 'worker', 'result': {'outcome': 'error', 'extra': 1}},
])
def test_owner_receipt_required_nullable_and_phase_result(value):
    with pytest.raises(ValueError):
        NativePublicMarket._completion_result(value)


@pytest.mark.parametrize('field,value', [('request', True), ('session', 'other'), ('candidate', True),
    ('configuration_revision', 2), ('phase', 'publication'), ('result', {'outcome': 'expired'}),
    ('accepted', 0), ('recorded', True), ('terminal', 0), ('error', None), ('extra', 0)])
def test_owner_receipt_denial_schema_correlation(field, value):
    row = denial(); row[field] = value
    with pytest.raises(RuntimeError):
        client()._check_owner_completion(row, metadata())


def test_owner_receipt_well_formed_negative_is_not_transport_failure():
    value = client(); value._check_owner_completion(denial(), metadata())
    assert value._failure is None


@pytest.mark.parametrize('enabled,capability', [(False, None), (True, None),
    (True, {'version': True, 'session': 'receipts'}),
    (True, {'version': 1, 'session': 'other'})])
def test_owner_receipt_missing_capability_or_disabled_unsolicited_fails(enabled, capability):
    value = client(); value._owner_completion_receipts = enabled; value._receipt_ready = False
    value._closed = True
    row = ({'type': 'ready', 'version': 3, 'production_permission': False} if enabled else denial())
    if capability is not None:
        row['owner_completion_receipts'] = capability
    value._child.stdout = io.BytesIO((json.dumps(row)+'\n').encode())
    value._read()
    assert value._failure is not None and value._receipt_ready is False


def test_owner_receipt_write_lock_consumes_single_total_deadline():
    value = client(); value._write_lock.acquire()
    before = time.monotonic()
    try:
        assert value.complete_owner(owner(), completion()) == {}
    finally:
        value._write_lock.release()
    elapsed = time.monotonic() - before
    assert .20 <= elapsed < .35
    assert 'write-lock deadline' in value._failure
    assert value._requests == value._request_metadata == {}


def test_owner_receipt_real_pipe_missing_reply_fences_without_retry():
    value = client(); read_fd, write_fd = os.pipe()
    writer = os.fdopen(write_fd, 'wb', buffering=0); os.set_blocking(write_fd, False)
    value._child.stdin = writer
    before = time.monotonic()
    try:
        assert value.complete_owner(owner(), completion()) == {}
        elapsed = time.monotonic() - before
        sent = os.read(read_fd, 16384)
        assert json.loads(sent)['type'] == 'owner_complete'
        assert sent.count(b'\n') == 1
        assert .20 <= elapsed < .35
        first = value._failure
        assert value.complete_owner(owner(), completion()) == {}
        assert value._failure == first
        assert value._requests == value._request_metadata == {}
    finally:
        writer.close(); os.close(read_fd)


def test_owner_receipt_cancellation_preserves_object_and_first_error():
    value = client(); cancellation = KeyboardInterrupt('owned cancellation')
    def cancel(data, deadline):
        value._fail('first transport error')
        raise cancellation
    value._send_owner_completion = cancel
    with pytest.raises(KeyboardInterrupt) as caught:
        value.complete_owner(owner(), completion())
    assert caught.value is cancellation
    assert value._failure == 'first transport error'
    assert value._requests == value._request_metadata == {}
