"""Named historical GET pagination preserves legacy endpoints and wire params."""
from unittest.mock import Mock
import pytest
from pytradekit.restful.binance_restful import BinanceClient
from pytradekit.restful.okex_restful import OkexClient
from pytradekit.utils.mongodb_operations import MongodbOperations, CollectionPath


def bn():
    client = object.__new__(BinanceClient)
    client._make_private_url = Mock(side_effect=lambda url_path, params: (url_path, params, 0))
    client.request = Mock(return_value=[])
    return client


def test_binance_capital_and_transfer_named_get_params():
    client = bn()
    client.get_deposit_history(start_time=1, end_time=9, offset=1000, limit=1000)
    assert client.request.call_args.args[0] == 'GET'
    assert client.request.call_args.kwargs['params'] == {'startTime': 1, 'endTime': 9, 'offset': 1000, 'limit': 1000}
    client.get_withdraw_history()
    assert client.request.call_args.kwargs['params'] == {'limit': 10}
    client.get_wallet_universal_transfer_history('MAIN_UMFUTURE', current=2, size=100, start_time=1, end_time=9)
    assert client.request.call_args.args[1] == '/sapi/v1/asset/transfer'
    assert client.request.call_args.kwargs['params']['current'] == 2
    client.get_subaccount_transfer_history(2, start_time=1, end_time=9, limit=100)
    assert client.request.call_args.kwargs['params']['type'] == 2
    with pytest.raises(ValueError): client.get_subaccount_transfer_history(0)
    client.request.side_effect = PermissionError('denied')
    with pytest.raises(PermissionError): client.get_deposit_history()


def test_binance_income_and_execution_page_parameters():
    client = bn()
    client.get_perp_income(start_time=1, end_time=9, page=2, limit=1000)
    assert client.request.call_args.kwargs['params']['page'] == 2
    client.get_perp_user_trades('TESTUSDT', order_id='3', from_id=4, limit=1000)
    assert client.request.call_args.kwargs['params']['fromId'] == 4


def test_okx_bill_id_cursor_and_historical_fills_are_named_gets():
    client = object.__new__(OkexClient)
    client._send_request = Mock(return_value={'code': '0', 'data': []})
    client.get_funding_bills_history(after='123', limit=100)
    call = client._send_request.call_args
    assert call.args[0] == '/api/v5/asset/bills-history'
    assert call.kwargs['params'] == {'pagingType': '2', 'limit': 100, 'after': '123'}
    client.get_trading_bills_archive(begin=1, end=9, after='123')
    assert client._send_request.call_args.args[0] == '/api/v5/account/bills-archive'
    client.get_fills_history(instType='SPOT', instId='TEST-USDT', ordId='42', after='123')
    assert client._send_request.call_args.args[0] == '/api/v5/trade/fills-history'
    client.get_deposit_history(after=9, before=1, limit=100)
    assert client._send_request.call_args.kwargs['params'] == {'after': 9, 'before': 1, 'limit': 100}


def test_atomic_document_insertion_replays_identical_content_and_rejects_conflict():
    client = object.__new__(MongodbOperations)
    collection = Mock()
    client.client = {'arbitrage': {'account_cashflow_batches': collection}}
    collection.update_one.return_value.upserted_id = None
    collection.find_one.return_value = {'_id': 'id', 'value': '1'}
    path = CollectionPath('arbitrage', 'account_cashflow_batches')
    assert client.insert_document_once(path, 'id', {'value': '1'}) == 'unchanged'
    assert collection.update_one.call_args.kwargs == {'upsert': True}
    with pytest.raises(ValueError): client.insert_document_once(path, 'id', {'value': '2'})


def test_fair_candidate_query_orders_oldest_attempt_before_identity():
    mongo = object.__new__(MongodbOperations)
    cursor = Mock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = [{'_id': 'old-open'}, {'_id': 'new'}]
    collection = Mock()
    collection.find.return_value = cursor
    mongo.client = {'arbitrage': {'trade_records': collection}}
    result = mongo.read_accounting_candidates(123, limit=1, order='attempt')
    cursor.sort.assert_called_once_with([('accounting.last_attempt_ms', 1), ('_id', 1)])
    cursor.limit.assert_called_once_with(2)
    assert result == {'records': [{'_id': 'old-open'}], 'truncated': True}
    assert {'status': {'$ne': 'closed'}} in collection.find.call_args.args[0]['$or']


def test_retry_query_retires_complete_revisions_before_selecting_oldest_partial():
    mongo = object.__new__(MongodbOperations)
    collection = Mock()
    collection.aggregate.return_value = []
    mongo.client = {'arbitrage': {'account_cashflow_batches': collection}}
    assert mongo.read_cashflow_retry_windows('scope', 123, limit=2) == []
    pipeline = collection.aggregate.call_args.args[0]
    assert pipeline[0] == {'$match': {'scope_id': 'scope', 'window_end_ms': {'$gte': 123}}}
    assert pipeline[1] == {'$sort': {'collected_ms': -1, '_id': -1}}
    assert pipeline[4] == {'$match': {'status': 'partial'}}
    assert pipeline[-2:] == [{'$sort': {'collected_ms': 1, '_id': 1}}, {'$limit': 2}]
