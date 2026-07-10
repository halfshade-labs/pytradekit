"""Tests for the listenKey-free BN WS-API user-data subscriber (#83).

Covers the issue checklist: Ed25519 signing, message unpacking (acks vs
event frames), and shadow-mode dispatch isolation.
"""
import base64
import json
import queue
from unittest.mock import Mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from nacl.signing import VerifyKey

from pytradekit.ws.binance_ws_api import BinanceWsApiUserData


def _make_keypair_pem():
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return pem, public_raw


def _make_client(shadow=True, business_queue=None):
    pem, public_raw = _make_keypair_pem()
    client = BinanceWsApiUserData(
        logger=Mock(), api_key='test-key', private_key_pem=pem,
        queue=business_queue, shadow=shadow, account_id='BN_TEST',
    )
    return client, public_raw


class FakeWs:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        self.closed = True


class TestSigning:
    def test_signature_verifies_over_sorted_payload(self):
        client, public_raw = _make_client()
        params = {'timestamp': 1783680000000, 'apiKey': 'test-key'}
        signature = client._sign(params)
        payload = 'apiKey=test-key&timestamp=1783680000000'
        VerifyKey(public_raw).verify(payload.encode(), base64.b64decode(signature))

    def test_logon_request_contains_signed_params(self):
        client, public_raw = _make_client()
        ws = FakeWs()
        client._logon(ws)
        msg = ws.sent[0]
        assert msg['method'] == 'session.logon'
        params = msg['params']
        assert params['apiKey'] == 'test-key'
        payload = f"apiKey={params['apiKey']}&timestamp={params['timestamp']}"
        VerifyKey(public_raw).verify(payload.encode(), base64.b64decode(params['signature']))


class TestSessionFlow:
    def test_logon_ack_triggers_subscribe(self):
        client, _ = _make_client()
        ws = FakeWs()
        client._logon(ws)
        logon_id = ws.sent[0]['id']
        client._on_message(ws, json.dumps({'id': logon_id, 'status': 200, 'result': {}}))
        assert client._logged_on
        assert ws.sent[1]['method'] == 'userDataStream.subscribe'

    def test_subscribe_ack_marks_healthy(self):
        client, _ = _make_client()
        ws = FakeWs()
        client._logon(ws)
        client._on_message(ws, json.dumps({'id': ws.sent[0]['id'], 'status': 200}))
        client._on_message(ws, json.dumps({'id': ws.sent[1]['id'], 'status': 200}))
        assert client.is_healthy()

    def test_rejected_logon_closes_socket_for_reconnect(self):
        client, _ = _make_client()
        ws = FakeWs()
        client._logon(ws)
        client._on_message(ws, json.dumps(
            {'id': ws.sent[0]['id'], 'status': 401, 'error': {'code': -2015, 'msg': 'invalid key'}}
        ))
        assert ws.closed
        assert not client._logged_on

    def test_close_resets_session_state(self):
        client, _ = _make_client()
        client._logged_on = True
        client._subscribed = True
        client._on_close(None, 1006, 'abnormal')
        assert not client.is_healthy()


class TestEventUnpacking:
    def test_wrapped_event_frame(self):
        client, _ = _make_client()
        frame = {'event': {'e': 'executionReport', 's': 'BTCUSDT'}}
        assert BinanceWsApiUserData._extract_event(frame)['e'] == 'executionReport'

    def test_raw_event_dict_tolerated(self):
        assert BinanceWsApiUserData._extract_event({'e': 'balanceUpdate'})['e'] == 'balanceUpdate'

    def test_non_event_frame_ignored(self):
        assert BinanceWsApiUserData._extract_event({'result': None}) is None

    def test_undecodable_frame_does_not_raise(self):
        client, _ = _make_client()
        client._on_message(FakeWs(), 'not-json')


class TestShadowDispatch:
    def test_shadow_counts_but_never_touches_queue(self):
        business_queue = queue.Queue()
        client, _ = _make_client(shadow=True, business_queue=business_queue)
        client._on_message(FakeWs(), json.dumps(
            {'event': {'e': 'executionReport', 's': 'ETHUSDT'}}
        ))
        assert client.event_counts['executionReport'] == 1
        assert client.last_event_ms is not None
        assert business_queue.empty()

    def test_live_mode_pushes_raw_event_to_queue(self):
        business_queue = queue.Queue()
        client, _ = _make_client(shadow=False, business_queue=business_queue)
        event = {'e': 'executionReport', 's': 'ETHUSDT', 'X': 'FILLED'}
        client._on_message(FakeWs(), json.dumps({'event': event}))
        assert business_queue.get_nowait() == event
        assert client.event_counts['executionReport'] == 1

    def test_stats_snapshot_for_channel_comparison(self):
        client, _ = _make_client(shadow=True)
        client._on_message(FakeWs(), json.dumps({'event': {'e': 'executionReport'}}))
        client._on_message(FakeWs(), json.dumps({'event': {'e': 'balanceUpdate'}}))
        stats = client.stats()
        assert stats['shadow'] is True
        assert stats['event_counts'] == {'executionReport': 1, 'balanceUpdate': 1}
        assert stats['account_id'] == 'BN_TEST'


class TestRequestIdFormat:
    def test_request_id_matches_bn_constraint(self):
        """Live probe caught -1135: ids must match ^[a-zA-Z0-9-_]{1,36}$
        (method names contain dots and must not leak into the id)."""
        import re
        client, _ = _make_client()
        ws = FakeWs()
        client._logon(ws)
        client._send(ws, 'userDataStream.subscribe')
        for sent in ws.sent:
            assert re.fullmatch(r'[a-zA-Z0-9-_]{1,36}', sent['id']), sent['id']
