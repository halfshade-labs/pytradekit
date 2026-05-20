"""BinanceWsManager subscribe() — listenKey routing for spot vs perp.

Background: Binance perp userDataStream only delivers events when the listenKey is
in the WebSocket URL path; the SUBSCRIBE method silently drops events on fstream.
The fix (this branch) makes perp connect to wss://fstream.binance.com/ws/<listenKey>
and skip SUBSCRIBE. Spot still uses the SUBSCRIBE-method path.
"""
from unittest.mock import MagicMock, patch


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
        mgr.ws = None
        mgr.status = 'INIT'
        if is_perp:
            mgr._url = BinanceAuxiliary.url_perp_ws.value
            mgr._listen_key_url = BinanceAuxiliary.perp_url.value + BinanceAuxiliary.user_perp_data_stream.value
        else:
            mgr._url = BinanceAuxiliary.url_ws.value
            mgr._listen_key_url = BinanceAuxiliary.url.value + BinanceAuxiliary.user_data_stream.value
        return mgr


class TestSubscribePerp:
    def test_perp_listen_key_goes_into_url_path_and_skips_subscribe(self, mocker):
        from pytradekit.utils.dynamic_types import BinanceAuxiliary
        mgr = _make_manager(is_perp=True)

        def fake_post_listen_key(_api):
            mgr._listen_key['SPOT'] = 'PERP_LK_TOKEN'
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
        mgr.status = 'ACTIVE'

        def fake_post_listen_key(_api):
            mgr._listen_key['SPOT'] = 'NEW_PERP_LK'
        mocker.patch.object(mgr, 'post_listen_key', side_effect=fake_post_listen_key)
        mocker.patch.object(mgr, 'connect')
        mocker.patch.object(mgr, '_ping')

        mgr.subscribe()

        stale_ws.close.assert_called_once()
        assert mgr.status == 'INIT'
        mgr.connect.assert_called_once()


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
