import hashlib
import hmac
import base64
import requests

from pytradekit.utils.time_handler import get_timestamp_ms
from pytradekit.utils.dynamic_types import HttpMmthod, KucoinAuxiliary


class KucoinClient:
    def __init__(self, logger, key=None, secret=None, passphrase=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.passphrase = passphrase
        self.account_id = account_id
        self.logger = logger
        if is_swap:
            self._url = KucoinAuxiliary.swap_url.value
        else:
            self._url = KucoinAuxiliary.url.value

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, use_sign=True):
        if params is None:
            params = {}
        url = f'{self._url}{api}'
        timestamp = get_timestamp_ms()
        headers = {}

        def get_sign(timestamp, api, method, params):
            msg = f'{timestamp}{method}{api}{params}'
            sign = base64.b64encode(
                hmac.new(self.secret_key.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).digest())
            return sign

        if use_sign:
            signature = get_sign(timestamp, api, method, '')
            headers = {
                'Content-Type': 'application/json',
                "KC-API-KEY": self.api_key,
                "KC-API-TIMESTAMP": str(timestamp),
                "KC-API-PASSPHRASE": self.passphrase,
                "KC-API-SIGN": signature
            }
        try:
            if method == HttpMmthod.GET.name:
                resp = requests.get(url, headers=headers, params=params, timeout=3)
            elif method == HttpMmthod.POST.name:
                resp = requests.post(url, data=params, headers=headers, timeout=3)
            else:
                return f'method {method} not support'
            return resp.json()
        except Exception as e:
            return e

    def get_ticker_24hr(self):
        url = KucoinAuxiliary.url_tickers.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=False)
        return datas

    def get_coin_ticker(self, symbol):
        url = KucoinAuxiliary.url_ticker.value
        params = {'symbol': symbol}
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=False)
        return datas

    def get_exchange_information(self):
        url = KucoinAuxiliary.url_exchange.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=False)
        return datas

    def get_orderbook(self, symbol=None, limit=100):
        url = KucoinAuxiliary.url_orderbook.value + str(limit)
        params = {'symbol': symbol}
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=False)
        return datas

    def get_balances(self):
        url = KucoinAuxiliary.url_balance.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_deposit_history(self):
        url = KucoinAuxiliary.url_deposits_history.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_withdraw_history(self):
        url = KucoinAuxiliary.url_withdraw_history.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_transfer_history(self):
        url = KucoinAuxiliary.url_transfer_history.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_orders(self, start_time=None, end_time=None):
        url = KucoinAuxiliary.url_orders.value + f'?startAt={start_time}&endAt={end_time}'
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_trades(self, start_time=None, end_time=None):
        url = KucoinAuxiliary.url_trades.value + f'?startAt={start_time}&endAt={end_time}'
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas
