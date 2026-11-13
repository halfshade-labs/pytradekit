"""Exercise signed GET retries over the real synchronous HTTP transport."""

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from threading import Thread
from typing import Any, Dict, Iterator, List, Tuple
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
import pytest

from pytradekit.restful.binance_restful import BinancePerpClient, RECVWINDOW
from pytradekit.utils import time_handler


INITIAL_TIMESTAMP_MS = 1789002976000
EXPECTED_RECV_WINDOW_MS = 10000
POSITION_RISK_PATH = "/fapi/v3/positionRisk"
TEST_SYMBOL = "BTCUSDT"
SOCKET_TIMEOUT_SECONDS = 1
SERVER_JOIN_TIMEOUT_SECONDS = 2
MAX_REPORTED_ATTEMPTS = 6
FUTURE_TIMESTAMP_TOLERANCE_MS = 1000


def test_signed_position_get_resigns_after_retry_crosses_recv_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, verification_key = _create_signing_client()
    scenario = _SignedGetScenario(verification_key)
    monkeypatch.setattr(time_handler, "get_timestamp_ms", scenario.read_timestamp)
    # Keep localhost traffic independent of proxy and netrc configuration.
    client.session.trust_env = False

    with client.session, _serve_scenario(scenario) as base_url:
        client._url = base_url
        try:
            assert RECVWINDOW == EXPECTED_RECV_WINDOW_MS
            assert client.get_perp_position_risk(symbol=TEST_SYMBOL) == []
            assert scenario.business_parameters_unchanged
            assert scenario.events == [
                {
                    "attempt": attempt,
                    "method": "GET",
                    "path": POSITION_RISK_PATH,
                    "timestamp": timestamp,
                    "recvWindow": EXPECTED_RECV_WINDOW_MS,
                    "signature_verified": True,
                    "status": status,
                }
                for attempt, timestamp, status in (
                    (1, INITIAL_TIMESTAMP_MS, 503),
                    (2, INITIAL_TIMESTAMP_MS + RECVWINDOW + 1, 200),
                )
            ]
        finally:
            # The original SDK exception obscures the HTTP response body.
            # Preserve only bounded, credential-free wire metadata on failure.
            print("SIGNED_GET_RETRY_EVENTS=" + json.dumps(
                scenario.events[:MAX_REPORTED_ATTEMPTS], separators=(",", ":"),
            ))


def _create_signing_client() -> Tuple[BinancePerpClient, Ed25519PublicKey]:
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    client = BinancePerpClient(
        logger=Mock(), key="signed-get-retry-test-only", secret=private_pem,
    )
    return client, private_key.public_key()


class _SignedGetScenario:
    def __init__(self, verification_key: Ed25519PublicKey) -> None:
        self.verification_key = verification_key
        self.timestamp_ms = INITIAL_TIMESTAMP_MS
        self.events: List[Dict[str, Any]] = []
        self.business_parameters_unchanged = True

    def read_timestamp(self) -> int:
        return self.timestamp_ms

    def receive_get(self, request_target: str) -> Tuple[int, bytes]:
        target = urlsplit(request_target)
        parameters = parse_qs(target.query, keep_blank_values=True)
        timestamp = _read_integer(parameters, "timestamp")
        recv_window = _read_integer(parameters, "recvWindow")
        verified = self._verify_signature(target.query)
        business_parameters = {
            name: value for name, value in parameters.items()
            if name not in {"timestamp", "recvWindow", "signature"}
        }
        self.business_parameters_unchanged &= (
            business_parameters == {"symbol": [TEST_SYMBOL]}
        )
        event = {
            "attempt": len(self.events) + 1,
            "method": "GET",
            "path": target.path,
            "timestamp": timestamp,
            "recvWindow": recv_window,
            "signature_verified": verified,
        }
        status, body = self._build_response(event)
        event["status"] = status
        self.events.append(event)
        return status, json.dumps(body).encode("ascii")

    def _build_response(self, event: Dict[str, Any]) -> Tuple[int, Any]:
        if not self.events:
            self.timestamp_ms += RECVWINDOW + 1
            return 503, {"code": -1000, "msg": "Temporary service failure"}
        if not event["signature_verified"]:
            return 400, {"code": -1022, "msg": "Signature is not valid"}
        if (not self.business_parameters_unchanged
                or event["recvWindow"] != RECVWINDOW):
            return 400, {"code": -1100, "msg": "Unexpected query parameters"}
        if (self.timestamp_ms - event["timestamp"] > event["recvWindow"]
                or event["timestamp"] >= (
                    self.timestamp_ms + FUTURE_TIMESTAMP_TOLERANCE_MS
                )):
            return 400, {"code": -1021, "msg": "Timestamp is outside recvWindow"}
        return 200, []

    def _verify_signature(self, encoded_query: str) -> bool:
        unsigned, separator, signature_field = encoded_query.rpartition(
            "&signature=",
        )
        if not separator:
            return False
        try:
            signature = parse_qs("signature=" + signature_field)["signature"][0]
            self.verification_key.verify(
                base64.b64decode(signature, validate=True), unsigned.encode("ascii"),
            )
        except (InvalidSignature, ValueError, KeyError):
            return False
        return True


def _read_integer(parameters: Dict[str, Any], name: str) -> int:
    try:
        return int(parameters.get(name, ["-1"])[0])
    except ValueError:
        return -1


class _LoopbackServer(HTTPServer):
    def __init__(self, scenario: _SignedGetScenario) -> None:
        self.scenario = scenario
        super().__init__(("127.0.0.1", 0), _RequestHandler)

    def get_request(self) -> Tuple[Any, Any]:
        connection, address = super().get_request()
        connection.settimeout(SOCKET_TIMEOUT_SECONDS)
        return connection, address


class _RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status, body = self.server.scenario.receive_get(self.path)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextmanager
def _serve_scenario(scenario: _SignedGetScenario) -> Iterator[str]:
    server = _LoopbackServer(scenario)
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
        assert not thread.is_alive(), "Loopback HTTP server did not stop"


# Keep the original regression above unchanged for the preserved first red.
from time import monotonic

import requests
from urllib3.exceptions import MaxRetryError, SSLError as Urllib3SSLError

from pytradekit.utils.exceptions import ExchangeException


@pytest.fixture
def policy_client() -> Iterator[Tuple[BinancePerpClient, Ed25519PublicKey]]:
    client, verification_key = _create_signing_client()
    client.session.trust_env = False
    try:
        yield client, verification_key
    finally:
        client.session.close()


def test_retry_preserves_unicode_custom_window_nullable_query_and_session(
    policy_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, verification_key = policy_client
    parameters = {
        "symbol": "牛来USDT", "origClientOrderId": "id/+&=% value",
        "recvWindow": 3000, "optional": None,
        "tags": ["first", None, "last"], "signature": "obsolete-test-value",
    }
    scenario = _PolicyScenario(verification_key, {
        "parameters": parameters,
        "expected": {
            "symbol": ["牛来USDT"], "origClientOrderId": ["id/+&=% value"],
            "tags": ["first", "last"],
        },
        "responses": [(503, {}), (200, {})],
        "check_session": True,
    })
    monkeypatch.setattr(time_handler, "get_timestamp_ms", scenario.read_timestamp)
    client.session.headers["X-Test-Setting"] = "preserved"
    client.session.cookies.set("test_cookie", "preserved")
    with _serve_policy_scenario(scenario) as base_url:
        client._url = base_url
        assert _send_policy_request(client, scenario) == []
    assert scenario.business_parameters_unchanged
    assert scenario.session_settings_preserved
    assert [event["recvWindow"] for event in scenario.events] == [3000, 3000]
    _assert_fresh_signed_events(scenario.events)
    assert "timestamp" not in parameters
    assert parameters["signature"] == "obsolete-test-value"
    assert parameters["optional"] is None
    assert parameters["tags"] == ["first", None, "last"]


@pytest.mark.parametrize("status", [502, 503, 504])
def test_signed_get_exhausts_after_exactly_four_sends(
    policy_client, monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    client, verification_key = policy_client
    scenario = _PolicyScenario(verification_key, {
        "responses": [(status, {})] * 5,
    })
    monkeypatch.setattr(time_handler, "get_timestamp_ms", scenario.read_timestamp)
    with _serve_policy_scenario(scenario) as base_url:
        client._url = base_url
        with pytest.raises(ExchangeException):
            _send_policy_request(client, scenario)
    assert [event["status"] for event in scenario.events] == [status] * 4
    assert scenario.business_parameters_unchanged
    _assert_fresh_signed_events(scenario.events)


def test_signed_get_honors_retry_after_before_resigning(
    policy_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, verification_key = policy_client
    scenario = _PolicyScenario(verification_key, {
        "responses": [(503, {"Retry-After": "1"}), (200, {})],
    })
    monkeypatch.setattr(time_handler, "get_timestamp_ms", scenario.read_timestamp)
    with _serve_policy_scenario(scenario) as base_url:
        client._url = base_url
        started = monotonic()
        assert _send_policy_request(client, scenario) == []
        assert monotonic() - started >= 1
    assert len(scenario.events) == 2
    _assert_fresh_signed_events(scenario.events)


@pytest.mark.parametrize("status", [400, 302, 307])
def test_signed_get_does_not_replay_expiry_or_follow_redirects(
    policy_client, monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    client, verification_key = policy_client
    scenario = _PolicyScenario(verification_key, {
        "responses": [(status, {"Location": "/must-not-receive-auth"}), (200, {})],
    })
    monkeypatch.setattr(time_handler, "get_timestamp_ms", scenario.read_timestamp)
    with _serve_policy_scenario(scenario) as base_url:
        client._url = base_url
        with pytest.raises(ExchangeException):
            _send_policy_request(client, scenario)
    assert [event["status"] for event in scenario.events] == [status]
    assert scenario.events[0]["signature_verified"]


@pytest.mark.parametrize("failure_type", [
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ConnectionError,
])
def test_transient_transport_failure_refreshes_timestamp_and_signature(
    policy_client, monkeypatch: pytest.MonkeyPatch, failure_type,
) -> None:
    client, verification_key = policy_client
    probe = _TransportFailureProbe(verification_key, failure_type)
    monkeypatch.setattr(time_handler, "get_timestamp_ms", probe.read_timestamp)
    router = client.session.get_adapter("http://127.0.0.1")
    monkeypatch.setattr(router.signed_get_adapter, "send", probe.send)
    client._url = "http://127.0.0.1:1"
    assert client.get_perp_position_risk(symbol=TEST_SYMBOL) == []
    assert len(probe.events) == 2
    _assert_fresh_signed_events(probe.events)


@pytest.mark.parametrize("failure_type", [
    requests.exceptions.SSLError,
    requests.exceptions.InvalidURL,
])
def test_nontransient_transport_failure_does_not_retry(
    policy_client, monkeypatch: pytest.MonkeyPatch, failure_type,
) -> None:
    client, verification_key = policy_client
    probe = _TransportFailureProbe(verification_key, failure_type)
    monkeypatch.setattr(time_handler, "get_timestamp_ms", probe.read_timestamp)
    router = client.session.get_adapter("http://127.0.0.1")
    monkeypatch.setattr(router.signed_get_adapter, "send", probe.send)
    client._url = "http://127.0.0.1:1"
    with pytest.raises(ExchangeException):
        client.get_perp_position_risk(symbol=TEST_SYMBOL)
    assert len(probe.events) == 1


@pytest.mark.parametrize("failure_type", [
    requests.exceptions.ConnectionError,
    requests.exceptions.SSLError,
    requests.exceptions.InvalidURL,
])
def test_signed_get_error_diagnostics_do_not_expose_prepared_query(
    policy_client, monkeypatch: pytest.MonkeyPatch, failure_type,
) -> None:
    client, _ = policy_client
    private_marker = "synthetic-private-query-marker"
    failure = failure_type(
        "http://localhost/private?signature=" + private_marker
        + "&symbol=synthetic-business-value X-MBX-APIKEY=" + private_marker,
    )
    send = Mock(side_effect=failure)
    router = client.session.get_adapter("http://127.0.0.1")
    monkeypatch.setattr(router.signed_get_adapter, "send", send)
    client._url = "http://127.0.0.1:1"
    with pytest.raises(ExchangeException) as captured:
        client.get_perp_position_risk(symbol=TEST_SYMBOL)
    diagnostics = repr(client.logger.mock_calls) + str(captured.value.note)
    assert private_marker not in diagnostics
    assert "signature=" not in diagnostics
    assert "synthetic-business-value" not in diagnostics
    assert client.api_key not in diagnostics
    assert failure_type.__name__ in diagnostics
    assert captured.value.__suppress_context__
    expected_sends = 4 if failure_type is requests.exceptions.ConnectionError else 1
    assert send.call_count == expected_sends


def test_wrapped_tls_error_fails_closed_with_safe_root_cause_diagnostics(
    policy_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = policy_client
    failure = requests.exceptions.ConnectionError(MaxRetryError(
        None, "/private?signature=synthetic-sensitive-value",
        Urllib3SSLError("Synthetic certificate verification failure"),
    ))
    send = Mock(side_effect=failure)
    router = client.session.get_adapter("http://127.0.0.1")
    monkeypatch.setattr(router.signed_get_adapter, "send", send)
    client._url = "http://127.0.0.1:1"
    with pytest.raises(ExchangeException) as captured:
        client.get_perp_position_risk(symbol=TEST_SYMBOL)
    diagnostics = repr(client.logger.mock_calls) + str(captured.value.note)
    assert "SSLError" in diagnostics
    assert "synthetic-sensitive-value" not in diagnostics
    assert "signature=" not in diagnostics
    assert send.call_count == 1


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_signed_writes_keep_once_only_status_policy(
    policy_client, monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    client, verification_key = policy_client
    scenario = _PolicyScenario(verification_key, {
        "method": method, "responses": [(503, {}), (200, {})],
    })
    monkeypatch.setattr(time_handler, "get_timestamp_ms", scenario.read_timestamp)
    with _serve_policy_scenario(scenario) as base_url:
        client._url = base_url
        with pytest.raises(ExchangeException):
            _send_policy_request(client, scenario)
    assert len(scenario.events) == 1
    assert scenario.events[0]["method"] == method
    assert scenario.events[0]["signature_verified"]


def test_unsigned_get_retains_transport_retry_and_redirect_behavior(
    policy_client,
) -> None:
    client, verification_key = policy_client
    scenario = _PolicyScenario(verification_key, {
        "responses": [(503, {}), (302, {"Location": "/public-next"}), (200, {})],
    })
    with _serve_policy_scenario(scenario) as base_url:
        assert client.request(
            "GET", base_url + "/public", params={"symbol": TEST_SYMBOL},
            use_sign=False,
        ) == []
    assert [event["status"] for event in scenario.events] == [503, 302, 200]
    assert not any(event["signature_verified"] for event in scenario.events)


def test_router_preserves_fallback_policy_kwargs_and_closes_both_adapters(
    policy_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = policy_client
    router = client.session.get_adapter("http://127.0.0.1")
    fallback_policy = router.fallback_adapter.max_retries
    assert (fallback_policy.total, fallback_policy.connect, fallback_policy.read) == (3, 3, 3)
    assert fallback_policy.allowed_methods == frozenset(["GET"])
    assert fallback_policy.status_forcelist == [502, 503, 504]
    assert fallback_policy.backoff_factor == 0.3
    assert not fallback_policy.raise_on_status
    assert router.signed_get_adapter.max_retries.total == 0
    signed_send, fallback_send = Mock(), Mock()
    monkeypatch.setattr(router.signed_get_adapter, "send", signed_send)
    monkeypatch.setattr(router.fallback_adapter, "send", fallback_send)
    settings = {"timeout": (2, 7), "verify": "test-ca.pem", "proxies": {
        "https": "http://proxy.invalid",
    }, "stream": True, "cert": "test-cert.pem"}
    for method in ("GET", "POST", "DELETE"):
        prepared = requests.Request(
            method, "https://example.invalid", headers={"X-MBX-APIKEY": "test-only"},
        ).prepare()
        if method == "GET":
            prepared.headers["X-PTK-Signed-Get-Transport"] = "single-attempt"
        router.send(prepared, **settings)
    public = requests.Request("GET", "https://example.invalid").prepare()
    router.send(public, **settings)
    assert signed_send.call_count == 1
    assert fallback_send.call_count == 3
    assert signed_send.call_args.kwargs == settings
    assert all(call.kwargs == settings for call in fallback_send.call_args_list)
    signed_close, fallback_close = Mock(), Mock()
    monkeypatch.setattr(router.signed_get_adapter, "close", signed_close)
    monkeypatch.setattr(router.fallback_adapter, "close", fallback_close)
    router.close()
    signed_close.assert_called_once_with()
    fallback_close.assert_called_once_with()


def test_signed_retry_state_is_per_call_and_does_not_change_adapter_policy(
    policy_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, verification_key = policy_client
    scenario = _PolicyScenario(verification_key, {
        "responses": ([(503, {})] * 3 + [(200, {})]) * 2,
    })
    monkeypatch.setattr(time_handler, "get_timestamp_ms", scenario.read_timestamp)
    router = client.session.get_adapter("http://127.0.0.1")
    fallback_policy = router.fallback_adapter.max_retries
    signed_policy = router.signed_get_adapter.max_retries
    with _serve_policy_scenario(scenario) as base_url:
        client._url = base_url
        for _ in range(2):
            assert _send_policy_request(client, scenario) == []
    assert len(scenario.events) == 8
    _assert_fresh_signed_events(scenario.events)
    assert router.fallback_adapter.max_retries is fallback_policy
    assert router.signed_get_adapter.max_retries is signed_policy
    assert fallback_policy.total == 3 and fallback_policy.history == ()
    assert signed_policy.total == 0 and signed_policy.history == ()


def _send_policy_request(client, scenario):
    # The legacy builder owns and mutates its input; retain scenario fixtures.
    url, params, _ = client._make_private_url("/test/signed", dict(scenario.parameters))
    return client.request(scenario.method, url, params=params)


def _assert_fresh_signed_events(events: List[Dict[str, Any]]) -> None:
    assert all(event["signature_verified"] for event in events)
    timestamps = [event["timestamp"] for event in events]
    assert timestamps == sorted(set(timestamps))


class _PolicyScenario:
    def __init__(self, verification_key: Ed25519PublicKey, config: dict) -> None:
        self.verifier = _SignedGetScenario(verification_key)
        self.parameters = config.get("parameters", {"symbol": TEST_SYMBOL})
        self.expected = config.get("expected", {"symbol": [TEST_SYMBOL]})
        self.responses = config["responses"]
        self.method = config.get("method", "GET")
        self.check_session = config.get("check_session", False)
        self.timestamp_ms = INITIAL_TIMESTAMP_MS
        self.events: List[Dict[str, Any]] = []
        self.business_parameters_unchanged = True
        self.session_settings_preserved = True

    def read_timestamp(self) -> int:
        return self.timestamp_ms

    def receive_request(self, request) -> Tuple[int, dict, bytes]:
        target = urlsplit(request.path)
        payload = target.query
        if request.command in {"POST", "DELETE"}:
            payload = request.rfile.read(int(request.headers["Content-Length"])).decode("ascii")
        parameters = parse_qs(payload, keep_blank_values=True)
        self.business_parameters_unchanged &= {
            name: value for name, value in parameters.items()
            if name not in {"timestamp", "recvWindow", "signature"}
        } == self.expected
        if self.check_session:
            self.session_settings_preserved &= (
                request.headers.get("X-Test-Setting") == "preserved"
                and request.headers.get("Cookie") == "test_cookie=preserved"
            )
        status, headers = self.responses[min(len(self.events), len(self.responses) - 1)]
        self.events.append({
            "attempt": len(self.events) + 1,
            "method": request.command, "path": target.path,
            "timestamp": _read_integer(parameters, "timestamp"),
            "recvWindow": _read_integer(parameters, "recvWindow"),
            "signature_verified": self.verifier._verify_signature(payload),
            "status": status,
        })
        self.timestamp_ms += EXPECTED_RECV_WINDOW_MS + 1
        body = [] if status == 200 else {"code": -1021, "msg": "Synthetic failure"}
        return status, headers, json.dumps(body).encode("ascii")


class _PolicyRequestHandler(_RequestHandler):
    def do_GET(self) -> None:
        status, headers, body = self.server.scenario.receive_request(self)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    do_POST = do_GET
    do_DELETE = do_GET


@contextmanager
def _serve_policy_scenario(scenario: _PolicyScenario) -> Iterator[str]:
    server = _LoopbackServer(scenario)
    server.RequestHandlerClass = _PolicyRequestHandler
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
        assert not thread.is_alive(), "Policy HTTP server did not stop"


class _TransportFailureProbe:
    def __init__(self, verification_key, failure_type) -> None:
        self.verifier = _SignedGetScenario(verification_key)
        self.failure_type = failure_type
        self.timestamp_ms = INITIAL_TIMESTAMP_MS
        self.events: List[Dict[str, Any]] = []

    def read_timestamp(self) -> int:
        return self.timestamp_ms

    def send(self, request, **kwargs):
        target = urlsplit(request.url)
        parameters = parse_qs(target.query)
        self.events.append({
            "attempt": len(self.events) + 1, "method": request.method,
            "path": target.path,
            "timestamp": _read_integer(parameters, "timestamp"),
            "recvWindow": _read_integer(parameters, "recvWindow"),
            "signature_verified": self.verifier._verify_signature(target.query),
            "status": 0 if len(self.events) == 0 else 200,
        })
        if len(self.events) == 1:
            self.timestamp_ms += EXPECTED_RECV_WINDOW_MS + 1
            raise self.failure_type("Synthetic transport failure")
        response = requests.Response()
        response.status_code = 200
        response._content = b"[]"
        response.request = request
        return response


def test_legacy_signed_builder_keeps_inplace_nullable_encoding(
    policy_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, verification_key = policy_client
    monkeypatch.setattr(time_handler, "get_timestamp_ms", lambda: INITIAL_TIMESTAMP_MS)
    parameters = {
        "symbol": "牛来USDT", "optional": None,
        "tags": ["first", None, "last"], "recvWindow": 3000,
    }
    _, returned, timestamp = client._make_private_url("/test/signed", parameters)
    assert returned is parameters
    assert timestamp == INITIAL_TIMESTAMP_MS
    assert parameters["timestamp"] == INITIAL_TIMESTAMP_MS
    assert parameters["optional"] is None
    assert parameters["tags"] == ["first", None, "last"]
    assert parameters["recvWindow"] == 3000
    assert list(parameters)[-1] == "signature"
    legacy_payload = (
        b"symbol=%E7%89%9B%E6%9D%A5USDT&optional=None&tags=first"
        b"&tags=None&tags=last&recvWindow=3000&timestamp=1789002976000"
    )
    verification_key.verify(base64.b64decode(parameters["signature"]), legacy_payload)


def test_legacy_unsigned_builder_keeps_inplace_timestamp_and_nullable_values(
    policy_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = policy_client
    monkeypatch.setattr(time_handler, "get_timestamp_ms", lambda: INITIAL_TIMESTAMP_MS)
    parameters = {"symbol": "牛来USDT", "optional": None, "tags": [None, "last"]}
    _, returned, timestamp = client._make_private_url(
        "/test/public", parameters, use_sign=False, timestamp=False,
    )
    assert returned is parameters
    assert timestamp == INITIAL_TIMESTAMP_MS
    assert parameters["timestamp"] == INITIAL_TIMESTAMP_MS
    assert parameters["optional"] is None
    assert parameters["tags"] == [None, "last"]
    assert "recvWindow" not in parameters
    assert "signature" not in parameters


def test_legacy_signed_builder_keeps_existing_signature_position_and_payload(
    policy_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, verification_key = policy_client
    monkeypatch.setattr(time_handler, "get_timestamp_ms", lambda: INITIAL_TIMESTAMP_MS)
    parameters = {"symbol": TEST_SYMBOL, "signature": "legacy-placeholder"}
    _, returned, _ = client._make_private_url("/test/signed", parameters)
    assert returned is parameters
    assert list(parameters) == ["symbol", "signature", "recvWindow", "timestamp"]
    legacy_payload = (
        b"symbol=BTCUSDT&signature=legacy-placeholder&recvWindow=10000"
        b"&timestamp=1789002976000"
    )
    verification_key.verify(base64.b64decode(parameters["signature"]), legacy_payload)


def test_signed_get_normalizes_only_its_per_call_copy(
    policy_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, verification_key = policy_client
    parameters = {
        "symbol": TEST_SYMBOL, "optional": None, "tags": [None, "last"],
        "signature": "legacy-placeholder", "timestamp": 0,
    }
    scenario = _PolicyScenario(verification_key, {
        "expected": {"symbol": [TEST_SYMBOL], "tags": ["last"]},
        "responses": [(503, {}), (200, {})],
    })
    monkeypatch.setattr(time_handler, "get_timestamp_ms", scenario.read_timestamp)
    with _serve_policy_scenario(scenario) as base_url:
        assert client.request("GET", base_url + "/test/signed", params=parameters) == []
    parameters_unchanged = parameters == {
        "symbol": TEST_SYMBOL, "optional": None, "tags": [None, "last"],
        "signature": "legacy-placeholder", "timestamp": 0,
    }
    assert parameters_unchanged
    assert scenario.business_parameters_unchanged
    _assert_fresh_signed_events(scenario.events)
