import json
import hmac
import base64
import hashlib
import urllib.parse
import gzip
from dataclasses import dataclass, field
from typing import Optional

from pytradekit.utils.dynamic_types import HuobiAuxiliary
from pytradekit.gateway.websocket.ws_manager import WsManager
from pytradekit.utils.time_handler import get_htx_timestamp, get_timestamp_s, sleep_min_time


@dataclass
class HuobiWsConfig:
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    url: str = field(default_factory=lambda: HuobiAuxiliary.url_ws.value)
    api_url: str = field(default_factory=lambda: HuobiAuxiliary.url.value)
    is_public: bool = True
    strategy_id: Optional[str] = None
    portfolio_id: Optional[str] = None
    account_id: Optional[str] = None

    def __post_init__(self):
        if not self.is_public:
            self.url = HuobiAuxiliary.url_ws_private.value


class HuobiWsManager(WsManager):

    def __init__(self, logger, queue=None, config: Optional[HuobiWsConfig] = None):
        config = config or HuobiWsConfig()
        super().__init__(config.api_key, logger, None)
        self.is_public = config.is_public
        self._api_url = config.api_url
        self._url = config.url
        self._api_key = config.api_key
        self._api_secret = config.api_secret
        self._queue = queue
        self._strategy_id = config.strategy_id
        self._portfolio_id = config.portfolio_id
        self._account_id = config.account_id
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
        if _rqs_orders not in self._subs:
            self._subs.append(_rqs_orders)
        self.send_json(_rqs_orders)

    def _send_trade(self):
        _rqs_trade = {"action": "sub", "ch": "trade.clearing#*#0"}
        if _rqs_trade not in self._subs:
            self._subs.append(_rqs_trade)
        self.send_json(_rqs_trade)

    def _wait_reconnection_time(self, n_seconds, reconnection_time) -> None:
        """Sleep in n_seconds intervals until reconnection_time expires."""
        now_times = get_timestamp_s()
        while True:
            sleep_min_time(n_seconds)
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

    def _reconnect_streams(self):
        """断线重连后的恢复钩子。

        私有 WS（is_public=False）必须先重新 auth；旧实现直接走 base 层的
        _reconnect_streams 把 _subs 里的 trade.clearing#*#0 在未登录状态下重发，
        HTX 立刻回 code=2002 invalid.auth.state，订阅在下次 subscribe() 主循环
        触发 _login（默认 3 小时）前永远是失效的。

        修复：私有连接重连后只发 auth，订阅交给 _on_message 收到 auth 成功
        回调时再补发（已有的 _send_trade 行为）。
        """
        if not self.is_public:
            self._login()
        else:
            super()._reconnect_streams()

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
                # v2 ping: only reply pong; subscriptions are handled elsewhere
                data = msg.get('data')
                if data is None:
                    self.logger.debug(f"huobi ws ping missing data: {msg}")
                    return
                self._pong(data['ts'])
                return
            if 'ch' in msg and 'bbo' in msg['ch']:
                self._queue.put_nowait(msg)
                return
            if 'ch' in msg and 'trade' in msg['ch'] and msg.get('data'):
                self._queue.put_nowait(msg['data'])
                return
            # Log unhandled messages for debugging (auth responses, errors, etc.)
            if 'action' in msg and msg['action'] == 'req' and msg.get('ch') == 'auth':
                code = msg.get('code')
                if code == 200:
                    self.logger.info(f"huobi ws auth success")
                    if not self.is_public:
                        self._send_trade()
                else:
                    self.logger.info(f"huobi ws auth failed: code={code}, msg={msg.get('message', '')}")
            elif 'action' in msg and msg['action'] == 'sub':
                code = msg.get('code')
                ch = msg.get('ch', '')
                if code == 200:
                    self.logger.info(f"huobi ws subscribed: {ch}")
                else:
                    self.logger.info(f"huobi ws subscribe failed: ch={ch}, code={code}, msg={msg.get('message', '')}")
        except Exception as e:
            self.logger.exception(e)
            self.logger.debug(f"huobi message error {message}")
