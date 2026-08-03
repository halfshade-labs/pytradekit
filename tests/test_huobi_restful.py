"""HuobiClient.get_commission_rate must hit the correct HTX v2 endpoint.

The previous implementation used `/v2/reference/transact-fee-rate/get` (404),
a singular `symbol` param (HTX requires `symbols`), and kebab-case response
fields (`maker-fee-rate`), so it always returned None and callers silently fell
back to static fees. HTX real fee (0.002) is higher than the static 0.0015, so
the fee floor was under-estimated.
"""
from unittest.mock import MagicMock

from pytradekit.restful.huobi_restful import HuobiClient
from pytradekit.utils.dynamic_types import HuobiAuxiliary
from pytradekit.utils.static_types import FeeStructureKey


def _bare_client():
    # __init__ makes a network call (get_accounts); bypass it.
    client = object.__new__(HuobiClient)
    client.logger = MagicMock()
    client._url = HuobiAuxiliary.url.value
    return client


def test_hits_v2_endpoint_with_plural_symbols():
    client = _bare_client()
    captured = {}

    def fake_send(url, method=None, params=None, use_sign=None):
        captured['url'] = url
        captured['params'] = params
        return {'code': 200, 'data': [{'symbol': 'btcusdt',
                                       'makerFeeRate': '0.002', 'takerFeeRate': '0.002'}]}

    client._send_request = fake_send
    result = client.get_commission_rate('BTCUSDT')

    assert captured['url'] == '/v2/reference/transact-fee-rate'   # no /get suffix
    assert captured['params'] == {'symbols': 'btcusdt'}           # plural, lowercased
    assert result == {FeeStructureKey.maker.name: 0.002, FeeStructureKey.taker.name: 0.002}


def test_endpoint_constant_has_no_get_suffix():
    assert HuobiAuxiliary.url_commission_rate.value == '/v2/reference/transact-fee-rate'


def test_returns_none_on_error_code():
    client = _bare_client()
    client._send_request = lambda *a, **k: {'code': 404, 'message': 'Not Found', 'success': False}
    assert client.get_commission_rate('btcusdt') is None
