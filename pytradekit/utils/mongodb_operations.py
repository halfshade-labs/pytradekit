"""
命名规范：
增：insert
删： delete
改： update
查： read
"""
import json
import re
from urllib.parse import urlparse, quote_plus, urlunparse
import functools

import pandas as pd
from pandas import DataFrame
import numpy as np
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, NetworkTimeout, OperationFailure, ServerSelectionTimeoutError

from pytradekit.utils.time_handler import DATETIME_FORMAT_DAY, get_yesterday_datetime, \
    get_timestamp_s, get_rounded_time_interval, TimeSpan
from pytradekit.utils.static_types import Database, OrderAttribute, TradeAttribute, AccountAttribute, \
    LastAggtradeAttribute, InstcodeBasicAttribute, \
    OrderBookAttribute, OrderDepthRatioAttribute, RankAttribute, BalanceAttribute, DepositWithdrawAttribute, \
    InventoryAttribute, DepthAttribute, PnlAttribute, VolumeFeeAttribute, BudgetAttribute, UserLoanAttribute, \
    MaxInventoryAttribute, ArbitragePoolsReportAttribute
from pytradekit.utils.dynamic_types import DepositWithdrawAuxiliary, DuplicateFields, ExchangeId
from pytradekit.utils.custom_types import InstCode
from pytradekit.utils.exceptions import NoDataException

SLOW_QUERY_THRESHOLD = 60
BATCH_SIZE = 1_000_000


class AtUser:
    debug = f''


class CollectionPath:
    def __init__(self, db_name, collection_name):
        self.db_name = db_name
        self.collection_name = collection_name


class MongodbOperations:
    _client = None

    @staticmethod
    def handle_mongodb_errors(default_return=None):
        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                instance_or_class = args[0] if args else None

                try:
                    if not instance_or_class._check_connection():
                        instance_or_class.logger.info(
                            f"MongoDB connection failed in {func.__name__}. Using default value. {AtUser.debug}")
                        return default_return
                    return func(*args, **kwargs)
                except (ConnectionFailure, NetworkTimeout, OperationFailure, ServerSelectionTimeoutError) as e:
                    instance_or_class.logger.info(
                        f"MongoDB operation failed in {func.__name__}: {str(e)}. Using default value. {AtUser.debug}")
                    return default_return
                except Exception as e:
                    instance_or_class.logger.info(f"Unexpected error in {func.__name__}: {str(e)} {AtUser.debug}")
                    raise

            return wrapper

        return decorator

    def __init__(self, mongodb_url, logger=None):
        if MongodbOperations._client is None:
            MongodbOperations._client = self._create_client(mongodb_url)
        self.client = MongodbOperations._client
        self.logger = logger

    def get_correct_dict(self, a_dict) -> dict:
        new_dict = {}
        for key1, val1 in a_dict.items():
            if isinstance(val1, dict):
                val1 = self.get_correct_dict(val1)
            if isinstance(val1, np.bool_):
                val1 = bool(val1)
            if isinstance(val1, np.int64):
                val1 = int(val1)
            if isinstance(val1, np.float64):
                val1 = float(val1)
            if isinstance(val1, pd.Timedelta):
                val1 = str(val1)
            new_dict[key1] = val1
        return new_dict

    def log_duplicates(self, df, subset, message):
        duplicate_rows = df[df.duplicated(subset=subset, keep=False)]
        if not duplicate_rows.empty:
            self.logger.debug(f'{message}: {len(duplicate_rows)} duplicate rows')
            return df.drop_duplicates(subset=subset, keep='last')
        else:
            return df

    @staticmethod
    def log_slow_query(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            instance_or_class = args[0] if args else None

            start_time = get_timestamp_s()
            result = func(*args, **kwargs)
            duration = get_timestamp_s() - start_time

            if duration > SLOW_QUERY_THRESHOLD:
                instance_or_class.logger.info(
                    f"Slow query detected in {func.__name__}: {duration} seconds. {AtUser.debug}")

            return result

        return wrapper

    @staticmethod
    def _create_client(mongodb_url):
        parsed = urlparse(mongodb_url)
        username = quote_plus(parsed.username or "")
        password = quote_plus(parsed.password or "")
        netloc = f"{username}:{password}@{parsed.hostname}:{parsed.port}"
        safe_mongodb_url = urlunparse(
            (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
        print(safe_mongodb_url)
        return MongoClient(safe_mongodb_url)

    def _check_connection(self):
        try:
            # 尝试获取MongoDB的服务器状态
            self.client.admin.command('ping')
            return True
        except (ConnectionFailure, NetworkTimeout):
            if self.logger:
                self.logger.warning("MongoDB connection lost. Attempting to reconnect.")
            return False

    def close(self):
        """
        关闭MongoDB连接并重置单例实例。
        """
        if self.client:
            self.client.close()
            if self.logger:
                self.logger.debug("MongoDB connection closed.")
            MongodbOperations._client = None

    def delete_coll(self, collection_path):
        """
        Deletes the entire specified MongoDB collection.

        :param db_name: The name of the database.
        :param collection_name: The name of the collection to be deleted.
        :return: A message indicating the outcome of the operation.
        """
        collection = self.client[collection_path.db_name][collection_path.collection_name]
        result = collection.delete_many({})  # Empty filter to match all documents

        if result.deleted_count > 0:
            if self.logger:
                self.logger.info(
                    f"Deleted {result.deleted_count} documents from {collection_path.db_name}.{collection_path.collection_name}. The collection has been emptied.")
            return f"Deleted {result.deleted_count} documents. The collection has been emptied."
        else:
            if self.logger:
                self.logger.warning(
                    f"No documents were found in {collection_path.db_name}.{collection_path.collection_name}, or the collection does not exist.")
            return "No documents were found, or the collection does not exist."

    def delete_timeseries(self, collection_path, time_span, columns=OrderAttribute.event_time_ms.name):
        collection = self.client[collection_path.db_name][collection_path.collection_name]
        query = {
            columns: {
                "$gte": time_span.start,
                "$lte": time_span.end
            }
        }
        result = collection.delete_many(query)
        if result.deleted_count > 0:
            if self.logger:
                self.logger.info(
                    f"Deleted {result.deleted_count} documents from {collection_path.db_name}_{collection_path.collection_name} between {time_span.start} and {time_span.end}.")
            return result.deleted_count
        else:
            if self.logger:
                self.logger.info(
                    f"No documents deleted from {collection_path.db_name}_{collection_path.collection_name} between {time_span.start} and {time_span.end}.")
            return 0

    # def delete_timeseries_batch(self, logs_list, collection_path, time_span, batch_size=BATCH_SIZE,
    #                             columns=OrderAttribute.event_time_ms.name):
    def delete_timeseries_batch(self, logs_list, collection_path, time_span, columns=OrderAttribute.event_time_ms.name):
        collection = self.client[collection_path.db_name][collection_path.collection_name]
        query = {
            columns: {
                "$gte": time_span.start,
                "$lte": time_span.end
            }
        }

        # total_deleted = 0
        collection.delete_many(query)
        # while True:
        #     documents = collection.find(query).sort(columns, 1).limit(batch_size)
        #     ids_to_delete = [doc['_id'] for doc in documents]
        #
        #     # 如果没有要删除的文档，跳出循环
        #     if not ids_to_delete:
        #         break
        #
        #     # 删除找到的文档
        #     result = collection.delete_many({"_id": {"$in": ids_to_delete}})
        #     deleted_count = result.deleted_count
        #     total_deleted += deleted_count
        #
        #     logs_list.append(
        #         f"delete_timeseries_batch from {collection_path.db_name}_{collection_path.collection_name} deleted {deleted_count} data.")
        #
        logs_list.append(
            f"delete_timeseries_batch total_deleted {collection_path.db_name}_{collection_path.collection_name} data.")

        return logs_list

    def delete_inst_code_basic(self, exchange_id, inst_code=None):

        collection = self.client[Database.raw_market.name][f'{exchange_id}_{Database.inst_code_basic.name}']
        query = {}
        if inst_code:
            query['inst_code'] = inst_code
        result = collection.delete_many(query)
        return result

    def insert_data_if_not_exists(self, data, collection_path):
        collection = self.client[collection_path.db_name][collection_path.collection_name]

        def data_exists(data_item):
            return collection.find_one({"_id": data_item["_id"]}) is not None

        if isinstance(data, list):
            for item in data:
                item_dict = self.get_correct_dict(item)
                if data_exists(item_dict):
                    if self.logger:
                        self.logger.info(f"Data already exists: {item_dict}")
                else:
                    collection.insert_one(item_dict)
        else:
            item_dict = self.get_correct_dict(data)
            if data_exists(item_dict):
                if self.logger:
                    self.logger.info(f"Data already exists: {item_dict}")
            else:
                collection.insert_one(item_dict)

    def insert_data(self, data, collection_path):
        if isinstance(data, list):
            self.client[collection_path.db_name][collection_path.collection_name].insert_many(
                [self.get_correct_dict(d) for d in data])
        else:
            self.client[collection_path.db_name][collection_path.collection_name].insert_one(
                self.get_correct_dict(data))

    def insert_orders(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.raw_orders.name,
                                         collection_name=f'{exchange_id}_{Database.orders.name}')
        self.insert_data(data, collection_path)

    def insert_orders_fix(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.raw_orders.name,
                                         collection_name=f'{exchange_id}_{Database.orders.name}_fix')
        self.insert_data(data, collection_path)

    def insert_orders_test(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.raw_orders.name,
                                         collection_name=f'{exchange_id}_{Database.orders.name}_test')
        self.insert_data(data, collection_path)

    def insert_orders_raw(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.raw_orders.name,
                                         collection_name=f'{exchange_id}_{Database.orders_raw.name}')
        self.insert_data(data, collection_path)

    def insert_trades(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.raw_orders.name,
                                         collection_name=f'{exchange_id}_{Database.trades.name}')
        self.insert_data(data, collection_path)

    def insert_trades_fix(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.raw_orders.name,
                                         collection_name=f'{exchange_id}_{Database.trades.name}_fix')
        self.insert_data(data, collection_path)

    def insert_trades_inventory(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.raw_orders.name,
                                         collection_name=f'{exchange_id}_{Database.trades_inventory.name}')
        self.insert_data(data, collection_path)

    def insert_inventory(self, data):
        collection_path = CollectionPath(db_name=Database.inventory_management.name,
                                         collection_name=f'{Database.inventory.name}')
        self.insert_data(data, collection_path)

    def insert_inventory_quantity(self, data):
        collection_path = CollectionPath(db_name=Database.inventory_management.name,
                                         collection_name=f'{Database.inventory_quantity.name}')
        self.insert_data(data, collection_path)

    def insert_inventory_unrealized_profit_close_point(self, data):
        collection_path = CollectionPath(db_name=Database.inventory_management.name,
                                         collection_name=f'{Database.unrealized_profit_close_point.name}')
        self.insert_data(data, collection_path)

    def insert_klines(self, inst_code, interval, data):
        collection_path = CollectionPath(db_name=Database.raw_kline.name,
                                         collection_name=f'{inst_code}_{interval}')
        self.insert_data(data, collection_path)

    def insert_orderbook(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.raw_market.name,
                                         collection_name=f'{exchange_id}_{Database.order_book.name}')
        self.insert_data(data, collection_path)

    def insert_orderbook_ws(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.raw_market.name,
                                         collection_name=f'{exchange_id}_{Database.order_book_ws.name}')
        self.insert_data(data, collection_path)

    def insert_order_depth_ratio(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.metrics_order_depth.name,
                                         collection_name=f'{exchange_id}_{Database.order_depth_ratio.name}')
        self.insert_data(data, collection_path)

    def insert_balances(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.raw_accounts.name,
                                         collection_name=f'{exchange_id}_{Database.balance.name}')
        self.insert_data(data, collection_path)

    def insert_deposit_withdraw(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.raw_accounts.name,
                                         collection_name=f'{exchange_id}_{Database.deposit_withdraw.name}')
        collection = self.client[collection_path.db_name][collection_path.collection_name]

        new_ids = {item["id"] for item in data}
        existing_ids = {item["id"] for item in collection.find({"id": {"$in": list(new_ids)}})}
        filtered_data = [item for item in data if item["id"] not in existing_ids]

        if not filtered_data:
            self.logger.debug("No new data to insert.")
            return False

        self.insert_data(filtered_data, collection_path)
        return filtered_data

    def insert_pnl(self, data):
        collection_path = CollectionPath(db_name=Database.raw_metrics.name,
                                         collection_name=Database.pnl.name)
        self.insert_data(data, collection_path)

    def insert_volume_fee(self, data):
        collection_path = CollectionPath(db_name=Database.raw_metrics.name,
                                         collection_name=Database.volume_fee.name)
        self.insert_data(data, collection_path)

    def insert_run_task(self, data):
        collection_path = CollectionPath(db_name=Database.raw_task.name,
                                         collection_name=Database.run_task.name)
        self.insert_data(data, collection_path)

    def insert_swap_position_risk(self, data):
        collection_path = CollectionPath(db_name=Database.raw_accounts.name,
                                         collection_name=Database.swap_position.name)
        self.insert_data(data, collection_path)

    def insert_swap_income(self, data):
        collection_path = CollectionPath(db_name=Database.raw_accounts.name,
                                         collection_name=Database.swap_income.name)
        self.insert_data(data, collection_path)

    def insert_last_agg_trade_time(self, data):
        collection_path = CollectionPath(db_name=Database.mvid_official.name,
                                         collection_name=Database.last_agg_trade_time.name)
        self.insert_data(data, collection_path)

    def insert_inst_code_basic(self, data, exchange_id=None):
        if not exchange_id:
            exchange_id = InstCode.from_string(data['inst_code']).exchange_id
        collection_path = CollectionPath(db_name=Database.raw_market.name,
                                         collection_name=f'{exchange_id}_{Database.inst_code_basic.name}')
        self.insert_data(data, collection_path)

    def insert_rank(self, data, exchange_id=None):
        if not exchange_id:
            exchange_id = InstCode.from_string(data['inst_code']).exchange_id
        collection_path = CollectionPath(db_name=Database.raw_market.name,
                                         collection_name=f'{exchange_id}_{Database.rank.name}')
        self.insert_data(data, collection_path)

    def insert_accounts(self, data):
        collection_path = CollectionPath(db_name=Database.raw_accounts.name,
                                         collection_name=Database.all_accounts.name)
        self.insert_data(data, collection_path)

    def insert_trades_other(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.raw_orders.name,
                                         collection_name=f'{exchange_id}_{Database.trades_other.name}')
        self.insert_data(data, collection_path)

    def insert_orders_other(self, data, exchange_id):
        collection_path = CollectionPath(db_name=Database.raw_orders.name,
                                         collection_name=f'{exchange_id}_{Database.orders_other.name}')
        self.insert_data(data, collection_path)

    def insert_depth(self, data):
        collection_path = CollectionPath(db_name=Database.raw_metrics.name,
                                         collection_name=Database.depth.name)
        self.insert_data(data, collection_path)

    def insert_budget(self, data):
        collection_path = CollectionPath(db_name=Database.raw_task.name,
                                         collection_name=Database.budget.name)
        self.insert_data(data, collection_path)

    def insert_daily_order(self, data):
        collection_path = CollectionPath(db_name=Database.raw_metrics.name,
                                         collection_name=Database.daily_order.name)
        self.insert_data(data, collection_path)

    def insert_max_inventory(self, data):
        collection_path = CollectionPath(db_name=Database.inventory_management.name,
                                         collection_name=Database.max_inventory.name)
        self.insert_data(data, collection_path)

    def check_duplicate_open_time(self, inst_code, interval, open_time_s):
        coll_name = f'{inst_code}_{interval}'
        duplicate_count = self.client[Database.raw_kline.name][coll_name].count_documents(
            {'inst_code': inst_code, 'frame': interval, 'open_time_s': open_time_s})
        return duplicate_count > 0

    @staticmethod
    def get_orders_collection_name(exchange_id, is_other, is_raw):
        if is_other:
            return f'{exchange_id}_{Database.orders_other.name}'
        elif is_raw:
            return f'{exchange_id}_{Database.orders_raw.name}'
        else:
            return f'{exchange_id}_{Database.orders.name}'

    def get_balance_time_span(self, account_id: str, time_span: TimeSpan) -> TimeSpan:
        res_first = self.read_balance(
            account_id=account_id,
            time_span=time_span,
            limit=1,
            asc=True,
            is_df=False  # 返回 list of dict
        )

        # 2) 获取最晚的一条数据 (降序)
        res_last = self.read_balance(
            account_id=account_id,
            time_span=time_span,
            limit=1,
            asc=False,
            is_df=False
        )

        first_hour_str = res_first[0][BalanceAttribute.hour.name]
        last_hour_str = res_last[0][BalanceAttribute.hour.name]
        return TimeSpan(start=first_hour_str, end=last_hour_str)

    @staticmethod
    def get_trades_collection_name(exchange_id, is_other, is_inventory=None):
        if is_other:
            return f'{exchange_id}_{Database.trades_other.name}'
        elif is_inventory:
            return f'{exchange_id}_{Database.trades_inventory.name}'
        else:
            return f'{exchange_id}_{Database.trades.name}'

    def read_accounts(self, account_id=None, account_name=None, exchange_id=None, user=None, is_mm=None, is_df=False):
        params = {}
        if account_name:
            params[AccountAttribute.account_name.name] = account_name
        if isinstance(account_id, list):
            params[BalanceAttribute.account_id.name] = {'$in': account_id}
        elif account_id:
            params[AccountAttribute.account_id.name] = account_id
        if is_mm is not None:
            params[AccountAttribute.is_mm.name] = 1
        if exchange_id:
            params[AccountAttribute.exchange_id.name] = exchange_id
        if user:
            params[AccountAttribute.user.name] = user
        res = self.client[Database.raw_accounts.name][Database.all_accounts.name].find(params)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f"No accounts found for account {account_id}")
        if is_df:
            return DataFrame(res)
        return res

    def read_exchanges(self, user=None):
        query = {"is_mm": 1}
        if user:
            if isinstance(user, str):
                user_list = [u.strip() for u in user.split(",") if u.strip()]
            elif isinstance(user, (list, tuple)):
                user_list = list(user)
            else:
                raise ValueError(f"Unsupported user type: {type(user)}")

            if len(user_list) == 1:
                query["user"] = user_list[0]
            else:
                query["user"] = {"$in": user_list}

        cursor = self.client["raw_accounts"]["all_accounts"].find(query, {"_id": 0, "exchange_id": 1})
        exchange_ids = list({doc["exchange_id"] for doc in cursor})
        return exchange_ids

    def read_inst_code_basic(self, inst_code=None, exchange_id=None):
        if not exchange_id:
            exchange_id = InstCode.from_string(inst_code).exchange_id
        params = {}
        if inst_code:
            if isinstance(inst_code, list):
                params[InstcodeBasicAttribute.inst_code.name] = {"$in": inst_code}
            else:
                params[InstcodeBasicAttribute.inst_code.name] = inst_code
        res = self.client[Database.raw_market.name][f'{exchange_id}_{Database.inst_code_basic.name}'].find(params)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No inst_code_basic found for inst_code {inst_code}')
        return res

    def read_pairs(self, inst_code_list: list, exchange_id=None) -> list:
        if not exchange_id:
            exchange_id = InstCode.from_string(inst_code_list[0]).exchange_id
        params = {}
        params[InstcodeBasicAttribute.inst_code.name] = {"$in": inst_code_list}
        res = self.client[Database.raw_market.name][f'{exchange_id}_inst_code_basic'].find(params)
        res = list(res)
        res = [i['pair'] for i in res]
        if len(res) == 0:
            raise NoDataException(f'No pairs found for exchange_id {exchange_id}')
        return res

    def read_coin_inst_code(self, coin: list, exchange_id=None) -> list:
        if not exchange_id:
            raise ValueError("exchange_id is required but not provided")

        params = {
            "$or": [
                {InstcodeBasicAttribute.base.name: {"$in": coin}},
                {InstcodeBasicAttribute.quote.name: {"$in": coin}}
            ]
        }

        collection_name = f"{exchange_id}_{Database.inst_code_basic.name}"
        res_cursor = self.client[Database.raw_market.name][collection_name].find(params)
        res_list = list(res_cursor)

        if len(res_list) == 0:
            raise NoDataException(f"No inst_code found where base/quote in {coin} for exchange_id={exchange_id}")
        return [doc[InstcodeBasicAttribute.inst_code.name] for doc in res_list]

    def read_last_agg_trade_time(self, inst_code=None, exchange_id=None):
        params = {}
        if inst_code:
            params[LastAggtradeAttribute.inst_code.name] = inst_code
        if exchange_id:
            params[LastAggtradeAttribute.exchange_id.name] = exchange_id
        res = self.client[Database.mvid_official.name][Database.last_agg_trade_time.name].find(
            params)  ## TODO 修改agg trade 的位置
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No last agg trade found for inst_code {inst_code}')
        return res

    def read_last_trade_time(self, exchange_id) -> DataFrame:
        collection = self.client[Database.mvid_official.name][Database.last_agg_trade_time.name]
        res = collection.find({LastAggtradeAttribute.exchange_id.name: exchange_id})
        df = pd.DataFrame(res)
        df = df.drop('_id', axis=1)
        df = df.set_index(LastAggtradeAttribute.inst_code.name)
        return df

    def read_last_kline(self, inst_code, interval):
        coll_name = f'{inst_code}_{interval}'
        last_kline = self.client[Database.raw_kline.name][coll_name].find().sort("open_time_s", -1).limit(1)
        return list(last_kline)

    def read_kline_df(self, inst_code, time_span, interval):
        coll_name = f'{inst_code}_{interval}'
        cursor = (self.client[Database.raw_kline.name][coll_name]
                  .find({
            "open_time_s": {
                "$gte": time_span.start,
                "$lte": time_span.end
            }
        })
                  .sort("open_time_s", 1))
        return pd.DataFrame(list(cursor))

    def read_budget(self, day=None, exchange_id=None, strategy_id=None, account_id=None, is_df=False):
        params = {}
        if day:
            params[BudgetAttribute.day.name] = day
        if account_id:
            params[BudgetAttribute.account_id.name] = account_id
        if exchange_id:
            params[BudgetAttribute.exchange_id.name] = exchange_id
        if strategy_id:
            params[BudgetAttribute.strategy_id.name] = strategy_id
        res = self.client[Database.raw_task.name][Database.budget.name].find(params)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No budget found for {params}')
        if is_df:
            return DataFrame(res)
        else:
            return res

    def read_last_orderbook(self, inst_code, exchange_id=None, is_other=False) -> dict:
        if not exchange_id:
            exchange_id = InstCode.from_string(inst_code).exchange_id
        if is_other:
            coll = f'{exchange_id}_{Database.order_book.name}_ws'
        else:
            coll = f'{exchange_id}_{Database.order_book.name}'
        last_orderbook = self.client[Database.raw_market.name][coll].find(
            {OrderBookAttribute.inst_code.name: inst_code}).sort(OrderBookAttribute.time_ms.name, -1).limit(1)
        last_orderbook_list = list(last_orderbook)
        if len(last_orderbook_list) == 0:
            raise NoDataException(f'No last order book found for inst_code {inst_code}')
        return last_orderbook_list[0]

    def read_orderbook(self, time_span, inst_code=None, exchange_id=None, is_df=False):
        if not exchange_id:
            exchange_id = InstCode.from_string(inst_code).exchange_id
        params = {}

        if time_span:
            params[OrderBookAttribute.time_ms.name] = {
                "$gte": time_span.start,
                "$lte": time_span.end
            }
        res = self.client[Database.raw_market.name][f'{exchange_id}_{Database.order_book.name}'].find(params)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No orderbook found for inst_code {inst_code}')
        if is_df:
            return DataFrame(res)
        else:
            return res

    def read_order_depth_ratio(self, time_span, exchange_id, inst_code=None, strategy_id=None, is_df=None, limit=None):
        if not exchange_id:
            exchange_id = InstCode.from_string(inst_code).exchange_id
        params = {}
        params[OrderDepthRatioAttribute.time_ms.name] = {
            "$gte": time_span.start,
            "$lte": time_span.end
        }

        if inst_code:
            params[OrderAttribute.inst_code.name] = inst_code

        if strategy_id:
            if isinstance(strategy_id, list):
                params[TradeAttribute.strategy_id.name] = {"$in": strategy_id}
            else:
                params[TradeAttribute.strategy_id.name] = strategy_id
        if limit:
            res = self.client[Database.metrics_order_depth.name][
                f'{exchange_id}_{Database.order_depth_ratio.name}'].find(
                params).sort(SwapPositionAttribute.event_time_ms.name, -1).limit(limit)
        else:
            res = self.client[Database.metrics_order_depth.name][
                f'{exchange_id}_{Database.order_depth_ratio.name}'].find(
                params)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No order depth ratio found for inst_code {inst_code}')
        if is_df:
            return DataFrame(res)
        else:
            return res

    def read_swap_position_risk(self, inst_code, account_id, time_span=None, limit=1):
        params = {}
        if inst_code:
            params[SwapPositionAttribute.inst_code.name] = inst_code
        if account_id:
            params[SwapPositionAttribute.account_id.name] = account_id
        if time_span:
            params[SwapPositionAttribute.event_time_ms.name] = {
                "$gte": time_span.start,
                "$lte": time_span.end
            }
        res = self.client[Database.raw_accounts.name][Database.swap_position.name].find(params).sort(
            SwapPositionAttribute.event_time_ms.name,
            -1).limit(limit)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No swap position found for inst_code {inst_code}')
        return res

    def read_swap_income(self, account_id, inst_code):
        params = {}
        if account_id:
            params[SwapIncomeAttribute.account_id.name] = account_id
        if inst_code:
            params[SwapIncomeAttribute.inst_code.name] = inst_code
        res = self.client[Database.raw_accounts.name][Database.swap_income.name].find(params)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No swap income found for inst_code {inst_code}')
        return res

    def read_balance(self, account_id=None, exchange_id=None, hour=None, time_span=None, is_df=False, limit=1,
                     asc=True):
        if not exchange_id:
            exchange_id = account_id.split('_')[0]
        params = {}

        if account_id:
            params[BalanceAttribute.account_id.name] = account_id

        if hour:
            params[BalanceAttribute.hour.name] = hour

        if time_span:
            params[BalanceAttribute.hour.name] = {
                "$gte": time_span.start,
                "$lte": time_span.end
            }

        sort_direction = 1 if asc else -1

        if limit:
            res = self.client[Database.raw_accounts.name][f'{exchange_id}_{Database.balance.name}'].find(params).sort(
                BalanceAttribute.event_time_ms.name, sort_direction).limit(limit)
        else:
            res = self.client[Database.raw_accounts.name][f'{exchange_id}_{Database.balance.name}'].find(params).sort(
                BalanceAttribute.event_time_ms.name, sort_direction)

        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No balance found for account_id {account_id}')
        if is_df:
            return DataFrame(res)
        else:
            return res

    def read_balance_df(self, account_id=None, hour=None):
        try:
            balance_list = self.read_balance(account_id=account_id, hour=hour)
            balance_df = DataFrame(json.loads(balance_list[0]['value']))
            return balance_df
        except NoDataException as e:
            self.logger.debug(f'No balance found for account {account_id}, at hour {hour}, {e.note}')
            return DataFrame()

    def read_loan_df(self, loan_version=None, user=None, loan_category=None):
        params = {}
        if user:
            params[UserLoanAttribute.user.name] = user

        if loan_category or loan_version is None:
            collections = self.client[Database.loan.name].list_collection_names()
            collection_name = f"{Database.loan.name}_{loan_category}" if loan_category else f"{Database.loan.name}"
            pattern = re.compile(rf'^{collection_name}_(\d{{4}}-\d{{2}}-\d{{2}})$')
            valid_versions = []
            for c in collections:
                match = pattern.match(c)
                if match:
                    valid_versions.append(match.group(1))

            if not valid_versions:
                raise NoDataException('No valid loan collections found')
            latest_version = max(valid_versions)
            loan_version = loan_category + '_' + latest_version if loan_category else latest_version

        collection_name = f'{Database.loan.name}_{loan_version}'
        res = self.client[Database.loan.name][collection_name].find(params)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No loan found for loan_version {loan_version}')
        return DataFrame(res), loan_version

    def read_deposit_withdraw(self, exchange_id=None, account_id=None, time_span=None):
        if not exchange_id:
            exchange_id = account_id.split('_')[0]
        params = {}

        if account_id:
            params[DepositWithdrawAttribute.account_id.name] = account_id

        if time_span:
            params[DepositWithdrawAttribute.time_ms.name] = {
                "$gte": time_span.start,
                "$lte": time_span.end
            }
        res = self.client[Database.raw_accounts.name][f'{exchange_id}_{Database.deposit_withdraw.name}'].find(params)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No deposit withdraw found for inst_code {exchange_id}')
        return list(res)

    def read_deposit_withdraw_df(self, account_id, time_span):
        try:
            deposit_withdraw = self.read_deposit_withdraw(account_id=account_id, time_span=time_span)
        except NoDataException as e:
            self.logger.debug(f'{e.note},from {time_span.start} to {time_span.end}')
            return DataFrame()
        deposit_withdraw_df = DataFrame(deposit_withdraw)
        deposit_withdraw_df = deposit_withdraw_df[
            [DepositWithdrawAttribute.coin.name, DepositWithdrawAttribute.quantity.name,
             DepositWithdrawAttribute.state.name]]

        deposit_withdraw_df = deposit_withdraw_df[
            deposit_withdraw_df[
                DepositWithdrawAttribute.state.name] == DepositWithdrawAuxiliary.deposit_comformed.value]
        deposit_withdraw_df = deposit_withdraw_df.groupby(DepositWithdrawAttribute.coin.name)[
            DepositWithdrawAttribute.quantity.name].sum().to_frame()
        return deposit_withdraw_df

    @log_slow_query
    def read_orders(self, time_span=None, inst_code=None, account_id=None, exchange_id=None, side=None, day=None,
                    strategy_id=None, is_other=False, is_raw=False, sort=False, limit=None, client_order_id=None,
                    order_id=None, is_df=True):
        if not exchange_id:
            exchange_id = InstCode.from_string(inst_code).exchange_id
        params = {}

        if inst_code:
            params[OrderAttribute.inst_code.name] = inst_code

        if order_id:
            if isinstance(order_id, list):
                params[OrderAttribute.order_id.name] = {"$in": order_id}
            else:
                params[OrderAttribute.order_id.name] = order_id

        if account_id:
            if isinstance(account_id, list):
                params[OrderAttribute.account_id.name] = {"$in": account_id}
            else:
                params[OrderAttribute.account_id.name] = account_id

        if side:
            params[OrderAttribute.side.name] = side

        if day:
            params[OrderAttribute.day.name] = day

        if client_order_id:
            params[OrderAttribute.client_order_id.name] = client_order_id

        if strategy_id:
            if isinstance(strategy_id, list):
                params[OrderAttribute.strategy_id.name] = {"$in": strategy_id}
            else:
                params[OrderAttribute.strategy_id.name] = strategy_id

        if time_span:
            params[OrderAttribute.order_time_ms.name] = {
                "$gte": time_span.start,
                "$lte": time_span.end
            }

        collection_name = self.get_orders_collection_name(exchange_id, is_other, is_raw)
        collection = self.client[Database.raw_orders.name][collection_name]
        if limit:
            if sort:
                res = collection.find(params).sort(OrderAttribute.order_time_ms.name, -1).limit(limit)
            else:
                res = collection.find(params).limit(limit)
        else:
            res = collection.find(params)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No orders found for inst_code {inst_code}')
        if is_df is False:
            return res
        order_df = DataFrame(res)
        if inst_code:
            msg = f'Duplicate orders for {inst_code} {AtUser.debug}'
        else:
            msg = f'Duplicate orders for {exchange_id} {AtUser.debug}'
        if exchange_id == ExchangeId.BFX.name:
            duplicates_col = DuplicateFields.order_bfx_raw.value
        else:
            duplicates_col = DuplicateFields.order.value
        order_df = self.log_duplicates(order_df, duplicates_col, msg)
        return order_df

    @log_slow_query
    def read_trades(self, time_span=None, inst_code=None, account_id=None, exchange_id=None, side=None, day=None,
                    strategy_id=None, is_other=False, is_inventory=None, limit=None, client_order_id=None) -> DataFrame:
        if not exchange_id:
            exchange_id = InstCode.from_string(inst_code).exchange_id
        params = {}

        if inst_code:
            params[TradeAttribute.inst_code.name] = inst_code

        if account_id:
            if isinstance(account_id, list):
                params[OrderAttribute.account_id.name] = {"$in": account_id}
            else:
                params[OrderAttribute.account_id.name] = account_id

        if side:
            params[TradeAttribute.side.name] = side

        if day:
            params[TradeAttribute.day.name] = day

        if strategy_id:
            if isinstance(strategy_id, list):
                params[OrderAttribute.strategy_id.name] = {"$in": strategy_id}
            else:
                params[TradeAttribute.strategy_id.name] = strategy_id

        if client_order_id:
            params[TradeAttribute.client_order_id.name] = client_order_id

        if time_span:
            params[TradeAttribute.traded_time_ms.name] = {
                "$gte": time_span.start,
                "$lte": time_span.end
            }

        collection_name = self.get_trades_collection_name(exchange_id=exchange_id, is_other=is_other,
                                                          is_inventory=is_inventory)
        collection = self.client[Database.raw_orders.name][collection_name]
        if limit:
            res = collection.find(params).sort(TradeAttribute.traded_time_ms.name, -1).limit(limit)
        else:
            res = collection.find(params)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No trades found for inst_code {inst_code}')
        trade_df = DataFrame(res)
        if inst_code:
            msg = f'Duplicate trades for {inst_code} {AtUser.debug}'
        else:
            msg = f'Duplicate trades for {exchange_id} {AtUser.debug}'
        trade_df = self.log_duplicates(trade_df, DuplicateFields.trade.value, msg)
        return trade_df

    def read_coll(self, collection_path) -> pd.DataFrame:
        collection = self.client[collection_path.db_name][collection_path.collection_name]
        cursor = collection.find({})
        df = pd.DataFrame(list(cursor))
        len_df = len(df)
        if self.logger:
            self.logger.info(
                f"Fetched {len_df} documents from {collection_path.db_name}.{collection_path.collection_name}.")
        return df

    def read_yesterday_coll(self, collection_path) -> pd.DataFrame:
        db_name = collection_path.db_name
        yesterday = get_yesterday_datetime().strftime(DATETIME_FORMAT_DAY)
        collection_name = f'{yesterday}_{collection_path.collection_name}'
        new_collection_path = CollectionPath(db_name, collection_name)
        return self.read_coll(new_collection_path)

    def read_timeseries(self, collection_path, time_span, columns=OrderAttribute.event_time_ms.name) -> DataFrame:
        collection = self.client[collection_path.db_name][collection_path.collection_name]
        query = {
            columns: {
                "$gte": time_span.start,
                "$lte": time_span.end
            }
        }
        cursor = collection.find(query)
        df = pd.DataFrame(list(cursor))
        len_df = len(df)
        if self.logger:
            self.logger.info(
                f" fetch {len_df} documents from {collection_path.db_name}_{collection_path.collection_name} between {time_span.start} and {time_span.end}.")
        return df

    def read_timeseries_batch(self, logs_list, collection_path, time_span, start_index, batch_size=BATCH_SIZE,
                              columns=OrderAttribute.event_time_ms.name):
        collection = self.client[collection_path.db_name][collection_path.collection_name]
        query = {
            columns: {
                "$gte": time_span.start,
                "$lte": time_span.end
            }
        }

        # cursor = collection.find(query).sort(columns, 1).skip(start_index).limit(batch_size)
        cursor = collection.find(query).skip(start_index).limit(batch_size)
        df = pd.DataFrame(list(cursor))
        logs_list.append(
            f"split read from {collection_path.db_name}_{collection_path.collection_name} between {time_span.start} and {time_span.end}.from num{start_index + 1}to{start_index + batch_size}")
        return df, logs_list

    def read_rank(self, time_span=None, exchange_id=None, inst_code: (list, str) = None, is_df=False, limit=1,
                  hour=None, volume_sort=None):
        params = {}
        if time_span:
            params[RankAttribute.hour.name] = {
                "$gte": time_span.start,
                "$lte": time_span.end
            }
        if hour:
            params[RankAttribute.hour.name] = hour
        if inst_code:
            if isinstance(inst_code, list):
                params[RankAttribute.inst_code.name] = {"$in": inst_code}
            else:
                params[RankAttribute.inst_code.name] = inst_code
        res = self.client[Database.raw_market.name][f'{exchange_id}_{Database.rank.name}'].find(params)
        if volume_sort:
            res = res.sort(RankAttribute.volume_24h.name, -1)
        elif not time_span:
            res = res.sort(RankAttribute.hour.name, -1).limit(limit)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No rank data for {time_span.start} to {time_span.end}')
        if is_df:
            return DataFrame(res)
        else:
            return res

    def read_depth(self, time_span=None, inst_code=None, limit=1, is_df=False):
        params = {}
        if not time_span and not inst_code:
            return list(
                self.client[Database.raw_metrics.name][Database.depth.name].find().sort(DepthAttribute.hour.name,
                                                                                        -1).limit(limit))
        params[DepthAttribute.hour.name] = {
            "$gte": time_span.start,
            "$lte": time_span.end
        }
        if inst_code:
            params[DepthAttribute.inst_code.name] = inst_code
        res = self.client[Database.raw_metrics.name][Database.depth.name].find(params)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No depth data for {time_span.start} to {time_span.end}')
        if is_df:
            return DataFrame(res)
        else:
            return res

    def read_pnl(self, day: (str, list) = None, inst_code: (str, list) = None, strategy_id=None, is_df=False):
        params = {}
        if day:
            if isinstance(day, list):
                if len(day) == 2:
                    params[PnlAttribute.day.name] = {"$gte": day[0], "$lte": day[1]}
                else:
                    raise ValueError(
                        f"day 列表需要正好包含两个元素(开始日, 结束日)，实际传入: {day}"
                    )
            else:
                params[PnlAttribute.day.name] = day
        if inst_code:
            if isinstance(inst_code, list):
                params[PnlAttribute.inst_code.name] = {"$in": inst_code}
            else:
                params[PnlAttribute.inst_code.name] = inst_code
        if strategy_id:
            if isinstance(strategy_id, list):
                params[PnlAttribute.strategy_id.name] = {"$in": strategy_id}
            else:
                params[PnlAttribute.strategy_id.name] = strategy_id
        res = self.client[Database.raw_metrics.name][Database.pnl.name].find(params)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No pnl data for {day}')
        if is_df:
            return DataFrame(res)
        else:
            return res

    def read_volume_fee(self, day: (str, list) = None, inst_code: (str, list) = None, strategy_id=None, is_df=False):
        params = {}
        if day:
            if isinstance(day, list):
                if len(day) == 2:
                    params[VolumeFeeAttribute.day.name] = {"$gte": day[0], "$lte": day[1]}
                else:
                    raise ValueError(
                        f"day 列表需要正好包含两个元素(开始日, 结束日)，实际传入: {day}"
                    )
            else:
                params[VolumeFeeAttribute.day.name] = day
        if inst_code:
            if isinstance(inst_code, list):
                params[VolumeFeeAttribute.inst_code.name] = {"$in": inst_code}
            else:
                params[VolumeFeeAttribute.inst_code.name] = inst_code
        if strategy_id:
            if isinstance(strategy_id, list):
                params[VolumeFeeAttribute.strategy_id.name] = {"$in": strategy_id}
            else:
                params[VolumeFeeAttribute.strategy_id.name] = strategy_id
        res = self.client[Database.raw_metrics.name][Database.volume_fee.name].find(params)
        res = list(res)
        if len(res) == 0:
            raise NoDataException(f'No volume fee data for {day}''')
        if is_df:
            return DataFrame(res)
        else:
            return res

    @handle_mongodb_errors()
    def read_inventory(self, back_hours=None, start_time=None, end_time=None, inst_code=None, limit=None) -> DataFrame:
        params = {}
        if inst_code:
            params[InventoryAttribute.inst_code.name] = inst_code
        if back_hours:
            start_time = get_rounded_time_interval(back_hours=back_hours).start
            params[InventoryAttribute.trade_update_time_ms.name] = {
                "$gte": start_time,
            }
        if start_time and end_time:
            params[InventoryAttribute.trade_update_time_ms.name] = {
                "$gte": start_time,
                "$lte": end_time
            }
        if limit:
            res = self.client[Database.inventory_management.name][Database.inventory.name].find(params).sort(
                InventoryAttribute.trade_update_time_ms.name, -1).limit(1)
            inventory = list(res)
        else:
            res = self.client[Database.inventory_management.name][Database.inventory.name].find(params)
            inventory = DataFrame(list(res))
        if len(inventory) == 0:
            raise NoDataException(f'No invenory data found for')
        return inventory

    @handle_mongodb_errors()
    def read_inventory_quantity(self):
        params = [
            {
                "$sort": {"event_time_ms": -1}
            },
            {
                "$group": {
                    "_id": "$inst_code",
                    "data": {"$first": "$$ROOT"}
                }
            },
            {
                "$replaceRoot": {"newRoot": "$data"}
            }
        ]

        inventory_quantity_list = list(
            self.client[Database.inventory_management.name][Database.inventory_quantity.name].aggregate(params))
        inventory_quantity_dict = {item['inst_code']: item for item in inventory_quantity_list}
        return inventory_quantity_dict

    @handle_mongodb_errors()
    def read_first_and_last_inventory(self, inst_code, day):
        params = [
            {
                "$match": {
                    "inst_code": inst_code,
                    "trade_update_time_ms": {
                        "$gte": f"{day} 00:00:00.000",
                        "$lte": f"{day} 23:59:59.999"
                    }
                }
            },
            {
                "$sort": {"trade_update_time_ms": 1}
            },
            {
                "$group": {
                    "_id": None,
                    "first": {"$first": "$$ROOT"},
                    "last": {"$last": "$$ROOT"}
                }
            }
        ]

        result = list(self.client[Database.inventory_management.name][Database.inventory.name].aggregate(params))
        if result:
            return result[0]['first'], result[0]['last']
        else:
            raise NoDataException(f'first_and_last_inventory {inst_code} in {day}')

    @handle_mongodb_errors()
    def read_inventory_unrealized_profit_close_point(self):
        params = [
            {
                "$sort": {"event_time_ms": -1}
            },
            {
                "$group": {
                    "_id": "$inst_code",
                    "data": {"$first": "$$ROOT"}
                }
            },
            {
                "$replaceRoot": {"newRoot": "$data"}
            }
        ]

        inventory_unrealized_profit_close_point_list = list(
            self.client[Database.inventory_management.name][Database.unrealized_profit_close_point.name].aggregate(
                params))
        inventory_unrealized_profit_close_point_dict = {item['inst_code']: item for item in
                                                        inventory_unrealized_profit_close_point_list}
        return inventory_unrealized_profit_close_point_dict

    def read_max_inventory(self, day, strategy_id=None, is_df=True):
        params = {}
        if day:
            params[MaxInventoryAttribute.day.name] = day
        if strategy_id:
            if isinstance(strategy_id, list):
                params[MaxInventoryAttribute.strategy_id.name] = {"$in": strategy_id}
            else:
                params[MaxInventoryAttribute.strategy_id.name] = strategy_id
        res = self.client[Database.inventory_management.name][Database.max_inventory.name].find(params)
        if is_df:
            max_inventory = DataFrame(list(res))
        else:
            max_inventory = list(res)
        if len(max_inventory) == 0:
            raise NoDataException(f'No max inventory data found for')
        return max_inventory

    def read_database_name(self):
        return self.client.list_database_names()

    def read_collection_name(self, db_name):
        return self.client[db_name].list_collection_names()

    def read_database_memory_mb(self, db_name):
        return int(self.client[db_name].command("dbStats").get("dataSize", 0) / (1024 * 1024))

    def read_collection_memory_mb(self, db_name, coll_name):
        return int(self.client[db_name].command("collStats", coll_name).get("size", 0) / (1024 * 1024))

    def get_data_count(self, collection_path, time_span=None):
        collection = self.client[collection_path.db_name][collection_path.collection_name]
        query = {}
        if time_span:
            query[OrderAttribute.event_time_ms.name] = {"$gte": time_span.start, "$lte": time_span.end}
        count = collection.count_documents(query)
        return count

    def update_last_agg_trade_time(self, inst_code=None, update_data=None):
        if update_data is None:
            update_data = {}
        params = {}
        if inst_code:
            params[LastAggtradeAttribute.inst_code.name] = inst_code
        update = {'$set': update_data}
        self.client[Database.mvid_official.name][Database.last_agg_trade_time.name].update_one(params, update)

    def update_inst_code_basic(self, inst_code=None, exchange_id=None, update_data=None):
        if update_data is None:
            update_data = {}
        if not exchange_id:
            exchange_id = InstCode.from_string(inst_code).exchange_id
        params = {}
        if inst_code:
            params[InstcodeBasicAttribute.inst_code.name] = inst_code
        update = {'$set': update_data}
        self.client[Database.raw_market.name][f'{exchange_id}_{Database.inst_code_basic.name}'].update_one(params,
                                                                                                           update)

    def insert_arbitrage_pools_report(self, report):
        """
        存储套利交易池报告
        
        Args:
            report: ArbitragePoolsReport 实例
        """
        collection_path = CollectionPath(
            db_name=Database.cross_exchange_arbitrage.name,
            collection_name="arbitrage_pools_report"
        )
        
        # 转换为字典，确保 report 字段中的 ArbitragePool 对象也被转换
        document = report.to_dict()
        
        # 生成 _id，保证同日同时间窗口唯一
        document_id = f"{report.day}_{report.open_time}_{report.close_time}"
        document["_id"] = document_id
        
        self.insert_data(document, collection_path)
        if self.logger:
            self.logger.info(f"Inserted arbitrage pools report: {document_id}")

    def read_arbitrage_pools_reports(self, day: str, latest: bool = True):
        """
        读取套利交易池报告
        
        Args:
            day: 报告日期 YYYY-MM-DD
            latest: 是否只返回最新的报告
        
        Returns:
            dict 或 list[dict]: 报告数据
        """
        collection_path = CollectionPath(
            db_name=Database.cross_exchange_arbitrage.name,
            collection_name=Database.arbitrage_pools_report.name
        )
        
        collection = self.client[collection_path.db_name][collection_path.collection_name]
        
        query = {ArbitragePoolsReportAttribute.day.name: day}
        cursor = collection.find(query).sort(ArbitragePoolsReportAttribute.event_time_ms.name, -1)
        
        if latest:
            cursor = cursor.limit(1)
        
        results = []
        for doc in cursor:
            # 移除 MongoDB 的 _id 字段，避免序列化问题
            doc.pop("_id", None)
            results.append(doc)
        
        if latest and results:
            return results[0]
        return results
