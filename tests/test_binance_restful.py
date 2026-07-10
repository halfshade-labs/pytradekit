import asyncio

import pytest
from unittest.mock import Mock, AsyncMock

from pytradekit.restful.binance_restful import BinanceClient
from pytradekit.utils.exceptions import MinNotionalException, LotSizeException


class FakeResp:
    """Minimal stand-in for an httpx Response used by async_request."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b""
        self.headers = {}

    def json(self):
        return self._payload


def _make_client():
    # Bypass __init__: it decrypts an Ed25519 PEM key, which is irrelevant here.
    # async_request only needs self.logger and self.api_key.
    client = BinanceClient.__new__(BinanceClient)
    client.logger = Mock()
    client.api_key = "test-api-key"
    return client


def _run_request(client):
    # http_client truthy -> async_request uses self.requests_result (mocked), no real network.
    return asyncio.new_event_loop().run_until_complete(
        client.async_request("POST", "http://example/order", http_client=object())
    )


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("Filter failure: MIN_NOTIONAL", MinNotionalException.__name__),
        ("Filter failure: NOTIONAL", MinNotionalException.__name__),
        ("Filter failure: LOT_SIZE", LotSizeException.__name__),
        ("Filter failure: MARKET_LOT_SIZE", LotSizeException.__name__),
    ],
)
def test_bn_1013_classified_by_msg(msg, expected):
    client = _make_client()
    client.requests_result = AsyncMock(return_value=FakeResp({"code": -1013, "msg": msg}))
    data, err = _run_request(client)
    assert data is None
    assert err == expected


def test_bn_1013_other_filter_surfaces_raw_msg():
    client = _make_client()
    client.requests_result = AsyncMock(
        return_value=FakeResp({"code": -1013, "msg": "Filter failure: PRICE_FILTER"})
    )
    data, err = _run_request(client)
    assert data is None
    assert "PRICE_FILTER" in err
    assert err not in (MinNotionalException.__name__, LotSizeException.__name__)


def test_get_perp_user_trades_builds_params():
    client = _make_client()
    captured = {}

    def fake_make_private_url(url_path, params, **kwargs):
        captured["url_path"] = url_path
        captured["params"] = dict(params)
        return f"https://fapi.binance.com{url_path}", params, 0

    client._make_private_url = fake_make_private_url
    client.request = Mock(return_value=[{"commission": "0.13", "commissionAsset": "USDT"}])

    out = client.get_perp_user_trades("ZECUSDT", order_id=123, limit=50)

    assert captured["url_path"] == "/fapi/v1/userTrades"
    assert captured["params"]["symbol"] == "ZECUSDT"
    assert captured["params"]["orderId"] == 123
    assert captured["params"]["limit"] == 50
    assert out[0]["commissionAsset"] == "USDT"


def test_get_perp_klines_builds_public_url():
    client = _make_client()
    client._url = "https://fapi.binance.com"
    captured = {}

    def fake_request(method, url, params=None, use_sign=True):
        captured["method"] = method
        captured["url"] = url
        captured["params"] = params
        captured["use_sign"] = use_sign
        return [[1700000000000, "1.0", "1.1", "0.9", "1.05", "100", 1700028799999]]

    client.request = fake_request

    out = client.get_perp_klines("ZECUSDT", "8h", start_time=1700000000000, limit=1000)

    assert "/fapi/v1/klines" in captured["url"]
    assert "symbol=ZECUSDT" in captured["url"]
    assert "interval=8h" in captured["url"]
    assert "startTime=1700000000000" in captured["url"]
    assert "limit=1000" in captured["url"]
    # params must NOT be passed again — requests would duplicate the query
    # and Binance rejects with -1101 "Duplicate values for parameter".
    assert captured["params"] is None
    assert captured["use_sign"] is False
    assert out[0][4] == "1.05"


def test_get_perp_funding_rate_sends_filter_params():
    client = _make_client()
    client._url = "https://fapi.binance.com"
    client.get_timestamp = lambda: 0
    captured = {}

    def fake_request(method, url, params=None, use_sign=True):
        captured["params"] = dict(params or {})
        return []

    client.request = fake_request
    client.get_perp_funding_rate(symbol="ZECUSDT", start_time=123, end_time=456, limit=1000)

    # _make_private_url keeps the query OUT of the url; params must be passed
    # through or the API silently returns the global latest-100 rows.
    assert captured["params"]["symbol"] == "ZECUSDT"
    assert captured["params"]["startTime"] == 123
    assert captured["params"]["endTime"] == 456
    assert captured["params"]["limit"] == 1000


def test_get_perp_ticker_price_sends_symbol_param():
    client = _make_client()
    client._url = "https://fapi.binance.com"
    client.get_timestamp = lambda: 0
    captured = {}

    def fake_request(method, url, params=None, use_sign=True):
        captured["params"] = dict(params or {})
        return {}

    client.request = fake_request
    client.get_perp_ticker_price(symbol="ZECUSDT")
    assert captured["params"]["symbol"] == "ZECUSDT"


def test_get_klines_does_not_duplicate_query_params():
    client = _make_client()
    client._url = "https://api.binance.com"
    captured = {}

    def fake_request(method, url, params=None, use_sign=True):
        captured["url"] = url
        captured["params"] = params
        captured["use_sign"] = use_sign
        return []

    client.request = fake_request
    client.get_klines("ZECUSDT", "8h", from_date=1700000000, to_date=1700100000)

    assert "symbol=ZECUSDT" in captured["url"]
    assert captured["params"] is None
    assert captured["use_sign"] is False


def test_bn_success_passthrough():
    client = _make_client()
    payload = {"orderId": 123, "status": "FILLED"}
    client.requests_result = AsyncMock(return_value=FakeResp(payload))
    data, err = _run_request(client)
    assert err is None
    assert data == payload


class TestRecvWindow:
    """Signed requests must carry recvWindow inside the signed payload;
    unsigned requests must not (BN rejects unknown params on some public
    endpoints and the constant was previously dead code — requests ran on
    the 5000ms server default and died with -1021 under latency spikes)."""

    def _client_with_capture(self):
        client = BinanceClient.__new__(BinanceClient)
        client.logger = Mock()
        client._url = "https://api.binance.com"
        captured = {}

        def fake_hashing(payload):
            captured['payload'] = payload
            return "sig"

        client._hashing = fake_hashing
        return client, captured

    def test_signed_request_includes_recv_window_in_payload(self):
        from pytradekit.restful.binance_restful import RECVWINDOW
        client, captured = self._client_with_capture()
        _, params, _ = client._make_private_url("/api/v3/order", {"symbol": "BTCUSDT"})
        assert params['recvWindow'] == RECVWINDOW
        assert f"recvWindow={RECVWINDOW}" in captured['payload']
        # signature computed over payload that already contains recvWindow
        assert captured['payload'].index('recvWindow') < captured['payload'].index('timestamp')

    def test_caller_override_is_respected(self):
        client, captured = self._client_with_capture()
        _, params, _ = client._make_private_url("/api/v3/order", {"recvWindow": 3000})
        assert params['recvWindow'] == 3000
        assert "recvWindow=3000" in captured['payload']

    def test_unsigned_request_has_no_recv_window(self):
        client, _ = self._client_with_capture()
        _, params, _ = client._make_private_url("/api/v3/exchangeInfo", {}, use_sign=False)
        assert 'recvWindow' not in params


class TestCancelOrderWiring:
    """Live probe on 2026-07-10 caught -2014 on every async cancel:
    http_client was passed positionally into use_sign, dropping the
    X-MBX-APIKEY header; and the inherited perp cancel hit the spot
    path against the fapi base. Same bug family as #87/#88."""

    def _wire(self, client):
        client._url = "https://example"
        client._hashing = lambda payload: "sig"
        captured = {}

        async def fake_async_request(method, url, use_sign=True, http_client=None, params=None):
            captured.update(method=method, url=url, use_sign=use_sign, params=params)
            return {"status": "CANCELED"}, None

        client.async_request = fake_async_request
        return captured

    def test_spot_cancel_keeps_api_key_header(self):
        client = _make_client()
        captured = self._wire(client)
        asyncio.new_event_loop().run_until_complete(
            client.cancel_order("BTCUSDT", client_order_id="cid-1")
        )
        assert captured['use_sign'] is True
        assert captured['method'] == 'DELETE'
        assert '/api/v3/order' in captured['url']

    def test_perp_cancel_uses_fapi_path(self):
        from pytradekit.restful.binance_restful import BinancePerpClient
        client = BinancePerpClient.__new__(BinancePerpClient)
        client.logger = Mock()
        client.api_key = "k"
        captured = self._wire(client)
        asyncio.new_event_loop().run_until_complete(
            client.cancel_order("DOGEUSDT", order_id=123)
        )
        assert captured['use_sign'] is True
        assert '/fapi/v1/order' in captured['url']
        assert captured['params']['orderId'] == 123
