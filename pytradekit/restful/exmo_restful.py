import hashlib
import hmac
from urllib.parse import urlencode
import requests
from pytradekit.utils.dynamic_types import ExmoAuxiliary, HttpMmthod
from pytradekit.utils.time_handler import get_timestamp_ms, get_timestamp_s


class ExmoClient:
    def __init__(self, logger, key=None, secret=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.session = requests.session()
        self.logger = logger
        if is_swap:
            self._url = ExmoAuxiliary.swap_url.value
        else:
            self._url = ExmoAuxiliary.url.value

    def sha512(self, data):
        H = hmac.new(key=bytes(self.secret_key, encoding='utf-8'), digestmod=hashlib.sha512)
        H.update(data.encode('utf-8'))
        return H.hexdigest()

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, use_sign=False):
        try:
            url = f'{self._url}{api}'
            headers = {}
            params = params if params else {}
            if use_sign:
                nonce = get_timestamp_ms()
                params['nonce'] = nonce
                params1 = urlencode(params)
                sign = self.sha512(params1)
                headers = {
                    "Content-type": "application/x-www-form-urlencoded",
                    "Key": self.api_key,
                    "Sign": sign
                }
            if method == HttpMmthod.GET.name:
                if params:
                    url = url+'?' + urlencode(params)
                response = requests.get(url=url, headers=headers, data={})
            elif method == HttpMmthod.POST.name:
                response = requests.post(url, data=params, headers=headers, timeout=3)
            else:
                return f'method {method} not support'
            return response.json()
        except Exception as e:
            return e

    def get_ticker_24hr(self):
        url = ExmoAuxiliary.url_ticker.value
        datas = self._send_request(url)
        return datas

    def get_exchange_information(self):
        url = ExmoAuxiliary.url_exchange.value
        datas = self._send_request(url)
        return datas

    def get_orderbook(self, symbol, limit=100):
        params = {'pair': symbol, 'limit': limit}
        url = ExmoAuxiliary.url_orderbook.value
        datas = self._send_request(url, method=HttpMmthod.POST.name, params=params)
        return datas

    def get_balance(self):
        url = ExmoAuxiliary.url_balance.value
        datas = self._send_request(url, method=HttpMmthod.POST.name, use_sign=True)
        return datas

    def get_deposit_withdraw_history(self):
        url = ExmoAuxiliary.url_deposit_withdraw_history.value
        datas = self._send_request(url, method=HttpMmthod.POST.name, use_sign=True)
        return datas

    def get_orders(self, pair):
        params = {"pair": pair}
        url = ExmoAuxiliary.url_orders.value
        datas = self._send_request(url, method=HttpMmthod.POST.name, params=params, use_sign=True)
        return datas

    def get_trades(self, pair):
        params = {"pair": pair}
        url = ExmoAuxiliary.url_trades.value
        datas = self._send_request(url, method=HttpMmthod.POST.name, params=params, use_sign=True)
        return datas

    def get_klines(self, pair, resolution, from_time):
        params = {"symbol": pair,
                  "resolution": resolution,
                  "from": from_time,
                  "to": get_timestamp_s()}
        url = ExmoAuxiliary.url_kline.value
        datas = self._send_request(url, params=params)
        return datas
