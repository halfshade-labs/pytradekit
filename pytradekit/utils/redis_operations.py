import json
from contextlib import ExitStack
from decimal import Decimal, InvalidOperation
from typing import Dict, Mapping, Tuple

import redis
from pytradekit.utils.dynamic_types import RedisFields
from pytradekit.utils.time_handler import TimeConvert
from pytradekit.utils.exceptions import DataTypeException, DependencyException


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


def _normalize_arbitrage_thresholds(
    thresholds: Mapping[str, Decimal],
) -> Dict[str, Decimal]:
    """Validate and normalize a venue-to-threshold mapping."""
    if not isinstance(thresholds, Mapping) or not thresholds:
        raise DataTypeException("Arbitrage thresholds must be a non-empty mapping")
    normalized = {}
    for venue, raw_threshold in thresholds.items():
        normalized_venue = str(venue).strip().upper()
        try:
            threshold = Decimal(str(raw_threshold))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise DataTypeException(
                f"Invalid arbitrage threshold for {normalized_venue or venue}: {raw_threshold!r}"
            ) from exc
        if not normalized_venue or not threshold.is_finite() or threshold < 0:
            raise DataTypeException(
                f"Invalid arbitrage threshold for {normalized_venue or venue}: {raw_threshold!r}"
            )
        normalized[normalized_venue] = threshold
    return normalized


TICKER_PRICE_EXPIRE_TIME = TimeConvert.MIN_TO_S * 10
ORDER_TICKER_EXPIRE_TIME = TimeConvert.MIN_TO_S * 30
ORDERS_EXPIRE_TIME = TimeConvert.MIN_TO_S * 60
PREMIUM_EXPIRE_TIME = TimeConvert.MIN_TO_S * 60 * 24 * 30
ORDER_LINK_EXPIRE_TIME = TimeConvert.DAY_TO_S
TRADE_CONTEXT_EXPIRE_TIME = TimeConvert.DAY_TO_S * 30
TRADE_CONTEXT_MAX_BYTES = 64 * 1024
# Merged portfolios snapshot for close-side premium checks; per-tick freshness
# is validated by the reader, TTL only prevents an eternally stale key.
PORTFOLIOS_EXPIRE_TIME = TimeConvert.MIN_TO_S * 60
# Daily threshold; TTL > 24h so a single missed analyzer run does not drop it
ARBITRAGE_THRESHOLD_EXPIRE_TIME = TimeConvert.DAY_TO_S * 2
TIMEOUT_SECOND = 5


class RedisOperations:
    def __init__(self, logger, redis_url):
        self.client = redis.StrictRedis.from_url(redis_url, decode_responses=True, socket_timeout=TIMEOUT_SECOND)
        self.logger = logger

    def get_lock_for_resource(self, key):
        try:
            lock = self.client.lock(key + '_lock', timeout=TIMEOUT_SECOND)
            return lock
        except Exception as e:
            self.logger.exception(e)
            raise DependencyException(f"Cannot acquire lock for resource: {key}") from e

    def get_json(self, key: str):
        """Read a JSON object; absence is None and corrupt values fail closed."""
        self._validate_redis_key(key)
        try:
            with self.get_lock_for_resource(key):
                raw = self.client.get(key)
        except Exception as exc:
            self.logger.debug("Redis JSON read failed", exc_info=True)
            raise DependencyException("Redis JSON read failed") from exc
        if raw is None:
            return None
        return self._decode_trade_context(raw, key)

    def set_json_with_ttl(self, key: str, value: Mapping, ttl: int) -> None:
        """Encode Decimal losslessly and apply expiry in the same SET command."""
        self._validate_redis_key(key)
        if not isinstance(value, Mapping):
            raise DataTypeException("Redis JSON value must be a mapping")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            raise DataTypeException("Redis TTL must be a positive integer")
        payload = json.dumps(dict(value), cls=_DecimalEncoder, allow_nan=False)
        try:
            with self.get_lock_for_resource(key):
                if not self.client.set(key, payload, ex=ttl):
                    raise DependencyException("Redis SET was not acknowledged")
        except Exception as exc:
            self.logger.debug("Redis JSON write failed", exc_info=True)
            raise DependencyException("Redis JSON write failed") from exc

    def delete_keys(self, keys: Tuple[str, ...]) -> bool:
        """Delete and verify keys under ordered locks shared with their writers."""
        if not isinstance(keys, tuple) or not keys:
            raise DataTypeException("Redis deletion requires a nonempty key tuple")
        for key in keys:
            self._validate_redis_key(key)
        try:
            with ExitStack() as stack:
                for key in sorted(set(keys)):
                    stack.enter_context(self.get_lock_for_resource(key))
                self.client.delete(*keys)
                return all(self.client.get(key) is None for key in keys)
        except Exception as exc:
            self.logger.debug("Redis key deletion failed", exc_info=True)
            raise DependencyException("Redis key deletion failed") from exc

    @staticmethod
    def _validate_redis_key(key: str) -> None:
        if not isinstance(key, str) or not key.strip():
            raise DataTypeException("Redis key must be a nonempty string")

    def merge_portfolios_snapshot(self, value: Mapping) -> None:
        """Merge fresh quotes without publishing an entry signal."""
        if not isinstance(value, Mapping):
            raise DataTypeException("Portfolio snapshot must be a mapping")
        key = RedisFields.portfolios.name
        try:
            with self.get_lock_for_resource(key):
                self._merge_portfolios_snapshot(value)
        except Exception as exc:
            self.logger.debug("Portfolio snapshot write failed", exc_info=True)
            raise DependencyException("Portfolio snapshot write failed") from exc

    def _merge_portfolios_snapshot(self, value: Mapping) -> None:
        key = RedisFields.portfolios.name
        raw = self.client.get(key)
        try:
            merged = self._decode_trade_context(raw, key)
        except DataTypeException:
            # Only this fresh market-data writer may replace a corrupt cache.
            merged = {}
        merged.update(value)
        payload = json.dumps(merged, cls=_DecimalEncoder, allow_nan=False)
        if not self.client.set(key, payload, ex=PORTFOLIOS_EXPIRE_TIME):
            raise DependencyException("Portfolio SET was not acknowledged")

    def set_ticker_price(self, exchange_id, value):
        key = exchange_id + "_" + RedisFields.ticker_price.name
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.hset(key, mapping=value)
                self.client.expire(key, TICKER_PRICE_EXPIRE_TIME)
        except Exception as e:
            self.logger.exception(f"Failed to set ticker price for {exchange_id}: {e}")
            raise DependencyException(f"Failed to set ticker price for {exchange_id}") from e

    def get_ticker_price(self, exchange_id) -> dict:
        key = exchange_id + "_" + RedisFields.ticker_price.name
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                return self.client.hgetall(key)
        except Exception as e:
            self.logger.exception(f"Failed to get ticker price for {exchange_id}: {e}")
            raise DependencyException(f"Failed to get ticker price for {exchange_id}") from e

    def get_order_book(self, inst_code):
        key = f"{RedisFields.orderbook.name}:{inst_code}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                return self.client.hgetall(key)
        except Exception as e:
            self.logger.exception(f"Failed to get order book for {key}: {e}")
            raise DependencyException(f"Failed to get order book for {key}") from e

    def set_book_ticker(self, inst_code, value):
        key = f"{RedisFields.book_ticker.name}:{inst_code}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.hset(key, mapping=value)
                self.client.expire(key, ORDER_TICKER_EXPIRE_TIME)
        except Exception as e:
            self.logger.exception(f"Failed to set book ticker for {inst_code}: {e}")
            raise DependencyException(f"Failed to set book ticker for {inst_code}") from e

    def set_orderbook_changed_within_threshold(self, inst_code, changed_within_threshold: int):
        key = f"{RedisFields.orderbook.name}:{inst_code}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.hset(key, "changed_within_threshold", changed_within_threshold)
                self.client.expire(key, ORDER_TICKER_EXPIRE_TIME)
        except Exception as e:
            self.logger.exception(f"Failed to set orderbook signal for {inst_code}: {e}")
            raise DependencyException(f"Failed to set orderbook signal for {inst_code}") from e

    def get_order_ticker(self, inst_code):
        key = f"{RedisFields.book_ticker.name}:{inst_code}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                return self.client.hgetall(key)
        except Exception as e:
            self.logger.exception(f"Failed to get book ticker for {key}: {e}")
            raise DependencyException(f"Failed to get book ticker for {key}") from e

    def set_orders(self, strategy_id, value):
        key = f"{RedisFields.orders.name}:{strategy_id}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.sadd(key, json.dumps(value, cls=_DecimalEncoder))
                self.client.expire(key, ORDERS_EXPIRE_TIME)
        except Exception as e:
            self.logger.exception(f"Failed to set orders for {strategy_id}: {e}")
            raise DependencyException(f"Failed to set orders for {strategy_id}") from e

    def get_orders_and_delete(self, strategy_id):
        key = f"{RedisFields.orders.name}:{strategy_id}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                data = self.client.smembers(key)
                self.client.delete(key)
                return data
        except Exception as e:
            self.logger.exception(f"Failed to get orders and delete for {key}: {e}")
            raise DependencyException(f"Failed to get orders and delete for {key}") from e

    def set_publish_trades(self, value, timestamp):
        key = f"{RedisFields.trades.name}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.zadd(key, {json.dumps(value, cls=_DecimalEncoder): timestamp})
                self.client.expire(key, ORDERS_EXPIRE_TIME)
                self.client.publish(key, json.dumps(value, cls=_DecimalEncoder))
        except Exception as e:
            self.logger.exception(f"Failed to set trades for : {e}")
            raise DependencyException("Failed to set trades for ") from e

    def get_trades(self):
        key = f"{RedisFields.trades.name}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                data = self.client.zrange(key, 0, -1)
                if data != []:
                    self.client.zrem(key, *data)
                return data
        except Exception as e:
            self.logger.exception(f"Failed to get trades for {key}: {e}")
            raise DependencyException(f"Failed to get trades for {key}") from e

    def set_publish_inventory(self, value):
        key = f"{RedisFields.inventory.name}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, value)
                self.client.expire(key, ORDER_TICKER_EXPIRE_TIME)
                self.client.publish(key, value)
        except Exception as e:
            self.logger.exception(f"Failed to set inventory for {key}: {e}")
            raise DependencyException(f"Failed to set inventory for {key}") from e

    def get_inventory(self):
        key = f"{RedisFields.inventory.name}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                data = self.client.get(key)
                return data
        except Exception as e:
            self.logger.exception(f"Failed to get inventory for {key}: {e}")
            raise DependencyException(f"Failed to get inventory for {key}") from e

    def get_trading_proposal(self):
        key = f"{RedisFields.trading_proposal.name}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                data = self.client.get(key)
                return data
        except Exception as e:
            self.logger.exception(f"Failed to get trading proposal for {key}: {e}")
            raise DependencyException(f"Failed to get trading proposal for {key}") from e

    def push_book_ticker(self, exchange_id, value):
        key = f"{RedisFields.book_ticker.name}:{exchange_id}"
        try:
            self.client.publish(key, json.dumps(value, cls=_DecimalEncoder))
        except Exception as e:
            self.logger.exception(f"Failed to push data for {key}: {e}")
            raise DependencyException(f"Failed to push data for {key}") from e

    def set_publish_inventory_close(self, value):
        key = f"{RedisFields.inventory_close.name}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, json.dumps(value, cls=_DecimalEncoder))
                self.client.publish(key, json.dumps(value, cls=_DecimalEncoder))
        except Exception as e:
            self.logger.exception(f"Failed to set profit_loss for {key}: {e}")
            raise DependencyException(f"Failed to set profit_loss for {key}") from e

    def set_trading_proposal(self, value):
        key = f"{RedisFields.trading_proposal.name}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, json.dumps(value, cls=_DecimalEncoder))
        except Exception as e:
            self.logger.exception(f"Failed to set trading_proposal for {key}: {e}")
            raise DependencyException(f"Failed to set trading_proposal for {key}") from e

    def subscribe_book_ticker(self, exchange_id):
        key = f"{RedisFields.book_ticker.name}:{exchange_id}"
        try:
            pubsub = self.client.pubsub()
            return pubsub.subscribe(key)
        except Exception as e:
            self.logger.exception(f"Failed to push data for {key}: {e}")
            raise DependencyException(f"Failed to push data for {key}") from e

    def set_new_book_ticker(self, exchange_id, value):
        key = f"{RedisFields.book_ticker.name}:{exchange_id}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, json.dumps(value, cls=_DecimalEncoder))
                self.client.expire(key, ORDER_TICKER_EXPIRE_TIME)
        except Exception as e:
            self.logger.exception(f"Failed to set book ticker for {exchange_id}: {e}")
            raise DependencyException(f"Failed to set book ticker for {exchange_id}") from e

    def get_new_book_ticker(self, exchange_id):
        key = f"{RedisFields.book_ticker.name}:{exchange_id}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                raw = self.client.get(key)
                if raw is None:
                    return None
                return json.loads(raw)
        except Exception as e:
            self.logger.exception(f"Failed to get book ticker for {exchange_id}: {e}")
            raise DependencyException(f"Failed to get book ticker for {exchange_id}") from e

    def delete_inventory_close(self):
        key = f"{RedisFields.inventory_close.name}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.delete(key)
        except Exception as e:
            self.logger.exception(f"Failed to delete inventory close for {key}: {e}")
            raise DependencyException(f"Failed to delete inventory close for {key}") from e

    def delete_trading_proposal(self):
        key = f"{RedisFields.trading_proposal.name}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.delete(key)
        except Exception as e:
            self.logger.exception(f"Failed to delete trading proposal for {key}: {e}")
            raise DependencyException(f"Failed to delete trading proposal for {key}") from e

    def set_publish_non_compliant_inst_code(self, exchange_id, value):
        key = f"{RedisFields.non_compliant_inst_code.name}:{exchange_id}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, json.dumps(value, cls=_DecimalEncoder))
                self.client.expire(key, TICKER_PRICE_EXPIRE_TIME)
                self.client.publish(key, json.dumps(value, cls=_DecimalEncoder))
        except Exception as e:
            self.logger.exception(f"Failed to set non_compliant_inst_code for {key}: {e}")
            raise DependencyException(f"Failed to set non_compliant_inst_code for {key}") from e

    def set_depth_order_theoretical(self, exchange_id, value):
        key = f"{RedisFields.depth_order_theoretical.name}:{exchange_id}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, json.dumps(value, cls=_DecimalEncoder))
                self.client.expire(key, TICKER_PRICE_EXPIRE_TIME)
                self.client.publish(key, json.dumps(value, cls=_DecimalEncoder))
        except Exception as e:
            self.logger.exception(f"Failed to set depth_order_theoretical for {key}: {e}")
            raise DependencyException(f"Failed to set depth_order_theoretical for {key}") from e

    def set_portfolios(self, value):
        """Merge `value` ({symbol: data}) into the stored portfolios snapshot and
        publish only the delta.

        The stored key is read by close-side services to evaluate the premium
        close condition across ALL open positions, so it must accumulate
        symbols: a plain SET left only the last signalled symbol in the key,
        silently disabling premium-based closes for every other position
        (cross_exchange_arbitrage#472). Pub/sub subscribers (arbitrage_executor)
        still receive just the per-signal delta. The key expires after
        PORTFOLIOS_EXPIRE_TIME so a quiet market cannot serve an arbitrarily
        old snapshot forever; per-tick freshness (ts_ms/local_ts) is the
        reader's responsibility.
        """
        key = f"{RedisFields.portfolios.name}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self._merge_portfolios_snapshot(value)
                self.client.publish(key, json.dumps(value, cls=_DecimalEncoder))
        except Exception as e:
            self.logger.exception(f"Failed to set portfolios for {key}: {e}")
            raise DependencyException(f"Failed to set portfolios for {key}") from e

    def set_order_link(self, spot_client_order_id: str, perp_client_order_id: str):
        """Store spot→perp client_order_id mapping with 24h TTL."""
        key = f"{RedisFields.order_link.name}:{spot_client_order_id}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, perp_client_order_id)
                self.client.expire(key, ORDER_LINK_EXPIRE_TIME)
        except Exception as e:
            self.logger.exception(f"Failed to set order link for {spot_client_order_id}: {e}")
            raise DependencyException(f"Failed to set order link for {spot_client_order_id}") from e

    def get_order_link(self, spot_client_order_id: str) -> str:
        """Retrieve perp client_order_id for a given spot client_order_id."""
        key = f"{RedisFields.order_link.name}:{spot_client_order_id}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                # client is created with decode_responses=True, so get() already
                # returns str (or None); no bytes decoding needed.
                return self.client.get(key)
        except Exception as e:
            self.logger.exception(f"Failed to get order link for {spot_client_order_id}: {e}")
            raise DependencyException(f"Failed to get order link for {spot_client_order_id}") from e

    def set_trade_context(self, trade_id: str, value: Mapping) -> None:
        """Merge a bounded JSON context for a trade and refresh its 30-day TTL."""
        key = self._trade_context_key(trade_id)
        if not isinstance(value, Mapping):
            raise DataTypeException("Trade context value must be a mapping")
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                current = self._decode_trade_context(self.client.get(key), key)
                merged = {**current, **dict(value)}
                payload = json.dumps(merged, cls=_DecimalEncoder, sort_keys=True)
                if len(payload.encode("utf-8")) > TRADE_CONTEXT_MAX_BYTES:
                    raise DataTypeException(
                        f"Trade context exceeds {TRADE_CONTEXT_MAX_BYTES} bytes"
                    )
                self.client.set(key, payload)
                self.client.expire(key, TRADE_CONTEXT_EXPIRE_TIME)
        except DataTypeException:
            raise
        except Exception as e:
            self.logger.debug(f"Failed to set trade context for {trade_id}: {e}", exc_info=True)
            raise DependencyException(f"Failed to set trade context for {trade_id}") from e

    def get_trade_context(self, trade_id: str):
        """Return the stored trade context, or None when the key is absent."""
        key = self._trade_context_key(trade_id)
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                raw = self.client.get(key)
        except Exception as e:
            self.logger.debug(f"Failed to get trade context for {trade_id}: {e}", exc_info=True)
            raise DependencyException(f"Failed to get trade context for {trade_id}") from e
        if raw is None:
            return None
        return self._decode_trade_context(raw, key)

    def get_liq_hedge(self, trade_id: str):
        """Return liquidation-hedge metadata for one trade, if present."""
        key = self._liq_hedge_key(trade_id)
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                raw = self.client.get(key)
        except Exception as e:
            self.logger.debug(
                f"Failed to get liquidation hedge for {trade_id}: {e}",
                exc_info=True,
            )
            raise DependencyException(
                f"Failed to get liquidation hedge for {trade_id}"
            ) from e
        if raw is None:
            return None
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise DataTypeException(
                f"Invalid liquidation hedge JSON for {key}"
            ) from e
        if not isinstance(decoded, dict):
            raise DataTypeException(
                f"Liquidation hedge for {key} must be a JSON object"
            )
        return decoded

    @staticmethod
    def _trade_context_key(trade_id: str) -> str:
        if not isinstance(trade_id, str) or not trade_id.strip():
            raise DataTypeException("Trade context id must be a non-empty string")
        return f"{RedisFields.trade_context.name}:{trade_id}"

    @staticmethod
    def _liq_hedge_key(trade_id: str) -> str:
        if not isinstance(trade_id, str) or not trade_id.strip():
            raise DataTypeException(
                "Liquidation hedge trade id must be a non-empty string"
            )
        return f"{RedisFields.liq_hedge.name}:{trade_id}"

    @staticmethod
    def _decode_trade_context(raw, key: str) -> dict:
        if raw is None:
            return {}
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise DataTypeException(f"Invalid trade context JSON for {key}") from e
        if not isinstance(decoded, dict):
            raise DataTypeException(f"Trade context for {key} must be a JSON object")
        return decoded

    def set_target_premium(self, client_order_id, premium):
        key = f"{RedisFields.premium.name}:{client_order_id}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, str(premium))
                self.client.expire(key, PREMIUM_EXPIRE_TIME)
        except Exception as e:
            self.logger.exception(f"Failed to set target premium for {client_order_id}: {e}")
            raise DependencyException(f"Failed to set target premium for {client_order_id}") from e

    def get_target_premium(self, order_id):
        key = f"{RedisFields.premium.name}:{order_id}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                value = self.client.get(key)
        except Exception as e:
            self.logger.debug(f"Failed to get target premium for {order_id}: {e}", exc_info=True)
            raise DependencyException(f"Failed to get target premium for {order_id}") from e

        if value is None:
            return None
        try:
            return Decimal(value)
        except InvalidOperation as e:
            self.logger.debug(f"Invalid premium value for {order_id}: {value!r}", exc_info=True)
            raise DataTypeException(f"Invalid premium value for {order_id}: {value!r}") from e

    def set_arbitrage_threshold(self, value):
        """Store the global arbitrage premium threshold (single key, no identifier).

        Written daily by the premium analyzer and read by realtime_compute_premium.
        """
        key = RedisFields.arbitrage_threshold.name
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, str(value))
                self.client.expire(key, ARBITRAGE_THRESHOLD_EXPIRE_TIME)
        except Exception as e:
            self.logger.exception(f"Failed to set arbitrage threshold: {e}")
            raise DependencyException("Failed to set arbitrage threshold") from e

    def get_arbitrage_threshold(self):
        """Return the global arbitrage premium threshold as Decimal, or None if unset."""
        key = RedisFields.arbitrage_threshold.name
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                value = self.client.get(key)
        except Exception as e:
            self.logger.debug(f"Failed to get arbitrage threshold: {e}", exc_info=True)
            raise DependencyException("Failed to get arbitrage threshold") from e

        if value is None:
            return None
        try:
            return Decimal(value)
        except InvalidOperation as e:
            self.logger.debug(f"Invalid arbitrage threshold value: {value!r}", exc_info=True)
            raise DataTypeException(f"Invalid arbitrage threshold value: {value!r}") from e

    def set_arbitrage_thresholds(self, thresholds: Mapping[str, Decimal]):
        """Store atomic per-venue arbitrage thresholds under a separate key."""
        normalized = _normalize_arbitrage_thresholds(thresholds)
        key = RedisFields.arbitrage_thresholds.name
        payload = json.dumps(normalized, cls=_DecimalEncoder, sort_keys=True)
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, payload)
                self.client.expire(key, ARBITRAGE_THRESHOLD_EXPIRE_TIME)
        except Exception as e:
            self.logger.exception(f"Failed to set arbitrage thresholds: {e}")
            raise DependencyException("Failed to set arbitrage thresholds") from e

    def get_arbitrage_thresholds(self):
        """Return per-venue Decimal thresholds, or None when the key is absent."""
        key = RedisFields.arbitrage_thresholds.name
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                value = self.client.get(key)
        except Exception as e:
            self.logger.debug(f"Failed to get arbitrage thresholds: {e}", exc_info=True)
            raise DependencyException("Failed to get arbitrage thresholds") from e
        if value is None:
            return None
        try:
            decoded = json.loads(value)
            return _normalize_arbitrage_thresholds(decoded)
        except (json.JSONDecodeError, DataTypeException, TypeError) as e:
            self.logger.debug(f"Invalid arbitrage thresholds value: {value!r}", exc_info=True)
            raise DataTypeException(f"Invalid arbitrage thresholds value: {value!r}") from e

    def ping(self):
        """Verify the Redis connection is alive. Raises DependencyException on failure."""
        try:
            self.client.ping()
        except Exception as e:
            self.logger.debug(f"Redis ping failed: {e}", exc_info=True)
            raise DependencyException("Redis ping failed") from e

    def create_pubsub(self):
        """Create and return a Redis pubsub object."""
        try:
            return self.client.pubsub()
        except Exception as e:
            self.logger.debug(f"Failed to create pubsub: {e}", exc_info=True)
            raise DependencyException("Failed to create pubsub") from e

    def close(self):
        """Close the underlying Redis connection. Raises DependencyException on failure."""
        try:
            self.client.close()
        except Exception as e:
            self.logger.debug(f"Failed to close Redis connection: {e}", exc_info=True)
            raise DependencyException("Failed to close Redis connection") from e
