"""Real HTTP counterexamples for the signed GET retry review findings."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
import json
from threading import Thread
from typing import Any, Dict, Iterator, Tuple

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import pytest
from urllib3.util.retry import Retry

from pytradekit.restful.binance_restful import (
    BinancePerpClient,
    _SIGNED_GET_TRANSPORT_HEADER,
    _SIGNED_GET_TRANSPORT_VALUE,
)
from pytradekit.utils import time_handler
from pytradekit.utils.exceptions import ExchangeException
from tests.test_binance_signed_get_retry import (
    EXPECTED_RECV_WINDOW_MS,
    INITIAL_TIMESTAMP_MS,
    MAX_REPORTED_ATTEMPTS,
    POSITION_RISK_PATH,
    SERVER_JOIN_TIMEOUT_SECONDS,
    TEST_SYMBOL,
    _LoopbackServer,
    _PolicyScenario,
    _RequestHandler,
    _create_signing_client,
)


EVENT_FIELDS = (
    "attempt", "method", "path", "timestamp", "recvWindow",
    "signature_verified", "status",
)
CONTROLLED_WAIT_MS = 11000


@pytest.fixture
def review_client() -> Iterator[Tuple[BinancePerpClient, Ed25519PublicKey]]:
    client, verification_key = _create_signing_client()
    client.session.trust_env = False
    try:
        yield client, verification_key
    finally:
        client.session.close()


@pytest.mark.parametrize("status", [429, 413])
def test_signed_retry_after_status_recovers_with_fresh_signature(
    review_client, monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    client, verification_key = review_client
    scenario = _ReviewScenario(verification_key, {
        "responses": [(status, {"Retry-After": "1"}), (200, {})],
    })
    monkeypatch.setattr(time_handler, "get_timestamp_ms", scenario.read_timestamp)
    with _serve_review_scenario(scenario) as base_url:
        client._url = base_url
        _assert_position_query_recovers(client, scenario)
    _assert_signed_attempts(scenario, (status, 200))


def test_signed_transient_headers_retry_before_reading_truncated_body(
    review_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, verification_key = review_client
    scenario = _ReviewScenario(verification_key, {
        "responses": [(503, {}), (200, {})], "truncate_first_body": True,
    })
    monkeypatch.setattr(time_handler, "get_timestamp_ms", scenario.read_timestamp)
    with _serve_review_scenario(scenario) as base_url:
        client._url = base_url
        _assert_position_query_recovers(client, scenario)
    _assert_signed_attempts(scenario, (503, 200))


def test_unsigned_get_with_ambient_api_header_keeps_fallback_status_retry(
    review_client,
) -> None:
    client, verification_key = review_client
    client.session.headers["X-MBX-APIKEY"] = "review-ambient-test-only"
    scenario = _ReviewScenario(verification_key, {
        "responses": [(503, {}), (200, {})], "require_signature": False,
    })
    with _serve_review_scenario(scenario) as base_url:
        try:
            result = client.request(
                "GET", base_url + "/test/public", params={"symbol": TEST_SYMBOL},
                use_sign=False,
            )
        except ExchangeException:
            pytest.fail("Unsigned GET did not recover: " + _wire_summary(scenario), pytrace=False)
        assert result == [], _wire_summary(scenario)
    assert [event["status"] for event in scenario.events] == [503, 200]
    assert not any(event["signature_verified"] for event in scenario.events)
    assert scenario.business_parameters_unchanged


def test_signed_get_signs_after_retry_after_wait_crosses_window(
    review_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, verification_key = review_client
    scenario = _ReviewScenario(verification_key, {
        "responses": [(503, {"Retry-After": "11"}), (200, {})],
        "advance_after_request_ms": 0,
    })
    wait_steps = []

    def controlled_wait(retry: Retry, response: Any = None) -> None:
        assert retry.get_retry_after(response) == 11
        before_wait = scenario.read_timestamp()
        scenario.timestamp_ms += CONTROLLED_WAIT_MS
        wait_steps.append((before_wait, scenario.read_timestamp()))

    monkeypatch.setattr(time_handler, "get_timestamp_ms", scenario.read_timestamp)
    monkeypatch.setattr(Retry, "sleep", controlled_wait)
    with _serve_review_scenario(scenario) as base_url:
        client._url = base_url
        _assert_position_query_recovers(client, scenario)
    assert wait_steps == [(INITIAL_TIMESTAMP_MS, INITIAL_TIMESTAMP_MS + CONTROLLED_WAIT_MS)]
    _assert_signed_attempts(scenario, (503, 200))
    assert [event["timestamp"] for event in scenario.events] == [
        INITIAL_TIMESTAMP_MS, INITIAL_TIMESTAMP_MS + CONTROLLED_WAIT_MS,
    ]


def _assert_position_query_recovers(client: BinancePerpClient, scenario: Any) -> None:
    try:
        result = client.get_perp_position_risk(symbol=TEST_SYMBOL)
    except ExchangeException:
        pytest.fail("Signed GET did not recover: " + _wire_summary(scenario), pytrace=False)
    assert result == [], _wire_summary(scenario)


def _assert_signed_attempts(scenario: Any, statuses: Tuple[int, ...]) -> None:
    events = scenario.events
    assert [event["status"] for event in events] == list(statuses), _wire_summary(scenario)
    assert all(event["signature_verified"] for event in events), _wire_summary(scenario)
    assert all(event["method"] == "GET" for event in events)
    assert all(event["path"] == POSITION_RISK_PATH for event in events)
    assert all(event["recvWindow"] == EXPECTED_RECV_WINDOW_MS for event in events)
    timestamps = [event["timestamp"] for event in events]
    assert timestamps == sorted(set(timestamps)), _wire_summary(scenario)
    assert scenario.business_parameters_unchanged


def _wire_summary(scenario: Any) -> str:
    events = [
        {name: event[name] for name in EVENT_FIELDS}
        for event in scenario.events[:MAX_REPORTED_ATTEMPTS]
    ]
    return json.dumps(events, separators=(",", ":"))


class _ReviewScenario(_PolicyScenario):
    def __init__(self, verification_key: Ed25519PublicKey, config: Dict[str, Any]) -> None:
        super().__init__(verification_key, config)
        self.require_signature = config.get("require_signature", True)
        self.truncate_first_body = config.get("truncate_first_body", False)
        self.advance_after_request_ms = config.get(
            "advance_after_request_ms", EXPECTED_RECV_WINDOW_MS + 1,
        )

    def receive_request(self, request: BaseHTTPRequestHandler) -> Tuple[int, dict, bytes]:
        server_timestamp = self.timestamp_ms
        status, headers, body = super().receive_request(request)
        self.timestamp_ms = server_timestamp + self.advance_after_request_ms
        event = self.events[-1]
        if self.require_signature and not event["signature_verified"]:
            status, body = 400, b'{"code":-1022,"msg":"Invalid signature"}'
        elif self.require_signature and (
            event["recvWindow"] != EXPECTED_RECV_WINDOW_MS
            or server_timestamp - event["timestamp"] > event["recvWindow"]
            or event["timestamp"] >= server_timestamp + 1000
        ):
            status, body = 400, b'{"code":-1021,"msg":"Expired timestamp"}'
        event["status"] = status
        return status, headers, body


class _ReviewRequestHandler(_RequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        status, headers, body = self.server.scenario.receive_request(self)
        truncated = (self.server.scenario.truncate_first_body
                     and len(self.server.scenario.events) == 1)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        if truncated:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        try:
            # Close midway through an eight-byte chunk; the recovery response
            # comes from a separate real request with a complete JSON body.
            self.wfile.write(b"8\r\npart" if truncated else body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


@contextmanager
def _serve_review_scenario(scenario: _ReviewScenario) -> Iterator[str]:
    server = _LoopbackServer(scenario)
    server.RequestHandlerClass = _ReviewRequestHandler
    thread = Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
    )
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=SERVER_JOIN_TIMEOUT_SECONDS)
        assert not thread.is_alive(), "Review HTTP server did not stop"


def test_private_transport_marker_stays_local_and_unsigned_calls_ignore_ambient_marker(
    review_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, verification_key = review_client
    client.session.headers[_SIGNED_GET_TRANSPORT_HEADER] = _SIGNED_GET_TRANSPORT_VALUE
    client.session.headers["X-MBX-APIKEY"] = "review-ambient-test-only"
    signed = _MarkerReviewScenario(verification_key, {
        "responses": [(503, {}), (200, {})],
    })
    monkeypatch.setattr(time_handler, "get_timestamp_ms", signed.read_timestamp)
    with _serve_review_scenario(signed) as base_url:
        client._url = base_url
        _assert_position_query_recovers(client, signed)
    _assert_signed_attempts(signed, (503, 200))
    assert signed.wire_marker_absent

    unsigned = _MarkerReviewScenario(verification_key, {
        "responses": [(503, {}), (200, {})], "require_signature": False,
    })
    with _serve_review_scenario(unsigned) as base_url:
        assert client.request(
            "GET", base_url + "/test/public", params={"symbol": TEST_SYMBOL},
            use_sign=False,
        ) == [], _wire_summary(unsigned)
    assert unsigned.wire_marker_absent
    assert [event["status"] for event in unsigned.events] == [503, 200]
    assert not any(event["signature_verified"] for event in unsigned.events)
    ambient_marker_unchanged = (
        client.session.headers.get(_SIGNED_GET_TRANSPORT_HEADER)
        == _SIGNED_GET_TRANSPORT_VALUE
    )
    assert ambient_marker_unchanged


def test_final_200_truncated_body_fails_once_closes_response_and_allows_next_call(
    review_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, verification_key = review_client
    scenario = _ReviewScenario(verification_key, {
        "responses": [(200, {}), (200, {})], "truncate_first_body": True,
    })
    raw_responses = []

    def observe_response(response: Any, **kwargs: Any) -> None:
        raw_responses.append(response.raw)

    client.session.hooks["response"].append(observe_response)
    monkeypatch.setattr(time_handler, "get_timestamp_ms", scenario.read_timestamp)
    with _serve_review_scenario(scenario) as base_url:
        client._url = base_url
        with pytest.raises(ExchangeException):
            client.get_perp_position_risk(symbol=TEST_SYMBOL)
        assert [event["status"] for event in scenario.events] == [200], _wire_summary(scenario)
        assert len(raw_responses) == 1
        assert raw_responses[0].closed
        diagnostics = repr(client.logger.mock_calls)
        assert "signature=" not in diagnostics
        assert client.api_key not in diagnostics
        _assert_position_query_recovers(client, scenario)
    _assert_signed_attempts(scenario, (200, 200))
    assert len(raw_responses) == 2
    assert all(raw.closed for raw in raw_responses)


class _MarkerReviewScenario(_ReviewScenario):
    def __init__(self, verification_key: Ed25519PublicKey, config: Dict[str, Any]) -> None:
        super().__init__(verification_key, config)
        self.wire_marker_absent = True

    def receive_request(self, request: BaseHTTPRequestHandler) -> Tuple[int, dict, bytes]:
        self.wire_marker_absent &= _SIGNED_GET_TRANSPORT_HEADER not in request.headers
        return super().receive_request(request)


def test_final_200_body_read_timeout_fails_once_and_closes_response(
    review_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, verification_key = review_client
    scenario = _BodyTimeoutScenario(verification_key)
    raw_responses = []

    def observe_response(response: Any, **kwargs: Any) -> None:
        raw_responses.append(response.raw)

    client.session.hooks["response"].append(observe_response)
    monkeypatch.setattr(time_handler, "get_timestamp_ms", scenario.read_timestamp)
    monkeypatch.setattr(
        "pytradekit.restful.binance_restful.SYNC_HTTP_TIMEOUT", (1, 0.25),
    )
    with _serve_body_timeout_scenario(scenario) as base_url:
        client._url = base_url
        with pytest.raises(ExchangeException) as captured:
            client.get_perp_position_risk(symbol=TEST_SYMBOL)
        assert scenario.headers_flushed.is_set()
        assert not scenario.first_body_finished.is_set()
        assert [event["status"] for event in scenario.events] == [200], _wire_summary(scenario)
        assert len(raw_responses) == 1
        assert raw_responses[0].closed
        diagnostics = repr(client.logger.mock_calls) + str(captured.value.note)
        assert "ConnectionError" in diagnostics
        assert "signature=" not in diagnostics
        assert client.api_key not in diagnostics
        assert _SIGNED_GET_TRANSPORT_HEADER not in diagnostics
        assert captured.value.__suppress_context__
        scenario.release_body.set()
        assert scenario.first_body_finished.wait(timeout=SERVER_JOIN_TIMEOUT_SECONDS)
        _assert_position_query_recovers(client, scenario)
    _assert_signed_attempts(scenario, (200, 200))
    assert scenario.wire_marker_absent
    assert len(raw_responses) == 2
    assert all(raw.closed for raw in raw_responses)


class _BodyTimeoutScenario(_MarkerReviewScenario):
    def __init__(self, verification_key: Ed25519PublicKey) -> None:
        from threading import Event

        super().__init__(verification_key, {"responses": [(200, {}), (200, {})]})
        self.headers_flushed = Event()
        self.release_body = Event()
        self.first_body_finished = Event()


class _BodyTimeoutRequestHandler(_RequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        scenario = self.server.scenario
        status, headers, body = scenario.receive_request(self)
        first_request = len(scenario.events) == 1
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.flush()
        if first_request:
            scenario.headers_flushed.set()
            scenario.release_body.wait(timeout=5)
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if first_request:
                scenario.first_body_finished.set()
        self.close_connection = True


@contextmanager
def _serve_body_timeout_scenario(scenario: _BodyTimeoutScenario) -> Iterator[str]:
    from socketserver import ThreadingMixIn

    class BodyTimeoutServer(ThreadingMixIn, _LoopbackServer):
        # Concurrent acceptance exposes unwanted replays while the first body
        # is held. server_close joins these owned, bounded request threads.
        daemon_threads = False
        block_on_close = True

    server = BodyTimeoutServer(scenario)
    server.RequestHandlerClass = _BodyTimeoutRequestHandler
    thread = Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
    )
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        scenario.release_body.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=SERVER_JOIN_TIMEOUT_SECONDS)
        assert not thread.is_alive(), "Body-timeout HTTP server did not stop"
