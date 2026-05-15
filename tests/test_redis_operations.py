from decimal import Decimal

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


def assert_wraps_as_dependency_exception(redis_ops, client_attr, op_name, *args):
    """Assert ops.<op_name>(*args) wraps a client error as DependencyException.

    Covers all three failure behaviors in one call:
    raises DependencyException, chains __cause__, calls logger.exception once.
    """
    ops, client, logger = redis_ops
    original = Exception("boom")
    getattr(client, client_attr).side_effect = original
    with pytest.raises(DependencyException) as exc_info:
        getattr(ops, op_name)(*args)
    assert exc_info.value.__cause__ is original
    logger.exception.assert_called_once()


class TestPing:
    def test_success_delegates_to_client(self, redis_ops):
        ops, client, _ = redis_ops
        ops.ping()
        client.ping.assert_called_once()

    def test_failure_wraps_as_dependency_exception(self, redis_ops):
        assert_wraps_as_dependency_exception(redis_ops, "ping", "ping")


class TestCreatePubsub:
    def test_returns_client_pubsub(self, redis_ops):
        ops, client, _ = redis_ops
        result = ops.create_pubsub()
        assert result is client.pubsub.return_value

    def test_failure_wraps_as_dependency_exception(self, redis_ops):
        assert_wraps_as_dependency_exception(redis_ops, "pubsub", "create_pubsub")


class TestClose:
    def test_success_delegates_to_client(self, redis_ops):
        ops, client, _ = redis_ops
        ops.close()
        client.close.assert_called_once()

    def test_failure_wraps_as_dependency_exception(self, redis_ops):
        assert_wraps_as_dependency_exception(redis_ops, "close", "close")


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
        assert_wraps_as_dependency_exception(redis_ops, "get", "get_target_premium", "perp_sell_x")
