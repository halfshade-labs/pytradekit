"""Bounded, payload-free observations of a public book-ticker connection."""

from decimal import Decimal, InvalidOperation
from uuid import uuid4

from pytradekit.utils.dynamic_types import BinanceWebSocket
from pytradekit.utils.time_handler import get_monotonic_timestamp_ns


class BookTickerHealth:
    """Owned by the manager's state lock; no network or logging in this object."""

    def __init__(self):
        self.stream_id = uuid4().hex
        self.generation = 0
        self.retired_generation = None
        self.disconnect_count = 0
        self.retired_generation_count = 0
        self.disconnect_reason = None
        self.last_error_type = None
        self.last_close_code = None
        self.disconnect_ms = None
        self._disconnect_ns = None
        self._outage_ns = None
        self._opened_ns = None
        self.opened_ms = None
        self.socket_rebuild_ms = None
        self.first_valid_quote_ms = None
        self.first_valid_quote_timestamp_basis = None
        self.first_valid_quote_delay_ms = None
        self.recovery_to_quote_ms = None
        self.quotes = {}
        self.subscription_sent_ms = None
        self.subscription_ack_ms = None
        self.subscription_error_code = None
        self._subscription_ids = set()
        self._subscription_tracking_lost = False

    def retire(self, generation, now_ms, reason):
        if self.retired_generation == generation:
            return False
        self.retired_generation = generation
        self.retired_generation_count += 1
        if self.generation == generation and self.opened_ms is not None:
            self.disconnect_count += 1
        self.disconnect_reason = reason
        self.disconnect_ms = now_ms
        self._disconnect_ns = get_monotonic_timestamp_ns()
        if self._outage_ns is None:
            self._outage_ns = self._disconnect_ns
        return True

    def prepare_generation(self, generation):
        if self.generation == generation:
            return
        self.generation = generation
        self.opened_ms = None
        self._opened_ns = None
        self.socket_rebuild_ms = None
        self.first_valid_quote_ms = None
        self.first_valid_quote_timestamp_basis = None
        self.first_valid_quote_delay_ms = None
        self.recovery_to_quote_ms = None
        self.quotes.clear()
        self.subscription_sent_ms = None
        self.subscription_ack_ms = None
        self.subscription_error_code = None
        self._subscription_ids.clear()
        self._subscription_tracking_lost = False

    def opened(self, generation, now_ms):
        # sock.connected can wake a sender before on_open runs. Preserve a
        # request registered for this generation during that small window.
        self.prepare_generation(generation)
        self.opened_ms = now_ms
        self._opened_ns = get_monotonic_timestamp_ns()
        self.socket_rebuild_ms = self._elapsed_ms(self._disconnect_ns, self._opened_ns)

    def subscription_sent(self, request_id, now_ms):
        if len(self._subscription_ids) >= 64:
            self._subscription_tracking_lost = True
            self._subscription_ids.clear()
        if not self._subscription_tracking_lost:
            self._subscription_ids.add(request_id)
        self.subscription_sent_ms = now_ms
        self.subscription_ack_ms = None

    def acknowledge(self, message, now_ms):
        request_id = message.get('id')
        if type(request_id) is not int or request_id not in self._subscription_ids:
            return False
        if message.get('result', False) is None:
            self._subscription_ids.discard(request_id)
            if not self._subscription_ids and self.subscription_error_code is None:
                self.subscription_ack_ms = now_ms
        elif type(message.get('code')) is int:
            self._subscription_ids.discard(request_id)
            self.subscription_error_code = message['code']
        else:
            return False
        return True

    @staticmethod
    def _source_time(message, received_ms, is_perp):
        source_ms = message.get(BinanceWebSocket.data_time.value)
        if source_ms is None:
            source_ms = message.get(BinanceWebSocket.agg_trade_time.value)
        if source_ms is None and not is_perp:
            return received_ms, 'local_receive'
        return (source_ms if type(source_ms) is int and source_ms > 0 else None), 'exchange'

    @staticmethod
    def valid_prices(message):
        try:
            bid, ask, bid_size, ask_size = (
                Decimal(str(message[field])) for field in ('b', 'a', 'B', 'A')
            )
            return all(value.is_finite() and value > 0 for value in (
                bid, ask, bid_size, ask_size,
            )) and bid <= ask
        except (KeyError, ValueError, InvalidOperation):
            return False

    def quote(self, message, received_ms, is_perp):
        source_ms, basis = self._source_time(message, received_ms, is_perp)
        if source_ms is None or not self.valid_prices(message):
            return False
        self.quotes[message[BinanceWebSocket.symbol.value]] = (received_ms, source_ms, basis)
        # This is an observation threshold, never an execution permission.
        if not 0 <= received_ms - source_ms <= 1000:
            return False
        if self.first_valid_quote_ms is None:
            now_ns = get_monotonic_timestamp_ns()
            self.first_valid_quote_ms = received_ms
            self.first_valid_quote_timestamp_basis = basis
            self.first_valid_quote_delay_ms = self._elapsed_ms(self._opened_ns, now_ns)
            self.recovery_to_quote_ms = self._elapsed_ms(self._outage_ns, now_ns)
            self._outage_ns = None
            return True
        return False

    @staticmethod
    def _elapsed_ms(start_ns, end_ns):
        return None if start_ns is None else max(0, (end_ns - start_ns) // 1_000_000)

    def snapshot(self, targets, now_ms, max_age_ms, connected):
        self.quotes = {symbol: stamp for symbol, stamp in self.quotes.items() if symbol in targets}
        fresh = {
            symbol for symbol, (received, source, _) in self.quotes.items()
            if symbol in targets and connected
            and 0 <= now_ms - received <= max_age_ms
            and 0 <= now_ms - source <= max_age_ms
        }
        unavailable = sorted(targets - fresh)
        return {
            'stream_id': self.stream_id,
            'generation': self.generation,
            'connected': connected,
            'disconnect_count': self.disconnect_count,
            'retired_generation_count': self.retired_generation_count,
            'disconnect_reason': self.disconnect_reason,
            'last_error_type': self.last_error_type,
            'last_close_code': self.last_close_code,
            'last_disconnect_ms': self.disconnect_ms,
            'last_open_ms': self.opened_ms,
            'socket_rebuild_ms': self.socket_rebuild_ms,
            'subscription_sent_ms': self.subscription_sent_ms,
            'subscription_ack_ms': self.subscription_ack_ms,
            'subscription_error_code': self.subscription_error_code,
            'subscription_tracking_lost': self._subscription_tracking_lost,
            'first_valid_quote_ms': self.first_valid_quote_ms,
            'first_valid_quote_timestamp_basis': self.first_valid_quote_timestamp_basis,
            'first_valid_quote_delay_ms': self.first_valid_quote_delay_ms,
            'recovery_to_quote_ms': self.recovery_to_quote_ms,
            'last_quote_received_ms': max((stamp[0] for stamp in self.quotes.values()), default=None),
            'first_valid_quote_max_age_ms': 1000,
            'freshness_max_age_ms': max_age_ms,
            'expected_symbol_count': len(targets),
            'observed_symbol_count': len(targets.intersection(self.quotes)),
            'local_receive_symbol_count': sum(stamp[2] == 'local_receive' for stamp in self.quotes.values()),
            'fresh_symbol_count': len(fresh),
            'unavailable_symbol_count': len(unavailable),
            'unavailable_symbols_sample': unavailable[:50],
            'unavailable_symbols_truncated': len(unavailable) > 50,
        }
