from urllib.parse import urlencode
import hashlib
import hmac
import requests
from pytradekit.utils.time_handler import get_timestamp_s, sleep_min_time
from pytradekit.utils.dynamic_types import HttpMmthod, GateioAuxiliary

RETRY_TIMES = 0
RETRY_FREQUENCY = 3


class GateioClient:
    def __init__(self, logger, key=None, secret=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.session = requests.session()
        self.logger = logger
        self._recvWindow = 5000
        if is_swap:
            self._url = GateioAuxiliary.swap_url.value
        else:
            self._url = GateioAuxiliary.url.value

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, use_sign=True):

        url = f'{self._url}{api}'
        timestamp = get_timestamp_s()
        params = params or {}
        data = urlencode(params)
        headers = {}

        if use_sign:
            m = hashlib.sha512()
            m.update(("").encode('utf-8'))
            hashed_payload = m.hexdigest()
            signature = f'{method}\n{api}\n{data or ""}\n{hashed_payload}\n{timestamp}'
            sign = hmac.new(self.secret_key.encode('utf-8'), msg=signature.encode('utf-8'),
                            digestmod=hashlib.sha512).hexdigest()
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'KEY': self.api_key,
                'Timestamp': str(timestamp),
                'SIGN': sign
            }

        try:
            for i in range(RETRY_FREQUENCY):
                if method == HttpMmthod.GET.name:
                    if data:
                        url = f'{url}?{data}'
                    resp = requests.get(url, headers=headers, timeout=3)
                elif method == HttpMmthod.POST.name:
                    resp = requests.post(url, data=data, headers=headers, timeout=3)
                else:
                    return None
                if "X-Gate-RateLimit-Reset-Timestamp" in resp.headers['X-Gate-RateLimit-Reset-Timestamp']:
                    sleep_times = int(resp.headers['X-Gate-RateLimit-Reset-Timestamp']) - get_timestamp_s()
                    if sleep_times > RETRY_TIMES:
                        sleep_min_time(min_second=sleep_times)
                return resp.json()
        except Exception as e:
            return e

    def get_ticker_24hr(self):
        url = GateioAuxiliary.url_ticker.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=False)
        return datas

    def get_exchange_information(self):
        url = GateioAuxiliary.url_exchange.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=False)
        return datas

    def get_orderbook(self, pair=None, limit=1000):
        params = {'currency_pair': pair, 'limit': limit}
        url = GateioAuxiliary.url_orderbook.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=False)
        return datas

    def get_balances(self):
        url = GateioAuxiliary.url_balance.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_deposit_history(self):
        url = GateioAuxiliary.url_deposit_history.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_withdraw_history(self):
        url = GateioAuxiliary.url_withdraw_history.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_transfer_history(self):
        url = GateioAuxiliary.url_transfer_history.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_orders(self, pair, start_time, end_time, page):
        url = GateioAuxiliary.url_orders.value
        params = {
            'currency_pair': pair,
            'from': start_time,
            'to': end_time,
            'status': 'finished',
            'page': page,
        }
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas

    def get_trades(self, pair, start_time, end_time, page, limit=1000):
        url = GateioAuxiliary.url_trades.value
        params = {
            'currency_pair': pair,
            'from': start_time,
            'to': end_time,
            'limit': limit,
            'page': page,
        }
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas

    def get_swap_position_risk(self, settle):
        url = GateioAuxiliary.url_swap_position.value + settle + GateioAuxiliary.url_swap_position_risk.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_swap_account(self, settle):
        url = GateioAuxiliary.url_swap_position.value + settle + GateioAuxiliary.url_swap_position_accounts.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_swap_income(self, settle, start_time, end_time):
        url = GateioAuxiliary.url_swap_position.value + settle + GateioAuxiliary.url_swap_position_income.value
        params = {
            'from': start_time,
            'to': end_time,
        }
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas
