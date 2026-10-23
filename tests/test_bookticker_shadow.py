"""Public probe validation and diagnostics must not initialize account clients."""
import argparse
import json

import pytest

from pytradekit.ws import bookticker_shadow as probe


@pytest.mark.parametrize('value', ['0', '-1', '3601'])
def test_duration_is_bounded(value):
    with pytest.raises(argparse.ArgumentTypeError):
        probe._bounded_duration(value)


@pytest.mark.parametrize('value', ['', ','.join(f'S{index}' for index in range(129)), '../secret', 'BTC/USDT'])
def test_symbols_are_bounded_public_identifiers(value):
    with pytest.raises(argparse.ArgumentTypeError):
        probe._parse_symbols(value)


def test_candidate_diagnostics_exports_only_allowlisted_integer_counts(tmp_path):
    source = tmp_path / 'synthetic-diagnostics.json'
    source.write_text(json.dumps({'account': 'must-not-appear', 'candidate_discard_counts': {
        'public_feed_invalidation': {'generation_changed': 3, 'private-value': 4},
        'private-stage': {'generation_changed': 2},
    }}))
    result = probe._read_candidate_counts(source)
    assert result == {'available': True, 'candidate_discard_counts': {
        'public_feed_invalidation': {'generation_changed': 3},
    }}
    assert not probe._read_candidate_counts(None)['available']


def test_observation_uses_only_local_sink_and_reports_verified_cleanup(monkeypatch):
    ticks = iter(range(0, 10_000_000_000, 250_000_000))
    monkeypatch.setattr(probe, 'get_monotonic_timestamp_ns', lambda: next(ticks))
    class Feed:
        def __init__(self, logger, sink):
            self.sink, self.closed = sink, False
        def update_bookticker_stream(self, symbols):
            self.sink.put_nowait({'s':symbols[0]})
        def get_public_feed_health(self):
            return {'joint_unavailable_ms': {'BTCUSDT': 0}, 'cleanup_pending': 0,
                    'selector_alive': False, 'cleanup_alive': False, 'cleanup_failures': {}}
        def close(self):
            self.closed = True
    result = probe.observe(('BTCUSDT',), 1, Feed)
    assert result['public_only']
    assert result['delivered_to_local_sink'] == 1
    assert result['cleanup_complete']


@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt])
def test_main_audits_cleanup_and_sanitizes_observation_error_or_interrupt(monkeypatch, capsys, failure):
    instances = []
    class Feed:
        def __init__(self, logger, sink):
            self.closed = False
            instances.append(self)
        def update_bookticker_stream(self, symbols):
            raise failure('/private/never-print-this-path')
        def close(self):
            self.closed = True
        def get_public_feed_health(self):
            return {'cleanup_pending': 0, 'selector_alive': False, 'cleanup_alive': False, 'cleanup_failures': {}}
    monkeypatch.setattr(probe, 'RedundantBookTickerFeed', Feed)
    assert probe.main(['--duration-s', '1']) == 1
    text = capsys.readouterr().out
    result = json.loads(text)
    assert result['error_type'] == failure.__name__
    assert result['cleanup_complete']
    assert instances[0].closed
    assert 'never-print-this-path' not in text


def test_malformed_diagnostics_outputs_no_path_or_traceback(tmp_path, capsys):
    path = tmp_path / 'sensitive-path.json'
    path.write_text('invalid json')
    assert probe.main(['--cea-diagnostics', str(path)]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)['error_type'] == 'JSONDecodeError'
    assert str(path) not in output.out + output.err
    assert 'Traceback' not in output.out + output.err


def test_main_close_failure_is_nonzero_and_reports_explicit_cleanup_failure(monkeypatch, capsys):
    ticks = iter(range(0, 50_000_000_000, 250_000_000))
    monkeypatch.setattr(probe, 'get_monotonic_timestamp_ns', lambda: next(ticks))
    class Feed:
        def __init__(self, logger, sink):
            pass
        def update_bookticker_stream(self, symbols):
            pass
        def close(self):
            raise RuntimeError('private-close-detail')
        def get_public_feed_health(self):
            return {'cleanup_pending': 0, 'selector_alive': False, 'cleanup_alive': False, 'cleanup_failures': {}}
    monkeypatch.setattr(probe, 'RedundantBookTickerFeed', Feed)
    assert probe.main(['--duration-s', '1']) == 1
    text = capsys.readouterr().out
    result = json.loads(text)
    assert result['cleanup_state'] == 'failed'
    assert result['cleanup_error_type'] == 'RuntimeError'
    assert not result['cleanup_complete']
    assert 'private-close-detail' not in text


def test_observe_incomplete_cleanup_is_bounded_and_explicit(monkeypatch):
    ticks = iter(range(0, 50_000_000_000, 500_000_000))
    monkeypatch.setattr(probe, 'get_monotonic_timestamp_ns', lambda: next(ticks))
    class Feed:
        def __init__(self, logger, sink):
            pass
        def update_bookticker_stream(self, symbols):
            raise RuntimeError('sanitized')
        def close(self):
            pass
        def get_public_feed_health(self):
            return {'cleanup_pending': 1, 'selector_alive': False, 'cleanup_alive': True,
                    'cleanup_failures': {'route': 'worker_still_alive'}}
    report = probe.observe(('BTCUSDT',), 1, Feed)
    assert not report['cleanup_complete']
    assert report['cleanup_state'] == 'pending'
    assert report['cleanup_pending'] == 1
    assert report['error_type'] == 'RuntimeError'


def test_main_cleanup_wait_interrupt_keeps_started_feed_ownership(monkeypatch, capsys):
    instances = []
    class Feed:
        def __init__(self, logger, sink):
            self.closed = False
            instances.append(self)
        def update_bookticker_stream(self, symbols):
            raise RuntimeError('private-observation-detail')
        def close(self):
            self.closed = True
        def get_public_feed_health(self):
            return {'cleanup_pending': 1, 'selector_alive': False, 'cleanup_alive': True,
                    'cleanup_failures': {'route': 'worker_still_alive'}}
    class InterruptedWait:
        def wait(self, timeout):
            raise KeyboardInterrupt('private-interrupt-detail')
    monkeypatch.setattr(probe, 'RedundantBookTickerFeed', Feed)
    monkeypatch.setattr(probe, 'Event', InterruptedWait)
    assert probe.main(['--duration-s', '1']) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert instances[0].closed
    assert result['cleanup_state'] == 'interrupted'
    assert result['cleanup_pending'] == 1
    assert not result['cleanup_complete']
    assert result['cleanup_error_type'] == 'KeyboardInterrupt'
    assert 'private-' not in output.out + output.err
