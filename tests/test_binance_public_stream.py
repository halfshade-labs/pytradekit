import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence, Tuple, Union
from unittest.mock import patch

import pytest
from websockets.exceptions import WebSocketException

from pytradekit.ws.binance_public_stream import (
    BinancePublicMarket,
    BinancePublicMessage,
    BinancePublicStream,
    BinancePublicStreamConfig,
    BinancePublicStreamPayloadError,
    BinanceTimestampUnit,
    ReconnectOptions,
    WebSocketConnectionOptions,
    build_binance_public_stream_url,
)


RawMessage = Union[str, bytes]
ConnectOutcome = Union["FakeConnection", Exception]


class FakeConnection:
    def __init__(self, messages: Sequence[RawMessage]) -> None:
        self._messages = iter(messages)
        self.closed = False

    def __aiter__(self) -> "FakeConnection":
        return self

    async def __anext__(self) -> RawMessage:
        try:
            return next(self._messages)
        except StopIteration as error:
            raise StopAsyncIteration from error

    async def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self, outcomes: Sequence[ConnectOutcome]) -> None:
        self._outcomes = iter(outcomes)
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    async def __call__(self, url: str, **kwargs: Any) -> FakeConnection:
        self.calls.append((url, kwargs))
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeStatusError(WebSocketException):
    def __init__(self, status_code: int, response_style: bool = False) -> None:
        super().__init__(f"server rejected the handshake with {status_code}")
        if response_style:
            self.response = SimpleNamespace(status_code=status_code)
        else:
            self.status_code = status_code


class BlockingConnector:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, url: str, **kwargs: Any) -> FakeConnection:
        del url, kwargs
        self.started.set()
        await self.release.wait()
        return self.connection


@pytest.mark.parametrize(
    ("market", "timestamp_unit", "expected_suffix"),
    [
        (
            BinancePublicMarket.SPOT,
            BinanceTimestampUnit.MICROSECOND,
            "&timeUnit=MICROSECOND",
        ),
        (
            BinancePublicMarket.PERPETUAL,
            BinanceTimestampUnit.MILLISECOND,
            "",
        ),
    ],
)
def test_build_binance_public_stream_url(
    market: BinancePublicMarket,
    timestamp_unit: BinanceTimestampUnit,
    expected_suffix: str,
) -> None:
    config = BinancePublicStreamConfig(
        streams=("btcusdt@depth@100ms", "btcusdt@trade"),
        market=market,
        timestamp_unit=timestamp_unit,
    )

    url = build_binance_public_stream_url(config)

    assert "streams=btcusdt@depth@100ms/btcusdt@trade" in url
    if expected_suffix:
        assert url.endswith(expected_suffix)
    else:
        assert "timeUnit" not in url


def test_events_decode_message_and_pass_connection_options() -> None:
    async def exercise() -> Tuple[
        BinancePublicMessage, FakeConnector, FakeConnection, int
    ]:
        connection = FakeConnection(
            [b'{"stream":"btcusdt@trade","data":{"p":"123.45"}}']
        )
        connector = FakeConnector([connection])
        options = WebSocketConnectionOptions(
            open_timeout_seconds=3.0,
            close_timeout_seconds=4.0,
            ping_interval_seconds=5.0,
            ping_timeout_seconds=6.0,
            max_queue=7,
            max_size_bytes=8,
        )
        stream = BinancePublicStream(
            BinancePublicStreamConfig(
                streams=("btcusdt@trade",),
                connection=options,
            ),
            connect=connector,
            connection_id_factory=lambda: "connection-1",
        )
        generator = stream.events()
        message = await generator.__anext__()
        await stream.stop()
        await generator.aclose()
        return message, connector, connection, stream.connections_opened

    with (
        patch("pytradekit.ws.binance_public_stream.get_timestamp_ns", return_value=101),
        patch(
            "pytradekit.ws.binance_public_stream.get_monotonic_timestamp_ns",
            return_value=202,
        ),
    ):
        message, connector, connection, connections_opened = asyncio.run(exercise())

    assert message.stream == "btcusdt@trade"
    assert message.data == {"p": "123.45"}
    assert message.received_at_ns == 101
    assert message.received_monotonic_ns == 202
    assert message.connection_id == "connection-1"
    assert connections_opened == 1
    assert connector.calls[0][1] == {
        "close_timeout": 4.0,
        "ping_interval": 5.0,
        "ping_timeout": 6.0,
        "max_queue": 7,
        "max_size": 8,
    }
    assert connection.closed


def test_connection_id_changes_after_successful_reconnect() -> None:
    async def exercise() -> Tuple[List[str], int]:
        connector = FakeConnector(
            [
                FakeConnection(['{"stream":"btcusdt@trade","data":{"p":"1"}}']),
                FakeConnection(['{"stream":"btcusdt@trade","data":{"p":"2"}}']),
            ]
        )
        connection_ids = iter(("connection-1", "connection-2"))
        stream = BinancePublicStream(
            BinancePublicStreamConfig(streams=("btcusdt@trade",)),
            connect=connector,
            connection_id_factory=connection_ids.__next__,
        )

        async def skip_delay(delay_seconds: float) -> None:
            del delay_seconds

        stream._wait_before_reconnect = skip_delay
        generator = stream.events()
        first = await generator.__anext__()
        second = await generator.__anext__()
        await stream.stop()
        await generator.aclose()
        return [first.connection_id, second.connection_id], stream.connections_opened

    observed_ids, connections_opened = asyncio.run(exercise())

    assert observed_ids == ["connection-1", "connection-2"]
    assert connections_opened == 2


def test_events_use_exponential_reconnect_delays() -> None:
    async def exercise() -> Tuple[List[float], int]:
        connection = FakeConnection(['{"stream":"btcusdt@bookTicker","data":{"u":1}}'])
        connector = FakeConnector([OSError("first"), OSError("second"), connection])
        stream = BinancePublicStream(
            BinancePublicStreamConfig(
                streams=("btcusdt@bookTicker",),
                reconnect=ReconnectOptions(
                    initial_delay_seconds=1.0,
                    maximum_delay_seconds=8.0,
                    multiplier=2.0,
                ),
            ),
            connect=connector,
        )
        delays: List[float] = []

        async def record_delay(delay_seconds: float) -> None:
            delays.append(delay_seconds)

        stream._wait_before_reconnect = record_delay
        generator = stream.events()
        await generator.__anext__()
        await stream.stop()
        await generator.aclose()
        return delays, len(connector.calls)

    delays, call_count = asyncio.run(exercise())

    assert delays == [1.0, 2.0]
    assert call_count == 3


def test_message_on_flapping_connection_does_not_reset_backoff() -> None:
    async def exercise() -> List[float]:
        connector = FakeConnector(
            [
                OSError("initial failure"),
                FakeConnection(['{"stream":"btcusdt@trade","data":{"p":"1"}}']),
                FakeConnection(['{"stream":"btcusdt@trade","data":{"p":"2"}}']),
            ]
        )
        stream = BinancePublicStream(
            BinancePublicStreamConfig(
                streams=("btcusdt@trade",),
                reconnect=ReconnectOptions(
                    initial_delay_seconds=1.0,
                    maximum_delay_seconds=8.0,
                    multiplier=2.0,
                    stable_connection_seconds=30.0,
                ),
            ),
            connect=connector,
        )
        delays: List[float] = []

        async def record_delay(delay_seconds: float) -> None:
            delays.append(delay_seconds)

        stream._wait_before_reconnect = record_delay
        generator = stream.events()
        await generator.__anext__()
        await generator.__anext__()
        await stream.stop()
        await generator.aclose()
        return delays

    with patch(
        "pytradekit.ws.binance_public_stream.get_monotonic_timestamp_ns",
        return_value=0,
    ):
        delays = asyncio.run(exercise())

    assert delays == [1.0, 2.0]


def test_stable_connection_resets_backoff() -> None:
    async def exercise() -> List[float]:
        connector = FakeConnector(
            [
                OSError("initial failure"),
                FakeConnection(['{"stream":"btcusdt@trade","data":{"p":"1"}}']),
                FakeConnection(['{"stream":"btcusdt@trade","data":{"p":"2"}}']),
            ]
        )
        stream = BinancePublicStream(
            BinancePublicStreamConfig(
                streams=("btcusdt@trade",),
                reconnect=ReconnectOptions(
                    initial_delay_seconds=1.0,
                    maximum_delay_seconds=8.0,
                    multiplier=2.0,
                    stable_connection_seconds=10.0,
                ),
            ),
            connect=connector,
        )
        delays: List[float] = []

        async def record_delay(delay_seconds: float) -> None:
            delays.append(delay_seconds)

        stream._wait_before_reconnect = record_delay
        generator = stream.events()
        await generator.__anext__()
        await generator.__anext__()
        await stream.stop()
        await generator.aclose()
        return delays

    monotonic_timestamps = iter(
        (
            0,
            1_000_000_000,
            11_000_000_000,
            12_000_000_000,
            13_000_000_000,
            14_000_000_000,
        )
    )
    with patch(
        "pytradekit.ws.binance_public_stream.get_monotonic_timestamp_ns",
        side_effect=monotonic_timestamps.__next__,
    ):
        delays = asyncio.run(exercise())

    assert delays == [1.0, 1.0]


@pytest.mark.parametrize(
    ("status_code", "response_style"),
    [(429, False), (503, True)],
)
def test_events_retry_transient_handshake_status(
    status_code: int,
    response_style: bool,
) -> None:
    async def exercise() -> Tuple[List[float], int]:
        connector = FakeConnector(
            [
                FakeStatusError(status_code, response_style=response_style),
                FakeConnection(['{"stream":"btcusdt@trade","data":{"p":"1"}}']),
            ]
        )
        stream = BinancePublicStream(
            BinancePublicStreamConfig(streams=("btcusdt@trade",)),
            connect=connector,
        )
        delays: List[float] = []

        async def record_delay(delay_seconds: float) -> None:
            delays.append(delay_seconds)

        stream._wait_before_reconnect = record_delay
        generator = stream.events()
        await generator.__anext__()
        await stream.stop()
        await generator.aclose()
        return delays, len(connector.calls)

    delays, call_count = asyncio.run(exercise())

    assert delays == [1.0]
    assert call_count == 2


def test_events_retry_eof_during_handshake() -> None:
    async def exercise() -> int:
        connector = FakeConnector(
            [
                EOFError("connection closed during handshake"),
                FakeConnection(['{"stream":"btcusdt@trade","data":{"p":"1"}}']),
            ]
        )
        stream = BinancePublicStream(
            BinancePublicStreamConfig(streams=("btcusdt@trade",)),
            connect=connector,
        )

        async def skip_delay(delay_seconds: float) -> None:
            del delay_seconds

        stream._wait_before_reconnect = skip_delay
        generator = stream.events()
        await generator.__anext__()
        await stream.stop()
        await generator.aclose()
        return len(connector.calls)

    assert asyncio.run(exercise()) == 2


def test_events_propagate_permanent_handshake_status() -> None:
    async def exercise() -> None:
        connector = FakeConnector([FakeStatusError(400)])
        stream = BinancePublicStream(
            BinancePublicStreamConfig(streams=("btcusdt@trade",)),
            connect=connector,
        )
        generator = stream.events()
        with pytest.raises(FakeStatusError):
            await generator.__anext__()
        await generator.aclose()

    asyncio.run(exercise())


def test_stop_while_connection_is_opening_closes_new_connection() -> None:
    async def exercise() -> Tuple[bool, int]:
        connection = FakeConnection([])
        connector = BlockingConnector(connection)
        stream = BinancePublicStream(
            BinancePublicStreamConfig(streams=("btcusdt@trade",)),
            connect=connector,
        )
        generator = stream.events()
        pending_message = asyncio.create_task(generator.__anext__())
        await connector.started.wait()
        await stream.stop()
        connector.release.set()
        with pytest.raises(StopAsyncIteration):
            await pending_message
        return connection.closed, stream.connections_opened

    connection_closed, connections_opened = asyncio.run(exercise())

    assert connection_closed
    assert connections_opened == 0


def test_events_reject_invalid_combined_stream_payload() -> None:
    async def exercise() -> None:
        connector = FakeConnector([FakeConnection(["[]"])])
        stream = BinancePublicStream(
            BinancePublicStreamConfig(streams=("btcusdt@trade",)),
            connect=connector,
        )
        generator = stream.events()
        with pytest.raises(BinancePublicStreamPayloadError):
            await generator.__anext__()
        await generator.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "streams",
    [
        (),
        ("",),
        (" btcusdt@trade",),
        ("btcusdt@trade ",),
        ("btcusdt@trade/ethusdt@trade",),
        ("btcusdt@trade",) * 1025,
    ],
)
def test_config_rejects_invalid_streams(streams: Tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        BinancePublicStreamConfig(streams=streams)
