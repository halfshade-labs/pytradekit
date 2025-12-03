import requests
import time
import hmac
import hashlib
from urllib.parse import urlencode
from pytradekit.utils.dynamic_types import PoloniexAuxiliary
from pytradekit.utils.time_handler import get_timestamp_ms

class PoloniexClient:
    def __init__(self, logger, key=None, secret=None):
        self.base_url = PoloniexAuxiliary.url.value
        self.api_key = key
        self.secret_key = secret
        self.session = requests.Session()
        self.logger = logger

    def _send_request(self, endpoint, method="GET", params=None, use_sign=False):
        """发送 API 请求"""
        url = f"{self.base_url}{endpoint}"
        headers = {"Content-Type": "application/json"}
        params = params or {}

        if use_sign:
            params["timestamp"] = str(get_timestamp_ms())
            headers.update(self._get_signature(endpoint, params))

        try:
            if method == "GET":
                response = self.session.get(url, params=params, headers=headers)
            else:
                response = self.session.post(url, json=params, headers=headers)

            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            return {"error": f"Request failed: {str(e)}"}

    def _get_signature(self, endpoint, params):
        """生成 HMAC-SHA256 签名"""
        query_string = urlencode(params)
        message = f"{endpoint}?{query_string}"
        mac = hmac.new(self.secret_key.encode(), message.encode(), hashlib.sha256)
        return {
            "Key": self.api_key,
            "Sign": mac.hexdigest()
        }

    def get_ticker_24hr(self):
        """获取 24 小时行情"""
        url = PoloniexAuxiliary.url_ticker.value
        tickers = self._send_request(url, "GET")

        return tickers

    def get_orderbook(self, symbol, depth=100):
        """获取订单簿"""
        endpoint = f"/markets/{symbol}/orderBook"
        params = {"limit": depth}
        return self._send_request(endpoint, "GET", params)

    def get_exchange_information(self):
        """获取交易对信息"""
        url = PoloniexAuxiliary.url_exchange.value
        return self._send_request(url, "GET")
