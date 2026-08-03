from dataclasses import dataclass, field
from decimal import Decimal
from typing import Tuple

import pytest
from pytradekit.utils.redis_operations import RedisOperations
from pytradekit.utils.exceptions import DataTypeException, DependencyException


@pytest.fixture
def redis_ops(mocker):
    mock_client = mocker.MagicMock()
    mocker.patch('redis.StrictRedis.from_url', return_value=mock_client)
    mock_logger = mocker.MagicMock()
    ops = RedisOperations(mock_logger, 'redis://localhost:6379')
    return ops, mock_client, mock_logger


@dataclass
class FailureSpec:
    """Spec for injecting a client-level failure into an ops method."""
    client_attr: str
    op_name: str
    op_args: Tuple = field(default_factory=tuple)


def assert_wraps_as_dependency_exception(redis_ops, spec: FailureSpec):
    """Assert ops.<op_name>(*op_args) wraps a client error as DependencyException."""
    ops, client, logger = redis_ops
    original = Exception("boom")
    getattr(client, spec.client_attr).side_effect = original
    with pytest.raises(DependencyException) as exc_info:
        getattr(ops, spec.op_name)(*spec.op_args)
    assert exc_info.value.__cause__ is original
    logger.debug.assert_called_once()
    # exc_info=True so the traceback still lands in logs even though level is debug
    _, kwargs = logger.debug.call_args
    assert kwargs.get('exc_info') is True


class TestPing:
    def test_success_delegates_to_client(self, redis_ops):
        ops, client, _ = redis_ops
        ops.ping()
        client.ping.assert_called_once()

    def test_failure_wraps_as_dependency_exception(self, redis_ops):
        assert_wraps_as_dependency_exception(
            redis_ops, FailureSpec(client_attr="ping", op_name="ping")
        )


class TestCreatePubsub:
    def test_returns_client_pubsub(self, redis_ops):
        ops, client, _ = redis_ops
        result = ops.create_pubsub()
        assert result is client.pubsub.return_value

    def test_failure_wraps_as_dependency_exception(self, redis_ops):
        assert_wraps_as_dependency_exception(
            redis_ops, FailureSpec(client_attr="pubsub", op_name="create_pubsub")
        )


class TestClose:
    def test_success_delegates_to_client(self, redis_ops):
        ops, client, _ = redis_ops
        ops.close()
        client.close.assert_called_once()

    def test_failure_wraps_as_dependency_exception(self, redis_ops):
        assert_wraps_as_dependency_exception(
            redis_ops, FailureSpec(client_attr="close", op_name="close")
        )


class TestGetTargetPremium:
    def test_returns_decimal_from_str_value(self, redis_ops):
        ops, client, _ = redis_ops
        client.get.return_value = "0.0123"
        assert ops.get_target_premium("perp_sell_x") == Decimal("0.0123")
        client.get.assert_called_once_with("premium:perp_sell_x")

    def test_returns_none_when_key_missing(self, redis_ops):
        ops, client, _ = redis_ops
        client.get.return_value = None
        assert ops.get_target_premium("perp_sell_x") is None
        client.get.assert_called_once_with("premium:perp_sell_x")

    def test_invalid_decimal_string_raises_data_type_exception(self, redis_ops):
        ops, client, _ = redis_ops
        client.get.return_value = "not-a-number"
        with pytest.raises(DataTypeException):
            ops.get_target_premium("perp_sell_x")

    def test_empty_string_raises_data_type_exception(self, redis_ops):
        ops, client, _ = redis_ops
        client.get.return_value = ""
        with pytest.raises(DataTypeException):
            ops.get_target_premium("perp_sell_x")

    def test_failure_wraps_as_dependency_exception(self, redis_ops):
        assert_wraps_as_dependency_exception(
            redis_ops,
            FailureSpec(client_attr="get", op_name="get_target_premium", op_args=("perp_sell_x",)),
        )


class TestSetPortfolios:
    """CEA#472: the stored key must accumulate symbols (merge), publish only the
    delta, and carry a TTL so a quiet market cannot serve an eternal snapshot."""

    def test_merges_into_existing_snapshot(self, redis_ops):
        import json
        from pytradekit.utils.redis_operations import PORTFOLIOS_EXPIRE_TIME
        ops, client, _ = redis_ops
        client.get.return_value = json.dumps({"BTCUSDT": {"short": {"ask": "1"}}})

        ops.set_portfolios({"REUSDT": {"short": {"ask": "2"}}})

        stored = json.loads(client.set.call_args.args[1])
        assert set(stored) == {"BTCUSDT", "REUSDT"}
        published = json.loads(client.publish.call_args.args[1])
        assert set(published) == {"REUSDT"}
        client.expire.assert_called_once()
        assert client.expire.call_args.args[1] == PORTFOLIOS_EXPIRE_TIME

    def test_new_value_overwrites_same_symbol(self, redis_ops):
        import json
        ops, client, _ = redis_ops
        client.get.return_value = json.dumps({"REUSDT": {"short": {"ask": "1"}}})

        ops.set_portfolios({"REUSDT": {"short": {"ask": "9"}}})

        stored = json.loads(client.set.call_args.args[1])
        assert stored["REUSDT"]["short"]["ask"] == "9"

    def test_corrupt_or_missing_existing_starts_fresh(self, redis_ops):
        import json
        ops, client, _ = redis_ops
        client.get.return_value = "not-json"

        ops.set_portfolios({"REUSDT": {"short": {"ask": "2"}}})

        stored = json.loads(client.set.call_args.args[1])
        assert set(stored) == {"REUSDT"}


class TestArbitrageThreshold:
    def test_set_writes_value_and_expiry(self, redis_ops):
        ops, client, _ = redis_ops
        ops.set_arbitrage_threshold(Decimal("0.0042"))
        client.set.assert_called_once_with("arbitrage_threshold", "0.0042")
        client.expire.assert_called_once()
        assert client.expire.call_args[0][0] == "arbitrage_threshold"

    def test_get_returns_decimal_from_str_value(self, redis_ops):
        ops, client, _ = redis_ops
        client.get.return_value = "0.0042"
        assert ops.get_arbitrage_threshold() == Decimal("0.0042")
        client.get.assert_called_once_with("arbitrage_threshold")

    def test_get_returns_none_when_key_missing(self, redis_ops):
        ops, client, _ = redis_ops
        client.get.return_value = None
        assert ops.get_arbitrage_threshold() is None

    def test_get_invalid_decimal_string_raises_data_type_exception(self, redis_ops):
        ops, client, _ = redis_ops
        client.get.return_value = "not-a-number"
        with pytest.raises(DataTypeException):
            ops.get_arbitrage_threshold()

    def test_set_failure_wraps_as_dependency_exception(self, redis_ops):
        ops, client, logger = redis_ops
        original = Exception("boom")
        client.set.side_effect = original
        with pytest.raises(DependencyException) as exc_info:
            ops.set_arbitrage_threshold(Decimal("0.0042"))
        assert exc_info.value.__cause__ is original

    def test_get_failure_wraps_as_dependency_exception(self, redis_ops):
        assert_wraps_as_dependency_exception(
            redis_ops,
            FailureSpec(client_attr="get", op_name="get_arbitrage_threshold"),
        )


class TestSetOrders:
    """#118: json.dumps must use _DecimalEncoder; order payloads carry Decimal
    price/qty fields, so a plain json.dumps(value) raised TypeError (swallowed
    and re-raised as DependencyException) and silently dropped every order."""

    def test_decimal_fields_serialize_as_str(self, redis_ops):
        import json
        ops, client, _ = redis_ops
        ops.set_orders("strat_x", {"price": Decimal("1.5"), "qty": Decimal("0.001")})
        member = json.loads(client.sadd.call_args.args[1])
        assert member == {"price": "1.5", "qty": "0.001"}


class TestSetPublishTrades:
    """#97: the except clause caught DependencyException, which the client's
    zadd/expire/publish calls never raise, so real client errors escaped
    uncaught instead of being wrapped like every other method in the module."""

    def test_client_error_wraps_as_dependency_exception(self, redis_ops):
        ops, client, _ = redis_ops
        original = Exception("connection reset")
        client.zadd.side_effect = original
        with pytest.raises(DependencyException) as exc_info:
            ops.set_publish_trades({"BTCUSDT": {"side": "B"}}, 1700000000000)
        assert exc_info.value.__cause__ is original

    def test_decimal_fields_serialize_as_str(self, redis_ops):
        """#119: zadd member and published payload both carry Decimal price
        fields; without _DecimalEncoder json.dumps raised TypeError."""
        import json
        ops, client, _ = redis_ops
        ops.set_publish_trades({"BTCUSDT": {"price": Decimal("1.5")}}, 1700000000000)
        member = next(iter(client.zadd.call_args.args[1]))
        assert json.loads(member) == {"BTCUSDT": {"price": "1.5"}}
        published = json.loads(client.publish.call_args.args[1])
        assert published == {"BTCUSDT": {"price": "1.5"}}


class TestGetNewBookTicker:
    """#98: json.loads was called on the raw get() result without a null check,
    so a normal cache miss (get -> None) raised inside json.loads and surfaced
    as a false DependencyException instead of a plain None."""

    def test_returns_none_on_cache_miss(self, redis_ops):
        ops, client, _ = redis_ops
        client.get.return_value = None
        assert ops.get_new_book_ticker("BN") is None

    def test_parses_value_when_present(self, redis_ops):
        import json
        ops, client, _ = redis_ops
        client.get.return_value = json.dumps({"bid": "1", "ask": "2"})
        assert ops.get_new_book_ticker("BN") == {"bid": "1", "ask": "2"}


class TestGetOrderLink:
    """#99: decode_responses=True means get() already returns str/None, so the
    isinstance(value, bytes) branch was dead code."""

    def test_returns_str_value(self, redis_ops):
        ops, client, _ = redis_ops
        client.get.return_value = "perp_abc123"
        assert ops.get_order_link("spot_xyz") == "perp_abc123"

    def test_returns_none_when_missing(self, redis_ops):
        ops, client, _ = redis_ops
        client.get.return_value = None
        assert ops.get_order_link("spot_xyz") is None


class TestHashWritesUseHset:
    """#112/#113/#117: hmset() is deprecated (removed in redis-py 6.x); hash
    writes must use hset(key, mapping=...) instead."""

    def test_set_ticker_price_uses_hset_mapping(self, redis_ops):
        ops, client, _ = redis_ops
        ops.set_ticker_price("BN", {"BTCUSDT": "1"})
        client.hset.assert_called_once()
        assert client.hset.call_args.kwargs["mapping"] == {"BTCUSDT": "1"}
        client.hmset.assert_not_called()

    def test_set_book_ticker_uses_hset_mapping(self, redis_ops):
        ops, client, _ = redis_ops
        ops.set_book_ticker("BTC-USDT_BN.SPOT", {"bid": "1", "ask": "2"})
        client.hset.assert_called_once()
        assert client.hset.call_args.kwargs["mapping"] == {"bid": "1", "ask": "2"}
        client.hmset.assert_not_called()


class TestDecimalEncoderConsistency:
    """#120/#114: every json.dumps of a business value must use _DecimalEncoder;
    book_ticker / depth / inventory-close payloads carry Decimal price fields and
    would raise TypeError otherwise."""

    def test_set_new_book_ticker_encodes_decimal(self, redis_ops):
        import json
        ops, client, _ = redis_ops
        ops.set_new_book_ticker("BN", {"bid": Decimal("1.5"), "ask": Decimal("1.6")})
        assert json.loads(client.set.call_args.args[1]) == {"bid": "1.5", "ask": "1.6"}

    def test_push_book_ticker_encodes_decimal(self, redis_ops):
        import json
        ops, client, _ = redis_ops
        ops.push_book_ticker("BN", {"bid": Decimal("1.5")})
        assert json.loads(client.publish.call_args.args[1]) == {"bid": "1.5"}

    def test_set_depth_order_theoretical_encodes_decimal(self, redis_ops):
        import json
        ops, client, _ = redis_ops
        ops.set_depth_order_theoretical("BN", {"px": Decimal("2.5")})
        assert json.loads(client.set.call_args.args[1]) == {"px": "2.5"}
        assert json.loads(client.publish.call_args.args[1]) == {"px": "2.5"}

    def test_set_publish_inventory_close_encodes_decimal(self, redis_ops):
        import json
        ops, client, _ = redis_ops
        ops.set_publish_inventory_close({"pnl": Decimal("-3.25")})
        assert json.loads(client.set.call_args.args[1]) == {"pnl": "-3.25"}
