import json

import hmac
import hashlib
import requests
from pytradekit.utils.dynamic_types import BitmartAuxiliary, HttpMmthod
from pytradekit.utils.time_handler import get_timestamp_ms


class BitmartClient:
    def __init__(self, logger, key=None, secret=None, memo=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.memo = memo
        self.account_id = account_id
        self.logger = logger
        if is_swap:
            self._url = BitmartAuxiliary.swap_url.value
        else:
            self._url = BitmartAuxiliary.url.value

    def generate_signature(self, timestamp, body):
        message = f'{str(timestamp)}#{self.memo}#{body}'
        return hmac.new(self.secret_key.encode(), message.encode(), hashlib.sha256).hexdigest()

    def generate_header(self, body):
        timestamp = str(get_timestamp_ms())
        return {
            'Content-Type': 'application/json',
            'X-BM-KEY': self.api_key,
            'X-BM-TIMESTAMP': timestamp,
            'X-BM-SIGN': self.generate_signature(timestamp, str(json.dumps(body)))
        }

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, use_sign=True):
        params = params if params else {}
        try:
            headers = {}
            if use_sign:
                headers = self.generate_header(params)
            url = f'{self._url}{api}'
            if method == HttpMmthod.GET.name:
                response = requests.get(url=url, headers=headers, params=params)
            elif method == HttpMmthod.POST.name:
                response = requests.post(url=url, headers=headers, json=params)
            else:
                return None
            return response.json()
        except Exception as e:
            return e

    def get_ticker_24hr(self, symbol=None):
        params = {}
        if symbol:
            params['symbol'] = symbol
            url = BitmartAuxiliary.url_ticker.value
        else:
            url = BitmartAuxiliary.url_tickers.value
        datas = self._send_request(url, params=params, use_sign=False)
        return datas

    def get_exchange_information(self):
        url = BitmartAuxiliary.url_exchange.value
        datas = self._send_request(url, use_sign=False)
        return datas

    def get_orderbook(self, symbol, limit=100):
        params = {'symbol': symbol, 'limit': limit}
        url = BitmartAuxiliary.url_orderbook.value
        datas = self._send_request(url, params=params, use_sign=False)
        return datas

    def get_balance(self):
        url = BitmartAuxiliary.url_balance.value
        datas = self._send_request(url)
        return datas

    def get_deposit_history(self):
        url = BitmartAuxiliary.url_deposit_withdraw_history.value
        params = {'operation_type': 'deposit',
                  'N': 100}
        datas = self._send_request(url, params=params)
        return datas

    def get_withdraw_history(self):
        url = BitmartAuxiliary.url_deposit_withdraw_history.value
        params = {'operation_type': 'withdraw',
                  'N': 100}
        datas = self._send_request(url, params=params)
        return datas

    def get_transfer_history(self):
        url = BitmartAuxiliary.url_transfer.value
        params = {'moveType': 'spot to spot',
                  'N': 100}
        datas = self._send_request(url, params=params)
        return datas

    def get_subaccount_list(self):
        url = BitmartAuxiliary.url_subaccount_list.value
        data = self._send_request(url)
        return data

    def get_orders(self, start_time, end_time):
        params = {
            'startTime': start_time,
            'endTime': end_time,
        }
        url = BitmartAuxiliary.url_orders.value
        datas = self._send_request(url, method=HttpMmthod.POST.name, params=params)
        return datas

    def get_trades(self, start_time, end_time):
        params = {
            'startTime': start_time,
            'endTime': end_time,
        }
        url = BitmartAuxiliary.url_trades.value
        datas = self._send_request(url, method=HttpMmthod.POST.name, params=params)
        return datas

    def get_ticker_trades(self, symbol):
        params = {}
        params['symbol'] = symbol
        url = BitmartAuxiliary.url_ticker_trades.value
        datas = self._send_request(url, params=params, use_sign=False)
        return datas
