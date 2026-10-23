"""Bounded public-only observation CLI. No account/config/secret loaders."""
import argparse
import json
import logging
from pathlib import Path
from queue import Empty, Queue
from threading import Event

from pytradekit.utils.time_handler import get_monotonic_timestamp_ns
from pytradekit.ws.redundant_bookticker import RedundantBookTickerFeed

MAX_DURATION_S = 3600
MAX_SYMBOLS = 128
CANDIDATE_STAGES = frozenset((
    'eligibility_result', 'publication_handoff', 'publication_check',
    'publication_result', 'public_feed_invalidation', 'unknown',
))
CANDIDATE_REASONS = frozenset((
    'worker_error', 'expired_before_dispatch', 'stopping', 'ingress_overflow',
    'deadline_elapsed', 'state_replaced', 'generation_changed', 'pool_changed',
    'notional_changed', 'disarmed', 'persistence_lost', 'cooldown', 'book_missing',
    'venue_ineligible', 'pool_metadata_invalid', 'quote_invalid', 'premium_invalid',
    'premium_threshold_rejected', 'eligibility_rejected', 'publisher_rejected', 'unknown',
))


def _bounded_duration(value):
    duration = int(value)
    if not 1 <= duration <= MAX_DURATION_S:
        raise argparse.ArgumentTypeError('duration must be between 1 and 3600 seconds')
    return duration


def _parse_symbols(value):
    symbols = tuple(sorted(set(symbol.strip().upper() for symbol in value.split(',') if symbol.strip())))
    if not 1 <= len(symbols) <= MAX_SYMBOLS:
        raise argparse.ArgumentTypeError('provide between 1 and 128 public symbols')
    if any(not symbol.isascii() or not symbol.isalnum() or len(symbol) > 32 for symbol in symbols):
        raise argparse.ArgumentTypeError('symbols must be ASCII exchange identifiers')
    return symbols


def _read_candidate_counts(path):
    if path is None:
        return {'available': False, 'reason': 'standalone_public_probe_has_no_cea_candidates'}
    source = Path(path)
    if source.stat().st_size > 262144:
        raise ValueError('CEA diagnostics exceeds 256 KiB')
    data = json.loads(source.read_text())
    counts = data.get('candidate_discard_counts', {})
    selected = {stage: {reason: value for reason, value in reasons.items()
                        if reason in CANDIDATE_REASONS and type(value) is int and value >= 0}
                for stage, reasons in counts.items() if stage in CANDIDATE_STAGES and isinstance(reasons, dict)}
    return {'available': True, 'candidate_discard_counts': selected}


def _audit_cleanup(feed):
    if feed is None:
        return {'cleanup_complete': True, 'cleanup_state': 'not_started', 'cleanup_pending': 0,
                'cleanup_failures': {}}
    close_error = None
    try:
        feed.close()
    except (Exception, KeyboardInterrupt) as error:
        close_error = type(error).__name__
    deadline = get_monotonic_timestamp_ns() + 5_000_000_000
    while True:
        try:
            health = feed.get_public_feed_health()
        except (Exception, KeyboardInterrupt) as error:
            return {'cleanup_complete': False, 'cleanup_state': 'unknown', 'cleanup_pending': None,
                    'cleanup_failures': {}, 'cleanup_error_type': close_error or type(error).__name__}
        pending = health.get('cleanup_pending')
        active = (pending is None or pending or health.get('selector_alive', True)
                  or health.get('cleanup_alive', True))
        if not active or get_monotonic_timestamp_ns() >= deadline:
            break
        try:
            Event().wait(0.05)
        except KeyboardInterrupt:
            return {'cleanup_complete': False, 'cleanup_state': 'interrupted',
                    'cleanup_pending': pending, 'cleanup_failures': health.get('cleanup_failures', {}),
                    'cleanup_error_type': 'KeyboardInterrupt'}
    result = {'cleanup_complete': not active and close_error is None,
              'cleanup_state': 'failed' if close_error else ('pending' if active else 'verified'),
              'cleanup_pending': pending, 'cleanup_failures': health.get('cleanup_failures', {})}
    if close_error:
        result['cleanup_error_type'] = close_error
    return result


def observe(symbols, duration_s, feed_factory=None):
    """Audit owned cleanup on success, error and interrupt; never expose exception text."""
    sink = Queue(maxsize=4096)
    logger = logging.getLogger('public-bookticker-shadow')
    logger.addHandler(logging.NullHandler())
    feed, delivered, report = None, 0, {}
    started = get_monotonic_timestamp_ns()
    try:
        feed = (feed_factory or RedundantBookTickerFeed)(logger, sink)
        feed.update_bookticker_stream(symbols)
        deadline = started + duration_s * 1_000_000_000
        while get_monotonic_timestamp_ns() < deadline:
            try:
                sink.get(timeout=0.05)
                delivered += 1
            except Empty:
                pass
        report = feed.get_public_feed_health()
    except (Exception, KeyboardInterrupt) as error:
        report = {'error_type': type(error).__name__}
    finally:
        cleanup = _audit_cleanup(feed)
    report.update(cleanup)
    report.update(duration_ms=(get_monotonic_timestamp_ns() - started) // 1_000_000,
                  delivered_to_local_sink=delivered, public_only=True,
                  limitations=['same_host_and_network_common_causes', 'natural_samples_only',
                               'bounded_recovery_sample_not_population_percentile'])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbols', type=_parse_symbols, default=('BTCUSDT', 'ETHUSDT'))
    parser.add_argument('--duration-s', type=_bounded_duration, default=60)
    parser.add_argument('--cea-diagnostics', help='Optional existing synthetic/approved CEA diagnostics JSON; only discard counts are read')
    args = parser.parse_args(argv)
    try:
        candidates = _read_candidate_counts(args.cea_diagnostics)
        report = observe(args.symbols, args.duration_s)
        report['cea_candidates'] = candidates
    except (Exception, KeyboardInterrupt) as error:
        report = {'error_type': type(error).__name__, 'public_only': True,
                  **_audit_cleanup(None)}
    print(json.dumps(report, sort_keys=True))
    return 0 if report['cleanup_complete'] and 'error_type' not in report else 1


if __name__ == '__main__':
    raise SystemExit(main())
