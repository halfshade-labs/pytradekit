"""BinanceWsManager subscribe() — listenKey routing for spot vs perp.

Background: Binance perp userDataStream only delivers events when the listenKey is
in the WebSocket URL path; the SUBSCRIBE method silently drops events on fstream.
The fix (this branch) makes perp connect to wss://fstream.binance.com/ws/<listenKey>
and skip SUBSCRIBE. Spot still uses the SUBSCRIBE-method path.
"""
import json
import queue
from unittest.mock import MagicMock, patch

from pytradekit.utils.dynamic_types import WebsocketStatus


def _make_manager(is_perp):
    """Build a BinanceWsManager without invoking the real WsManager __init__."""
    with patch('pytradekit.ws.binance_ws.WsManager.__init__', return_value=None):
        from pytradekit.ws.binance_ws import BinanceWsManager
        from pytradekit.utils.dynamic_types import BinanceAuxiliary
        mgr = BinanceWsManager.__new__(BinanceWsManager)
        mgr.logger = MagicMock()
        mgr._api_key = 'KEY'
        mgr._api_secret = 'SECRET'
        mgr._listen_key = {}
        mgr._send_params = None
        mgr._is_perp = is_perp
        mgr._msg_count = 0
        mgr._msg_log_full_n = 0
        mgr._queue = queue.Queue()
        mgr._ticker_queue = None
        mgr.verify_bookticker_duplicate = {}
        mgr.ws = None
        mgr.status = WebsocketStatus.INIT.name
        if is_perp:
            mgr._url = BinanceAuxiliary.url_perp_ws.value
            mgr._listen_key_url = BinanceAuxiliary.perp_url.value + BinanceAuxiliary.user_perp_data_stream.value
        else:
            mgr._url = BinanceAuxiliary.url_ws.value
            mgr._listen_key_url = BinanceAuxiliary.url.value + BinanceAuxiliary.user_data_stream.value
        return mgr


def _logged_messages(logger):
    messages = []
    for level in ('debug', 'info', 'warning', 'error', 'exception', 'critical'):
        for call in getattr(logger, level).call_args_list:
            messages.extend(str(arg) for arg in call.args)
    return '\n'.join(messages)


class TestSubscribePerp:
    def test_perp_listen_key_goes_into_url_path_and_skips_subscribe(self, mocker):
        from pytradekit.utils.dynamic_types import BinanceAuxiliary
        mgr = _make_manager(is_perp=True)

        def fake_post_listen_key(_api):
            mgr._listen_key['PERP'] = 'PERP_LK_TOKEN'
        mocker.patch.object(mgr, 'post_listen_key', side_effect=fake_post_listen_key)
        mocker.patch.object(mgr, 'connect')
        mocker.patch.object(mgr, 'start_subscribe')
        # Stop after first iteration so the test does not loop forever.
        mocker.patch.object(mgr, '_ping')

        mgr.subscribe()

        expected_url = f"{BinanceAuxiliary.url_perp_ws.value}/PERP_LK_TOKEN"
        assert mgr._url == expected_url
        mgr.connect.assert_called_once()
        # SUBSCRIBE method must NOT be sent for perp userDataStream.
        mgr.start_subscribe.assert_not_called()

    def test_perp_renewal_closes_stale_ws_before_reconnect(self, mocker):
        mgr = _make_manager(is_perp=True)
        stale_ws = MagicMock()
        mgr.ws = stale_ws
        mgr.status = WebsocketStatus.ACTIVE.name

        def fake_post_listen_key(_api):
            mgr._listen_key['PERP'] = 'NEW_PERP_LK'
        mocker.patch.object(mgr, 'post_listen_key', side_effect=fake_post_listen_key)
        mocker.patch.object(mgr, 'connect')
        mocker.patch.object(mgr, 'start_subscribe')
        mocker.patch.object(mgr, '_ping')

        mgr.subscribe()

        stale_ws.close.assert_called_once()
        assert mgr.status == WebsocketStatus.INIT.name
        mgr.connect.assert_called_once()
        # SUBSCRIBE method must NOT be sent for perp userDataStream, even on renewal.
        mgr.start_subscribe.assert_not_called()

    def test_perp_private_url_is_never_logged(self, mocker):
        mgr = _make_manager(is_perp=True)
        listen_key = 'PERP_LK_TOKEN_DO_NOT_LOG'

        def fake_post_listen_key(_api):
            mgr._listen_key['PERP'] = listen_key

        mocker.patch.object(mgr, 'post_listen_key', side_effect=fake_post_listen_key)
        mocker.patch.object(mgr, 'connect')
        mocker.patch.object(mgr, '_ping')
        mocker.patch.object(mgr, 'reconnect')

        mgr.subscribe()
        private_url = mgr._url
        mgr._on_open(None)
        mgr._on_error(None, RuntimeError(f'failed to connect {private_url}'))
        mgr._on_close(None, 1006, f'closing {private_url}')

        logged = _logged_messages(mgr.logger)
        assert listen_key not in logged
        assert private_url not in logged
        assert 'market=perp' in logged
        assert f'listen_key_len={len(listen_key)}' in logged
        assert 'error_type=RuntimeError' in logged


class TestSubscribeSpot:
    def test_spot_keeps_subscribe_method_with_listen_key(self, mocker):
        from pytradekit.utils.dynamic_types import BinanceAuxiliary
        mgr = _make_manager(is_perp=False)
        original_url = mgr._url

        def fake_post_listen_key(_api):
            mgr._listen_key['SPOT'] = 'SPOT_LK_TOKEN'
        mocker.patch.object(mgr, 'post_listen_key', side_effect=fake_post_listen_key)
        mocker.patch.object(mgr, 'connect')
        mocker.patch.object(mgr, 'start_subscribe')
        mocker.patch.object(mgr, '_ping')

        mgr.subscribe()

        assert mgr._url == original_url
        mgr.start_subscribe.assert_called_once_with(['SPOT_LK_TOKEN'])
        mgr.connect.assert_not_called()

    def test_spot_subscribe_params_are_never_logged(self, mocker):
        mgr = _make_manager(is_perp=False)
        listen_key = 'SPOT_LK_TOKEN_DO_NOT_LOG'

        def fake_post_listen_key(_api):
            mgr._listen_key['SPOT'] = listen_key

        mocker.patch.object(mgr, 'post_listen_key', side_effect=fake_post_listen_key)
        mocker.patch.object(mgr, 'start_subscribe')
        mocker.patch.object(mgr, '_ping')

        mgr.subscribe()

        logged = _logged_messages(mgr.logger)
        assert listen_key not in logged
        assert 'market=spot' in logged
        assert f'listen_key_len={len(listen_key)}' in logged

    def test_subscribe_error_does_not_log_exception_secret(self, mocker):
        mgr = _make_manager(is_perp=False)
        listen_key = 'SPOT_LK_TOKEN_IN_EXCEPTION'
        mocker.patch.object(
            mgr,
            'post_listen_key',
            side_effect=RuntimeError(f'private URL ended with {listen_key}'),
        )

        mgr.subscribe()

        logged = _logged_messages(mgr.logger)
        assert listen_key not in logged
        assert 'market=spot' in logged
        assert 'error_type=RuntimeError' in logged

    def test_start_subscribe_error_does_not_log_key_or_message(self, mocker):
        mgr = _make_manager(is_perp=False)
        listen_key = 'SPOT_LK_TOKEN_IN_SEND_ERROR'
        mgr._listen_key['SPOT'] = listen_key
        mocker.patch.object(
            mgr,
            'send_json',
            side_effect=RuntimeError(f'failed to send params [{listen_key}]'),
        )

        mgr.start_subscribe([listen_key])

        logged = _logged_messages(mgr.logger)
        assert listen_key not in logged
        assert 'market=spot' in logged
        assert f'listen_key_len={len(listen_key)}' in logged
        assert 'error_type=RuntimeError' in logged


class TestBookTickerReceiveTimestamp:
    def test_spot_bookticker_without_exchange_timestamps_gets_receive_time(self, mocker):
        from pytradekit.utils.dynamic_types import BinanceWebSocket

        mgr = _make_manager(is_perp=False)
        payload = {
            'u': 123,
            's': 'BTCUSDT',
            'b': '50000.1',
            'B': '1.2',
            'a': '50000.2',
            'A': '1.3',
        }
        receive_time_ms = 1_788_000_000_123
        mocker.patch(
            'pytradekit.ws.binance_ws.get_timestamp_ms',
            return_value=receive_time_ms,
        )

        mgr._on_message(None, json.dumps(payload))

        queued = mgr._queue.get_nowait()
        assert queued == {
            **payload,
            BinanceWebSocket.run_time_ms.value: receive_time_ms,
        }
        assert 'E' not in queued
        assert 'T' not in queued

    def test_perp_bookticker_preserves_exchange_fields_and_gets_receive_time(self, mocker):
        from pytradekit.utils.dynamic_types import BinanceWebSocket

        mgr = _make_manager(is_perp=True)
        payload = {
            'e': 'bookTicker',
            'u': 456,
            's': 'ETHUSDT',
            'b': '4000.1',
            'B': '2.2',
            'a': '4000.2',
            'A': '2.3',
            'E': 1_788_000_000_100,
            'T': 1_788_000_000_101,
        }
        receive_time_ms = 1_788_000_000_123
        mocker.patch(
            'pytradekit.ws.binance_ws.get_timestamp_ms',
            return_value=receive_time_ms,
        )

        mgr._on_message(None, json.dumps(payload))

        queued = mgr._queue.get_nowait()
        assert queued == {
            **payload,
            BinanceWebSocket.run_time_ms.value: receive_time_ms,
        }

    def test_remote_receive_time_is_replaced_and_duplicate_filter_unchanged(self, mocker):
        from pytradekit.utils.dynamic_types import BinanceWebSocket

        mgr = _make_manager(is_perp=False)
        remote_receive_time_ms = 1_788_000_000_111
        local_receive_time_ms = 1_788_000_000_222
        duplicate_receive_time_ms = 1_788_000_000_333
        payload = {
            'u': 789,
            's': 'SOLUSDT',
            'b': '200.1',
            'B': '3.2',
            'a': '200.2',
            'A': '3.3',
            BinanceWebSocket.run_time_ms.value: remote_receive_time_ms,
        }
        timestamp = mocker.patch(
            'pytradekit.ws.binance_ws.get_timestamp_ms',
            side_effect=[local_receive_time_ms, duplicate_receive_time_ms],
        )

        mgr._on_message(None, json.dumps(payload))
        mgr._on_message(None, json.dumps(payload))

        queued = mgr._queue.get_nowait()
        assert queued == {
            **payload,
            BinanceWebSocket.run_time_ms.value: local_receive_time_ms,
        }
        assert queued[BinanceWebSocket.run_time_ms.value] != remote_receive_time_ms
        assert mgr._queue.empty()
        assert timestamp.call_count == 2

    def test_private_message_and_ack_do_not_capture_bookticker_receive_time(self, mocker):
        mgr = _make_manager(is_perp=False)
        private_payload = {
            'e': 'executionReport',
            's': 'BTCUSDT',
            'X': 'FILLED',
        }
        ack_payload = {'result': None, 'id': 1}
        timestamp = mocker.patch('pytradekit.ws.binance_ws.get_timestamp_ms')

        mgr._on_message(None, json.dumps(private_payload))
        mgr._on_message(None, json.dumps(ack_payload))

        assert mgr._queue.get_nowait() == private_payload
        assert mgr._queue.empty()
        timestamp.assert_not_called()
