import hashlib
import hmac
from urllib.parse import urlencode
import requests
from pytradekit.utils.time_handler import get_timestamp_ms
from pytradekit.utils.dynamic_types import HttpMmthod, MexcAuxiliary


class MexcClient:
    def __init__(self, logger, key=None, secret=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.logger = logger
        if is_swap:
            self._url = MexcAuxiliary.swap_url.value
        else:
            self._url = MexcAuxiliary.url.value

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, use_sign=True):
        url = f'{self._url}{api}'
        req_time = get_timestamp_ms()
        headers = {}
        params = params or {}
        params['timestamp'] = req_time

        if use_sign:
            params_encode = urlencode(params)
            signature = hmac.new(self.secret_key.encode('utf-8'), params_encode.encode('utf-8'),
                                 hashlib.sha256).hexdigest()
            params['signature'] = signature
            headers = {
                'x-mexc-apikey': self.api_key,
                'Content-Type': 'application/json',
            }
        try:
            if method == HttpMmthod.GET.name:
                resp = requests.get(url, params=params, headers=headers, timeout=3)
            elif method == HttpMmthod.POST.name:
                resp = requests.post(url, params=params, headers=headers, timeout=3)
            else:
                return f'method {method} not support'
            return resp.json()
        except Exception as e:
            return e

    def get_ticker_24hr(self):
        params = {}
        url = MexcAuxiliary.url_ticker.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=False)
        return datas

    def get_exchange_information(self):
        params = {}
        url = MexcAuxiliary.url_exchange.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=False)
        return datas

    def get_orderbook(self, symbol=None, limit=1000):
        params = {'symbol': symbol, 'limit': limit}
        url = MexcAuxiliary.url_orderbook.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=False)
        return datas

    def get_balances(self):
        url = MexcAuxiliary.url_balance.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_deposit_history(self):
        url = MexcAuxiliary.url_deposit_history.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_withdraw_history(self):
        url = MexcAuxiliary.url_withdraw_history.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    # def get_transfer_history(self):
    #     url = MexcAuxiliary.url_transfer_history.value
    #     datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
    #     return datas

    def get_internal_transfer_history(self):
        url = MexcAuxiliary.url_internal_transfer_history.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_orders(self, symbol, start_time, end_time):
        params = {
            'symbol': symbol,
            'startTime': start_time,
            'endTime': end_time,
        }
        url = MexcAuxiliary.url_orders.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas

    def get_trades(self, symbol, start_time, end_time):
        params = {
            'symbol': symbol,
            'startTime': start_time,
            'endTime': end_time,
        }
        url = MexcAuxiliary.url_trades.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas
