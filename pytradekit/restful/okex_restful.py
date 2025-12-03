import json
import hashlib
import hmac
import base64
from urllib.parse import urlencode
import requests
from pytradekit.utils.time_handler import get_ok_timestamp
from pytradekit.utils.dynamic_types import HttpMmthod, OkexAuxiliary, InstCodeType


class OkexClient:
    def __init__(self, logger, key=None, secret=None, passphrase=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.passphrase = passphrase
        self.session = requests.session()
        self.logger = logger
        if is_swap:
            self._url = OkexAuxiliary.swap_url.value
        else:
            self._url = OkexAuxiliary.url.value

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, use_sign=True):
        url = f'{self._url}{api}'
        headers = {}
        params = params or {}
        timestamp = get_ok_timestamp()

        path = api
        if method == HttpMmthod.GET.name:
            body = ''
            if params:
                path = f'{api}?{urlencode(params)}'
        else:
            body = json.dumps(params)
        message = f'{timestamp}{method}{path}{body}'
        if use_sign:
            sign = hmac.new(self.secret_key.encode('utf-8'), msg=message.encode('utf-8'),
                            digestmod=hashlib.sha256).digest()
            sign = base64.b64encode(sign)

            headers['Content-Type'] = 'application/json'
            headers['OK-ACCESS-KEY'] = self.api_key
            headers['OK-ACCESS-SIGN'] = sign
            headers['OK-ACCESS-TIMESTAMP'] = timestamp
            headers['OK-ACCESS-PASSPHRASE'] = self.passphrase

        try:
            if method == HttpMmthod.GET.name:
                if params:
                    url = f'{url}?{urlencode(params)}'
                resp = requests.get(url, headers=headers)
            elif method == HttpMmthod.POST.name:
                resp = requests.post(url, json=params, headers=headers)
            else:
                return f'method {method} not support'
            if resp.status_code != 200:
                return f'http err:{resp.status_code} result:{resp.content}'
            result = resp.json()
            return result
        except Exception as e:
            return e

    def get_swap_position(self, symbol):
        params = {'instType': InstCodeType.SWAP.name, 'instId': symbol + "-" + InstCodeType.SWAP.name}
        url = OkexAuxiliary.url_swap_position.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params)
        return datas

    def get_swap_interest(self, start_time, end_time):
        params = {'after': start_time, 'before': end_time}
        url = OkexAuxiliary.url_swap_interest.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params)
        return datas

    def get_ticker_24hr(self, inst_type='SPOT'):
        params = {'instType': inst_type}
        url = OkexAuxiliary.url_ticker.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=False)
        return datas

    def get_exchange_information(self, inst_type='SPOT'):
        params = {'instType': inst_type}
        url = OkexAuxiliary.url_exchange.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=False)
        return datas

    def get_orderbook(self, symbol=None, limit=100):
        params = {'instId': symbol,
                  "sz": limit}
        url = OkexAuxiliary.url_orderbook.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=False)
        return datas

    def get_balances(self):
        url = OkexAuxiliary.url_balance.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_asset_balances(self):
        url = OkexAuxiliary.url_asset_balance.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_deposit_history(self):
        url = OkexAuxiliary.url_deposit_history.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_withdraw_history(self):
        url = OkexAuxiliary.url_withdraw_history.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_transfer_history(self):
        url = OkexAuxiliary.url_transfer_history.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_orders(self, order_id, start_time, end_time):
        params = {'instType': InstCodeType.SPOT.name,
                  "begin": start_time,
                  "end": end_time,
                  }
        if order_id:
            params['after'] = order_id
        url = OkexAuxiliary.url_orders.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas

    def get_trades(self, trade_id, start_time, end_time):
        params = {'instType': InstCodeType.SPOT.name,
                  "begin": start_time,
                  "end": end_time,
                  }
        if trade_id:
            params['after'] = trade_id
        url = OkexAuxiliary.url_trades.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas
