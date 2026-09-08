"""Single-attempt, cancellable exchange requests with exact signed wire bytes."""

import asyncio
import base64
import hashlib
import hmac
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from pytradekit.restful.binance_restful import BinanceClient
from pytradekit.restful.okex_restful import OkexClient


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['GET', 'POST'])
async def test_okx_signature_matches_actual_wire_and_uses_supplied_pool(method):
    exchange = OkexClient(Mock(), key='test-key', secret='test-secret', passphrase='test-passphrase')
    observed = []

    def handler(request):
        observed.append(request)
        timestamp = request.headers['OK-ACCESS-TIMESTAMP']
        signed = timestamp.encode() + method.encode() + request.url.raw_path
        signed += request.content if method == 'POST' else b''
        signature = base64.b64encode(hmac.new(b'test-secret', signed, hashlib.sha256).digest()).decode()
        assert request.headers['OK-ACCESS-SIGN'] == signature
        assert request.headers['OK-ACCESS-KEY'] == 'test-key'
        return httpx.Response(200, json={'code': '0', 'data': []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as pool:
        with patch('pytradekit.restful.okex_restful.httpx.AsyncClient', side_effect=AssertionError('must reuse pool')):
            result = await exchange.async_send_request('/api/v5/trade/order', method=method,
                params={'instId': 'BTC-USDT', 'clOrdId': 'a b/+'}, http_client=pool)
        assert result['code'] == '0'
        assert not pool.is_closed
    assert len(observed) == 1
    if method == 'POST':
        assert observed[0].content == b'{"instId": "BTC-USDT", "clOrdId": "a b/+"}'


@pytest.mark.asyncio
async def test_okx_cancelled_request_is_not_replayed():
    exchange = OkexClient(Mock(), key='test-key', secret='test-secret', passphrase='test-passphrase')
    started = asyncio.Event()
    finished = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as pool:
        task = asyncio.create_task(exchange.async_send_request('/api/v5/trade/order', method='POST',
            params={'clOrdId': 'cancelled1'}, http_client=pool))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('order_type', ['market', 'limit'])
async def test_okx_async_order_helpers_preserve_exact_client_id_and_sizing(order_type):
    exchange = OkexClient(Mock(), key='test-key', secret='test-secret', passphrase='test-passphrase')
    calls = []

    def handler(request):
        import json
        calls.append(request)
        expected = {'instId': 'BTC-USDT', 'tdMode': 'cash', 'side': 'sell',
                    'ordType': order_type, 'sz': '0.01', 'clOrdId': 'identity123'}
        expected.update({'tgtCcy': 'base_ccy'} if order_type == 'market' else {'px': '100000'})
        assert json.loads(request.content) == expected
        assert request.method == 'POST' and request.url.path == '/api/v5/trade/order'
        return httpx.Response(200, json={'code': '0', 'data': [{'ordId': '12'}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as pool:
        if order_type == 'market':
            await exchange.async_place_spot_market_order('BTC-USDT', 'sell', '0.01',
                client_order_id='identity123', target_currency='base_ccy', http_client=pool)
        else:
            await exchange.async_place_spot_limit_order('BTC-USDT', 'sell', '0.01', '100000',
                client_order_id='identity123', http_client=pool)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_binance_rate_limit_wait_is_async_and_does_not_replay():
    exchange = BinanceClient(Mock(), key='test-key')
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={'Retry-After': '2'}, json={'code': -1003})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as pool:
        with patch('pytradekit.restful.binance_restful.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with patch('pytradekit.restful.binance_restful.time.sleep', side_effect=AssertionError('blocking sleep')):
                result, error = await exchange.async_request_once('POST', 'https://example.test/order', http_client=pool)
    sleep.assert_awaited_once_with(2)
    assert result is None and error
    assert len(requests) == 1
