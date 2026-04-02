import pytest
from unittest.mock import MagicMock
from pytradekit.utils.redis_operations import RedisOperations
from pytradekit.utils.exceptions import DependencyException


@pytest.fixture
def redis_ops(mocker):
    mock_client = mocker.MagicMock()
    mocker.patch('redis.StrictRedis.from_url', return_value=mock_client)
    mock_logger = mocker.MagicMock()
    ops = RedisOperations(mock_logger, 'redis://localhost:6379')
    return ops, mock_client, mock_logger


class TestPing:
    def test_ping_success_delegates_to_client(self, redis_ops):
        ops, client, _ = redis_ops
        ops.ping()
        client.ping.assert_called_once()

    def test_ping_failure_raises_dependency_exception(self, redis_ops):
        ops, client, _ = redis_ops
        client.ping.side_effect = Exception("connection refused")
        with pytest.raises(DependencyException):
            ops.ping()

    def test_ping_failure_logs_exception(self, redis_ops):
        ops, client, logger = redis_ops
        client.ping.side_effect = Exception("connection refused")
        with pytest.raises(DependencyException):
            ops.ping()
        logger.exception.assert_called_once()

    def test_ping_failure_chains_original_exception(self, redis_ops):
        ops, client, _ = redis_ops
        original = Exception("connection refused")
        client.ping.side_effect = original
        with pytest.raises(DependencyException) as exc_info:
            ops.ping()
        assert exc_info.value.__cause__ is original


class TestCreatePubsub:
    def test_create_pubsub_returns_client_pubsub(self, redis_ops):
        ops, client, _ = redis_ops
        result = ops.create_pubsub()
        assert result is client.pubsub.return_value

    def test_create_pubsub_failure_raises_dependency_exception(self, redis_ops):
        ops, client, _ = redis_ops
        client.pubsub.side_effect = Exception("pubsub error")
        with pytest.raises(DependencyException):
            ops.create_pubsub()

    def test_create_pubsub_failure_logs_exception(self, redis_ops):
        ops, client, logger = redis_ops
        client.pubsub.side_effect = Exception("pubsub error")
        with pytest.raises(DependencyException):
            ops.create_pubsub()
        logger.exception.assert_called_once()

    def test_create_pubsub_failure_chains_original_exception(self, redis_ops):
        ops, client, _ = redis_ops
        original = Exception("pubsub error")
        client.pubsub.side_effect = original
        with pytest.raises(DependencyException) as exc_info:
            ops.create_pubsub()
        assert exc_info.value.__cause__ is original


class TestClose:
    def test_close_success_delegates_to_client(self, redis_ops):
        ops, client, _ = redis_ops
        ops.close()
        client.close.assert_called_once()

    def test_close_failure_raises_dependency_exception(self, redis_ops):
        ops, client, _ = redis_ops
        client.close.side_effect = Exception("close failed")
        with pytest.raises(DependencyException):
            ops.close()

    def test_close_failure_logs_exception(self, redis_ops):
        ops, client, logger = redis_ops
        client.close.side_effect = Exception("close failed")
        with pytest.raises(DependencyException):
            ops.close()
        logger.exception.assert_called_once()

    def test_close_failure_chains_original_exception(self, redis_ops):
        ops, client, _ = redis_ops
        original = Exception("close failed")
        client.close.side_effect = original
        with pytest.raises(DependencyException) as exc_info:
            ops.close()
        assert exc_info.value.__cause__ is original
