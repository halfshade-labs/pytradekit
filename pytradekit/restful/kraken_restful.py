import time
import hmac
import hashlib
import requests
import base64
from urllib.parse import urlencode
from pytradekit.utils.dynamic_types import HttpMmthod, KrakenAuxiliary
from pytradekit.utils.time_handler import get_timestamp_ms


class KrakenClient:

    def __init__(self, logger, key=None, secret=None, account_id=None):
        self.api_key = key
        self.api_secret = secret
        self.session = requests.Session()
        self.account_id = account_id
        self.logger = logger

    def _get_signature(self, url_path, data):
        post_data = urlencode(data).encode()
        message = url_path.encode() + hashlib.sha256(data["nonce"].encode() + post_data).digest()
        mac = hmac.new(base64.b64decode(self.api_secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, use_sign=False):
        try:
            url = f"{KrakenAuxiliary.url.value}{api}"
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            params = params or {}

            if use_sign:
                params["nonce"] = str(int(time.time() * 1000))
                signature = self._get_signature(api, params)
                headers.update({
                    "API-Key": self.api_key,
                    "API-Sign": signature
                })
            response = self.session.post(url, data=params, headers=headers) if method == HttpMmthod.POST.name else \
                self.session.get(url, params=params, headers=headers)

            return response.json()

        except requests.exceptions.RequestException as e:
            return {"error": f"Request failed: {str(e)}"}
        except ValueError as e:
            return {"error": f"JSON Decode Error: {str(e)}"}

    def get_balance(self):
        url = KrakenAuxiliary.url_balance.value
        return self._send_request(url, method=HttpMmthod.POST.name, use_sign=True)

    def get_orderbook(self, symbol):
        url = KrakenAuxiliary.url_orderbook.value
        params = {'pair': symbol}
        return self._send_request(url, params=params)

    def get_ticker_24hr(self, symbol=None):
        url = KrakenAuxiliary.url_ticker.value
        if symbol:
            params = {'pair': symbol}
            return self._send_request(url, params=params)
        else:
            return self._send_request(url)

    def get_exchange_information(self):
        url = KrakenAuxiliary.url_exchange.value
        return self._send_request(url, method=HttpMmthod.GET.name)

    def convert_kraken_symbol(self):
        info_res = self.get_exchange_information()
        convert_symbol = {}
        for symbol, info in info_res['result'].items():
            convert_symbol[symbol] = info['wsname'].replace('/', '')
        return convert_symbol

    def get_orders(self, start_time, end_time):
        url = KrakenAuxiliary.url_orders.value
        params = {"trades": True, 'start': start_time, 'end': end_time}
        return self._send_request(url, method=HttpMmthod.POST.name, params=params, use_sign=True)

    def get_trades(self, start_time, end_time):
        url = KrakenAuxiliary.url_trades.value
        params = {"type": "all", "trades": True, 'start': start_time, 'end': end_time}
        return self._send_request(url, method=HttpMmthod.POST.name, params=params, use_sign=True)

    def convert_kraken_coin(self):
        url = '/0/public/Assets'
        info_res = self._send_request(url)
        convert_coin = {}
        for coin, info in info_res['result'].items():
            convert_coin[coin] = info['altname']
        return convert_coin
