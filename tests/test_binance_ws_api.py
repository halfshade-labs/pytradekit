"""Tests for the WebSocket API user-data subscription client (pytradekit#83)."""
import base64
import json
import queue
from unittest.mock import Mock
from urllib.parse import urlencode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pytradekit.ws.binance_ws_api import (
    BinanceWsApiUserData,
    Ed25519RequestSigner,
    LOGON_ID,
    SUBSCRIBE_ID,
)


def _make_signer():
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return Ed25519RequestSigner(pem), key.public_key()


def _make_client(**kwargs):
    signer, public = _make_signer()
    client = BinanceWsApiUserData(logger=Mock(), api_key='test_key', signer=signer, **kwargs)
    client._send = Mock()
    return client, public


class TestEd25519Signer:
    def test_signature_verifies_against_public_key(self):
        signer, public = _make_signer()
        params = {'timestamp': 1751900000000, 'apiKey': 'abc'}
        sig = signer.sign(params)
        payload = urlencode(sorted(params.items()))
        # raises InvalidSignature if wrong
        public.verify(base64.b64decode(sig), payload.encode('ascii'))

    def test_payload_uses_alphabetical_order(self):
        signer, public = _make_signer()
        sig = signer.sign({'b': '2', 'a': '1'})
        public.verify(base64.b64decode(sig), b'a=1&b=2')


class TestRequestBuilders:
    def test_build_logon_shape(self):
        client, public = _make_client()
        req = client.build_logon()
        assert req['method'] == 'session.logon'
        assert req['id'] == LOGON_ID
        params = req['params']
        assert params['apiKey'] == 'test_key'
        assert isinstance(params['timestamp'], int)
        payload = urlencode(sorted({k: v for k, v in params.items() if k != 'signature'}.items()))
        public.verify(base64.b64decode(params['signature']), payload.encode('ascii'))

    def test_build_subscribe_shape(self):
        req = BinanceWsApiUserData.build_subscribe()
        assert req == {'id': SUBSCRIBE_ID, 'method': 'userDataStream.subscribe'}


class TestMessageHandling:
    def test_logon_ack_triggers_subscribe(self):
        client, _ = _make_client()
        client.handle_message(json.dumps({'id': LOGON_ID, 'status': 200, 'result': {}}))
        sent = client._send.call_args[0][0]
        assert sent['method'] == 'userDataStream.subscribe'

    def test_logon_failure_does_not_subscribe(self):
        client, _ = _make_client()
        client.handle_message(json.dumps({'id': LOGON_ID, 'status': 401, 'error': {'msg': 'bad'}}))
        client._send.assert_not_called()

    def test_subscribe_ack_sets_flag(self):
        client, _ = _make_client()
        assert not client.subscribed
        client.handle_message(json.dumps({'id': SUBSCRIBE_ID, 'status': 200}))
        assert client.subscribed

    def test_wrapped_event_unwrapped_and_queued(self):
        q = queue.Queue()
        client, _ = _make_client(queue=q)
        client.handle_message(json.dumps({'event': {'e': 'executionReport', 'X': 'FILLED'}}))
        assert q.get_nowait() == {'e': 'executionReport', 'X': 'FILLED'}
        assert client.event_counts == {'executionReport': 1}

    def test_bare_event_handled(self):
        q = queue.Queue()
        client, _ = _make_client(queue=q)
        client.handle_message(json.dumps({'e': 'outboundAccountPosition'}))
        assert q.get_nowait()['e'] == 'outboundAccountPosition'

    def test_shadow_counts_but_never_queues(self):
        q = queue.Queue()
        client, _ = _make_client(queue=q, shadow=True)
        client.handle_message(json.dumps({'event': {'e': 'executionReport'}}))
        client.handle_message(json.dumps({'event': {'e': 'executionReport'}}))
        assert client.event_counts == {'executionReport': 2}
        assert client.last_event_ms > 0
        assert q.empty()

    def test_on_event_callback_preferred_over_queue(self):
        q = queue.Queue()
        received = []
        client, _ = _make_client(queue=q, on_event=received.append)
        client.handle_message(json.dumps({'e': 'balanceUpdate'}))
        assert received == [{'e': 'balanceUpdate'}]
        assert q.empty()

    def test_non_json_tolerated(self):
        client, _ = _make_client()
        client.handle_message('not json at all')
        client._send.assert_not_called()
