import requests

from pytradekit.utils.dynamic_types import MercadoAuxiliary, HttpMmthod
from pytradekit.utils.time_handler import get_timestamp_ms



class MercadoClient:
    def __init__(self, logger, key=None, secret=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.session = requests.session()
        self.logger = logger
        if is_swap:
            self._url = MercadoAuxiliary.swap_url.value
        else:
            self._url = MercadoAuxiliary.url.value

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, auth=None):

        try:
            url = f'{self._url}{api}'
            headers = {}
            if auth:
                headers['Authorization'] = f'Bearer {auth}'
            if method == HttpMmthod.GET.name:
                response = requests.get(url=url, params=params, headers=headers)
            elif method == HttpMmthod.POST.name:
                response = requests.post(url=url, data=params, headers=headers)
            else:
                return f'method {method} not support'
            return response.json()
        except Exception as e:
            return e

    def get_exchange_information(self):
        url = MercadoAuxiliary.url_exchange.value
        datas = self._send_request(url)
        return datas

    def get_orderbook(self, symbol, limit=100):
        params = {'limit': limit}
        url = "/" + symbol + MercadoAuxiliary.url_orderbook.value
        datas = self._send_request(url, params=params)
        return datas

    def get_ticker_24hr(self, symbols):
        url = f"{MercadoAuxiliary.url_ticker.value}?symbols={symbols}"
        datas = self._send_request(url)
        return datas

    def get_accounts(self, auth):
        url = f"{MercadoAuxiliary.url_accounts.value}"
        datas = self._send_request(url, auth=auth)
        return datas

    def get_authorize(self):
        url = f"{MercadoAuxiliary.url_auth.value}"
        params = {'login': self.api_key, 'password': self.secret_key}
        datas = self._send_request(url, method=HttpMmthod.POST.name, params=params)
        return datas

    def get_orders(self, auth, account_id, pair, start_tmp, end_tmp):
        params = {'created_at_from': start_tmp, 'created_at_to': end_tmp}
        url = f"{MercadoAuxiliary.url_accounts.value}/{account_id}/{pair}/{MercadoAuxiliary.url_orders.value}"
        datas = self._send_request(url, auth=auth, params=params)
        return datas

    def get_trades(self, auth, account_id, order_id, symbol):
        url = f"{MercadoAuxiliary.url_accounts.value}/{account_id}/{symbol}/{MercadoAuxiliary.url_orders.value}/{order_id}"
        datas = self._send_request(url, auth=auth)
        return datas

    def get_balance(self, auth, account_id):
        url = f"{MercadoAuxiliary.url_accounts.value}/{account_id}/{MercadoAuxiliary.url_balance.value}"
        datas = self._send_request(url, auth=auth)
        return datas

    def get_klines(self, symbol, countback):
        params = {'symbol': symbol,
                  'resolution': '1d',
                  'to': get_timestamp_ms(),
                  'countback': countback}
        url = MercadoAuxiliary.url_kline.value
        datas = self._send_request(url, params=params)
        return datas
