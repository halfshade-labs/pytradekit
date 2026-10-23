"""Public perpetual bookTicker selects its route before any initial connect."""
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pytradekit.utils.dynamic_types import BinanceAuxiliary, WebsocketStatus
from pytradekit.ws.binance_ws import BinanceWsManager


PUBLIC_PERP_URL = 'wss://fstream.binance.com/public/ws'


def observe_initial_connections(monkeypatch, manager):
    urls = []

    def connect():
        urls.append(manager._get_url())
        manager.status = WebsocketStatus.ACTIVE.name
        manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock())

    monkeypatch.setattr(manager, 'connect', connect)
    monkeypatch.setattr(manager, '_ping', Mock())
    return urls


@pytest.mark.parametrize('start_mode', ['start', 'update', 'empty_then_update'])
@pytest.mark.parametrize('is_perp', [False, True])
def test_first_bookticker_connect_uses_market_route(monkeypatch, start_mode, is_perp):
    manager = BinanceWsManager(Mock(), queue=Queue(), is_perp=is_perp)
    urls = observe_initial_connections(monkeypatch, manager)
    if start_mode == 'start':
        manager.start_bookticker_stream(['BTCUSDT'])
    else:
        if start_mode == 'empty_then_update':
            manager.start_bookticker_stream([])
            assert not urls
        manager.update_bookticker_stream(['BTCUSDT'])
    expected = PUBLIC_PERP_URL if is_perp else BinanceAuxiliary.url_ws.value
    assert urls and set(urls) == {expected}
    assert manager._get_url() == expected
    assert manager._subs == [{'method': 'SUBSCRIBE', 'params': ['btcusdt@bookTicker']}]


@pytest.mark.parametrize('is_perp', [False, True])
def test_non_bookticker_route_is_preserved(monkeypatch, is_perp):
    manager = BinanceWsManager(Mock(), is_perp=is_perp)
    urls = observe_initial_connections(monkeypatch, manager)
    manager.start_subscribe(['btcusdt@aggTrade'])
    expected = BinanceAuxiliary.url_perp_ws.value if is_perp else BinanceAuxiliary.url_ws.value
    assert urls and set(urls) == {expected}
    assert not manager._public_bookticker


def test_private_perp_route_is_preserved(monkeypatch):
    manager = BinanceWsManager(Mock(), is_perp=True)
    urls = observe_initial_connections(monkeypatch, manager)
    manager._listen_key = {'PERP': 'test-placeholder'}
    manager._connect_perp_private_stream()
    assert urls == [BinanceAuxiliary.url_perp_ws.value + '/test-placeholder']
    assert not manager._public_bookticker


def test_spot_custom_route_is_preserved(monkeypatch):
    custom = 'wss://example.invalid/ws'
    manager = BinanceWsManager(Mock(), url=custom, queue=Queue())
    urls = observe_initial_connections(monkeypatch, manager)
    manager.start_bookticker_stream(['BTCUSDT'])
    assert urls and set(urls) == {custom}


def test_public_perp_explicit_url_rotation_is_preserved(monkeypatch):
    manager = BinanceWsManager(Mock(), queue=Queue(), is_perp=True)
    observe_initial_connections(monkeypatch, manager)
    manager.start_bookticker_stream(['BTCUSDT'])
    manager._url = 'wss://example.invalid/public-rotation'
    assert manager._get_url() == 'wss://example.invalid/public-rotation'
