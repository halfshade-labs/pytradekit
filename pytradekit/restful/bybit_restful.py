from urllib.parse import urlencode

import hashlib
import hmac
import requests

from pytradekit.utils.time_handler import get_timestamp_ms
from pytradekit.utils.dynamic_types import HttpMmthod, BybitAuxiliary


class BybitClient:
    def __init__(self, logger, key=None, secret=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.session = requests.session()
        self.logger = logger
        self._recvWindow = 5000
        if is_swap:
            self._url = BybitAuxiliary.swap_url.value
        else:
            self._url = BybitAuxiliary.url.value

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, use_sign=True):

        try:
            params = params or {}
            url = f'{self._url}{api}'
            data = {}
            if use_sign:
                params.update(
                    {'api_key': self.api_key, 'timestamp': get_timestamp_ms(), 'recvWindow': self._recvWindow})
                params = sorted(params.items()) or {}
                data = urlencode(params)

                signature = hmac.new(self.secret_key.encode(encoding='UTF8'), msg=data.encode(encoding='UTF8'),
                                     digestmod=hashlib.sha256).hexdigest()
                sign_real = {"sign": signature}
                data = dict(params, **sign_real)
                signed = f'{data}&sign={signature}'
                url = f'{url}?{signed}'

            headers = {"Content-Type": "application/x-www-form-urlencoded"}

            if method == HttpMmthod.POST.name:
                response = requests.post(url=url, data=data, headers=headers)
            elif method == HttpMmthod.GET.name:
                response = requests.get(url=url, params=params, headers=headers)
            else:
                return 'request method err'

            return response.json()
        except Exception as e:
            return e

    def get_swap_position(self, symbol):
        params = {'category': 'linear', 'symbol': symbol}
        url = BybitAuxiliary.url_swap_position.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params)
        return datas

    def get_swap_interest(self, coin, start_time, end_time):
        params = {'currency': coin, 'startTime': start_time, 'endTime': end_time}
        url = BybitAuxiliary.url_swap_interest.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params)
        return datas

    def get_ticker_24hr(self, category='spot'):
        params = {'category': category}
        url = BybitAuxiliary.url_ticker.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=False)
        return datas

    def get_exchange_information(self, category='spot'):
        params = {'category': category}
        url = BybitAuxiliary.url_exchange.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=False)
        return datas

    def get_orderbook(self, category='spot', symbol=None, limit=200):
        params = {'category': category, 'symbol': symbol, 'limit': limit}
        url = BybitAuxiliary.url_orderbook.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=False)
        return datas

    def get_balances(self):
        params = {'accountType': 'UNIFIED'}
        url = BybitAuxiliary.url_balance.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas

    def get_transfer_history(self):
        url = BybitAuxiliary.url_transfer.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_withdraw_history(self):
        url = BybitAuxiliary.url_withdraw.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_deposit_history(self):
        url = BybitAuxiliary.url_deposit.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_deposit_internal_history(self):
        url = BybitAuxiliary.url_deposit_internal.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_orders(self, types='spot', start_time=None, end_time=None, cursor=None):
        params = {'category': types, 'limit': 50}
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time
        if cursor:
            params['cursor'] = cursor
        url = BybitAuxiliary.url_order.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas

    def get_trades(self, types='spot', start_time=None, end_time=None, cursor=None):
        params = {'category': types, 'limit': 50}
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time
        if cursor:
            params['cursor'] = cursor
        url = BybitAuxiliary.url_trade.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas
