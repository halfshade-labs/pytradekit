"""Lightweight asynchronous client for Binance public spot REST data."""

from typing import Any, Dict, List, Optional, TypedDict, cast

import httpx


SPOT_DEPTH_URL = "https://api.binance.com/api/v3/depth"
MIN_SPOT_DEPTH_LIMIT = 1
MAX_SPOT_DEPTH_LIMIT = 5000
PUBLIC_REST_TIMEOUT = httpx.Timeout(
    timeout=10.0,
    connect=3.0,
    read=8.0,
    write=5.0,
    pool=3.0,
)


class BinanceSpotDepth(TypedDict):
    """Validated shape returned by Binance's spot depth endpoint."""

    lastUpdateId: int
    bids: List[List[str]]
    asks: List[List[str]]


class BinancePublicRestResponseError(ValueError):
    """Raised when Binance returns an unexpected successful response body."""


class BinancePublicRestClient:
    """Fetch unauthenticated Binance spot market-data snapshots."""

    def __init__(
        self,
        http_client: Optional[httpx.AsyncClient] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        if http_client is not None and transport is not None:
            raise ValueError("Provide either http_client or transport, not both")
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            timeout=PUBLIC_REST_TIMEOUT,
            transport=transport,
        )

    async def get_spot_depth(
        self,
        symbol: str,
        limit: int = 100,
    ) -> BinanceSpotDepth:
        """Return a validated spot order-book snapshot without retrying."""
        _validate_symbol(symbol)
        _validate_depth_limit(limit)
        response = await self._http_client.get(
            SPOT_DEPTH_URL,
            params={"symbol": symbol, "limit": limit},
            timeout=PUBLIC_REST_TIMEOUT,
        )
        response.raise_for_status()
        return _parse_depth_response(response)

    async def aclose(self) -> None:
        """Close only the internally owned HTTP client."""
        if self._owns_http_client:
            await self._http_client.aclose()

    async def __aenter__(self) -> "BinancePublicRestClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.aclose()


def _validate_symbol(symbol: str) -> None:
    if not isinstance(symbol, str) or not symbol or not symbol.isalnum():
        raise ValueError("symbol must be a non-empty alphanumeric string")


def _validate_depth_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if not MIN_SPOT_DEPTH_LIMIT <= limit <= MAX_SPOT_DEPTH_LIMIT:
        raise ValueError("limit must be between 1 and 5000")


def _parse_depth_response(response: httpx.Response) -> BinanceSpotDepth:
    try:
        payload = response.json()
    except ValueError as error:
        raise BinancePublicRestResponseError(
            "Binance spot depth response is not valid JSON"
        ) from error
    _validate_depth_payload(payload)
    return cast(BinanceSpotDepth, payload)


def _validate_depth_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise BinancePublicRestResponseError("Spot depth response must be an object")
    _validate_last_update_id(payload)
    _validate_depth_side(payload, "bids")
    _validate_depth_side(payload, "asks")


def _validate_last_update_id(payload: Dict[str, Any]) -> None:
    update_id = payload.get("lastUpdateId")
    if isinstance(update_id, bool) or not isinstance(update_id, int):
        raise BinancePublicRestResponseError(
            "Spot depth response has an invalid lastUpdateId"
        )


def _validate_depth_side(payload: Dict[str, Any], side: str) -> None:
    levels = payload.get(side)
    if not isinstance(levels, list):
        raise BinancePublicRestResponseError(
            "Spot depth response has invalid {}".format(side)
        )
    if not all(_is_depth_level(level) for level in levels):
        raise BinancePublicRestResponseError(
            "Spot depth response has malformed {} levels".format(side)
        )


def _is_depth_level(level: Any) -> bool:
    return (
        isinstance(level, list)
        and len(level) == 2
        and all(isinstance(value, str) for value in level)
    )
