import json

import pytest

from pytradekit.utils.dynamic_types import BinanceAuxiliary, BinanceWebSocket
from pytradekit.ws.binance_ws import BinanceWsManager
from pytradekit.ws.huobi_ws import HuobiWsManager
from pytradekit.ws.okex_ws import OkexWsManager
from pytradekit.ws.subscription_update import SubscriptionUpdate


def _make_binance_manager(mocker):
    manager = BinanceWsManager.__new__(BinanceWsManager)
    manager._bookticker_symbols = {"BTCUSDT", "XRPUSDT"}
    manager._subs = [
        {
            "method": BinanceWebSocket.subscribe.value,
            "params": ["btcusdt@bookTicker", "xrpusdt@bookTicker"],
        }
    ]
    manager.send_json = mocker.Mock()
    return manager


def _make_htx_manager(mocker):
    manager = HuobiWsManager.__new__(HuobiWsManager)
    manager._bookticker_symbols = {"BTCUSDT", "XRPUSDT"}
    manager._subs = [
        {"sub": "market.btcusdt.bbo", "id": 1},
        {"sub": "market.xrpusdt.bbo", "id": 2},
    ]
    manager.send_json = mocker.Mock()
    return manager


def _make_okx_manager(mocker):
    manager = OkexWsManager.__new__(OkexWsManager)
    manager._bookticker_symbols = {"BTC-USDT", "XRP-USDT"}
    manager._subs = [
        {
            "op": "subscribe",
            "args": [
                {"channel": "tickers", "instId": "BTC-USDT"},
                {"channel": "tickers", "instId": "XRP-USDT"},
            ],
        }
    ]
    manager.send_json = mocker.Mock()
    return manager


def test_binance_updates_bookticker_subscriptions(mocker):
    manager = _make_binance_manager(mocker)

    update = manager.update_bookticker_stream(["BTCUSDT", "ETHUSDT"])

    assert update == SubscriptionUpdate(
        added=("ETHUSDT",),
        removed=("XRPUSDT",),
    )
    assert manager.send_json.call_args_list == [
        mocker.call(
            {
                "method": BinanceWebSocket.subscribe.value,
                "params": ["ethusdt@bookTicker"],
            }
        ),
        mocker.call(
            {
                "method": "UNSUBSCRIBE",
                "params": ["xrpusdt@bookTicker"],
            }
        ),
    ]
    assert manager._subs == [
        {
            "method": BinanceWebSocket.subscribe.value,
            "params": ["btcusdt@bookTicker", "ethusdt@bookTicker"],
        }
    ]


def test_binance_starts_idle_for_empty_bookticker_targets(mocker):
    manager = _make_binance_manager(mocker)
    manager.start_subscribe = mocker.Mock()
    manager._ping = mocker.Mock()

    manager.start_bookticker_stream([])

    assert manager._bookticker_symbols == frozenset()
    assert manager._subs == []
    manager.start_subscribe.assert_not_called()
    manager._ping.assert_called_once_with(
        BinanceAuxiliary.ws_ping_sleep.value,
        is_listen_key=False,
    )


def test_binance_empty_update_can_later_add_bookticker_targets(mocker):
    manager = _make_binance_manager(mocker)

    empty_update = manager.update_bookticker_stream([])

    assert empty_update == SubscriptionUpdate(
        added=(),
        removed=("BTCUSDT", "XRPUSDT"),
    )
    assert manager._bookticker_symbols == frozenset()
    assert manager._subs == []
    assert manager.send_json.call_args_list == [
        mocker.call(
            {
                "method": "UNSUBSCRIBE",
                "params": ["btcusdt@bookTicker", "xrpusdt@bookTicker"],
            }
        )
    ]

    manager.send_json.reset_mock()
    added_update = manager.update_bookticker_stream(["ETHUSDT"])

    assert added_update == SubscriptionUpdate(added=("ETHUSDT",), removed=())
    assert manager._subs == [
        {
            "method": BinanceWebSocket.subscribe.value,
            "params": ["ethusdt@bookTicker"],
        }
    ]
    manager.send_json.assert_called_once_with(
        {
            "method": BinanceWebSocket.subscribe.value,
            "params": ["ethusdt@bookTicker"],
        }
    )


def test_htx_updates_bookticker_subscriptions(mocker):
    manager = _make_htx_manager(mocker)

    update = manager.update_bookticker_stream(["BTCUSDT", "ETHUSDT"])

    assert update == SubscriptionUpdate(
        added=("ETHUSDT",),
        removed=("XRPUSDT",),
    )
    assert manager.send_json.call_args_list == [
        mocker.call({"sub": "market.ethusdt.bbo"}),
        mocker.call({"unsub": "market.xrpusdt.bbo"}),
    ]
    assert manager._subs == [
        {"sub": "market.btcusdt.bbo", "id": 1},
        {"sub": "market.ethusdt.bbo", "id": 2},
    ]


def test_okx_updates_bookticker_subscriptions(mocker):
    manager = _make_okx_manager(mocker)

    update = manager.update_bookticker_stream(["BTC-USDT", "ETH-USDT"])

    assert update == SubscriptionUpdate(
        added=("ETH-USDT",),
        removed=("XRP-USDT",),
    )
    assert manager.send_json.call_args_list == [
        mocker.call(
            {
                "op": "subscribe",
                "args": [{"channel": "tickers", "instId": "ETH-USDT"}],
            }
        ),
        mocker.call(
            {
                "op": "unsubscribe",
                "args": [{"channel": "tickers", "instId": "XRP-USDT"}],
            }
        ),
    ]
    assert manager._subs == [
        {
            "op": "subscribe",
            "args": [
                {"channel": "tickers", "instId": "BTC-USDT"},
                {"channel": "tickers", "instId": "ETH-USDT"},
            ],
        }
    ]


@pytest.mark.parametrize(
    "manager_factory, symbols",
    [
        (_make_binance_manager, ["XRPUSDT", "BTCUSDT", "BTCUSDT"]),
        (_make_htx_manager, ["XRPUSDT", "BTCUSDT", "BTCUSDT"]),
        (_make_okx_manager, ["XRP-USDT", "BTC-USDT", "BTC-USDT"]),
    ],
)
def test_bookticker_subscription_update_is_idempotent(
    mocker,
    manager_factory,
    symbols,
):
    manager = manager_factory(mocker)

    update = manager.update_bookticker_stream(symbols)

    assert update == SubscriptionUpdate(added=(), removed=())
    manager.send_json.assert_not_called()


def test_bookticker_update_keeps_old_state_when_addition_fails(mocker):
    manager = _make_binance_manager(mocker)
    original_subscriptions = manager._subs
    manager.send_json.side_effect = RuntimeError("connection unavailable")

    with pytest.raises(RuntimeError, match="connection unavailable"):
        manager.update_bookticker_stream(["BTCUSDT", "ETHUSDT"])

    assert manager._subs is original_subscriptions
    assert manager._bookticker_symbols == {"BTCUSDT", "XRPUSDT"}
    assert manager.send_json.call_count == 1


@pytest.mark.parametrize(
    "order_update, should_forward",
    [
        ({"state": "partially_filled", "fillSz": "0.25"}, True),
        ({"state": "filled", "fillSz": "0"}, True),
        ({"state": "canceled", "fillSz": "0"}, False),
    ],
)
def test_okx_private_order_stream_forwards_real_partial_fills(
    mocker,
    order_update,
    should_forward,
):
    manager = OkexWsManager.__new__(OkexWsManager)
    manager._queue = mocker.Mock()
    manager.logger = mocker.Mock()
    message = {
        "arg": {"channel": "orders"},
        "data": [order_update],
    }

    manager._on_message(None, json.dumps(message))

    if should_forward:
        manager._queue.put_nowait.assert_called_once_with(order_update)
    else:
        manager._queue.put_nowait.assert_not_called()
