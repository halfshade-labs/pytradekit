"""HuobiWsManager 重连流程测试。

主要验证 #51 / #52 之后的 _reconnect_streams 行为：
- 公开 WS（is_public=True）保留旧逻辑，重发 _subs；
- 私有 WS（is_public=False）必须先 _login，订阅由 auth 成功回调再发，
  避免 invalid.auth.state。
"""
from unittest.mock import MagicMock, patch

import pytest


def _make_manager(is_public, mocker):
    """构造一个 HuobiWsManager 实例，跳过底层 WS 连接。"""
    with patch('pytradekit.ws.huobi_ws.WsManager.__init__', return_value=None):
        from pytradekit.ws.huobi_ws import HuobiWsManager
        mgr = HuobiWsManager.__new__(HuobiWsManager)
        # 手填必要属性，覆盖 super().__init__() 没执行的部分
        mgr.is_public = is_public
        mgr.logger = MagicMock()
        mgr._subs = [{'action': 'sub', 'ch': 'trade.clearing#*#0'}]
        mgr._api_key = 'key'
        mgr._api_secret = 'secret'
        mgr.send_json = MagicMock()
        # WsManager.send_json 在父类里，避免被它访问 _cur_id
        return mgr


class TestReconnectStreams:
    def test_private_ws_relogins_instead_of_resending_subs(self, mocker):
        mgr = _make_manager(is_public=False, mocker=mocker)
        mocker.patch.object(mgr, '_login')
        mocker.patch('pytradekit.gateway.websocket.base_ws_manager.BaseWebsocketManager._reconnect_streams')

        mgr._reconnect_streams()

        # 私有连接：只调 _login，不走父类的 _reconnect_streams
        mgr._login.assert_called_once()

    def test_public_ws_delegates_to_super(self, mocker):
        mgr = _make_manager(is_public=True, mocker=mocker)
        mocker.patch.object(mgr, '_login')
        super_mock = mocker.patch(
            'pytradekit.gateway.websocket.ws_manager.WsManager._reconnect_streams'
        )

        mgr._reconnect_streams()

        # 公开行情：不应触发 _login；走父类重发订阅
        mgr._login.assert_not_called()
        super_mock.assert_called_once()
