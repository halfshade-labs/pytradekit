import time
import json
from threading import Thread

import requests
from websocket import WebSocketApp

from pytradekit.utils.dynamic_types import BinanceAuxiliary, BinanceWebSocket
from pytradekit.utils.time_handler import get_timestamp_ms, get_timestamp_s
from pytradekit.utils.dynamic_types import SlackUser, WebsocketStatus
from pytradekit.utils.exceptions import ExchangeException


class AtUser:
    debug = f''


class BinanceWsManager:
    _listen_key = {}

    def __init__(self, logger, queue=None, api_key=None, strategy_id=None, portfolio_id=None, account_id=None,
                 url=BinanceAuxiliary.url_ws.value, api_url=BinanceAuxiliary.url.value, is_swap=False,
                 ticker_queue=None):
        self._api_url = api_url
        self._url = url
        self._api_key = api_key
        self._queue = queue
        self._ticker_queue = ticker_queue
        self._strategy_id = strategy_id
        self._portfolio_id = portfolio_id
        self._account_id = account_id
        self.logger = logger
        self._cur_id = 0
        self._ping_interval = 0
        self._msg = []
        self.status = WebsocketStatus.INIT.name
        self.ws = None
        if is_swap:
            self._listen_key_url = BinanceAuxiliary.swap_url.value + BinanceAuxiliary.user_swap_data_stream.value
        else:
            self._listen_key_url = self._get_api_url() + BinanceAuxiliary.user_data_stream.value

    def _get_api_url(self) -> str:
        return self._api_url

    def _get_url(self) -> str:
        return self._url

    def post_listen_key(self, api):
        self._listen_key[api] = requests.post(url=self._listen_key_url, headers={'X-MBX-APIKEY': self._api_key}).json()[
            'listenKey']

    def put_listen_key(self):
        requests.put(url=self._listen_key_url, headers={'X-MBX-APIKEY': self._api_key}).json()

    def delete_listen_key(self):
        requests.delete(url=self._listen_key_url, headers={'X-MBX-APIKEY': self._api_key}).json()

    def _pong(self) -> None:
        now_times = get_timestamp_s()
        self.ws.send(json.dumps(self._msg))
        while self.status != WebsocketStatus.STOP.name:
            if int(get_timestamp_s() - now_times) >= BinanceAuxiliary.ws_ping_sleep.value:
                now_times = get_timestamp_s()
                self.ws.send(json.dumps({"pong": get_timestamp_ms()}))

    def _ping(self) -> None:
        now_times = get_timestamp_s()
        while self.status != WebsocketStatus.STOP.name:
            try:
                if int(get_timestamp_s() - now_times) >= BinanceAuxiliary.ws_ping_sleep.value:
                    self.put_listen_key()
            except Exception as e:
                self.logger.info(f"binance ws order trade listen key error: {e} {AtUser.debug}")
                time.sleep(BinanceAuxiliary.ws_listen_key_sleep.value)
                continue
            if int(get_timestamp_s() - now_times) >= BinanceAuxiliary.reconnection_time_sleep.value:
                self.status = WebsocketStatus.STOP.name
        self.subscribe()

    def subscribe(self):
        try:
            self.status = WebsocketStatus.INIT.name
            self.post_listen_key('SPOT')
            self.start_subscribe([self._listen_key['SPOT']])
        except Exception as e:
            self.logger.exception(e)

    def start_subscribe(self, params):
        try:
            self._msg = {'method': BinanceWebSocket.subscribe.value, 'params': params, 'id': self._cur_id}
            self._cur_id += 1
            self._run_websocket()
        except Exception as e:
            self.logger.exception(e)
            self.logger.info(f"binance ws start subscribe error: {e} {AtUser.debug}")

    def _run_websocket(self):
        try:
            self.ws = WebSocketApp(self._get_url(),
                                   on_message=self._on_message,
                                   on_open=self._on_open,
                                   on_close=self._on_close,
                                   on_error=self._on_error, )
            self.ws.run_forever(ping_interval=self._ping_interval)
            self.status = WebsocketStatus.ACTIVE.name
        except Exception as e:
            raise ExchangeException(f'Unexpected error while running websocket: {e}') from e

    def _on_close(self, _ws):
        self.logger.debug("WebSocket connection closed")
        self.status = WebsocketStatus.STOP.name
        self.subscribe()

    def _on_error(self, _ws, error):
        self.logger.debug("WebSocket error occurred: " + str(error))
        self.status = WebsocketStatus.STOP.name
        self.subscribe()

    def _on_open(self):
        self.logger.debug("WebSocket connection opened")
        pong = Thread(target=self._pong)
        pong.start()
        ping = Thread(target=self._ping)
        ping.start()

    def _on_message(self, _ws, message):
        try:
            msg = json.loads(message)
            if isinstance(msg, list):
                self._ticker_queue.put_nowait(msg)
            if BinanceWebSocket.event_type.value in msg:
                if msg[BinanceWebSocket.event_type.value] == BinanceWebSocket.execution_report.value:
                    msg[BinanceWebSocket.portfolio_id.value] = self._portfolio_id
                    msg[BinanceWebSocket.strategy_id.value] = self._strategy_id
                    msg[BinanceWebSocket.account_id.value] = self._account_id
                    self._queue.put_nowait(msg)
                elif msg[BinanceWebSocket.event_type.value] == BinanceWebSocket.account_balance.value:
                    msg[BinanceWebSocket.portfolio_id.value] = self._portfolio_id
                    msg[BinanceWebSocket.strategy_id.value] = self._strategy_id
                    msg[BinanceWebSocket.account_id.value] = self._account_id
                    self._queue.put_nowait(msg)
                elif msg[BinanceWebSocket.event_type.value] == BinanceWebSocket.balance_update.value:
                    msg[BinanceWebSocket.portfolio_id.value] = self._portfolio_id
                    msg[BinanceWebSocket.strategy_id.value] = self._strategy_id
                    msg[BinanceWebSocket.account_id.value] = self._account_id
                    self._queue.put_nowait(msg)
                elif msg[BinanceWebSocket.event_type.value] == BinanceWebSocket.agg_trade.value:
                    self._queue.put_nowait(msg)
                elif msg[BinanceWebSocket.event_type.value] == BinanceWebSocket.kline_raw.value:
                    self._queue.put_nowait(msg)
        except Exception as e:
            self.logger.exception(e)
