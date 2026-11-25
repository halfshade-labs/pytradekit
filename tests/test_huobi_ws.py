"""HuobiWsManager 重连流程测试。

主要验证 #51 / #52 之后的 _reconnect_streams 行为：
- 公开 WS（is_public=True）保留旧逻辑，重发 _subs；
- 私有 WS（is_public=False）必须先 _login，订阅由 auth 成功回调再发，
  避免 invalid.auth.state。
"""
from unittest.mock import MagicMock, patch

from pytradekit.ws import huobi_ws


def _make_manager(is_public):
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
        mgr = _make_manager(is_public=False)
        mocker.patch.object(mgr, '_login')
        # super()._reconnect_streams() in HuobiWsManager resolves to WsManager,
        # not BaseWebsocketManager — patch the MRO-correct target so assert_not_called
        # actually rules out the parent path running on the private branch.
        super_mock = mocker.patch(
            'pytradekit.gateway.websocket.ws_manager.WsManager._reconnect_streams'
        )

        mgr._reconnect_streams()

        # 私有连接：只调 _login，不走父类的 _reconnect_streams
        mgr._login.assert_called_once()
        super_mock.assert_not_called()

    def test_public_ws_delegates_to_super(self, mocker):
        mgr = _make_manager(is_public=True)
        mocker.patch.object(mgr, '_login')
        super_mock = mocker.patch(
            'pytradekit.gateway.websocket.ws_manager.WsManager._reconnect_streams'
        )

        mgr._reconnect_streams()

        # 公开行情：不应触发 _login；走父类重发订阅
        mgr._login.assert_not_called()
        super_mock.assert_called_once()


class _PublicRefreshSchedule:
    def __init__(self, stop_after=1, stopped=False):
        self.now = 100_000
        self.waits = []
        self.stop_after = stop_after
        self.stopped = stopped

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        if len(self.waits) == self.stop_after:
            self.stopped = True
            return True
        self.now += seconds
        return False


def _collect_public_subscriptions(monkeypatch, schedule):
    manager = huobi_ws.HuobiWsManager(object())

    def reject_blocking_sleep(_seconds):
        raise AssertionError('public subscription refresh must stop without blocking sleep')

    monkeypatch.setattr(huobi_ws, 'sleep_min_time', reject_blocking_sleep)
    manager._stop_event = schedule
    monkeypatch.setattr(huobi_ws, 'get_timestamp_s', lambda: schedule.now)
    sends = []
    manager._send_all_subscriptions = lambda: sends.append((schedule.now, tuple(manager._subs)))
    manager.start_bookticker_stream(('BTCUSDT',))
    return sends


def test_periodic_refresh_keeps_intervals_and_request(monkeypatch):
    schedule = _PublicRefreshSchedule(stop_after=5)
    assert _collect_public_subscriptions(monkeypatch, schedule) == [
        (100_000, ({'sub': 'market.btcusdt.bbo', 'id': 1},)),
        (107_200, ({'sub': 'market.btcusdt.bbo', 'id': 1},)),
    ]
    assert schedule.waits == [1800] * 5


def test_stop_during_wait_prevents_resubscription(monkeypatch):
    schedule = _PublicRefreshSchedule()
    assert len(_collect_public_subscriptions(monkeypatch, schedule)) == 1
    assert schedule.waits == [1800]


def test_preexisting_stop_prevents_initial_subscription(monkeypatch):
    schedule = _PublicRefreshSchedule(stopped=True)
    assert _collect_public_subscriptions(monkeypatch, schedule) == []
    assert schedule.waits == []
