from decimal import Decimal
from unittest.mock import Mock

import pytest

from pytradekit.utils.mongodb_operations import MongodbOperations


@pytest.mark.parametrize('matched', [0, 1])
def test_compare_and_set_uses_snapshot_and_never_upserts(matched):
    mongo = MongodbOperations.__new__(MongodbOperations)
    collection = Mock()
    collection.update_one.return_value.matched_count = matched
    mongo.client = {'arbitrage': {'trade_records': collection}}
    before = {'_id': 'trade', 'trade_id': 'trade', 'status': 'closed', 'legs': {}}
    result = mongo.update_trade_record_if_unchanged(before, {'pnl': Decimal('1.2')})
    collection.update_one.assert_called_once_with(before, {'$set': {'pnl': '1.2'}}, upsert=False)
    assert result is bool(matched)


def test_compare_and_set_refuses_identity_change():
    mongo = MongodbOperations.__new__(MongodbOperations)
    with pytest.raises(ValueError):
        mongo.update_trade_record_if_unchanged({'trade_id': 'trade'}, {})
    with pytest.raises(ValueError):
        mongo.update_trade_record_if_unchanged({'_id': 'trade', 'trade_id': 'trade'}, {'trade_id': 'other'})
