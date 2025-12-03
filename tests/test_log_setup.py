import os
import logging
import pytest
from pytradekit.utils.log_setup import LoggerConfig, get_logger, WebhookHandler, WebhookChannel


TEST_LOG_NAME = "test_logger"
TEST_MODE = 'a'
TEST_REPORT = ''
TEST_WATCH_JWJ = ""
TEST_MSG = 'test jwj basis log setup'


@pytest.fixture
def logger_config():
    # 设置测试时使用的LoggerConfig实例
    return LoggerConfig(
        log_name=TEST_LOG_NAME,
        report_webhook=TEST_REPORT,
        watch_webhook=TEST_WATCH_JWJ
    )


@pytest.fixture
def cleanup(logger_config):
    # 测试结束后清理生成的日志文件
    yield
    log_file = os.path.join(logger_config.log_path, logger_config.log_name + logger_config.log_suffix)
    if os.path.exists(log_file):
        os.remove(log_file)


@pytest.fixture
def memory_logger(logger_config):
    logger = get_logger(logger_config)  # 假设这个函数不需要参数，或者提供必要的参数
    memory_handler = logging.handlers.MemoryHandler(capacity=10, flushLevel=logging.ERROR)
    logger.addHandler(memory_handler)
    logger.setLevel(logging.DEBUG)  # 根据需要设置日志级别
    yield logger, memory_handler
    logger.removeHandler(memory_handler)


def test_logger_setup(memory_logger):
    logger, memory_handler = memory_logger

    # 发送一个测试消息
    logger.debug(TEST_MSG)
    logger.info(TEST_MSG)
    logger.warning(TEST_MSG)

    # 确保 MemoryHandler 有记录
    assert len(memory_handler.buffer) > 0, "MemoryHandler 没有记录到任何日志消息"

    # 验证消息内容
    last_log_record = memory_handler.buffer[-1]  # 获取最后一条日志记录
    assert last_log_record.getMessage() == TEST_MSG, "日志消息内容不匹配"


def test_webhook_handler_payload():
    slack_handler = WebhookHandler("https://example.com", channel=WebhookChannel.SLACK)
    slack_payload = slack_handler.build_payload("hello slack")
    assert slack_payload["text"].startswith("```")

    lark_handler = WebhookHandler("https://example.com")
    lark_payload = lark_handler.build_payload("hello lark")
    assert lark_payload["msg_type"] == "text"
    assert lark_payload["content"]["text"] == "hello lark"


if __name__ == '__main__':
    logger_config = LoggerConfig(TEST_LOG_NAME,
                                 TEST_REPORT,
                                 TEST_WATCH_JWJ)
    logger = get_logger(logger_config)
    logger.debug(TEST_MSG)
    logger.info(TEST_MSG)
    logger.warning(TEST_MSG)
