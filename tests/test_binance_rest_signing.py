"""Verify Ed25519 signatures against bytes prepared by both HTTP transports."""
import asyncio
import base64
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import httpx
import pytest
import requests
from nacl.signing import SigningKey
from requests.adapters import BaseAdapter

from pytradekit.restful.binance_restful import BinancePerpClient


@pytest.fixture
def signing_client():
    key = SigningKey.generate()
    client = BinancePerpClient.__new__(BinancePerpClient)
    client.api_key = 'test-secret-value'
    client.secret_key = base64.b64encode(bytes(key)).decode('ascii')
    client.logger = Mock()
    client._url = 'https://example.invalid'
    client.get_timestamp = lambda: 1789002976000
    client.session = requests.Session()
    yield client, key.verify_key
    client.session.close()


def verify_wire_signature(payload, verification_key):
    if isinstance(payload, bytes):
        payload = payload.decode('ascii')
    unsigned, separator, signature_field = payload.rpartition('&signature=')
    assert separator, 'Signature must be the last encoded parameter'
    signature = parse_qs('signature=' + signature_field)['signature'][0]
    # Verify the unmodified wire bytes, independently of the client's encoder.
    verification_key.verify(unsigned.encode('ascii'), base64.b64decode(signature))
    return parse_qs(unsigned)


class VerifyingAdapter(BaseAdapter):
    def __init__(self, verification_key):
        self.verification_key = verification_key
        self.received = []

    def send(self, request, **kwargs):
        payload = urlsplit(request.url).query if request.method == 'GET' else request.body
        self.received.append(verify_wire_signature(payload, self.verification_key))
        response = requests.Response()
        response.status_code = 200
        response._content = b'[]'
        response.request = request
        return response

    def close(self):
        pass


PARAMETERS = [
    pytest.param({'symbol': 'BTCUSDT'}, id='ascii-symbol'),
    pytest.param({'symbol': '牛来USDT'}, id='incident-symbol'),
    pytest.param({'symbol': '币安人生USDT'}, id='second-unicode-symbol'),
    pytest.param({'symbol': 'BTCUSDT', 'origClientOrderId': 'id/+&=% value'}, id='reserved-characters'),
    pytest.param({'symbol': 'BTCUSDT', 'quantity': Decimal('0.00123000'), 'recvWindow': 3000}, id='decimal-and-window'),
]


@pytest.mark.parametrize('method', ['GET', 'POST', 'DELETE'])
@pytest.mark.parametrize('parameters', PARAMETERS)
def test_sync_signature_matches_transmitted_parameters(signing_client, method, parameters):
    client, verification_key = signing_client
    adapter = VerifyingAdapter(verification_key)
    client.session.mount(client._url, adapter)
    url, params, _ = client._make_private_url('/test/signed', dict(parameters))

    assert client.request(method, url, params=params) == []
    assert len(adapter.received) == 1
    for name, value in parameters.items():
        assert adapter.received[0][name] == [str(value)]


@pytest.mark.parametrize('method', ['GET', 'POST', 'DELETE'])
@pytest.mark.parametrize('parameters', PARAMETERS)
def test_async_signature_matches_transmitted_parameters(signing_client, method, parameters):
    client, verification_key = signing_client
    received = []

    def handle(request):
        received.append(verify_wire_signature(request.url.query, verification_key))
        return httpx.Response(200, json=[])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
            url, params, _ = client._make_private_url('/test/signed', dict(parameters))
            return await client.async_request_once(method, url, http_client=transport, params=params)

    assert asyncio.run(run()) == ([], None)
    assert len(received) == 1
    for name, value in parameters.items():
        assert received[0][name] == [str(value)]


def test_unicode_position_risk_query_uses_valid_signature(signing_client):
    client, verification_key = signing_client
    adapter = VerifyingAdapter(verification_key)
    client.session.mount(client._url, adapter)

    assert client.get_perp_position_risk(symbol='牛来USDT') == []
    assert adapter.received[0]['symbol'] == ['牛来USDT']


def test_unsigned_request_does_not_sign_or_add_auth_window(signing_client):
    client, _ = signing_client
    client._hashing = Mock(side_effect=AssertionError('Unsigned request must not sign'))

    _, params, _ = client._make_private_url('/test/public', {'symbol': '牛来USDT'}, use_sign=False)

    assert params['symbol'] == '牛来USDT'
    assert 'signature' not in params
    assert 'recvWindow' not in params
