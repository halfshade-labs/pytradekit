import hashlib
import json
import logging
from urllib.parse import parse_qs, urlsplit

import pytest
from requests import Response
from requests.adapters import BaseAdapter

from pytradekit.restful import binance_restful as bn
from pytradekit.utils.exceptions import ExchangeException


SERVER_MS = 1_800_000_000_000
POSITION = '/fapi/v3/positionRisk'
SERVER_CLOCK = '/fapi/v1/time'


class ExchangeFixture(BaseAdapter):
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []

    def send(self, request, **kwargs):
        parsed = urlsplit(request.url)
        assert parsed.scheme == 'https' and parsed.hostname == 'fapi.binance.com'
        self.calls.append((request.method, parsed.path, parse_qs(parsed.query), 'X-MBX-APIKEY' in request.headers))
        status, body = next(self.answers)
        response = Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        response._content_consumed = True
        return response

    def close(self):
        pass


def setup_client(monkeypatch, answers, ticks=None):
    client = bn.BinanceClient(logging.getLogger(__name__), key='fixture', is_perp=True)
    client.session.trust_env = False
    adapter = ExchangeFixture(answers)
    client.session.mount('https://', adapter)
    monkeypatch.setattr(client, 'get_timestamp', lambda: SERVER_MS - 12_000)
    monkeypatch.setattr(client, '_hashing', lambda value: hashlib.sha256(value.encode()).hexdigest())
    if ticks is not None:
        clock = iter(ticks)
        monkeypatch.setattr(bn.time, 'monotonic_ns', lambda: next(clock))
    return client, adapter


def test_position_query_recovers_once_from_timestamp_rejection(monkeypatch):
    client, adapter = setup_client(monkeypatch, [
        (400, {'code': -1021}), (200, {'serverTime': SERVER_MS}), (200, []),
    ], ticks=[0, 100_000_000])
    assert client.get_perp_position_risk('BTCUSDT') == []
    first, clock, replay = adapter.calls
    assert [row[:2] for row in adapter.calls] == [('GET', POSITION), ('GET', SERVER_CLOCK), ('GET', POSITION)]
    assert clock[2:] == ({}, False)
    assert first[3] is replay[3] is True
    assert replay[2]['timestamp'] == [str(SERVER_MS)]
    assert replay[2]['signature'] != first[2]['signature']
    assert first[2]['recvWindow'] == replay[2]['recvWindow'] == [str(bn.RECVWINDOW)]
    assert first[2]['symbol'] == replay[2]['symbol'] == ['BTCUSDT']


@pytest.mark.parametrize('clock_reply,ticks', [
    ((429, {}), [0]),
    ((200, {'serverTime': True}), [0, 100_000_000]),
    ((200, {'serverTime': SERVER_MS}), [0, 1_001_000_000]),
])
def test_unusable_clock_propagates_rejection_without_replaying(monkeypatch, clock_reply, ticks):
    client, adapter = setup_client(monkeypatch, [(400, {'code': -1021}), clock_reply], ticks=ticks)
    with pytest.raises(ExchangeException) as failed:
        client.get_perp_position_risk()
    assert failed.value.note.startswith('HTTP 400')
    assert [row[1] for row in adapter.calls] == [POSITION, SERVER_CLOCK]


def test_replay_rejected_returns_error_without_a_second_clock_probe(monkeypatch):
    client, adapter = setup_client(monkeypatch, [
        (400, {'code': -1021}), (200, {'serverTime': SERVER_MS}), (400, {'code': -1021}),
    ], ticks=[0, 100_000_000])
    with pytest.raises(ExchangeException) as failed:
        client.get_perp_position_risk()
    assert failed.value.note.startswith('HTTP 400')
    assert [row[1] for row in adapter.calls] == [POSITION, SERVER_CLOCK, POSITION]


@pytest.mark.parametrize('method,path,code', [
    ('POST', POSITION, -1021), ('GET', '/fapi/v3/account', -1021), ('GET', POSITION, -1022),
])
def test_recovery_scope_does_not_include_writes_other_reads_or_other_codes(monkeypatch, method, path, code):
    client, adapter = setup_client(monkeypatch, [(400, {'code': code})])
    url, params, _ = client._make_private_url(path, {})
    with pytest.raises(ExchangeException) as failed:
        client.request(method, url, params)
    assert failed.value.note.startswith('HTTP 400')
    assert [row[:2] for row in adapter.calls] == [(method, path)]
