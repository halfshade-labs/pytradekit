import json
from urllib.parse import urlencode

import hashlib
import hmac
import base64
import requests

from pytradekit.utils.dynamic_types import HttpMmthod, BitgetAuxiliary
from pytradekit.utils.time_handler import get_timestamp_ms, TimeConvert


class BitgetClient:
    def __init__(self, logger, key=None, secret=None, passphrase=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.session = requests.session()
        self.passphrase = passphrase
        self.logger = logger
        self._recvWindow = 5000
        if is_swap:
            self._url = BitgetAuxiliary.swap_url.value
        else:
            self._url = BitgetAuxiliary.url.value

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, use_sign=True):
        url = f'{self._url}{api}'
        params = params or {}
        headers = {
            'Content-Type': 'application/json'
        }

        if use_sign:
            timestamp = str(get_timestamp_ms())
            params = list(params.items())
            params.sort(key=lambda x: x[0])
            data1 = urlencode(params)
            params1 = json.dumps(params)
            if data1 != '':
                data1 = '?' + data1
            if method == HttpMmthod.GET.name:
                msg = timestamp + method + api + data1
            else:
                msg = timestamp + method + api + params1
            sign = base64.b64encode(
                hmac.new(self.secret_key.encode('utf-8'), msg=msg.encode('utf-8'), digestmod=hashlib.sha256).digest())
            headers = {
                'Content-Type': 'application/json',
                'ACCESS-KEY': self.api_key,
                'ACCESS-SIGN': sign,
                'ACCESS-TIMESTAMP': timestamp,
                'ACCESS-PASSPHRASE': self.passphrase
            }
            url = url + data1
        try:
            if method == HttpMmthod.GET.name:
                resp = requests.get(url, headers=headers, timeout=3)
            elif method == HttpMmthod.POST.name:
                resp = requests.post(url, json=params, headers=headers, timeout=3)
            else:
                return None
            return resp.json()
        except Exception as e:
            return e

    def get_ticker_24hr(self):
        url = BitgetAuxiliary.url_ticker.value
        datas = self._send_request(url, use_sign=False)
        return datas

    def get_exchange_information(self):
        url = BitgetAuxiliary.url_exchange.value
        datas = self._send_request(url, use_sign=False)
        return datas

    def get_orderbook(self, symbol, limit=150):
        url = BitgetAuxiliary.url_orderbook.value + f'?symbol={symbol}&limit={limit}'
        datas = self._send_request(url, use_sign=False)
        return datas

    def get_balances(self):
        url = BitgetAuxiliary.url_balances.value
        datas = self._send_request(url)
        return datas

    def get_deposit_history(self):
        params = {
            'startTime': str(get_timestamp_ms() - TimeConvert.DAT_TO_MS),
            'endTime': str(get_timestamp_ms())
        }
        url = BitgetAuxiliary.url_deposit_history.value
        datas = self._send_request(url, params=params)
        return datas

    def get_withdraw_history(self):
        params = {
            'startTime': str(get_timestamp_ms() - TimeConvert.DAT_TO_MS),
            'endTime': str(get_timestamp_ms())
        }
        url = BitgetAuxiliary.url_withdraw_history.value
        datas = self._send_request(url, params=params)
        return datas

    def get_transfer_history(self, coin):
        params = {'coin':coin}
        url = BitgetAuxiliary.url_transfer_history.value
        datas = self._send_request(url, params=params)
        return datas

    def get_orders(self, order_id, start_time, end_time):
        params = {
            'startTime': start_time,
            'endTime': end_time,
        }
        if order_id:
            params['idLessThan'] = order_id
        url = BitgetAuxiliary.url_orders.value
        datas = self._send_request(url, params=params)
        return datas

    def get_trades(self, symbol, trade_id, start_time, end_time):
        params = {
            'symbol': symbol,
            'startTime': start_time,
            'endTime': end_time,
        }
        if trade_id:
            params['idLessThan'] = trade_id
        url = BitgetAuxiliary.url_trades.value
        datas = self._send_request(url, params=params)
        return datas
