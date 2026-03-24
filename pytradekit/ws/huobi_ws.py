import time
import json
import hmac
import base64
import hashlib
import urllib.parse
import gzip

from pytradekit.utils.dynamic_types import HuobiAuxiliary, HuobiWebSocket
from pytradekit.gateway.websocket.ws_manager import WsManager
from pytradekit.utils.time_handler import get_htx_timestamp, get_timestamp_s, get_millisecond_str, get_datetime


class HuobiWsManager(WsManager):
    _listen_key = {}

    def __init__(self, logger, queue=None, api_key=None, api_secret=None, strategy_id=None, portfolio_id=None,
                 account_id=None, url=HuobiAuxiliary.url_ws.value, api_url=HuobiAuxiliary.url.value,
                 is_reconnecting_queue=None, start_end_time_dict=None, is_public=True):
        super().__init__(api_key, logger, is_reconnecting_queue, start_end_time_dict)
        self.is_public = is_public
        self._api_url = api_url
        if not is_public:
            url = HuobiAuxiliary.url_ws_private.value
        self._url = url
        self._api_key = api_key
        self._api_secret = api_secret
        self._queue = queue
        self._strategy_id = strategy_id
        self._portfolio_id = portfolio_id
        self._account_id = account_id
        self._ws_connected = False
        self.logger = logger

    def get_signature(self, params):
        sorted_params = sorted(params.items(), key=lambda d: d[0], reverse=False)
        paramurl = urllib.parse.urlencode(sorted_params)
        payload = ["GET", 'api.huobi.pro', "/ws/v2", paramurl]
        signatureraw = "\n".join(payload)
        digest = hmac.new(self._api_secret.encode(encoding='UTF8'), signatureraw.encode(encoding='UTF8'),
                          digestmod=hashlib.sha256).digest()
        signature = base64.b64encode(digest)
        signature = signature.decode()
        return signature

    def get_sign_params(self):
        return {
            'accessKey': self._api_key,
            'signatureMethod': 'HmacSHA256',
            'signatureVersion': '2.1',
            'timestamp': get_htx_timestamp()
        }

    def _get_api_url(self) -> str:
        return self._api_url

    def _get_url(self) -> str:
        return self._url

    def _pong(self, ts) -> None:
        self.send(json.dumps({"action": "pong", "data": {"ts": ts}}))

    def _send_order(self):
        _rqs_orders = {'action': 'sub', 'ch': 'orders#*'}
        if not self._subs:
            self._subs.append(_rqs_orders)
        self.send_json(_rqs_orders)

    def _send_trade(self):
        _rqs_orders = {"action": "sub","ch": "trade.clearing#*#0"}
        if not self._subs:
            self._subs.append(_rqs_orders)
        self.send_json(_rqs_orders)

    def _wait_reconnection_time(self, n_seconds, reconnection_time) -> None:
        """Sleep in n_seconds intervals until reconnection_time expires."""
        now_times = get_timestamp_s()
        while True:
            time.sleep(n_seconds)
            if int(get_timestamp_s() - now_times) >= reconnection_time:
                return

    def _send_all_subscriptions(self) -> None:
        """Send all cached subscription requests."""
        for req in self._subs:
            self.start_subscribe(req)

    def start_bookticker_stream(self, symbol_list):
        """
        Start Huobi(HTX) spot BBO websocket stream for given symbols.

        公共行情：
        - 使用老版公共 WS 协议：{"sub": "market.xxxusdt.bbo", "id": n}
        - 逐条发送订阅
        - 定期重订阅保持连接活跃
        """
        self._subs = []

        for idx, symbol in enumerate(symbol_list, start=1):
            ch = f"market.{symbol.lower()}.bbo"
            req = {"sub": ch, "id": idx}
            self._subs.append(req)

        self._send_all_subscriptions()

        while True:
            self._wait_reconnection_time(
                HuobiAuxiliary.ws_ping_sleep.value,
                HuobiAuxiliary.reconnection_time_sleep.value,
            )
            self._send_all_subscriptions()

    def subscribe(self):
        """
        统一订阅入口：
        - 公共行情(is_public=True)：使用 {"sub": "market.xxxusdt.bbo", "id": n}，不登录
        - 私有频道(is_public=False)：保持原来的登录 + ping 逻辑
        """
        try:
            if self.is_public:
                self._send_all_subscriptions()
            else:
                self._login()

            while True:
                self._wait_reconnection_time(
                    HuobiAuxiliary.ws_ping_sleep.value,
                    HuobiAuxiliary.reconnection_time_sleep.value,
                )
                if self.is_public:
                    self._send_all_subscriptions()
                else:
                    self._login()
        except Exception as e:
            self.logger.exception(e)

    def _login(self):
        payload = self.get_sign_params()
        signature = self.get_signature(payload)
        params = {**payload, **{'authType': 'api', 'signature': signature}}
        _rqs = {'action': 'req', 'ch': 'auth', 'params': params}
        self.start_subscribe(_rqs)

    def start_subscribe(self, params):
        try:
            self.send_json(params)
        except Exception as e:
            self.logger.exception(e)

    def _on_message(self, _ws, message):
        try:
            if isinstance(message, bytes):
                message = gzip.decompress(message).decode('utf-8')
            msg = json.loads(message)
            if 'ping' in msg:
                self.send(json.dumps({'pong': msg['ping']}))
                return
            if 'action' in msg and msg['action'] == 'ping':
                # v2 心跳：
                # - 公共行情：已经在 start_bookticker_stream/subscribe 里发过订阅，这里只需要回 pong
                # - 私有频道：继续保持原有 _send_trade 逻辑
                if not self.is_public:
                    self._send_trade()
                # 公共和私有都要回 pong
                self._pong(msg['data']['ts'])
                return
            if 'ch' in msg and 'bbo' in msg['ch']:
                self._queue.put_nowait(msg)
                return
            if 'ch' in msg and 'trade' in msg['ch'] and msg['data']:
                self._queue.put_nowait(msg['data'])
                return
            # Log unhandled messages for debugging (auth responses, errors, etc.)
            if 'action' in msg and msg['action'] == 'req' and msg.get('ch') == 'auth':
                code = msg.get('code')
                if code == 200:
                    self.logger.info(f"huobi ws auth success")
                else:
                    self.logger.error(f"huobi ws auth failed: code={code}, msg={msg.get('message', '')}")
            elif 'action' in msg and msg['action'] == 'sub':
                code = msg.get('code')
                ch = msg.get('ch', '')
                if code == 200:
                    self.logger.info(f"huobi ws subscribed: {ch}")
                else:
                    self.logger.error(f"huobi ws subscribe failed: ch={ch}, code={code}, msg={msg.get('message', '')}")
        except Exception as e:
            self.logger.exception(e)
            self.logger.debug(f"huobi message error {message}")
