import json
from urllib.parse import urlencode

import hmac
import hashlib
import requests

from pytradekit.utils.dynamic_types import WooxAuxiliary, HttpMmthod
from pytradekit.utils.time_handler import get_timestamp_ms


class WooxClient:
    def __init__(self, logger, key=None, secret=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.logger = logger
        if is_swap:
            self._url = WooxAuxiliary.swap_url.value
        else:
            self._url = WooxAuxiliary.url.value

    def generate_signature(self, timestamp, params):
        params = sorted(params.items()) or {}
        body = urlencode(params)
        message = f'{body}|{str(timestamp)}'
        return hmac.new(self.secret_key.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()

    def generate_signature_v3(self, timestamp, params, method, api):
        body = json.dumps(params) if params else ''
        signString = f'{str(timestamp)}{method}{api}{body}'
        return hmac.new(self.secret_key.encode('utf-8'), signString.encode('utf-8'), hashlib.sha256).hexdigest()

    def generate_header(self, params, method, api):
        timestamp = str(get_timestamp_ms())
        if '/v3/' in api:
            return {
                'Content-Type': 'application/json',
                'Cache-Control': 'no-cache',
                'x-api-key': self.api_key,
                'x-api-timestamp': timestamp,
                'x-api-signature': self.generate_signature_v3(timestamp, params, method, api)
            }
        else:
            return {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Cache-Control': 'no-cache',
                'x-api-key': self.api_key,
                'x-api-timestamp': timestamp,
                'x-api-signature': self.generate_signature(timestamp, params)
            }

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, use_sign=True):
        params = params if params else {}
        headers = {}

        if use_sign:
            headers = self.generate_header(params, method, api)

        url = f'{self._url}{api}'

        if method == HttpMmthod.GET.name:
            response = requests.get(url=url, headers=headers, params=params)
        elif method == HttpMmthod.POST.name:
            response = requests.post(url=url, headers=headers, json=params)
        else:
            return None

        return response.json()

    def get_kline(self, symbol, types, limit=100):
        params = {'symbol': symbol, 'type': types, 'limit': limit}
        url = WooxAuxiliary.url_version_v1.value + WooxAuxiliary.url_kline.value
        datas = self._send_request(url, params=params, use_sign=False)
        return datas

    def get_exchange_information(self):
        url = WooxAuxiliary.url_version_v1.value + WooxAuxiliary.url_exchange.value
        datas = self._send_request(url, use_sign=False)
        return datas

    def get_orderbook(self, symbol, limit=100):
        params = {'max_level': limit}
        url = WooxAuxiliary.url_version_v1.value + WooxAuxiliary.url_orderbook.value + f"/{symbol}"
        datas = self._send_request(url, params=params, use_sign=False)
        return datas

    def get_balance(self):
        url = WooxAuxiliary.url_version_v3.value + WooxAuxiliary.url_balance.value
        datas = self._send_request(url)
        return datas

    def get_orders(self, start_tmp, end_tmp):
        params = {'start_t': start_tmp, 'end_t': end_tmp}
        url = WooxAuxiliary.url_version_v1.value + WooxAuxiliary.url_orders.value
        datas = self._send_request(url, params=params)
        return datas

    def get_trades(self, start_tmp, end_tmp):
        params = {'start_t': start_tmp, 'end_t': end_tmp}
        url = WooxAuxiliary.url_version_v1.value + WooxAuxiliary.url_trades.value
        datas = self._send_request(url, params=params)
        return datas
