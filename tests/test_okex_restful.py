from unittest.mock import Mock

from pytradekit.restful.okex_restful import OkexClient, SYNC_HTTP_TIMEOUT
from pytradekit.utils.dynamic_types import OkexInstrumentType


def _make_client():
    client = OkexClient.__new__(OkexClient)
    client._url = "https://www.okx.com"
    client.logger = Mock()
    return client


def test_okx_instrument_type_wire_values():
    assert OkexInstrumentType.SPOT.value == "SPOT"
    assert OkexInstrumentType.SWAP.value == "SWAP"
    assert OkexInstrumentType.FUTURES.value == "FUTURES"


def test_get_exchange_information_accepts_futures_wire_value():
    client = _make_client()
    captured = {}

    def fake_send_request(api, method="GET", params=None, use_sign=True):
        captured["api"] = api
        captured["params"] = params
        captured["use_sign"] = use_sign
        return {"code": "0", "data": []}

    client._send_request = fake_send_request
    client.get_exchange_information(OkexInstrumentType.FUTURES.value)

    assert captured["api"] == "/api/v5/public/instruments"
    assert captured["params"] == {"instType": "FUTURES"}
    assert captured["use_sign"] is False


def test_get_orderbook_l2_uses_standard_books_endpoint():
    client = _make_client()
    captured = {}

    def fake_send_request(api, method="GET", params=None, use_sign=True):
        captured["api"] = api
        captured["params"] = params
        captured["use_sign"] = use_sign
        return {"code": "0", "data": []}

    client._send_request = fake_send_request
    client.get_orderbook_l2("BTC-USDT-260925", limit=100)

    assert captured["api"] == "/api/v5/market/books"
    assert captured["params"] == {"instId": "BTC-USDT-260925", "sz": 100}
    assert captured["use_sign"] is False


def test_public_get_has_bounded_timeout():
    client = _make_client()
    client.session = Mock()
    response = Mock()
    response.status_code = 200
    response.json.return_value = {"code": "0", "data": []}
    client.session.get.return_value = response

    client._send_request(
        "/api/v5/public/instruments",
        params={"instType": "FUTURES"},
        use_sign=False,
    )

    assert client.session.get.call_args.kwargs["timeout"] == SYNC_HTTP_TIMEOUT
