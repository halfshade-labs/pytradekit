"""The monitor's latest-account and closed-window reads need matching indexes."""
from unittest.mock import Mock

import pytest

from pytradekit.utils.dynamic_types import ExchangeId
from pytradekit.utils.mongodb_operations import MongodbOperations


def test_monitor_plan_covers_account_sort_run_and_closed_window():
    specs = MongodbOperations.monitoring_index_specs((ExchangeId.BN, ExchangeId.OKX))
    keys = {(item['collection'], tuple(item['keys'])) for item in specs}
    for collection in ('perp_position', 'BN_balance', 'OKX_balance'):
        assert (collection, (('account_id', 1), ('event_time_ms', -1), ('_id', -1))) in keys
        assert (collection, (('event_time_ms', -1), ('_id', -1))) in keys
    assert ('perp_position', (('account_id', 1), ('snapshot_run_id', 1))) in keys
    assert ('trade_records', (('status', 1), ('closed_time_ms', -1))) in keys
    assert len(specs) == 8
    assert not any(item['collection'] == 'HTX_balance' for item in specs)


def test_monitor_index_apply_does_not_drop_or_make_indexes_unique():
    operations = object.__new__(MongodbOperations)
    collections = {}
    specs = operations.monitoring_index_specs((ExchangeId.BN,))
    for spec in specs:
        collections.setdefault(spec['database'], {}).setdefault(spec['collection'], Mock())
    operations.client = collections
    applied = operations.ensure_monitoring_indexes((ExchangeId.BN,))
    assert len(applied) == len(specs)
    for database in collections.values():
        for collection in database.values():
            collection.drop_indexes.assert_not_called()
            for request in collection.create_index.call_args_list:
                assert request.kwargs.get('unique', False) is False
                assert request.kwargs['name'].startswith('idx_monitor_')


def test_index_failure_is_visible_to_the_migration_caller():
    operations = object.__new__(MongodbOperations)
    collection = Mock()
    collection.create_index.side_effect = RuntimeError('index creation failed')
    operations.client = {'raw_accounts': {'perp_position': collection}}
    with pytest.raises(RuntimeError, match='index creation failed'):
        operations.ensure_monitoring_indexes(())


@pytest.mark.parametrize('venues', [('BN',), (object(),)])
def test_index_plan_requires_typed_exchange_ids(venues):
    with pytest.raises(ValueError, match='ExchangeId'):
        MongodbOperations.monitoring_index_specs(venues)
