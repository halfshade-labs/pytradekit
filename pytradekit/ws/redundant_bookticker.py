"""Redundant synchronous Binance perpetual BBO transport, without account clients.

Physical identities and timestamps are retained. Logical epochs retire lost
coverage or an undeliverable transition, never merely a healthy socket switch. Depth is not
supported: bookTicker contains a complete BBO, whereas depth needs alignment.
"""
from collections import Counter, deque
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from threading import Event, Lock, RLock, Thread, current_thread
from uuid import uuid4

from pytradekit.utils.dynamic_types import BinanceWebSocket, WebsocketStatus
from pytradekit.utils.time_handler import get_timestamp_ms, get_monotonic_timestamp_ns
from pytradekit.ws.binance_ws import BinanceWsManager
from pytradekit.ws.bookticker_health import BookTickerHealth
from pytradekit.ws.subscription_update import (
    calculate_subscription_update, normalize_subscription_targets,
)

LOGICAL_FEED_ID = '_ptk_logical_feed_id'
SYMBOL_EPOCH = '_ptk_symbol_epoch'
PHYSICAL_ROUTE = '_ptk_physical_route'
INGRESS_SERIAL = '_ptk_ingress_serial'
INGRESS_MONOTONIC_MS = '_ptk_ingress_monotonic_ms'
INGRESS_WALL_MS = '_ptk_ingress_wall_ms'
MEMBERSHIP_VERSION = '_ptk_membership_version'
DAY_MS = 86_400_000


@dataclass(frozen=True)
class RedundantBookTickerOptions:
    mode: str = 'redundant'
    max_age_ms: int = 1000
    ingress_capacity: int = 4096
    drain_batch: int = 256
    rotation_ms: int = 79_200_000  # 22h, then the second slot at 23h.
    stagger_ms: int = 3_600_000
    replacement_timeout_ms: int = 30_000
    retry_rotation_ms: int = 60_000
    sample_capacity: int = 256

    def __post_init__(self):
        if self.mode not in ('shadow', 'redundant'):
            raise ValueError('mode must be shadow or redundant')
        values = (self.max_age_ms, self.ingress_capacity, self.drain_batch,
                  self.rotation_ms, self.stagger_ms, self.replacement_timeout_ms,
                  self.retry_rotation_ms, self.sample_capacity)
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError('feed limits must be positive integers')
        if self.rotation_ms + self.stagger_ms >= DAY_MS:
            raise ValueError('both initial slots must rotate before 24h')


class _Clock:
    wall_ms = staticmethod(get_timestamp_ms)
    monotonic_ms = staticmethod(lambda: get_monotonic_timestamp_ns() // 1_000_000)


class _Ingress:
    """Called under a physical manager lock: enqueue only; never call feed gates."""

    def __init__(self, capacity, wake, clock):
        self.clock = clock
        self.queue = Queue(maxsize=capacity)
        self.wake = wake
        self.lock = Lock()
        self.serial = 0
        self.drops = 0
        self.memberships = {}

    def put_nowait(self, message):
        with self.lock:
            try:
                frame = dict(message)
                frame[INGRESS_MONOTONIC_MS] = self.clock.monotonic_ms()
                frame[INGRESS_WALL_MS] = self.clock.wall_ms()
                frame[MEMBERSHIP_VERSION] = self.memberships.get(frame.get('s'))
                self.queue.put_nowait((self.serial, frame))
            except Full:
                self.serial += 1
                self.drops += 1
        self.wake.set()

    def set_memberships(self, memberships):
        with self.lock:
            self.memberships = dict(memberships)

    def snapshot(self):
        with self.lock:
            return self.serial, self.drops, self.queue.qsize()


@dataclass
class _Route:
    key: str
    slot: int
    manager: object
    ingress: _Ingress
    created_ms: int
    due_ms: int
    replacement: bool = False
    quotes: dict = field(default_factory=dict)
    highwater: dict = field(default_factory=dict)
    applied: object = None
    serial: int = 0
    errors: int = 0
    cleanup_error: str = None
    close_completed: bool = False
    wake: Event = field(default_factory=Event)
    worker: object = None


class RedundantBookTickerFeed:
    """Two independent managers plus at most one make-before-break replacement.

    update() publishes desired targets synchronously; each route reconciles them
    independently. No logical lock is held over connect/update/close network I/O.
    The existing physical lifecycle worker alone owns socket recovery/backoff.
    """

    def __init__(self, logger, queue, options=None, manager_factory=None, clock=None):
        self.logger = logger
        self._output = queue
        self.options = options or RedundantBookTickerOptions()
        self._factory = manager_factory or (
            lambda ingress: BinanceWsManager(logger, queue=ingress, is_perp=True))
        self._clock = clock or _Clock()
        self._lock = RLock()
        self._stop = Event()
        self._wake = Event()
        self._thread = None
        self._routes = {}
        self._targets = frozenset()
        self._memberships = {}
        self._epochs = {}
        self._gap_fenced = set()
        self._available = {}
        self._selected = {}
        self._highwater = {}  # Successfully enqueued to the consumer transport.
        self._staged_highwater = {}
        self._consumer_gated_highwater = {}
        self._delivery_outbox = deque()
        self._delivery_capacity = self.options.ingress_capacity * 3
        self._delivery_drops = 0
        self._observed_highwater = {}  # All fresh, current, ordered physical observations.
        self._primary_highwater = {}  # Shadow observations cannot change A's delivery gate.
        self._stream_id = uuid4().hex
        self._rejections = Counter()
        self._switches = deque(maxlen=self.options.sample_capacity)
        self._recovery = deque(maxlen=self.options.sample_capacity)
        self._unavailable_since = {}
        self._unavailable_ms = Counter()
        self._rotation_counts = Counter()
        self._retired_drops = 0
        self._cleanup = Queue(maxsize=6)
        self._cleaner = None
        self._cleanup_pending = {}
        self._joint_available = {}
        self._joint_since = {}
        self._joint_ms = Counter()
        self._coverage_until = {}
        self._joint_until = {}
        self.status = WebsocketStatus.INIT.name

    def start_bookticker_stream(self, symbols):
        self.update_bookticker_stream(symbols)
        self._stop.wait()

    def update_bookticker_stream(self, symbols):
        targets = normalize_subscription_targets(symbols)
        with self._lock:
            previous = self._targets
            if self._stop.is_set():
                return calculate_subscription_update(previous, previous)
            self._targets = targets
            for symbol in previous ^ targets:
                self._memberships[symbol] = self._memberships.get(symbol, 0) + 1
            memberships = {symbol: self._memberships[symbol] for symbol in targets}
            for route in self._routes.values():
                route.ingress.set_memberships(memberships)
            now = self._clock.monotonic_ms()
            for symbol in previous - targets:
                self._gap_fenced.discard(symbol)
                self._epochs[symbol] = self._epochs.get(symbol, 0) + 1
                self._available.pop(symbol, None)
                self._selected.pop(symbol, None)
                self._staged_highwater.pop(symbol, None)
                self._consumer_gated_highwater.pop(symbol, None)
                self._unavailable_since.pop(symbol, None)
                self._joint_available.pop(symbol, None)
                self._joint_since.pop(symbol, None)
                self._coverage_until.pop(symbol, None)
                self._joint_until.pop(symbol, None)
                for route in self._routes.values():
                    route.quotes.pop(symbol, None)
            for symbol in targets - previous:
                self._epochs.setdefault(symbol, 0)
                self._available[symbol] = False
                self._unavailable_since[symbol] = now
                self._joint_available[symbol] = False
                self._joint_since[symbol] = now
            if self._thread is None:
                self._cleaner = Thread(target=self._clean_routes, daemon=True, name='bookticker-cleanup')
                self._cleaner.start()
                self._create_route(0, now)
                self._create_route(1, now)
                self.status = WebsocketStatus.ACTIVE.name
                self._thread = Thread(target=self._run, daemon=True, name='redundant-bookticker')
                self._thread.start()
            for route in self._routes.values():
                route.wake.set()
        self._wake.set()
        return calculate_subscription_update(previous, targets)

    def _create_route(self, slot, now, replacement=False):
        key = uuid4().hex
        ingress = _Ingress(self.options.ingress_capacity, self._wake, self._clock)
        ingress.set_memberships({symbol: self._memberships[symbol] for symbol in self._targets})
        route = _Route(key, slot, self._factory(ingress), ingress, now,
                       now + self.options.rotation_ms + (self.options.stagger_ms if slot == 1 else 0),
                       replacement=replacement)
        self._routes[key] = route
        route.worker = Thread(target=self._reconcile, args=(route,), daemon=True,
                              name=f'bookticker-slot-{slot}')
        route.worker.start()
        return route

    def _reconcile(self, route):
        while not self._stop.is_set():
            route.wake.clear()
            with self._lock:
                if route.key not in self._routes:
                    return
                targets = self._targets
            try:
                route.manager.update_bookticker_stream(targets)
                with self._lock:
                    route.applied = targets
            except Exception as error:
                with self._lock:
                    route.errors += 1
                self.logger.debug(f'bookticker reconcile error_type={type(error).__name__}')
            # A failed connect on one route never holds up the other route.
            route.wake.wait(0.1)

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self._poll()
            except Exception as error:
                # Unexpected selector failures retire every queued epoch.
                with self._lock:
                    self._rejections['selector_error'] += 1
                    self._lose_coverage()
                self.logger.debug(f'bookticker selector error_type={type(error).__name__}')
            self._wake.wait(0.02)

    def _poll(self):
        retired = []
        with self._lock:
            if self._stop.is_set():
                return
            self._qualify_pending()
            self._select()
            self._flush_deliveries()
            self._reap_cleanup()
            retired = self._rotate()
            for route in retired:
                self._enqueue_cleanup(route)
            self._flush_deliveries()

    def _enqueue_cleanup(self, route):
        with self._lock:
            self._cleanup_pending[route.key] = route
        self._cleanup.put_nowait(route)

    def _clean_routes(self):
        while True:
            try:
                route = self._cleanup.get(timeout=0.1)
            except Empty:
                with self._lock:
                    self._reap_cleanup()
                    if (self._stop.is_set() and self._cleanup.empty()
                            and all(not route.close_completed or route.cleanup_error not in (None, 'worker_still_alive')
                                    for route in self._cleanup_pending.values())):
                        return
                continue
            try:
                route.manager.close()
                with self._lock:
                    route.close_completed = True
                for worker in self._owned_workers(route):
                    if worker and worker is not current_thread():
                        worker.join(timeout=0.2)
                with self._lock:
                    self._reap_cleanup()
            except Exception as error:
                self.logger.debug(f'bookticker cleanup error_type={type(error).__name__}')
                with self._lock:
                    route.cleanup_error = type(error).__name__
                    self._rejections['cleanup_error'] += 1

    @staticmethod
    def _owned_workers(route):
        inspect = getattr(route.manager, '_get_owned_worker_threads', None)
        physical = inspect() if callable(inspect) else [
            getattr(route.manager, '_monitorThread', None), getattr(route.manager, '_socket_thread', None)]
        return [worker for worker in [route.worker, *physical] if worker is not None]

    def _reap_cleanup(self):
        for key, route in list(self._cleanup_pending.items()):
            if not route.close_completed:
                continue  # A close failure is retained even if its workers later exit.
            if any(worker.is_alive() for worker in self._owned_workers(route)):
                route.cleanup_error = 'worker_still_alive'
                continue
            inspect = getattr(route.manager, '_get_cleanup_result', None)
            if callable(inspect):
                result = inspect()
                if result.get('complete') is not True:
                    route.cleanup_error = result.get('error_type') or 'physical_cleanup_unknown'
                    continue
            self._retired_drops += route.ingress.snapshot()[1]
            self._cleanup_pending.pop(key)
            route.cleanup_error = None

    def _contributes_delivery(self, route):
        return self.options.mode != 'shadow' or route.slot == 0 and not route.replacement

    def _sync_ingress_serial(self, route):
        serial = route.ingress.snapshot()[0]
        if serial != route.serial:
            route.serial = serial
            route.quotes.clear()
            self._rejections['ingress_gap'] += 1
            if self._contributes_delivery(route):
                # The dropped symbol is unknown. Fence once per observed gap,
                # even when the other route still provides physical coverage.
                for symbol in self._targets:
                    self._epochs[symbol] += 1
                    self._gap_fenced.add(symbol)
        return serial

    def _qualify_pending(self):
        pending = []
        for route in self._routes.values():
            self._sync_ingress_serial(route)
            for _ in range(self.options.ingress_capacity):
                try:
                    stamp, message = route.ingress.queue.get_nowait()
                except Empty:
                    break
                pending.append((message[INGRESS_MONOTONIC_MS], route, stamp, message))
        pending.sort(key=lambda item: (item[0], item[3].get('u', -1)
                                     if type(item[3].get('u')) is int else -1))
        for _, route, serial, message in pending:
            if serial != self._sync_ingress_serial(route):
                self._rejections['ingress_retired'] += 1
                continue
            self._ingest(route, message)

    def _drain_route(self, route):
        for _ in range(self.options.drain_batch):
            try:
                serial, message = route.ingress.queue.get_nowait()
            except Empty:
                return
            if serial != self._sync_ingress_serial(route):
                self._rejections['ingress_retired'] += 1
                continue
            self._ingest(route, message)

    def _ingest(self, route, message):
        symbol = message.get('s')
        reason = self._rejection_reason(route, message)
        if reason:
            self._rejections[reason] += 1
            if (reason == 'retired_generation' and self._contributes_delivery(route)
                    and type(message.get('u')) is int
                    and message['u'] > self._consumer_gated_highwater.get(symbol, -1)
                    and self._valid_at_arrival(message)
                    and not self._has_current_equivalent(message)):
                self._epochs[symbol] += 1
                self._rejections['retired_raw_transition'] += 1
            return
        route.quotes[symbol] = message
        route.highwater[symbol] = message
        high = self._observed_highwater.get(symbol)
        if high is None or (message['u'] > high['u'] and self._source_time(message) >= self._source_time(high)):
            self._observed_highwater[symbol] = message
        if route.slot == 0 and not route.replacement:
            self._primary_highwater[symbol] = message
        self._observe_arrival(route, symbol, message)
        if self._fresh(message):
            self._select(symbols=(symbol,))  # Stage every ordered transition, including during queries.
        elif self.options.mode != 'shadow' or route.slot == 0 and not route.replacement:
            high = self._staged_highwater.get(symbol) or self._highwater.get(symbol)
            if high is None or message['u'] > high['u']:
                self._epochs[symbol] += 1
                self._staged_highwater[symbol] = dict(message)
                self._rejections['expired_transition'] += 1

    def _observe_arrival(self, route, symbol, message):
        arrival = message[INGRESS_MONOTONIC_MS]
        wall = message[INGRESS_WALL_MS]
        expiry = arrival + self.options.max_age_ms - max(
            wall - self._source_time(message), wall - message[BinanceWebSocket.run_time_ms.value], 0)
        high = self._observed_highwater.get(symbol)
        if high is None or self._fingerprint(message) == self._fingerprint(high):
            joint_until = self._joint_until.get(symbol)
            if joint_until is not None and arrival > joint_until:
                self._observe_joint(symbol, False, joint_until)
            self._observe_joint(symbol, True, arrival)
            self._joint_until[symbol] = max(expiry, joint_until or expiry)
        if self.options.mode == 'shadow' and (route.slot != 0 or route.replacement):
            return
        until = self._coverage_until.get(symbol)
        if until is not None and arrival > until:
            self._mark_unavailable(symbol, until)
        self._restore_coverage(symbol, arrival)
        self._coverage_until[symbol] = max(expiry, until or expiry)

    def _restore_coverage(self, symbol, arrival):
        if not self._available.get(symbol):
            started = self._unavailable_since.pop(symbol, arrival)
            elapsed = max(0, arrival - started)
            self._unavailable_ms[symbol] += elapsed
            self._recovery.append(elapsed)
        self._available[symbol] = True

    def _valid_at_arrival(self, message):
        symbol = message.get('s')
        return bool(symbol in self._targets
                    and message.get(MEMBERSHIP_VERSION) == self._memberships[symbol]
                    and BookTickerHealth.valid_prices(message)
                    and type(message.get('u')) is int and message['u'] >= 0
                    and self._fresh_at(message, message[INGRESS_WALL_MS], message[INGRESS_MONOTONIC_MS]))

    def _has_current_equivalent(self, message):
        symbol = message['s']
        return any(self._contributes_delivery(route) and self._qualified(route, symbol)
                   and self._fingerprint(route.quotes[symbol]) == self._fingerprint(message)
                   for route in self._routes.values())

    def _rejection_reason(self, route, message):
        if message.get('s') not in self._targets:
            return 'removed_symbol'
        if message.get(MEMBERSHIP_VERSION) != self._memberships[message['s']]:
            return 'retired_subscription'
        if not route.manager.is_current_bookticker_message(message):
            return 'retired_generation'
        if not BookTickerHealth.valid_prices(message):
            return 'malformed'
        if type(message.get('u')) is not int or message['u'] < 0:
            return 'malformed'
        if not self._fresh_at(message, message[INGRESS_WALL_MS], message[INGRESS_MONOTONIC_MS]):
            return 'timestamp'
        previous = route.highwater.get(message['s'])
        if previous and self._physical_token(previous) == self._physical_token(message):
            if message['u'] <= previous['u']:
                return 'route_duplicate_or_regression'
            if self._source_time(message) < self._source_time(previous):
                return 'source_regression'
        if previous and self._source_time(message) < self._source_time(previous):
            return 'source_regression'
        high = (self._primary_highwater if self.options.mode == 'shadow' and route.slot == 0
                else self._observed_highwater).get(message['s'])
        if (high and message['u'] < high['u']) or (previous and message['u'] < previous['u']):
            return 'global_regression'
        if high and message['u'] == high['u'] and self._fingerprint(message) != self._fingerprint(high):
            return 'conflicting_duplicate'
        if high and self._source_time(message) < self._source_time(high):
            return 'source_regression'
        return None

    @staticmethod
    def _physical_token(message):
        return (message.get(BinanceWebSocket.connection_id.value),
                message.get(BinanceWebSocket.connection_generation.value))

    @staticmethod
    def _source_time(message):
        return message.get('E') if message.get('E') is not None else message.get('T')

    def _fingerprint(self, message):
        return tuple(message.get(field) for field in ('u', 'b', 'a', 'B', 'A', 'E', 'T'))

    def _fresh(self, message, max_age_ms=None):
        return self._fresh_at(message, self._clock.wall_ms(), self._clock.monotonic_ms(), max_age_ms)

    def _fresh_at(self, message, now, monotonic_ms, max_age_ms=None):
        limit = self.options.max_age_ms if max_age_ms is None else min(max_age_ms, self.options.max_age_ms)
        stamps = (message.get(BinanceWebSocket.run_time_ms.value), self._source_time(message))
        stamp = message.get(INGRESS_MONOTONIC_MS)
        age = monotonic_ms - stamp if type(stamp) is int else -1
        return 0 <= age <= limit and all(type(value) is int and 0 <= now - value <= limit for value in stamps)

    def _qualified(self, route, symbol, max_age_ms=None, states=None, highwater=None):
        message = route.quotes.get(symbol)
        if (message is None or message.get(MEMBERSHIP_VERSION) != self._memberships[symbol]
                or not self._fresh(message, max_age_ms)):
            return False
        state = (states or {}).get(route.key)
        if states is None:
            if not route.manager.is_current_bookticker_message(message):
                return False
        elif not state or not state.get('connected') or self._physical_token(message) != (
                state.get('stream_id'), state.get('generation')):
            return False
        high = (self._observed_highwater if highwater is None else highwater).get(symbol)
        return high is None or (message['u'] >= high['u']
            and self._source_time(message) >= self._source_time(high) and (
            message['u'] != high['u'] or self._fingerprint(message) == self._fingerprint(high)))

    def _select(self, publish=True, symbols=None):
        if self._stop.is_set():
            return
        states = {}
        for route in self._routes.values():
            try:
                states[route.key] = route.manager.get_public_feed_state()
            except Exception:
                states[route.key] = {'connected': False}
        for route in self._routes.values():
            self._sync_ingress_serial(route)
        now = self._clock.monotonic_ms()
        for symbol in self._targets if symbols is None else symbols:
            if symbol not in self._targets:
                continue
            union = [route for route in self._routes.values()
                     if self._qualified(route, symbol, states=states)]
            self._observe_joint(symbol, bool(union),
                                min(now, self._joint_until.get(symbol, now)) if not union else now)
            candidates = union
            if self.options.mode == 'shadow':
                candidates = [route for route in self._routes.values() if route.slot == 0
                              and not route.replacement and self._qualified(
                                  route, symbol, states=states, highwater=self._primary_highwater)]
            selected = self._routes.get(self._selected.get(symbol))
            if selected not in candidates:
                selected = max(candidates, key=lambda route: route.quotes[symbol]['u'], default=None)
            if selected is None:
                self._mark_unavailable(symbol, min(now, self._coverage_until.get(symbol, now)))
                continue
            old = self._selected.get(symbol)
            if old is not None and old != selected.key:
                quote = selected.quotes[symbol]
                self._switches.append({'symbol': symbol, 'from': old, 'to': selected.key,
                                       'age_ms': self._clock.wall_ms() - self._source_time(quote),
                                       'receive_age_ms': self._clock.wall_ms() - quote[BinanceWebSocket.run_time_ms.value],
                                       'previous_update_id': self._highwater.get(symbol, {}).get('u'),
                                       'update_id': quote['u'], 'ordered': True})
            self._selected[symbol] = selected.key
            quote = selected.quotes[symbol]
            expiry = quote[INGRESS_MONOTONIC_MS] + self.options.max_age_ms - max(
                quote[INGRESS_WALL_MS] - self._source_time(quote),
                quote[INGRESS_WALL_MS] - quote[BinanceWebSocket.run_time_ms.value], 0)
            self._coverage_until[symbol] = max(self._coverage_until.get(symbol, expiry), expiry)
            self._restore_coverage(symbol, quote[INGRESS_MONOTONIC_MS])
            self._gap_fenced.discard(symbol)
            if publish:
                self._forward(selected, symbol)

    def _observe_joint(self, symbol, available, now):
        if available and not self._joint_available.get(symbol):
            self._joint_ms[symbol] += max(0, now - self._joint_since.pop(symbol, now))
        elif not available and self._joint_available.get(symbol):
            self._joint_since[symbol] = now
        self._joint_available[symbol] = available

    def _mark_unavailable(self, symbol, now):
        if self._available.get(symbol):
            if symbol not in self._gap_fenced:
                self._epochs[symbol] += 1
            self._unavailable_since[symbol] = now
        self._available[symbol] = False
        self._gap_fenced.discard(symbol)

    def _forward(self, route, symbol):
        message = route.quotes[symbol]
        high = self._staged_highwater.get(symbol) or self._highwater.get(symbol)
        if high and message['u'] <= high['u']:
            return
        frame = dict(message)
        frame.update({LOGICAL_FEED_ID: self._stream_id, SYMBOL_EPOCH: self._epochs[symbol],
                      PHYSICAL_ROUTE: route.key, INGRESS_SERIAL: route.serial})
        self._staged_highwater[symbol] = dict(message)
        if len(self._delivery_outbox) >= self._delivery_capacity:
            self._delivery_drops += 1
            self._epochs[symbol] += 1
            self._rejections['delivery_outbox_full'] += 1
            return
        self._delivery_outbox.append(frame)

    def _delivery_is_current(self, frame):
        symbol = frame.get('s')
        route = self._routes.get(frame.get(PHYSICAL_ROUTE))
        return bool(not self._stop.is_set() and symbol in self._targets
                    and self._available.get(symbol) and self._fresh(frame)
                    and frame.get(LOGICAL_FEED_ID) == self._stream_id
                    and frame.get(SYMBOL_EPOCH) == self._epochs[symbol]
                    and frame.get(MEMBERSHIP_VERSION) == self._memberships[symbol]
                    and route and frame.get(INGRESS_SERIAL) == route.ingress.snapshot()[0]
                    and route.manager.is_current_bookticker_message(frame))

    def _retire_undelivered_transition(self, frame):
        symbol = frame.get('s')
        if (symbol in self._targets and frame.get(LOGICAL_FEED_ID) == self._stream_id
                and frame.get(SYMBOL_EPOCH) == self._epochs[symbol]
                and frame.get('u', -1) > self._consumer_gated_highwater.get(symbol, -1)):
            self._epochs[symbol] += 1
            self._rejections['undeliverable_transition'] += 1
            return True
        return False

    def _flush_deliveries(self):
        while self._delivery_outbox:
            frame = self._delivery_outbox.popleft()
            symbol = frame['s']
            if not self._delivery_is_current(frame):
                self._delivery_drops += 1
                self._retire_undelivered_transition(frame)
                continue
            high = self._highwater.get(symbol)
            if high and frame['u'] <= high['u']:
                continue
            try:
                drops = getattr(self._output, 'dropped', 0)
                self._output.put_nowait(frame)
                if getattr(self._output, 'dropped', 0) != drops:
                    raise Full
            except Full:
                self._delivery_drops += 1
                self._rejections['output_full'] += 1
                for candidate in self._routes.values():
                    candidate.quotes.pop(symbol, None)
                self._mark_unavailable(symbol, self._clock.monotonic_ms())
                continue
            self._highwater[symbol] = dict(frame)

    def _rotate(self):
        now = self._clock.monotonic_ms()
        replacement = next((route for route in self._routes.values() if route.replacement), None)
        if replacement:
            old = next(route for route in self._routes.values()
                       if route.slot == replacement.slot and not route.replacement)
            if (replacement.applied == self._targets
                    and all(self._qualified(replacement, symbol, highwater=(
                        self._primary_highwater if self.options.mode == 'shadow' and replacement.slot == 0
                        else self._observed_highwater)) for symbol in self._targets)):
                replacement.replacement = False
                replacement.due_ms = now + self.options.rotation_ms
                self._routes.pop(old.key)
                self._rotation_counts['completed'] += 1
                self._select()
                return [old]
            if now - replacement.created_ms >= self.options.replacement_timeout_ms:
                self._routes.pop(replacement.key)
                old.due_ms = now + self.options.retry_rotation_ms
                self._rotation_counts['timeout'] += 1
                self._select()
                return [replacement]
            return []
        due = [route for route in self._routes.values() if now >= route.due_ms]
        if due and self._targets and not self._cleanup_pending:
            old = min(due, key=lambda route: route.due_ms)
            self._create_route(old.slot, now, replacement=True)
            self._rotation_counts['started'] += 1
        return []

    def _lose_coverage(self):
        for route in self._routes.values():
            route.quotes.clear()
        for symbol in self._targets:
            self._mark_unavailable(symbol, self._clock.monotonic_ms())

    def is_current_bookticker_message(self, message):
        with self._lock:
            symbol = message.get('s')
            if not self._stop.is_set():
                self._qualify_pending()
            self._select(publish=False, symbols=(symbol,))
            accepted = self._delivery_is_current(message)
            if accepted:
                self._consumer_gated_highwater[symbol] = max(
                    message['u'], self._consumer_gated_highwater.get(symbol, -1))
            elif self._retire_undelivered_transition(message):
                self._delivery_drops += 1
            return accepted

    def get_public_feed_state(self):
        with self._lock:
            self._reap_cleanup()
            if not self._stop.is_set():
                self._qualify_pending()
                self._select(publish=False)
            return {'stream_id': self._stream_id, 'generation': 1,
                    'connected': bool(self._thread and not self._stop.is_set()), 'logical': True,
                    'symbols': {symbol: {'epoch': self._epochs[symbol],
                                         'available': self._available.get(symbol, False),
                                         'delivery_pending': self._staged_highwater.get(symbol, {}).get('u', -1)
                                             > self._consumer_gated_highwater.get(symbol, -1)}
                                for symbol in self._targets}}

    @staticmethod
    def _socket_generation_count(route):
        inspect = getattr(route.manager, '_get_owned_socket_threads', None)
        return len(inspect()) if callable(inspect) else None

    def _route_diagnostics(self, route, max_age_ms):
        symbols = sorted(self._targets.intersection(route.quotes))
        sample = symbols[:self.options.sample_capacity]
        return {'slot': route.slot, 'replacement': route.replacement,
                'applied_targets': sorted(route.applied or ()), 'reconcile_errors': route.errors,
                'owned_socket_generation_count': self._socket_generation_count(route),
                'symbol_observations': {symbol: self._symbol_diagnostics(route, symbol, max_age_ms)
                                        for symbol in sample},
                'symbol_observations_truncated': len(symbols) > len(sample),
                'physical': route.manager.get_public_feed_health(max_age_ms)}

    def _symbol_diagnostics(self, route, symbol, max_age_ms):
        message = route.quotes[symbol]
        high = self._observed_highwater.get(symbol, {})
        return {'source_ms': self._source_time(message),
                'receive_ms': message[BinanceWebSocket.run_time_ms.value],
                'ingress_monotonic_ms': message[INGRESS_MONOTONIC_MS],
                'source_age_ms': self._clock.wall_ms() - self._source_time(message),
                'receive_age_ms': self._clock.wall_ms() - message[BinanceWebSocket.run_time_ms.value],
                'monotonic_age_ms': self._clock.monotonic_ms() - message[INGRESS_MONOTONIC_MS],
                'update_id': message['u'], 'observed_highwater': high.get('u'),
                'staged_highwater': self._staged_highwater.get(symbol, {}).get('u'),
                'enqueued_highwater': self._highwater.get(symbol, {}).get('u'),
                'consumer_gated_highwater': self._consumer_gated_highwater.get(symbol),
                'at_observed_highwater': message['u'] == high.get('u'),
                'qualified': self._qualified(route, symbol, max_age_ms)}

    def get_public_feed_health(self, max_age_ms=1000):
        with self._lock:
            state = self.get_public_feed_state()
            now = self._clock.monotonic_ms()
            unavailable = {symbol: self._unavailable_ms[symbol] + max(0, now - started)
                           for symbol, started in self._unavailable_since.items()}
            unavailable.update({symbol: value for symbol, value in self._unavailable_ms.items()
                                if symbol not in unavailable and symbol in self._targets})
            fresh = {symbol for symbol in self._targets if any(
                self._qualified(route, symbol, max_age_ms, highwater=(
                    self._primary_highwater if self.options.mode == 'shadow' else None))
                and (self.options.mode != 'shadow' or route.slot == 0 and not route.replacement)
                for route in self._routes.values())}
            observed = set().union(*(set(route.quotes) for route in self._routes.values())) & self._targets
            missing = sorted(self._targets - fresh)
            owners = list(self._routes.values()) + list(self._cleanup_pending.values())
            generations = [self._socket_generation_count(route) for route in owners]
            samples = sorted(self._recovery)
            percentile = lambda percent: samples[min(len(samples) - 1, (len(samples) * percent + 99) // 100 - 1)] if samples else None
            return dict(state, mode=self.options.mode, freshness_max_age_ms=min(max_age_ms, self.options.max_age_ms),
                        expected_symbol_count=len(self._targets), observed_symbol_count=len(observed),
                        fresh_symbol_count=len(fresh), unavailable_symbol_count=len(missing),
                        unavailable_symbols_sample=missing[:50], unavailable_symbols_truncated=len(missing) > 50,
                        route_manager_count=len(owners), physical_socket_count=None,
                        owned_socket_generation_count=sum(generations) if all(value is not None for value in generations) else None,
                        measurement_limits={'freshness_expiry_accounted': True,
                            'transport_loss_detection': 'sampled_at_query_or_selector',
                            'physical_disconnect_time_known': False,
                            'pending_qualification_bound_per_route': self.options.ingress_capacity},
                        delivery_unavailable_ms=unavailable,
                        joint_unavailable_ms={symbol: self._joint_ms[symbol] + (
                            max(0, now - self._joint_since[symbol]) if symbol in self._joint_since else 0)
                            for symbol in self._targets},
                        delivery_outbox_depth=len(self._delivery_outbox),
                        delivery_outbox_capacity=self._delivery_capacity, delivery_drops=self._delivery_drops,
                        staged_watermark_symbol_count=len(self._staged_highwater),
                        consumer_gated_symbol_count=len(self._consumer_gated_highwater),
                        delivery_watermark_symbol_bound=len(self._targets),
                        delivery_status='enqueued_highwater_is_transport_not_business_success',
                        cleanup_pending=len(self._cleanup_pending),
                        cleanup_failures={route.key: route.cleanup_error for route in self._cleanup_pending.values()
                                          if route.cleanup_error},
                        selector_alive=bool(self._thread and self._thread.is_alive()),
                        cleanup_alive=bool(self._cleaner and self._cleaner.is_alive()),
                        recovery_sample_count=len(samples),
                        recovery_p95_ms=percentile(95), recovery_p99_ms=percentile(99),
                        switches=list(self._switches), rejections=dict(self._rejections),
                        rotations=dict(self._rotation_counts),
                        ingress_drops=self._retired_drops + sum(route.ingress.snapshot()[1] for route in list(self._routes.values()) + list(self._cleanup_pending.values())),
                        ingress_depth=sum(route.ingress.snapshot()[2] for route in list(self._routes.values()) + list(self._cleanup_pending.values())),
                        routes={route.key: self._route_diagnostics(route, max_age_ms)
                                for route in self._routes.values()})


    def close(self):
        with self._lock:
            if self._stop.is_set():
                return
            self._stop.set()
            self.status = WebsocketStatus.STOP.name
            self._lose_coverage()
            self._delivery_outbox.clear()
            routes = list(self._routes.values())
            self._routes.clear()
            self._wake.set()
            for route in routes:
                route.wake.set()
                self._enqueue_cleanup(route)
        for worker in [self._thread, self._cleaner]:
            if worker and worker is not current_thread():
                worker.join(timeout=0.2)
