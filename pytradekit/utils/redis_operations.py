import json

import redis
from pytradekit.utils.dynamic_types import RedisFields
from pytradekit.utils.time_handler import TimeConvert
from pytradekit.utils.exceptions import DependencyException

TICKER_PRICE_EXPIRE_TIME = TimeConvert.MIN_TO_S * 10
ORDER_TICKER_EXPIRE_TIME = TimeConvert.MIN_TO_S * 30
ORDERS_EXPIRE_TIME = TimeConvert.MIN_TO_S * 60
PREMIUM_EXPIRE_TIME = TimeConvert.MIN_TO_S * 60 * 24 * 30
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

    def set_ticker_price(self, exchange_id, value):
        key = exchange_id + "_" + RedisFields.ticker_price.name
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.hmset(key, value)
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
                self.client.hmset(key, value)
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
                self.client.sadd(key, json.dumps(value))
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
                self.client.zadd(key, {json.dumps(value): timestamp})
                self.client.expire(key, ORDERS_EXPIRE_TIME)
                self.client.publish(key, json.dumps(value))
        except DependencyException as e:
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
            self.logger.exception(f"Failed to get inventory for {key}: {e}")
            raise DependencyException(f"Failed to get inventory for {key}") from e

    def push_book_ticker(self, exchange_id, value):
        key = f"{RedisFields.book_ticker.name}:{exchange_id}"
        try:
            self.client.publish(key, json.dumps(value))
        except Exception as e:
            self.logger.exception(f"Failed to push data for {key}: {e}")
            raise DependencyException(f"Failed to push data for {key}") from e

    def set_publish_inventory_close(self, value):
        key = f"{RedisFields.inventory_close.name}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, json.dumps(value))
                self.client.publish(key, json.dumps(value))
        except Exception as e:
            self.logger.exception(f"Failed to set profit_loss for {key}: {e}")
            raise DependencyException(f"Failed to set profit_loss for {key}") from e

    def set_trading_proposal(self, value):
        key = f"{RedisFields.trading_proposal.name}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, json.dumps(value))
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
                self.client.set(key, json.dumps(value))
                self.client.expire(key, ORDER_TICKER_EXPIRE_TIME)
        except Exception as e:
            self.logger.exception(f"Failed to set book ticker for {exchange_id}: {e}")
            raise DependencyException(f"Failed to set book ticker for {exchange_id}") from e

    def get_new_book_ticker(self, exchange_id):
        key = f"{RedisFields.book_ticker.name}:{exchange_id}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                return json.loads(self.client.get(key))
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
                self.client.set(key, json.dumps(value))
                self.client.expire(key, TICKER_PRICE_EXPIRE_TIME)
                self.client.publish(key, json.dumps(value))
        except Exception as e:
            self.logger.exception(f"Failed to set non_compliant_inst_code for {key}: {e}")
            raise DependencyException(f"Failed to set non_compliant_inst_code for {key}") from e

    def set_depth_order_theoretical(self, exchange_id, value):
        key = f"{RedisFields.depth_order_theoretical.name}:{exchange_id}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, json.dumps(value))
                self.client.expire(key, TICKER_PRICE_EXPIRE_TIME)
                self.client.publish(key, json.dumps(value))
        except Exception as e:
            self.logger.exception(f"Failed to set depth_order_theoretical for {key}: {e}")
            raise DependencyException(f"Failed to set depth_order_theoretical for {key}") from e

    def set_portfolios(self, value):
        key = f"{RedisFields.portfolios.name}"
        lock = self.get_lock_for_resource(key)
        try:
            with lock:
                self.client.set(key, json.dumps(value))
                self.client.publish(key, json.dumps(value))
        except Exception as e:
            self.logger.exception(f"Failed to set portfolios for {key}: {e}")
            raise DependencyException(f"Failed to set portfolios for {key}") from e

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
                return float(self.client.get(key))
        except Exception as e:
            self.logger.exception(f"Failed to get target premium for {order_id}: {e}")
            raise DependencyException(f"Failed to get target premium for {order_id}") from e