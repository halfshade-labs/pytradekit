from decimal import Decimal

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
