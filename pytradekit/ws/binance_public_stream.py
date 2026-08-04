"""Asynchronous Binance public market-data streams."""

import asyncio
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)
from urllib.parse import quote

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from pytradekit.utils.clock import (
    get_monotonic_timestamp_ns,
    get_timestamp_ns,
)


RawMessage = Union[str, bytes]


class BinancePublicMarket(str, Enum):
    """Binance public WebSocket market endpoints."""

    SPOT = "spot"
    PERPETUAL = "perpetual"


class BinanceTimestampUnit(str, Enum):
    """Timestamp precision supported by Binance spot streams."""

    MILLISECOND = "MILLISECOND"
    MICROSECOND = "MICROSECOND"


@dataclass(frozen=True)
class WebSocketConnectionOptions:
    """Network and buffer settings for one WebSocket connection."""

    open_timeout_seconds: float = 10.0
    close_timeout_seconds: float = 10.0
    ping_interval_seconds: float = 20.0
    ping_timeout_seconds: float = 20.0
    max_queue: int = 1024
    max_size_bytes: int = 2**20

    def __post_init__(self) -> None:
        _validate_positive_number(self.open_timeout_seconds, "open_timeout_seconds")
        _validate_positive_number(self.close_timeout_seconds, "close_timeout_seconds")
        _validate_positive_number(self.ping_interval_seconds, "ping_interval_seconds")
        _validate_positive_number(self.ping_timeout_seconds, "ping_timeout_seconds")
        _validate_positive_integer(self.max_queue, "max_queue")
        _validate_positive_integer(self.max_size_bytes, "max_size_bytes")

    def as_connect_kwargs(self) -> Dict[str, Union[float, int]]:
        """Return options accepted by ``websockets.connect``."""
        return {
            "close_timeout": self.close_timeout_seconds,
            "ping_interval": self.ping_interval_seconds,
            "ping_timeout": self.ping_timeout_seconds,
            "max_queue": self.max_queue,
            "max_size": self.max_size_bytes,
        }


@dataclass(frozen=True)
class ReconnectOptions:
    """Exponential reconnect policy."""

    initial_delay_seconds: float = 1.0
    maximum_delay_seconds: float = 30.0
    multiplier: float = 2.0
    stable_connection_seconds: float = 30.0

    def __post_init__(self) -> None:
        _validate_positive_number(self.initial_delay_seconds, "initial_delay_seconds")
        _validate_positive_number(self.maximum_delay_seconds, "maximum_delay_seconds")
        _validate_positive_number(self.multiplier, "multiplier")
        _validate_positive_number(
            self.stable_connection_seconds,
            "stable_connection_seconds",
        )
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least one")
        if self.maximum_delay_seconds < self.initial_delay_seconds:
            raise ValueError(
                "maximum_delay_seconds must not be smaller than initial_delay_seconds"
            )

    def calculate_next_delay(self, current_delay_seconds: float) -> float:
        """Calculate the next bounded reconnect delay."""
        return min(
            current_delay_seconds * self.multiplier,
            self.maximum_delay_seconds,
        )


@dataclass(frozen=True)
class BinancePublicStreamConfig:
    """Configuration for a Binance combined public stream."""

    streams: Tuple[str, ...]
    market: BinancePublicMarket = BinancePublicMarket.SPOT
    timestamp_unit: BinanceTimestampUnit = BinanceTimestampUnit.MICROSECOND
    connection: WebSocketConnectionOptions = field(
        default_factory=WebSocketConnectionOptions
    )
    reconnect: ReconnectOptions = field(default_factory=ReconnectOptions)

    def __post_init__(self) -> None:
        _validate_enum(self.market, BinancePublicMarket, "market")
        _validate_enum(self.timestamp_unit, BinanceTimestampUnit, "timestamp_unit")
        normalized_streams = _validate_streams(self.streams)
        object.__setattr__(self, "streams", normalized_streams)


@dataclass(frozen=True)
class BinancePublicMessage:
    """One decoded Binance event with local receive timestamps."""

    stream: str
    data: Mapping[str, Any]
    raw_message: str
    received_at_ns: int
    received_monotonic_ns: int
    connection_id: str = ""


class BinancePublicStreamError(Exception):
    """Base error for Binance public streams."""


class BinancePublicStreamPayloadError(BinancePublicStreamError):
    """Raised when Binance sends an invalid combined-stream payload."""


class BinancePublicStreamDisconnected(BinancePublicStreamError):
    """Raised internally when a connection ends without a stop request."""


class WebSocketConnection(Protocol):
    """Small protocol required from a WebSocket client connection."""

    def __aiter__(self) -> AsyncIterator[RawMessage]:
        """Iterate over received frames."""

    async def close(self) -> None:
        """Close the connection."""


ConnectCallable = Callable[..., Awaitable[WebSocketConnection]]
ConnectionIdFactory = Callable[[], str]


SPOT_STREAM_BASE_URL = "wss://stream.binance.com:9443"
PERPETUAL_STREAM_BASE_URL = "wss://fstream.binance.com"


def build_binance_public_stream_url(config: BinancePublicStreamConfig) -> str:
    """Build a Binance combined-stream URL."""
    base_url = _get_base_url(config.market)
    stream_path = "/".join(quote(stream, safe="@") for stream in config.streams)
    url = f"{base_url}/stream?streams={stream_path}"
    if config.market == BinancePublicMarket.SPOT:
        url = f"{url}&timeUnit={config.timestamp_unit.value}"
    return url


def decode_binance_public_message(
    raw_message: RawMessage,
    received_at_ns: int,
    received_monotonic_ns: int,
    connection_id: str = "",
) -> BinancePublicMessage:
    """Decode and validate one Binance combined-stream message."""
    raw_text = _decode_raw_message(raw_message)
    try:
        decoded = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise BinancePublicStreamPayloadError("message is not valid JSON") from error
    stream, data = _validate_decoded_message(decoded)
    return BinancePublicMessage(
        stream=stream,
        data=data,
        raw_message=raw_text,
        received_at_ns=received_at_ns,
        received_monotonic_ns=received_monotonic_ns,
        connection_id=connection_id,
    )


def generate_connection_id() -> str:
    """Generate an opaque ID for one successful connection."""
    return uuid.uuid4().hex


class BinancePublicStream:
    """Reconnectable asynchronous iterator for Binance public events."""

    def __init__(
        self,
        config: BinancePublicStreamConfig,
        connect: ConnectCallable = websockets.connect,
        logger: logging.Logger = logging.getLogger(__name__),
        connection_id_factory: ConnectionIdFactory = generate_connection_id,
    ) -> None:
        self._config = config
        self._connect = connect
        self._logger = logger
        self._connection_id_factory = connection_id_factory
        self._stop_event = asyncio.Event()
        self._connection: Optional[WebSocketConnection] = None
        self._connections_opened = 0
        self._last_connection_duration_seconds: Optional[float] = None

    @property
    def url(self) -> str:
        """Return the configured combined-stream URL."""
        return build_binance_public_stream_url(self._config)

    @property
    def connections_opened(self) -> int:
        """Return the number of successfully opened connections."""
        return self._connections_opened

    async def events(self) -> AsyncIterator[BinancePublicMessage]:
        """Yield decoded events, reconnecting after recoverable disconnects."""
        delay = self._config.reconnect.initial_delay_seconds
        while not self._stop_event.is_set():
            try:
                async for message in self._receive_connection_events():
                    yield message
                self._raise_if_connection_ended()
            except (
                OSError,
                asyncio.TimeoutError,
                EOFError,
                ConnectionClosed,
                BinancePublicStreamDisconnected,
            ) as error:
                if self._stop_event.is_set():
                    return
                delay = self._reset_delay_after_stable_connection(delay)
                self._log_reconnect(error, delay)
                await self._wait_before_reconnect(delay)
                delay = self._config.reconnect.calculate_next_delay(delay)
            except WebSocketException as error:
                if not _is_retryable_websocket_error(error):
                    raise
                if self._stop_event.is_set():
                    return
                delay = self._reset_delay_after_stable_connection(delay)
                self._log_reconnect(error, delay)
                await self._wait_before_reconnect(delay)
                delay = self._config.reconnect.calculate_next_delay(delay)

    async def stop(self) -> None:
        """Stop reconnects and close the active connection."""
        self._stop_event.set()
        connection = self._connection
        if connection is not None:
            await connection.close()

    async def _receive_connection_events(
        self,
    ) -> AsyncIterator[BinancePublicMessage]:
        self._last_connection_duration_seconds = None
        connection = await self._open_connection()
        self._connection = connection
        connection_started_at_ns = get_monotonic_timestamp_ns()
        try:
            if self._stop_event.is_set():
                return
            connection_id = self._create_connection_id()
            self._connections_opened += 1
            async for raw_message in connection:
                if self._stop_event.is_set():
                    return
                received_at_ns = get_timestamp_ns()
                received_monotonic_ns = get_monotonic_timestamp_ns()
                yield decode_binance_public_message(
                    raw_message,
                    received_at_ns,
                    received_monotonic_ns,
                    connection_id,
                )
        finally:
            connection_ended_at_ns = get_monotonic_timestamp_ns()
            self._last_connection_duration_seconds = max(
                0.0,
                (connection_ended_at_ns - connection_started_at_ns) / 1_000_000_000,
            )
            if self._connection is connection:
                self._connection = None
            await connection.close()

    async def _open_connection(self) -> WebSocketConnection:
        connect_awaitable = self._connect(
            self.url,
            **self._config.connection.as_connect_kwargs(),
        )
        return await asyncio.wait_for(
            connect_awaitable,
            timeout=self._config.connection.open_timeout_seconds,
        )

    def _create_connection_id(self) -> str:
        connection_id = self._connection_id_factory()
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("connection_id_factory must return a non-empty string")
        return connection_id

    def _raise_if_connection_ended(self) -> None:
        if not self._stop_event.is_set():
            raise BinancePublicStreamDisconnected(
                "connection ended without a stop request"
            )

    def _reset_delay_after_stable_connection(
        self,
        current_delay_seconds: float,
    ) -> float:
        connection_duration = self._last_connection_duration_seconds
        if (
            connection_duration is not None
            and connection_duration >= self._config.reconnect.stable_connection_seconds
        ):
            return self._config.reconnect.initial_delay_seconds
        return current_delay_seconds

    async def _wait_before_reconnect(self, delay_seconds: float) -> None:
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=delay_seconds,
            )
        except asyncio.TimeoutError:
            return

    def _log_reconnect(self, error: Exception, delay_seconds: float) -> None:
        self._logger.warning(
            "Binance public stream disconnected; reconnecting in %.3fs: %s",
            delay_seconds,
            error,
        )


def _get_base_url(market: BinancePublicMarket) -> str:
    if market == BinancePublicMarket.SPOT:
        return SPOT_STREAM_BASE_URL
    return PERPETUAL_STREAM_BASE_URL


def _decode_raw_message(raw_message: RawMessage) -> str:
    if isinstance(raw_message, str):
        return raw_message
    try:
        return raw_message.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BinancePublicStreamPayloadError("message is not valid UTF-8") from error


def _validate_decoded_message(
    decoded: Any,
) -> Tuple[str, Mapping[str, Any]]:
    if not isinstance(decoded, dict):
        raise BinancePublicStreamPayloadError("message must be a JSON object")
    stream = decoded.get("stream")
    data = decoded.get("data")
    if not isinstance(stream, str) or not stream:
        raise BinancePublicStreamPayloadError("message has no valid stream name")
    if not isinstance(data, dict):
        raise BinancePublicStreamPayloadError("message has no valid data object")
    return stream, data


def _validate_streams(streams: Sequence[str]) -> Tuple[str, ...]:
    if isinstance(streams, str) or not streams:
        raise ValueError("streams must contain at least one stream name")
    normalized = tuple(streams)
    if any(not isinstance(stream, str) or not stream.strip() for stream in normalized):
        raise ValueError("every stream name must be a non-empty string")
    if len(normalized) > 1024:
        raise ValueError("streams must contain at most 1024 stream names")
    if any(stream != stream.strip() for stream in normalized):
        raise ValueError("stream names must not contain leading or trailing whitespace")
    if any("/" in stream for stream in normalized):
        raise ValueError("individual stream names must not contain '/'")
    return normalized


def _is_retryable_websocket_error(error: WebSocketException) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
    return isinstance(status_code, int) and (
        status_code == 429 or 500 <= status_code < 600
    )


def _validate_positive_number(value: float, name: str) -> None:
    if not _is_finite_number(value) or value <= 0:
        raise ValueError(f"{name} must be greater than zero")


def _validate_positive_integer(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _is_finite_number(value: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _validate_enum(value: Enum, expected_type: type, name: str) -> None:
    if not isinstance(value, expected_type):
        raise ValueError(f"{name} must be a {expected_type.__name__}")
