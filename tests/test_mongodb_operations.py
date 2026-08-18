from decimal import Decimal

import pytest

from pytradekit.utils.custom_types import InstCode
from pytradekit.utils.mongodb_operations import MongodbOperations


MONGODB_URL = "mongodb://username:password@localhost:27017"


# 测试MongoDB客户端的创建
def test_create_client(mocker):
    # Reset singleton so _create_client is actually invoked
    MongodbOperations._client = None
    MongodbOperations._indexes_ensured = False
    # Mock _ensure_indexes to avoid requiring a live MongoDB connection
    mocker.patch.object(MongodbOperations, '_ensure_indexes')
    spy = mocker.spy(MongodbOperations, '_create_client')
    MongodbOperations(MONGODB_URL)
    # 现在使用spy对象来断言_create_client是否被正确调用
    spy.assert_called_once_with(MONGODB_URL)


# 测试关闭MongoDB连接
def test_close(mocker):
    # Reset singleton state so this test is isolated from prior runs.
    MongodbOperations._client = None
    MongodbOperations._indexes_ensured = False
    mocker.patch('pytradekit.utils.mongodb_operations.MongoClient', return_value=mocker.MagicMock())
    mocker.patch.object(MongodbOperations, '_ensure_indexes')
    mongodb_operations = MongodbOperations(MONGODB_URL)
    mongodb_operations.close()
    assert MongodbOperations._client is None
    # #133: the index-ensured flag is bound to the connection lifecycle and must
    # reset on close so a reconnect re-ensures indexes.
    assert MongodbOperations._indexes_ensured is False


def test_reconnect_after_close_reensures_indexes(mocker):
    # #132/#133: after close(), a new instance must build a fresh client AND
    # re-ensure indexes (the flag no longer sticks from the old connection).
    MongodbOperations._client = None
    MongodbOperations._indexes_ensured = False
    mocker.patch('pytradekit.utils.mongodb_operations.MongoClient', return_value=mocker.MagicMock())
    ensure = mocker.patch.object(MongodbOperations, '_ensure_indexes')

    ops = MongodbOperations(MONGODB_URL)
    assert ensure.call_count == 1

    ops.close()
    MongodbOperations(MONGODB_URL)
    assert ensure.call_count == 2


def test_check_connection(mocker):
    # 重置 singleton 状态，使 mocker.patch 的 MongoClient 一定被注入
    # （如果 _client 已被前序测试初始化过，__init__ 会跳过 _create_client，
    # mocked_mongo_client 永远不会成为 self.client，断言会因执行顺序失败）
    MongodbOperations._client = None
    MongodbOperations._indexes_ensured = False

    # 创建MongoClient的模拟实例
    mocked_mongo_client = mocker.Mock()
    # 创建admin属性的模拟对象
    mocked_admin = mocker.Mock()
    # 将mocked_admin设置为mocked_mongo_client的admin属性的返回值
    mocked_mongo_client.admin = mocked_admin
    # 设置admin.command方法的返回值为True
    mocked_admin.command.return_value = True

    # 使用patch替换MongoClient，使其返回模拟的MongoClient实例
    mocker.patch('pytradekit.utils.mongodb_operations.MongoClient', return_value=mocked_mongo_client)
    # _ensure_indexes 在 __init__ 中会被调用，mock 掉避免访问 mock client 的索引方法链
    mocker.patch.object(MongodbOperations, '_ensure_indexes')

    mongodb_operations_instance = MongodbOperations(MONGODB_URL)
    # 调用待测试的方法
    connection_status = mongodb_operations_instance._check_connection()

    # 验证admin.command方法是否被正确调用
    mocked_admin.command.assert_called_once_with('ping')
    # 验证返回值是否为True，即连接正常
    assert connection_status is True


class TestUpdateTradeRecordStripsDecimal:
    """update_trade_record was previously skipping get_correct_dict; raw Decimal in the
    payload caused pymongo to raise `cannot encode object: Decimal(...)` and the update
    failed silently. This guards the symmetric conversion with insert_data."""

    def _make_ops(self, mocker):
        MongodbOperations._client = None
        MongodbOperations._indexes_ensured = False
        mocked_client = mocker.MagicMock()
        mocker.patch('pytradekit.utils.mongodb_operations.MongoClient', return_value=mocked_client)
        mocker.patch.object(MongodbOperations, '_ensure_indexes')
        ops = MongodbOperations("mongodb://x:y@localhost:27017")
        return ops, mocked_client

    def test_decimal_fields_converted_to_str(self, mocker):
        ops, mocked_client = self._make_ops(mocker)
        update_data = {
            'legs': {
                'SHORT_LEG': {'fee': Decimal('0.00057500'), 'entry_price': Decimal('523.17')},
            },
            'status': 'open',
        }
        ops.update_trade_record('trade_xyz', update_data)

        collection = mocked_client['arbitrage']['trade_records']
        collection.update_one.assert_called_once()
        sent = collection.update_one.call_args[0]
        sent_filter, sent_update = sent[0], sent[1]
        assert sent_filter == {'trade_id': 'trade_xyz'}
        legs = sent_update['$set']['legs']['SHORT_LEG']
        assert legs['fee'] == '0.00057500'
        assert legs['entry_price'] == '523.17'
        # Non-decimal values pass through unchanged
        assert sent_update['$set']['status'] == 'open'

    def test_decimal_inside_list_converted(self, mocker):
        """get_correct_dict previously only recursed into dicts; Decimal values nested
        inside lists (e.g. ArbitragePoolsReport.report = list[pool dict]) reached
        pymongo unconverted and raised `cannot encode object: Decimal(...)`."""
        ops, mocked_client = self._make_ops(mocker)
        report_document = {
            'day': '2026-07-20',
            'report': [
                {
                    'coin': 'BTC',
                    'short_leg': {
                        'inst_code': str(InstCode.from_string('BTC-USDT_BN.PERP')),
                        'price': Decimal('64609.3'),
                    },
                    'long_legs': [
                        {
                            'inst_code': str(InstCode.from_string('BTC-USDT_OKX.SPOT')),
                            'price': Decimal('64605.1'),
                        },
                    ],
                },
            ],
        }
        converted = ops.get_correct_dict(report_document)
        pool = converted['report'][0]
        assert pool['short_leg']['price'] == '64609.3'
        assert pool['long_legs'][0]['price'] == '64605.1'
        assert converted['day'] == '2026-07-20'

    def test_tuple_converted_to_list_with_decimals(self, mocker):
        ops, _ = self._make_ops(mocker)
        converted = ops.get_correct_dict({'prices': (Decimal('1.5'), Decimal('2.5'))})
        assert converted['prices'] == ['1.5', '2.5']

    def test_nested_decimal_in_long_leg_converted(self, mocker):
        ops, mocked_client = self._make_ops(mocker)
        # inst_code 通过 InstCode 类构造而非裸字符串，遵守 CLAUDE.md
        # 「交易对标识必须使用 InstCode 类和转换方法」，同时验证字符串格式
        # 可被 InstCode.from_string 解析。
        update_data = {
            'legs': {
                'LONG_LEG': {
                    'inst_code': str(InstCode.from_string('ZEC-USDT_BN.SPOT')),
                    'position_size': Decimal('0.57500000'),
                },
            },
        }
        ops.update_trade_record('perp_sell_xxx', update_data)
        sent_update = mocked_client['arbitrage']['trade_records'].update_one.call_args[0][1]
        assert sent_update['$set']['legs']['LONG_LEG']['position_size'] == '0.57500000'


class TestInsertDepositWithdraw:
    def _make_ops(self, mocker):
        MongodbOperations._client = None
        MongodbOperations._indexes_ensured = False
        MongodbOperations._deposit_withdraw_indexes_ensured.clear()
        mocked_client = mocker.MagicMock()
        mocker.patch('pytradekit.utils.mongodb_operations.MongoClient', return_value=mocked_client)
        mocker.patch.object(MongodbOperations, '_ensure_indexes')
        return MongodbOperations(MONGODB_URL), mocked_client

    @staticmethod
    def _record(account_id, transaction_id, quantity='1.25'):
        return {
            'account_id': account_id,
            'id': transaction_id,
            'quantity': Decimal(quantity),
        }

    def test_uses_atomic_upserts_and_returns_only_inserted_records(self, mocker):
        ops, client = self._make_ops(mocker)
        collection = client['raw_accounts']['BN_deposit_withdraw']
        collection.bulk_write.return_value.upserted_ids = {1: 'inserted-id'}
        records = [
            self._record('BN_000', 'transaction-1'),
            self._record('BN_000', 'transaction-2'),
        ]

        inserted = ops.insert_deposit_withdraw(records, 'BN')

        collection.create_index.assert_called_once_with(
            [('account_id', 1), ('id', 1)],
            unique=True,
            name='idx_account_transaction',
            background=True,
        )
        operations = collection.bulk_write.call_args.args[0]
        assert collection.bulk_write.call_args.kwargs == {'ordered': False}
        assert [operation._filter for operation in operations] == [
            {'account_id': 'BN_000', 'id': 'transaction-1'},
            {'account_id': 'BN_000', 'id': 'transaction-2'},
        ]
        assert all(operation._upsert for operation in operations)
        assert operations[0]._doc == {
            '$setOnInsert': {
                'account_id': 'BN_000',
                'id': 'transaction-1',
                'quantity': '1.25',
            },
        }
        assert inserted == [records[1]]

    def test_deduplicates_within_batch_by_account_and_transaction(self, mocker):
        ops, client = self._make_ops(mocker)
        collection = client['raw_accounts']['BN_deposit_withdraw']
        collection.bulk_write.return_value.upserted_ids = {0: 'inserted-id'}
        records = [
            self._record('BN_000', 'shared-id', '1'),
            self._record('BN_000', 'shared-id', '2'),
            self._record('BN_001', 'shared-id', '3'),
        ]

        inserted = ops.insert_deposit_withdraw(records, 'BN')

        operations = collection.bulk_write.call_args.args[0]
        assert len(operations) == 2
        assert [operation._filter for operation in operations] == [
            {'account_id': 'BN_000', 'id': 'shared-id'},
            {'account_id': 'BN_001', 'id': 'shared-id'},
        ]
        assert operations[0]._doc['$setOnInsert']['quantity'] == '2'
        assert inserted == [records[1]]

    def test_returns_false_when_every_record_already_exists(self, mocker):
        ops, client = self._make_ops(mocker)
        collection = client['raw_accounts']['OKX_deposit_withdraw']
        collection.bulk_write.return_value.upserted_ids = {}

        result = ops.insert_deposit_withdraw(
            [self._record('OKX_000', 'transaction-1')],
            'OKX',
        )

        assert result is False

    def test_creates_each_collection_index_once_per_client(self, mocker):
        ops, client = self._make_ops(mocker)
        collection = client['raw_accounts']['BN_deposit_withdraw']
        collection.bulk_write.return_value.upserted_ids = {}
        records = [self._record('BN_000', 'transaction-1')]

        ops.insert_deposit_withdraw(records, 'BN')
        ops.insert_deposit_withdraw(records, 'BN')

        collection.create_index.assert_called_once()


class TestDeleteInstCodeBasicGuard:
    """#110: a falsy inst_code made query == {}, so delete_many wiped the entire
    {exchange_id}_inst_code_basic collection. The guard must refuse and never
    issue a delete_many({})."""

    def _make_ops(self, mocker):
        MongodbOperations._client = None
        MongodbOperations._indexes_ensured = False
        mocked_client = mocker.MagicMock()
        mocker.patch('pytradekit.utils.mongodb_operations.MongoClient', return_value=mocked_client)
        mocker.patch.object(MongodbOperations, '_ensure_indexes')
        ops = MongodbOperations("mongodb://x:y@localhost:27017")
        return ops, mocked_client

    def test_empty_inst_code_refuses_delete(self, mocker):
        ops, client = self._make_ops(mocker)
        collection = client['raw_market']['BN_inst_code_basic']
        result = ops.delete_inst_code_basic('BN', inst_code=None)
        assert result is None
        collection.delete_many.assert_not_called()

    def test_present_inst_code_deletes_scoped(self, mocker):
        ops, client = self._make_ops(mocker)
        collection = client['raw_market']['BN_inst_code_basic']
        ops.delete_inst_code_basic('BN', inst_code='BTC-USDT_BN.SPOT')
        collection.delete_many.assert_called_once_with({'inst_code': 'BTC-USDT_BN.SPOT'})


class TestGetBalanceTimeSpanNoData:
    """#109: empty read_balance result raised an uncontrolled IndexError on res[0];
    it must raise NoDataException like the rest of the module's empty-result paths."""

    def _make_ops(self, mocker):
        MongodbOperations._client = None
        MongodbOperations._indexes_ensured = False
        mocked_client = mocker.MagicMock()
        mocker.patch('pytradekit.utils.mongodb_operations.MongoClient', return_value=mocked_client)
        mocker.patch.object(MongodbOperations, '_ensure_indexes')
        ops = MongodbOperations("mongodb://x:y@localhost:27017")
        return ops

    def test_empty_result_raises_no_data_exception(self, mocker):
        from pytradekit.utils.exceptions import NoDataException
        from pytradekit.utils.time_handler import TimeSpan
        ops = self._make_ops(mocker)
        mocker.patch.object(ops, 'read_balance', return_value=[])
        with pytest.raises(NoDataException):
            ops.get_balance_time_span('BN_000', TimeSpan(start=0, end=1))
