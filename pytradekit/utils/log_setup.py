import os

import importlib
import json
import logging
from enum import Enum
from logging import StreamHandler
import requests

from pytradekit.utils.dynamic_types import ConfigSection, ConfigKey
from pytradekit.utils.exceptions import DependencyException
from pytradekit.utils.tools import retry, get_git_branch


ConcurrentRotatingFileHandler = None
try:  # pragma: no cover - optional dependency
    ConcurrentRotatingFileHandler = importlib.import_module(
        "concurrent_log_handler").ConcurrentRotatingFileHandler
except ImportError:
    ConcurrentRotatingFileHandler = None


class WebhookChannel(Enum):
    LARK = 'lark'
    SLACK = 'slack'

    @classmethod
    def from_value(cls, value):
        if not value:
            return cls.LARK
        value = value.strip().lower()
        if value == cls.SLACK.value:
            return cls.SLACK
        return cls.LARK


class WebhookHandler(logging.Handler):
    def __init__(self, webhook_url, channel=WebhookChannel.LARK, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.webhook_url = webhook_url
        self.channel = channel

    def emit(self, record):
        msg = self.format(record)
        payload = self.build_payload(msg)
        self.send_message(payload)

    def build_payload(self, msg):
        if self.channel == WebhookChannel.SLACK:
            return {'text': f"```\n{msg}\n```"}
        return {"msg_type": "text", "content": {"text": msg}}

    @retry()
    def send_message(self, payload):
        headers = {'Content-type': 'application/json'}
        response = requests.post(self.webhook_url, headers=headers, data=json.dumps(payload))
        return response.text


class SingleLevelFilter(logging.Filter):
    def __init__(self, pass_level):
        super().__init__()
        self.pass_level = pass_level

    def filter(self, record):
        return record.levelno == self.pass_level


class LoggerConfig:
    def __init__(self, log_name, report_webhook, watch_webhook, channel=WebhookChannel.LARK):
        self.backup_count = 10
        self.max_bytes = 2_000_000
        self.mode = 'a'
        self.log_name = f'{log_name}@{get_git_branch()}'
        self.log_path = './logs/'
        self.log_suffix = '.log'
        self.log_format = '[%(filename)s:%(lineno)d] %(asctime)s %(levelname)8s: %(message)s'
        self.log_level = logging.DEBUG
        self.report_webhook = report_webhook
        self.report_level = logging.WARNING
        self.report_log_format = '%(asctime)s - %(name)s - %(message)s'
        self.watch_webhook = watch_webhook
        self.watch_level = logging.INFO
        self.channel = channel


def create_webhook_handler(webhook_url, log_format, log_level, channel):
    webhook_handler = WebhookHandler(webhook_url, channel=channel)
    slack_formatter = logging.Formatter(log_format)
    webhook_handler.setFormatter(slack_formatter)
    webhook_handler.setLevel(log_level)
    webhook_handler.addFilter(SingleLevelFilter(log_level))
    return webhook_handler


def extract_logger_config(config):
    log_name = config.outer_name or config.get_str(config.inner, ConfigSection.log.value, ConfigKey.log_name.value)
    report_webhook = config.get_str(config.inner, ConfigSection.web_hook_url.value, ConfigKey.report.value)
    watch_webhook = config.get_str(config.inner, ConfigSection.web_hook_url.value, ConfigKey.watch.value)
    channel_value = None
    try:
        channel_value = config.get_str(config.inner, ConfigSection.web_hook_url.value, ConfigKey.channel.value)
    except DependencyException:
        channel_value = None
    channel = WebhookChannel.from_value(channel_value)
    return LoggerConfig(log_name, report_webhook, watch_webhook, channel=channel)


def get_logger(logger_config):
    if ConcurrentRotatingFileHandler is None:
        raise ImportError("concurrent-log-handler is required but not installed.")
    os.makedirs(logger_config.log_path, exist_ok=True)
    log_file_name = os.path.join(logger_config.log_path, logger_config.log_name + logger_config.log_suffix)
    formatter = logging.Formatter(logger_config.log_format)
    logger = logging.getLogger(logger_config.log_name)
    if logger.hasHandlers():
        return logger
    logger.setLevel(logger_config.log_level)
    logger.addHandler(create_webhook_handler(logger_config.report_webhook,
                                             logger_config.report_log_format,
                                             logger_config.report_level,
                                             logger_config.channel))

    logger.addHandler(create_webhook_handler(logger_config.watch_webhook,
                                             logger_config.report_log_format,
                                             logger_config.watch_level,
                                             logger_config.channel))

    rotating_filehandler = ConcurrentRotatingFileHandler(filename=log_file_name,
                                                         mode=logger_config.mode,
                                                         maxBytes=logger_config.max_bytes,
                                                         backupCount=logger_config.backup_count)
    rotating_filehandler.setFormatter(formatter)
    logger.addHandler(rotating_filehandler)

    console_handler = StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger
