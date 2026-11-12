"""Strict owner validation negatives/deadlines; no fabricated positive native ACK."""
import copy
import io
import json
import os
import queue
import time

import pytest

from pytradekit.utils.time_handler import get_monotonic_timestamp_ns
from .test_native_public_market_receipts import client, owner


def expected():
    value = owner(); value['generation'] = 1
    return value


def metadata():
    value = expected()
    return {'kind': 'validation', 'owner_validation': True, 'identity': owner(),
            'phase': 'publication', 'owner_json': json.dumps(value, separators=(',', ':'), sort_keys=True),
            'deadline': get_monotonic_timestamp_ns() + 250_000_000, 'delivered': False}


def negative():
    return {'type': 'validation', 'request': 1, 'owner': expected(), 'accepted': False,
            'market_snapshot': None}


@pytest.mark.parametrize('mutation', ['request_bool', 'accepted_int', 'null_owner', 'nested_bool',
                                      'revision', 'session', 'extra', 'kind', 'bad_snapshot'])
def test_owner_validation_strict_negative_correlation(mutation):
    row = negative()
    if mutation == 'request_bool': row['request'] = True
    elif mutation == 'accepted_int': row['accepted'] = 0
    elif mutation == 'null_owner': row['owner'] = None
    elif mutation == 'nested_bool': row['owner']['generation'] = True
    elif mutation == 'revision': row['owner']['configuration_revision'] += 1
    elif mutation == 'session': row['owner']['session'] = 'other'
    elif mutation == 'extra': row['other'] = 0
    elif mutation == 'kind': row['type'] = 'snapshot'
    else: row['market_snapshot'] = []
    with pytest.raises(RuntimeError, match='invalid native owner validation response'):
        client()._check_owner_validation(row, metadata())


def test_owner_validation_matching_negative_is_not_positive_permission():
    value = client(); value._check_owner_validation(negative(), metadata())
    assert value._failure is None


@pytest.mark.parametrize('mutation', ['late', 'duplicate', 'wrong_kind'])
def test_owner_validation_real_reader_rejects_late_duplicate_and_kind(mutation):
    value = client(); value._closed = True
    target = queue.Queue(maxsize=1)
    value._requests = {1: target}; value._request_metadata = {1: metadata()}
    row = negative()
    if mutation == 'late': row['request'] = 2
    if mutation == 'wrong_kind': row['type'] = 'snapshot'
    rows = [row, row] if mutation == 'duplicate' else [row]
    value._child.stdout = io.BytesIO(b''.join((json.dumps(item)+'\n').encode() for item in rows))
    value._read()
    assert value._failure is not None
    if mutation == 'duplicate': assert target.get_nowait() == negative()
    else: assert target.get_nowait() == {'accepted': False}
    first = value._failure; value._fail('later'); assert value._failure == first


def test_owner_validation_write_lock_total_deadline():
    value = client(); value._write_lock.acquire()
    started = time.monotonic()
    try:
        assert value.validate_owner(expected(), 'publication') == {}
    finally:
        value._write_lock.release()
    assert .20 <= time.monotonic() - started < .40
    assert 'write-lock deadline' in value._failure
    assert value._requests == value._request_metadata == {}


def test_owner_validation_real_pipe_no_reply_preserves_legacy_command_fields():
    value = client(); read_fd, write_fd = os.pipe(); writer = os.fdopen(write_fd, 'wb', buffering=0)
    os.set_blocking(write_fd, False); value._child.stdin = writer
    try:
        started = time.monotonic()
        assert value.validate_owner(expected(), 'publication') == {}
        assert .20 <= time.monotonic() - started < .40
        command = json.loads(os.read(read_fd, 16384))
        assert set(command) == {'type', 'request', 'candidate', 'symbol', 'venue', 'session', 'phase'}
        assert command['type'] == 'validate' and command['phase'] == 'publication'
        assert value._failure == 'native owner validation deadline'
        assert value._requests == value._request_metadata == {}
    finally:
        writer.close(); os.close(read_fd)


def test_owner_validation_cancellation_keeps_original_object():
    value = client(); original = KeyboardInterrupt('owned validation cancellation')
    def cancel(data, deadline): raise original
    value._send_owner_completion = cancel
    with pytest.raises(KeyboardInterrupt) as caught:
        value.validate_owner(expected(), 'publication')
    assert caught.value is original
    assert value._failure == 'native owner validation cancelled'
    assert value._requests == value._request_metadata == {}
