import pytest
import logging
from io import StringIO
from pytradekit.utils.tools import retry, ExchangeException


@pytest.fixture
def logger():
    log_stream = StringIO()

    logger = logging.getLogger('retry_test_logger')
    logger.setLevel(logging.INFO)

    logger.handlers = []
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)

    logger.propagate = False

    return logger, log_stream


def test_retry_success_after_retries(logger):
    logger, log_stream = logger
    call_count = {"count": 0}

    @retry(try_times=3, interval=0.05)
    def flaky_function(logger):
        call_count["count"] += 1
        if call_count["count"] < 3:
            raise ValueError("Temporary failure")
        return "success"

    result = flaky_function(logger)
    assert result == "success"

    log_output = log_stream.getvalue()
    assert "Attempt failed (1/3)" in log_output
    assert "Attempt failed (2/3)" in log_output
    assert "Success after 2 retry attempts" in log_output


def test_retry_all_failures_raise(logger):
    logger, log_stream = logger
    call_count = {"count": 0}

    @retry(try_times=2, interval=0.05)
    def always_fail(logger):
        call_count["count"] += 1
        raise RuntimeError("Always fails")

    with pytest.raises(ExchangeException, match="exchange connect error"):
        always_fail(logger)

    log_output = log_stream.getvalue()
    assert "Attempt failed (1/2)" in log_output
    assert "All retry attempts failed" in log_output
    assert call_count["count"] == 2


def test_retry_no_logger():
    call_count = {"count": 0}

    @retry(try_times=2, interval=0.01)
    def fail_then_succeed():
        call_count["count"] += 1
        if call_count["count"] < 2:
            raise Exception("fail")
        return "ok"

    result = fail_then_succeed()
    assert result == "ok"
    assert call_count["count"] == 2