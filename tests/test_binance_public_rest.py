import asyncio

import httpx
import pytest

from pytradekit.restful.binance_public_rest import (
    BinancePublicRestClient,
    BinancePublicRestResponseError,
)


DEPTH_PAYLOAD = {
    "lastUpdateId": 42,
    "bids": [["100.10", "1.25"]],
    "asks": [["100.20", "2.50"]],
}


def test_get_spot_depth_uses_public_endpoint_and_explicit_timeouts():
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, json=DEPTH_PAYLOAD)

    async def scenario():
        client = BinancePublicRestClient(transport=httpx.MockTransport(handler))
        try:
            return await client.get_spot_depth("BTCFDUSD", 1000)
        finally:
            await client.aclose()

    result = asyncio.run(scenario())
    request = captured["request"]
    assert result == DEPTH_PAYLOAD
    assert request.url.path == "/api/v3/depth"
    assert dict(request.url.params) == {"symbol": "BTCFDUSD", "limit": "1000"}
    assert "X-MBX-APIKEY" not in request.headers
    assert request.extensions["timeout"] == {
        "connect": 3.0,
        "read": 8.0,
        "write": 5.0,
        "pool": 3.0,
    }


@pytest.mark.parametrize("limit", [0, 5001, -1])
def test_get_spot_depth_rejects_out_of_range_limit(limit):
    async def scenario():
        client = BinancePublicRestClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ValueError, match="between 1 and 5000"):
                await client.get_spot_depth("BTCUSDT", limit)
        finally:
            await client.aclose()

    def handler(request):
        raise AssertionError("Invalid input must not issue an HTTP request")

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [True, 100.0, "100"])
def test_get_spot_depth_rejects_non_integer_limit(limit):
    async def scenario():
        client = BinancePublicRestClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(TypeError, match="integer"):
                await client.get_spot_depth("BTCUSDT", limit)
        finally:
            await client.aclose()

    def handler(request):
        raise AssertionError("Invalid input must not issue an HTTP request")

    asyncio.run(scenario())


@pytest.mark.parametrize("symbol", ["", "BTC/USDT", 123])
def test_get_spot_depth_rejects_invalid_symbol(symbol):
    async def scenario():
        client = BinancePublicRestClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ValueError, match="symbol"):
                await client.get_spot_depth(symbol)
        finally:
            await client.aclose()

    def handler(request):
        raise AssertionError("Invalid input must not issue an HTTP request")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"bids": [], "asks": []},
        {"lastUpdateId": True, "bids": [], "asks": []},
        {"lastUpdateId": 1, "bids": {}, "asks": []},
        {"lastUpdateId": 1, "bids": [["1"]], "asks": []},
        {"lastUpdateId": 1, "bids": [], "asks": [[1, "2"]]},
    ],
)
def test_get_spot_depth_rejects_malformed_success_payload(payload):
    def handler(request):
        return httpx.Response(200, json=payload)

    async def scenario():
        client = BinancePublicRestClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(BinancePublicRestResponseError):
                await client.get_spot_depth("BTCUSDT")
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_get_spot_depth_propagates_http_error_without_retry():
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(429, json={"code": -1003})

    async def scenario():
        client = BinancePublicRestClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_spot_depth("BTCUSDT")
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert call_count == 1


def test_aclose_does_not_close_injected_client():
    async def scenario():
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=DEPTH_PAYLOAD)
            )
        )
        client = BinancePublicRestClient(http_client=http_client)
        await client.aclose()
        assert not http_client.is_closed
        await http_client.aclose()

    asyncio.run(scenario())


def test_get_spot_depth_propagates_cancellation_without_retry():
    calls = 0
    started = asyncio.Event()
    never = asyncio.Event()

    async def handler(request):
        nonlocal calls
        calls += 1
        started.set()
        await never.wait()
        return httpx.Response(200, json=DEPTH_PAYLOAD)

    async def scenario():
        client = BinancePublicRestClient(transport=httpx.MockTransport(handler))
        task = asyncio.create_task(client.get_spot_depth("BTCUSDT"))
        await started.wait()
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert calls == 1
