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
