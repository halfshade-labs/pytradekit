import urllib.parse
import hashlib
import hmac
import requests

from pytradekit.utils.dynamic_types import HttpMmthod
from pytradekit.utils.time_handler import get_timestamp_ms, sleep_min_time
from pytradekit.utils.dynamic_types import HashkeyAuxiliary

RETRY_TIMES = 60
RETRY_FREQUENCY = 3


class HashkeyClient:
    def __init__(self, logger, key=None, secret=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.session = requests.session()
        self.logger = logger
        self._recvWindow = 5000
        if is_swap:
            self._url = HashkeyAuxiliary.swap_url.value
        else:
            self._url = HashkeyAuxiliary.url.value

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, use_sign=True):
        try:
            if params is None:
                params = {}
            url = f'{self._url}{api}'
            params['timestamp'] = get_timestamp_ms()
            headers = {}
            if use_sign:
                headers['X-HK-APIKEY'] = self.api_key
                sign = self.create_signature(params)
                params['signature'] = sign
            for i in range(RETRY_FREQUENCY):
                if method == HttpMmthod.GET.name:
                    response = requests.get(url=url, params=params, headers=headers)
                elif method == HttpMmthod.POST.name:
                    response = requests.post(url, data=params, headers=headers, timeout=3)
                else:
                    return f'method {method} not support'
                result = response.json()
                if "code" in result:
                    if result['code'] == '0003' or result['code'] == '429':
                        sleep_min_time(min_second=RETRY_TIMES)
                        continue
                return result
        except Exception as e:
            return e

    def create_signature(self, params):
        query_string = urllib.parse.urlencode(params)
        signature = hmac.new(self.secret_key.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()
        return signature

    def get_ticker_24hr(self):
        url = HashkeyAuxiliary.url_ticker.value
        datas = self._send_request(url, use_sign=False)
        return datas

    def get_exchange_information(self):
        url = HashkeyAuxiliary.url_exchange.value
        datas = self._send_request(url, use_sign=False)
        return datas

    def get_orderbook(self, symbol, limit=100):
        params = {'symbol': symbol, 'limit': limit}
        url = HashkeyAuxiliary.url_orderbook.value
        datas = self._send_request(url, params=params, use_sign=False)
        return datas

    def get_all_orders(self, start_time, end_time, limit=1000, symbol=None):
        url = HashkeyAuxiliary.url_orders.value
        params = {
            'startTime': start_time,
            'endTime': end_time,
            'limit': limit
        }
        if symbol:
            params['symbol'] = symbol
        datas = self._send_request(url, params=params)
        return datas

    def get_trades(self, order_id):
        url = HashkeyAuxiliary.url_trade.value
        params = {
            'orderId': order_id
        }
        datas = self._send_request(url, params=params)
        return datas

    def get_balance(self):
        url = HashkeyAuxiliary.url_balances.value
        datas = self._send_request(url)
        return datas
