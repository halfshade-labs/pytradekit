from unittest.mock import Mock

import pandas as pd
import pytest

from process import monitor_server_processes as monitor


def test_process_monitor_requires_webhook_before_collecting_or_sending(monkeypatch):
    monkeypatch.delenv("LARK_WEBHOOK_URL", raising=False)
    chat = Mock()
    collect = Mock()
    monkeypatch.setattr(monitor, "LarkChatApp", chat)
    monkeypatch.setattr(monitor, "get_process_info", collect)
    with pytest.raises(KeyError, match="LARK_WEBHOOK_URL"):
        monitor.run(None)
    chat.assert_not_called()
    collect.assert_not_called()


def test_process_monitor_uses_only_injected_webhook(monkeypatch):
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://example.com/test-hook")
    chat = Mock()
    monkeypatch.setattr(monitor, "LarkChatApp", chat)
    monkeypatch.setattr(monitor, "get_process_info", Mock(return_value=(0, 0, pd.DataFrame())))
    monitor.run(None)
    chat.assert_called_once_with(webhook_url="https://example.com/test-hook")
