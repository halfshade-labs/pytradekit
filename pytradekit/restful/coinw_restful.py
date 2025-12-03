import hashlib
import hmac
import requests
import time
from urllib.parse import urlencode


class CoinWClient:
    BASE_URL = "https://api.coinw.com/api/v1"

    def __init__(self, logger, key=None, secret=None):
        self.api_key = key
        self.secret_key = secret
        self.session = requests.Session()
        self.logger = logger

    def _get_timestamp_ms(self):
        """返回当前时间戳（毫秒级）"""
        return int(time.time() * 1000)

    def _sign_params(self, params):
        """生成 HMAC-SHA256 签名"""
        params["api_key"] = self.api_key
        params["timestamp"] = self._get_timestamp_ms()
        sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = hmac.new(self.secret_key.encode(), sorted_params.encode(), hashlib.sha256).hexdigest()
        params["sign"] = signature
        return params

    def _send_request(self, endpoint, method="GET", params=None, use_sign=True):
        """发送 HTTP 请求"""
        try:
            params = params or {}
            url = f"{self.BASE_URL}{endpoint}"
            headers = {"Content-Type": "application/json"}

            if use_sign:
                params = self._sign_params(params)

            if method == "GET":
                response = self.session.get(url, params=params, headers=headers)
            elif method == "POST":
                response = self.session.post(url, json=params, headers=headers)
            else:
                return {"error": "Invalid HTTP method"}

            return response.json()
        except Exception as e:
            return {"error": str(e)}

    def get_ticker(self, symbol="BTC_USDT"):
        """获取市场行情"""
        endpoint = "/public/ticker"
        params = {"symbol": symbol}
        return self._send_request(endpoint, method="GET", params=params, use_sign=False)

    def get_balance(self):
        """查询账户余额"""
        endpoint = "/private/account"
        return self._send_request(endpoint, method="GET", use_sign=True)

    def get_orders(self, symbol=None, limit=50):
        """获取订单信息"""
        endpoint = "/private/orders"
        params = {"symbol": symbol, "limit": limit} if symbol else {"limit": limit}
        return self._send_request(endpoint, method="GET", params=params, use_sign=True)

    def get_trades(self, symbol=None, limit=50):
        """获取交易记录"""
        endpoint = "/private/trades"
        params = {"symbol": symbol, "limit": limit} if symbol else {"limit": limit}
        return self._send_request(endpoint, method="GET", params=params, use_sign=True)
