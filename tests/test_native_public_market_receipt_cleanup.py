"""A secondary cleanup failure cannot replace the original receipt cancellation."""
import time

import pytest

from .test_native_public_market_receipts import client, completion, owner


def test_receipt_secondary_cleanup_failure_preserves_original_cancellation_and_first_error():
    value = client()
    original = KeyboardInterrupt('original receipt cancellation')
    secondary = OSError('secondary cleanup failure ' + 'x' * 300)
    finished = []
    real_finish = value._finish_owner_completion

    def cancel_send(data, deadline):
        value._fail('original transport error')
        raise original

    def finish_then_fail(metadata, deadline):
        finished.append(real_finish(metadata, deadline))
        raise secondary

    value._send_owner_completion = cancel_send
    value._finish_owner_completion = finish_then_fail
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt) as caught:
        value.complete_owner(owner(), completion())
    assert caught.value is original
    assert time.monotonic() - started < .40
    assert finished == [True], 'real retirement cleanup must run before secondary failure'
    assert value._requests == value._request_metadata == {}
    assert value._failure == 'original transport error'
    diagnostic = value.owner_completion_cleanup_error
    assert type(diagnostic) is str and 0 < len(diagnostic) <= 256
    assert diagnostic.startswith('secondary cleanup failure')
