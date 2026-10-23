"""Synthetic full-BBO failure matrix; no exchange/account/network access."""
from queue import Queue
from threading import Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pytradekit.utils.dynamic_types import BinanceWebSocket
from pytradekit.ws.binance_ws import BinanceWsManager
from pytradekit.ws.redundant_bookticker import (
    INGRESS_SERIAL, LOGICAL_FEED_ID, PHYSICAL_ROUTE, SYMBOL_EPOCH,
    RedundantBookTickerFeed, RedundantBookTickerOptions,
)

NOW = 1_800_000_000_000
RECEIVE = BinanceWebSocket.run_time_ms.value
ID = BinanceWebSocket.connection_id.value
GEN = BinanceWebSocket.connection_generation.value


class Clock:
    now = NOW
    mono = 0

    def wall_ms(self):
        return self.now

    def monotonic_ms(self):
        return self.mono

    def advance(self, elapsed):
        self.now += elapsed
        self.mono += elapsed


class Physical:
    def __init__(self, ingress):
        self.ingress = ingress
        self.stream = str(id(self))
        self.generation = 1
        self.connected = True
        self.closed = False
        self.targets = frozenset()
        self.applied = Event()
        self.block = None

    def update_bookticker_stream(self, targets):
        if self.block is not None:
            self.block.wait()
        self.targets = targets
        self.applied.set()

    def is_current_bookticker_message(self, message):
        return (not self.closed and self.connected and message.get(ID) == self.stream
                and message.get(GEN) == self.generation)

    def get_public_feed_state(self):
        return {'stream_id': self.stream, 'generation': self.generation, 'connected': self.connected}

    def get_public_feed_health(self, max_age_ms):
        return {'connected': self.connected, 'generation': self.generation}

    def close(self):
        self.closed = True
        self.connected = False
        if self.block:
            self.block.set()

    def quote(self, update=1, **changes):
        frame = dict({'s': 'BTCUSDT', 'u': update, 'b': '100', 'a': '101',
                      'B': '2', 'A': '3', 'E': NOW, 'T': NOW, RECEIVE: NOW,
                      ID: self.stream, GEN: self.generation}, **changes)
        self.ingress.put_nowait(frame)
        return frame


@pytest.fixture
def rig(monkeypatch):
    # Drive selector deterministically while still exercising real per-route workers.
    monkeypatch.setattr(RedundantBookTickerFeed, '_run', lambda self: self._stop.wait())
    clock, managers = Clock(), []
    def factory(ingress):
        value = Physical(ingress)
        managers.append(value)
        return value
    feed = RedundantBookTickerFeed(Mock(), Queue(maxsize=32),
        RedundantBookTickerOptions(rotation_ms=10_000, stagger_ms=5_000,
                                  replacement_timeout_ms=500, ingress_capacity=4),
        factory, clock)
    feed.update_bookticker_stream(('BTCUSDT', 'ETHUSDT'))
    assert all(value.applied.wait(1) for value in managers)
    yield feed, managers, clock
    feed.close()


def drain(feed):
    result = []
    while not feed._output.empty():
        result.append(feed._output.get_nowait())
    return result


def epoch(feed, symbol='BTCUSDT'):
    return feed.get_public_feed_state()['symbols'][symbol]['epoch']


def test_single_failure_duplicate_backup_preserves_epoch_and_original_times(rig):
    feed, (a, b), clock = rig
    a.quote(10)
    b.quote(10)
    feed._poll()
    original = drain(feed)[0]
    assert feed.is_current_bookticker_message(original)  # The existing consumer cached this BBO.
    token = feed.get_public_feed_state()['stream_id']
    a.connected = False
    feed._poll()
    assert feed.get_public_feed_state()['stream_id'] == token
    assert epoch(feed) == 0
    assert not feed.is_current_bookticker_message(original)
    assert drain(feed) == []
    clock.advance(10)
    b.quote(12, E=clock.now, **{RECEIVE: clock.now})  # Non-contiguous IDs are valid.
    feed._poll()
    current = drain(feed)[0]
    assert current[RECEIVE] == clock.now
    assert current[ID] == b.stream
    assert current[SYMBOL_EPOCH] == 0
    assert feed.is_current_bookticker_message(current)
    assert len(feed.get_public_feed_health()['switches']) == 1


@pytest.mark.parametrize('changes', [
    {'E': NOW - 1001}, {'E': NOW + 1}, {'b': 'NaN'}, {'B': '0'},
    {'u': 8}, {'u': True}, {'u': 10, 'a': '102'}, {'u': 10, 'E': NOW - 1},
])
def test_invalid_backup_never_covers_newer_primary(rig, changes):
    feed, (a, b), _ = rig
    a.quote(10)
    feed._poll()
    old = drain(feed)[0]
    b.quote(10, **changes)
    feed._poll()
    a.connected = False
    feed._poll()
    assert epoch(feed) == 1
    assert not feed.get_public_feed_state()['symbols']['BTCUSDT']['available']
    assert not feed.is_current_bookticker_message(old)
    assert drain(feed) == []


def test_joint_outage_is_symbol_local_and_old_epochs_never_revive(rig):
    feed, (a, b), clock = rig
    a.quote(10)
    a.quote(20, s='ETHUSDT')
    b.quote(10)
    b.quote(20, s='ETHUSDT')
    feed._poll()
    old = drain(feed)
    clock.advance(1001)
    b.quote(21, s='ETHUSDT', E=clock.now, **{RECEIVE: clock.now})
    feed._poll()
    assert epoch(feed) == 1
    assert epoch(feed, 'ETHUSDT') == 1  # There was a real interval with no fresh ETH.
    assert not any(feed.is_current_bookticker_message(frame) for frame in old)
    assert feed.get_public_feed_state()['symbols']['ETHUSDT']['available']
    assert feed.get_public_feed_health()['joint_unavailable_ms']['BTCUSDT'] >= 0


def test_new_generation_replay_does_not_reset_highwater(rig):
    feed, (a, b), _ = rig
    a.quote(10)
    feed._poll()
    queued = drain(feed)[0]
    a.generation += 1
    a.quote(1)
    b.quote(9)
    feed._poll()
    assert epoch(feed) == 1
    assert drain(feed) == []
    assert not feed.is_current_bookticker_message(queued)
    a.quote(11)
    feed._poll()
    assert drain(feed)[0][SYMBOL_EPOCH] == 1


def test_generation_change_cannot_accept_queued_old_callbacks(rig):
    feed, (a, _), _clock = rig
    a.quote(1)
    a.generation += 1
    feed._poll()
    assert drain(feed) == []
    assert feed.get_public_feed_health()['rejections']['retired_generation'] == 1


def test_removed_and_readded_symbol_cannot_reuse_queued_epoch(rig):
    feed, (a, _), _ = rig
    a.quote(1)
    feed._poll()
    old = drain(feed)[0]
    feed.update_bookticker_stream(('ETHUSDT',))
    a.quote(2)
    feed._poll()
    feed.update_bookticker_stream(('BTCUSDT', 'ETHUSDT'))
    assert not feed.is_current_bookticker_message(old)
    assert drain(feed) == []


def test_ingress_overflow_is_bounded_and_retires_old_queue(rig):
    feed, (a, _), _ = rig
    a.quote(1)
    feed._poll()
    old = drain(feed)[0]
    for value in range(2, 8):
        a.quote(value)
    assert a.ingress.queue.qsize() == 4
    feed._poll()
    assert feed.get_public_feed_health()['ingress_drops'] == 2
    assert epoch(feed) == 1
    assert not feed.is_current_bookticker_message(old)
    assert drain(feed) == []
    a.quote(8)
    feed._poll()
    assert drain(feed)[0][SYMBOL_EPOCH] == 1


def test_output_overflow_retires_previously_queued_epoch(rig):
    feed, (a, _), _ = rig
    feed._output = Queue(maxsize=1)
    a.quote(1)
    feed._poll()
    a.quote(2)
    feed._poll()
    old = drain(feed)[0]
    assert epoch(feed) == 1
    assert not feed.is_current_bookticker_message(old)
    assert feed.get_public_feed_health()['rejections']['output_full'] == 1


def test_rotation_requires_final_targets_on_replacement_before_break(rig):
    feed, managers, clock = rig
    a, b = managers
    a.quote(1)
    b.quote(1)
    feed._poll()
    drain(feed)
    clock.advance(10_000)
    feed._poll()
    replacement = managers[2]
    assert replacement.applied.wait(1)
    assert not a.closed
    feed.update_bookticker_stream(('BTCUSDT',))
    replacement.quote(2, E=clock.now, **{RECEIVE: clock.now})
    # Reconciliation is deliberately unfinished: no premature close.
    with feed._lock:
        route = feed._routes[replacement.stream] if replacement.stream in feed._routes else next(
            route for route in feed._routes.values() if route.manager is replacement)
        route.applied = frozenset(('BTCUSDT', 'ETHUSDT'))
        feed._drain_route(route)
        assert feed._rotate() == []
        route.applied = frozenset(('BTCUSDT',))
    feed._poll()
    feed._cleaner.join(timeout=0.01)  # Cleanup runs independently of selection.
    assert a.closed
    assert not b.closed
    assert len(managers) == 3
    assert len(feed._routes) == 2


def test_rotation_timeout_retains_old_and_never_rotates_both_slots(rig):
    feed, managers, clock = rig
    clock.advance(15_000)
    feed._poll()
    assert len(managers) == 3
    replacement = managers[-1]
    clock.advance(500)
    feed._poll()
    feed._cleaner.join(timeout=0.01)
    assert replacement.closed
    assert not managers[0].closed
    assert not managers[1].closed
    assert feed.get_public_feed_health()['rotations']['timeout'] == 1


def test_stop_is_terminal_during_connect_and_replacement(rig):
    feed, managers, clock = rig
    clock.advance(10_000)
    feed._poll()
    feed.close()
    feed.update_bookticker_stream(('BTCUSDT',))
    for manager in managers:
        manager.quote(100)
    feed._poll()
    assert all(manager.closed for manager in managers)
    assert len(managers) == 3
    assert not feed.get_public_feed_state()['connected']
    assert drain(feed) == []


def test_slow_route_reconciliation_does_not_block_healthy_route(rig):
    feed, (a, b), _ = rig
    a.block = Event()
    a.applied.clear()
    b.applied.clear()
    feed.update_bookticker_stream(('BTCUSDT',))
    assert b.applied.wait(1)
    b.quote(1)
    feed._poll()
    assert drain(feed)[0][ID] == b.stream
    feed.close()
    assert a.closed


def test_shadow_never_publishes_backup_or_uses_it_to_keep_trading_available(rig):
    feed, (a, b), _ = rig
    object.__setattr__(feed.options, 'mode', 'shadow')
    a.quote(10)
    b.quote(11)
    feed._poll()
    assert [frame['u'] for frame in drain(feed)] == [10]
    a.connected = False
    feed._poll()
    assert not feed.get_public_feed_state()['symbols']['BTCUSDT']['available']
    assert drain(feed) == []


def test_real_physical_callback_only_enqueues_under_nonreentrant_state_lock(rig):
    feed, _managers, _ = rig
    manager = BinanceWsManager(Mock(), queue=next(iter(feed._routes.values())).ingress, is_perp=True)
    manager._public_bookticker = True
    manager._bookticker_symbols = frozenset(('BTCUSDT',))
    manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True))
    manager._generation = 1
    manager._publish_public_bookticker(manager.ws, {'s':'BTCUSDT','u':1,'b':'1','a':'2',
                                                   'B':'1','A':'1','E':NOW}, NOW)
    assert next(iter(feed._routes.values())).ingress.queue.qsize() == 1


def test_queries_do_not_publish_and_single_message_gate_checks_only_its_symbol(rig):
    feed, (a, b), _ = rig
    a.quote(1)
    feed._poll()
    frame = drain(feed)[0]
    b.quote(2)
    route = next(route for route in feed._routes.values() if route.manager is b)
    serial, pending = route.ingress.queue.get_nowait()
    route.quotes['BTCUSDT'] = pending
    a.connected = False
    feed.get_public_feed_state()
    feed.is_current_bookticker_message(frame)
    assert drain(feed) == []


def test_health_really_applies_narrower_freshness_limit_and_shadow_joint_union(rig):
    feed, (a, b), clock = rig
    object.__setattr__(feed.options, 'mode', 'shadow')
    a.quote(10)
    b.quote(10)
    feed._poll()
    drain(feed)
    clock.advance(100)
    assert feed.get_public_feed_health(max_age_ms=50)['fresh_symbol_count'] == 0
    a.connected = False
    feed._poll()
    clock.advance(100)
    health = feed.get_public_feed_health()
    assert health['delivery_unavailable_ms']['BTCUSDT'] == 100
    assert health['joint_unavailable_ms']['BTCUSDT'] == 0


def test_slow_old_socket_cleanup_never_blocks_selector(rig):
    feed, managers, clock = rig
    clock.advance(10_000)
    feed._poll()
    replacement = managers[-1]
    assert replacement.applied.wait(1)
    release, entered = Event(), Event()
    original = managers[0].close
    def slow_close():
        entered.set()
        release.wait(1)
        original()
    managers[0].close = slow_close
    for symbol in ('BTCUSDT', 'ETHUSDT'):
        replacement.quote(10, s=symbol, E=clock.now, **{RECEIVE: clock.now})
    feed._poll()
    assert entered.wait(1)
    drain(feed)
    replacement.quote(11, E=clock.now, **{RECEIVE: clock.now})
    feed._poll()
    assert drain(feed)[0]['u'] == 11
    clock.advance(15_000)
    feed._poll()
    assert len(managers) == 3  # A retiring socket still consumes the third slot.
    assert feed.get_public_feed_health()['cleanup_pending'] == 1
    release.set()


@pytest.mark.parametrize('advance_generation', [True, False])
def test_observed_backup_watermark_excludes_replay_even_before_primary_catches_up(rig, advance_generation):
    feed, (a, b), _ = rig
    a.quote(10)
    feed._poll()
    b.quote(1000)
    feed._poll()
    assert [frame['u'] for frame in drain(feed)] == [10, 1000]
    if advance_generation:
        b.generation += 1
    b.quote(11)
    a.quote(12)
    feed._poll()
    assert feed._observed_highwater['BTCUSDT']['u'] == 1000
    assert drain(feed) == []
    if advance_generation:
        assert not feed.get_public_feed_state()['symbols']['BTCUSDT']['available']


def test_backup_observation_never_advances_shadow_primary_delivery_watermark(rig):
    feed, (a, b), _ = rig
    object.__setattr__(feed.options, 'mode', 'shadow')
    a.quote(10)
    feed._poll()
    b.quote(1000)
    feed._poll()
    a.quote(11)
    feed._poll()
    assert [frame['u'] for frame in drain(feed)] == [10, 11]
    assert feed._observed_highwater['BTCUSDT']['u'] == 1000
    assert feed.get_public_feed_state()['symbols']['BTCUSDT']['available']


def test_equal_update_id_with_different_transaction_time_cannot_prove_backup_coverage(rig):
    feed, (a, b), _ = rig
    a.quote(10)
    feed._poll()
    drain(feed)
    b.quote(10, T=NOW - 1)
    feed._poll()
    a.connected = False
    feed._poll()
    assert not feed.get_public_feed_state()['symbols']['BTCUSDT']['available']


def test_wall_clock_rollback_cannot_revive_cached_or_queued_quote(rig):
    feed, (a, _), clock = rig
    a.quote(10)
    feed._poll()
    frame = drain(feed)[0]
    clock.advance(1001)
    clock.now = NOW  # Source/receive ages now look young; monotonic age remains old.
    assert not feed.is_current_bookticker_message(frame)
    assert epoch(feed) == 1
    assert not feed.get_public_feed_state()['symbols']['BTCUSDT']['available']
    assert drain(feed) == []


def test_monotonic_ingress_stamp_includes_time_spent_waiting_in_queue(rig):
    feed, (a, _), clock = rig
    a.quote(10)
    clock.advance(1001)
    clock.now = NOW
    feed._poll()
    assert drain(feed) == []
    assert not feed.get_public_feed_state()['symbols']['BTCUSDT']['available']


def test_38_symbol_dual_feed_replay_failover_has_no_duplicates_epochs_or_drops(rig):
    feed, (a, b), _clock = rig
    symbols = tuple(f'SYN{index}USDT' for index in range(38))
    # This is bounded synthetic functional coverage, not a production load benchmark.
    object.__setattr__(feed.options, 'ingress_capacity', 128)
    feed._delivery_capacity = 128 * 3
    for route in feed._routes.values():
        route.ingress.queue = Queue(maxsize=128)
    feed._output = Queue(maxsize=256)
    feed.update_bookticker_stream(symbols)
    for update in (1, 2, 3):
        for symbol in symbols:
            a.quote(update, s=symbol)
            b.quote(update, s=symbol)
        feed._poll()
        frames = drain(feed)
        assert len(frames) == 38
        assert {(frame['s'], frame['u']) for frame in frames} == {(symbol, update) for symbol in symbols}
    before = {symbol: state['epoch'] for symbol, state in feed.get_public_feed_state()['symbols'].items()}
    a.connected = False
    for symbol in symbols:
        b.quote(4, s=symbol)
    feed._poll()
    frames = drain(feed)
    assert len(frames) == 38
    assert all(feed.is_current_bookticker_message(frame) for frame in frames)
    after = feed.get_public_feed_state()['symbols']
    assert {symbol: state['epoch'] for symbol, state in after.items()} == before
    assert all(state['available'] for state in after.values())
    health = feed.get_public_feed_health()
    assert health['ingress_drops'] == health['ingress_depth'] == 0


def test_targets_converge_on_both_routes_and_blocked_replacement(rig):
    feed, managers, clock = rig
    clock.advance(10_000)
    feed._poll()
    replacement = managers[-1]
    assert replacement.applied.wait(1)
    entered, release = Event(), Event()
    final = frozenset(('BTCUSDT', 'SOLUSDT'))
    final_applied = [Event() for _ in managers]
    for index, manager in enumerate(managers):
        original = manager.update_bookticker_stream
        def update(targets, index=index, original=original):
            if index == 2 and targets == frozenset(('BTCUSDT',)):
                entered.set()
                release.wait(1)
            original(targets)
            if targets == final:
                final_applied[index].set()
        manager.update_bookticker_stream = update
    try:
        feed.update_bookticker_stream(('BTCUSDT',))
        assert entered.wait(1)
        feed.update_bookticker_stream(final)
        assert final_applied[0].wait(1)
        assert final_applied[1].wait(1)
        replacement.quote(10, s='ETHUSDT', E=clock.now, **{RECEIVE: clock.now})
        managers[1].quote(10, E=clock.now, **{RECEIVE: clock.now})
        feed._poll()
        assert all(frame['s'] != 'ETHUSDT' for frame in drain(feed))
        assert not managers[0].closed
        release.set()
        assert final_applied[2].wait(1)
        assert all(manager.targets == final for manager in managers)
    finally:
        release.set()


def test_membership_aba_rejects_pre_remove_ingress_without_retiring_other_symbols(rig):
    feed, (a, b), _ = rig
    a.quote(1)
    b.quote(1)
    a.quote(1, s='ETHUSDT')
    b.quote(1, s='ETHUSDT')
    feed._poll()
    drain(feed)
    eth_epoch = epoch(feed, 'ETHUSDT')
    a.quote(2)  # Already in physical ingress before removal; not yet selected.
    a.quote(2, s='ETHUSDT')
    feed.update_bookticker_stream(('ETHUSDT',))
    feed.update_bookticker_stream(('BTCUSDT', 'ETHUSDT'))
    feed._poll()
    assert [frame['s'] for frame in drain(feed)] == ['ETHUSDT']
    assert not feed.get_public_feed_state()['symbols']['BTCUSDT']['available']
    assert epoch(feed, 'ETHUSDT') == eth_epoch
    assert feed.get_public_feed_health()['rejections']['retired_subscription'] == 1
    a.quote(3)
    feed._poll()
    current = drain(feed)[0]
    assert current['s'] == 'BTCUSDT'
    assert feed.is_current_bookticker_message(current)


@pytest.mark.parametrize('query_first', [False, True])
def test_arrived_fresh_pending_quote_prevents_false_outage_epoch(rig, query_first):
    feed, (a, b), clock = rig
    a.quote(1)
    feed._poll()
    drain(feed)
    clock.advance(500)
    b.quote(2, E=clock.now, **{RECEIVE: clock.now})
    clock.advance(501)
    if query_first:
        assert feed.get_public_feed_state()['symbols']['BTCUSDT']['available']
        assert drain(feed) == []  # Queries qualify but never publish.
    feed._poll()
    assert epoch(feed) == 0
    assert [frame['u'] for frame in drain(feed)] == [2]
    assert feed.get_public_feed_health()['joint_unavailable_ms']['BTCUSDT'] == 0


def test_delayed_poll_accounts_real_gap_from_original_freshness_expiry(rig):
    feed, (a, b), clock = rig
    a.quote(1)
    feed._poll()
    drain(feed)
    clock.advance(1500)
    b.quote(2, E=clock.now, **{RECEIVE: clock.now})
    clock.advance(100)
    feed._poll()
    assert epoch(feed) == 1
    health = feed.get_public_feed_health()
    assert health['delivery_unavailable_ms']['BTCUSDT'] == 500
    assert health['joint_unavailable_ms']['BTCUSDT'] == 500
    clock.advance(2000)
    health = feed.get_public_feed_health()
    assert health['delivery_unavailable_ms']['BTCUSDT'] == 1600


def test_shadow_primary_rotation_uses_primary_watermark_when_backup_is_ahead(rig):
    feed, managers, clock = rig
    object.__setattr__(feed.options, 'mode', 'shadow')
    a, b = managers
    a.quote(10)
    b.quote(1000)
    feed._poll()
    drain(feed)
    clock.advance(10_000)
    feed._poll()
    replacement = managers[-1]
    feed.update_bookticker_stream(('BTCUSDT',))
    assert replacement.applied.wait(1)
    # Both target reconciliation and readiness are deterministic under the feed lock.
    with feed._lock:
        route = next(route for route in feed._routes.values() if route.manager is replacement)
        route.applied = frozenset(('BTCUSDT',))
    replacement.quote(11, E=clock.now, **{RECEIVE: clock.now})
    feed._poll()
    feed._cleaner.join(timeout=0.02)
    assert a.closed
    assert not replacement.closed
    assert [frame['u'] for frame in drain(feed)] == [11]


def test_late_cleanup_worker_is_reaped_and_close_errors_remain_blocking(rig):
    feed, managers, clock = rig
    clock.advance(15_000)
    feed._poll()
    replacement = managers[-1]
    release, closed = Event(), Event()
    old_generation = __import__('threading').Thread(target=release.wait, daemon=True)
    old_generation.start()
    replacement._get_owned_worker_threads = lambda: [old_generation]
    original = replacement.close
    def close():
        original()
        closed.set()
    replacement.close = close
    try:
        clock.advance(500)
        feed._poll()
        assert closed.wait(1)
        assert feed.get_public_feed_health()['cleanup_pending'] == 1
        feed._poll()
        assert len(managers) == 3
        pending = next(iter(feed._cleanup_pending.values()))
        workers = feed._owned_workers(pending)
        release.set()
        for worker in workers:
            worker.join(1)
            assert not worker.is_alive()
        # Query audit must reap a late exit without requiring another rotation.
        assert feed.get_public_feed_health()['cleanup_pending'] == 0
        feed._poll()
        assert len(managers) == 4
        failed = managers[-1]
        entered = Event()
        def fail_close():
            entered.set()
            raise RuntimeError('unpublished detail')
        failed.close = fail_close
        clock.advance(500)
        feed._poll()
        assert entered.wait(1)
        # Wait for the exception handler to publish its sanitized status.
        for _ in range(100):
            if feed.get_public_feed_health()['cleanup_failures']:
                break
            Event().wait(0.002)
        assert 'RuntimeError' in feed.get_public_feed_health()['cleanup_failures'].values()
        assert feed.get_public_feed_health()['cleanup_pending'] == 1
        clock.advance(60_000)
        feed._poll()
        assert len(managers) == 4
    finally:
        release.set()


def test_background_selector_delivers_through_public_start_and_owns_shutdown():
    ready, managers, clock, sink = Event(), [], Clock(), Queue(maxsize=8)
    def factory(ingress):
        manager = Physical(ingress)
        managers.append(manager)
        if len(managers) == 2:
            ready.set()
        return manager
    feed = RedundantBookTickerFeed(Mock(), sink, manager_factory=factory, clock=clock)
    caller = __import__('threading').Thread(target=feed.start_bookticker_stream,
                                           args=(('BTCUSDT',),), daemon=True)
    caller.start()
    try:
        assert ready.wait(1)
        assert all(manager.applied.wait(1) for manager in managers)
        managers[0].quote(1)
        frame = sink.get(timeout=1)
        assert frame['u'] == 1
        assert feed.is_current_bookticker_message(frame)
        feed.update_bookticker_stream(('BTCUSDT', 'ETHUSDT'))
        managers[1].quote(2, s='ETHUSDT')
        assert sink.get(timeout=1)['s'] == 'ETHUSDT'
    finally:
        feed.close()
        caller.join(1)
        feed._cleaner.join(1)
    assert not caller.is_alive()
    assert not feed._thread.is_alive()
    assert not feed._cleaner.is_alive()
    assert feed.get_public_feed_health()['cleanup_pending'] == 0
    assert all(manager.closed for manager in managers)


def test_health_includes_bounded_original_quote_ages_and_compatible_coverage(rig):
    feed, (a, _), clock = rig
    object.__setattr__(feed.options, 'sample_capacity', 1)
    a.quote(1)
    a.quote(1, s='ETHUSDT')
    feed._poll()
    clock.advance(20)
    health = feed.get_public_feed_health()
    assert health['expected_symbol_count'] == health['observed_symbol_count'] == 2
    assert health['fresh_symbol_count'] == 2
    assert health['unavailable_symbol_count'] == 0
    assert health['physical_socket_count'] is None
    route = next(value for value in health['routes'].values() if value['symbol_observations'])
    assert route['symbol_observations_truncated']
    assert len(route['symbol_observations']) == 1
    stamp = next(iter(route['symbol_observations'].values()))
    assert stamp['source_ms'] == stamp['receive_ms'] == NOW
    assert stamp['source_age_ms'] == stamp['receive_age_ms'] == stamp['monotonic_age_ms'] == 20


def test_query_stages_every_fresh_transition_and_poll_preserves_order(rig):
    feed, (a, b), _ = rig
    a.quote(10)
    feed._poll()
    assert feed.is_current_bookticker_message(drain(feed)[0])
    a.quote(11, b='90', a='91')
    b.quote(11, b='90', a='91')
    a.quote(12)
    b.quote(12)
    assert epoch(feed) == 0
    assert feed.get_public_feed_health()['delivery_outbox_depth'] == 2
    assert drain(feed) == []
    feed._poll()
    frames = drain(feed)
    assert [frame['u'] for frame in frames] == [11, 12]
    assert [frame['b'] for frame in frames] == ['90', '100']
    assert all(feed.is_current_bookticker_message(frame) for frame in frames)
    assert epoch(feed) == 0


def test_expired_staged_transition_retires_same_batch_without_retagging(rig):
    feed, (a, _), clock = rig
    a.quote(10)
    feed._poll()
    assert feed.is_current_bookticker_message(drain(feed)[0])
    a.quote(11, b='90', a='91')
    a.quote(12)
    feed.get_public_feed_state()
    clock.advance(500)
    a.quote(13, E=clock.now, **{RECEIVE: clock.now})
    clock.advance(501)  # Coverage is continuous; only the old outbox transitions expired.
    feed._poll()
    assert drain(feed) == []
    assert epoch(feed) > 0
    a.quote(14, E=clock.now, **{RECEIVE: clock.now})
    feed._poll()
    current = drain(feed)[0]
    assert current['u'] == 14
    assert current[SYMBOL_EPOCH] == epoch(feed)


def test_outbox_overflow_is_bounded_visible_and_fences_queued_old_epochs(rig):
    feed, (a, _), _ = rig
    feed._delivery_capacity = 1
    a.quote(1)
    a.quote(2, b='90', a='91')
    feed.get_public_feed_state()
    health = feed.get_public_feed_health()
    assert health['delivery_outbox_depth'] == health['delivery_outbox_capacity'] == 1
    assert health['delivery_drops'] == 1
    assert health['staged_watermark_symbol_count'] <= health['delivery_watermark_symbol_bound']
    feed._poll()
    assert drain(feed) == []


def test_real_base_socket_close_error_remains_pending_without_live_workers(rig):
    feed, _managers, _ = rig
    route = next(iter(feed._routes.values()))
    manager = BinanceWsManager(Mock(), is_perp=True)
    manager._bookticker_symbols = frozenset(('BTCUSDT', 'ETHUSDT'))
    manager.status = 'ACTIVE'
    attempted = Event()
    def fail_close(**kwargs):
        attempted.set()
        raise OSError('private-close-detail')
    manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True), close=fail_close)
    route.manager = manager
    feed.close()
    assert attempted.wait(1)
    route.worker.join(1)
    feed._cleaner.join(1)
    assert not manager._get_owned_worker_threads()
    assert manager._get_cleanup_result() == {'complete': False, 'error_type': 'OSError'}
    health = feed.get_public_feed_health()
    assert health['cleanup_pending'] == 1
    assert health['cleanup_failures'][route.key] == 'OSError'


@pytest.mark.parametrize('retirement', ['disconnect', 'generation'])
@pytest.mark.parametrize('update', [10, 11])
def test_retired_raw_transition_fences_only_unconsumed_updates(rig, retirement, update):
    feed, (a, b), _ = rig
    a.quote(10)
    b.quote(10)
    feed._poll()
    assert feed.is_current_bookticker_message(drain(feed)[0])
    a.quote(update, b='90' if update == 11 else '100')
    b.quote(12)
    if retirement == 'disconnect':
        a.connected = False
    else:
        a.generation += 1
    feed._poll()
    assert epoch(feed) == (1 if update == 11 else 0)
    assert [frame['u'] for frame in drain(feed)] == [12]
    assert feed._observed_highwater['BTCUSDT']['u'] == 12
    assert next(route for route in feed._routes.values() if route.manager is a).highwater['BTCUSDT']['u'] == 10


@pytest.mark.parametrize('retirement', ['disconnect', 'generation'])
@pytest.mark.parametrize('enqueued,identical', [(False, True), (True, True), (False, False)])
def test_retired_latest_raw_copy_requires_identical_qualified_retained_evidence(rig, retirement, enqueued, identical):
    feed, (a, b), clock = rig
    a.quote(10)
    b.quote(10)
    feed._poll()
    assert feed.is_current_bookticker_message(drain(feed)[0])
    b.quote(12)
    if enqueued:
        feed._poll()  # The current B12 is queued but has not passed the consumer gate.
    clock.mono += 1
    a.quote(12, b='100' if identical else '90')
    if retirement == 'disconnect':
        a.connected = False
    else:
        a.generation += 1
    feed._poll()
    assert epoch(feed) == (0 if identical else 1)
    frames = drain(feed)
    assert [frame['u'] for frame in frames] == ([12] if identical else [])
    if identical:
        assert frames[0][ID] == b.stream
        assert feed.is_current_bookticker_message(frames[0])
    assert feed._observed_highwater['BTCUSDT']['b'] == '100'
    assert next(route for route in feed._routes.values() if route.manager is a).highwater['BTCUSDT']['u'] == 10


@pytest.mark.parametrize('mode,overflow_slot,expected_epoch', [
    ('redundant', 0, 1), ('redundant', 1, 1), ('shadow', 1, 0),
])
def test_ingress_gap_fences_once_despite_healthy_backup(rig, mode, overflow_slot, expected_epoch):
    feed, managers, _ = rig
    object.__setattr__(feed.options, 'mode', mode)
    for manager in managers:
        manager.quote(10)
    feed._poll()
    assert feed.is_current_bookticker_message(drain(feed)[0])
    failed, healthy = managers[overflow_slot], managers[1 - overflow_slot]
    for update in range(11, 17):
        failed.quote(update, b='90')
    healthy.quote(12)
    feed._poll()
    assert epoch(feed) == expected_epoch
    assert feed.get_public_feed_state()['symbols']['BTCUSDT']['available']
    assert feed.get_public_feed_health()['ingress_drops'] == 2
    assert feed.get_public_feed_health()['rejections']['ingress_gap'] == 1
    assert failed.ingress.queue.qsize() == 0
    assert [frame['u'] for frame in drain(feed)] == [12]
    assert epoch(feed) == expected_epoch


def test_cleanup_reads_physical_result_only_after_worker_finally_exits(rig):
    feed, _, _ = rig
    manager = BinanceWsManager(Mock(), is_perp=True)
    manager._stop_event.set()
    release, exited, inspected = Event(), Event(), Event()
    def fail_close(**kwargs):
        raise OSError('private-finally-close')
    ws = SimpleNamespace(close=fail_close)
    def finish_generation():
        release.wait()
        try:
            manager._close_socket(ws)
        finally:
            exited.set()
    worker = Thread(target=finish_generation, daemon=True)
    manager._owned_socket_threads.add(worker)
    worker.start()
    original = manager._get_cleanup_result
    def racing_result():
        # Old ordering snapshots success, then finally records failure and exits.
        result = original()
        inspected.set()
        release.set()
        assert exited.wait(1)
        worker.join(1)
        return result
    manager._get_cleanup_result = racing_result
    pending = SimpleNamespace(key='finally-race', manager=manager, worker=None,
                              close_completed=True, cleanup_error=None)
    try:
        with feed._lock:
            feed._cleanup_pending[pending.key] = pending
            feed._reap_cleanup()
            assert not inspected.is_set()
            assert pending.cleanup_error == 'worker_still_alive'
            release.set()
            assert exited.wait(1)
            worker.join(1)
            feed._reap_cleanup()
            assert inspected.is_set()
            assert feed._cleanup_pending[pending.key] is pending
            assert pending.cleanup_error == 'OSError'
            feed._cleanup_pending.pop(pending.key)
    finally:
        release.set()
        worker.join(1)
        with feed._lock:
            feed._cleanup_pending.pop(pending.key, None)
