import time
import json
from threading import Thread

import requests

from pytradekit.utils.dynamic_types import BinanceAuxiliary, BinanceWebSocket, WebsocketStatus
from pytradekit.gateway.websocket.ws_manager import WsManager
from pytradekit.utils.time_handler import get_timestamp_ms, get_timestamp_s, get_millisecond_str, get_datetime, TimeSpan
from pytradekit.utils.dynamic_types import SlackUser
from pytradekit.ws.save_restful_bn_deposit_withdraw import HandleRestfulDepositWithdraw
from pytradekit.ws.bn_add_missing_orders import get_binance_trade
from pytradekit.utils.tools import get_redis


class AtUser:
    debug = f''


class BinanceWsManager(WsManager):
    _listen_key = {}

    def __init__(self, logger, config=None, running_mode=None, queue=None, api_key=None, api_secret=None,
                 strategy_id=None, portfolio_id=None,
                 account_id=None, url=BinanceAuxiliary.url_ws.value, api_url=BinanceAuxiliary.url.value, is_perp=False,
                 ticker_queue=None, kline_queue=None, start_end_time_dict=None,
                 send_params=None, is_supplement=False, bn_client=None, mm_symbol_list=None):
        super().__init__(api_key, logger, start_end_time_dict)
        self.logger = logger
        self.config = config
        self.running_mode = running_mode
        self._api_url = api_url
        if is_perp:
            self._url = BinanceAuxiliary.url_perp_ws.value
        else:
            self._url = url
        self._api_key = api_key
        self._api_secret = api_secret
        self._queue = queue
        self._ticker_queue = ticker_queue
        self._strategy_id = strategy_id
        self._portfolio_id = portfolio_id
        self._account_id = account_id
        self._ws_connected = False
        self._kline_queue = kline_queue
        # DEBUG counters (raw msg trace): first N messages logged in full, rest summarized.
        self._is_perp = is_perp
        self._msg_count = 0
        self._msg_log_full_n = 20
        if is_perp:
            self._listen_key_url = BinanceAuxiliary.perp_url.value + BinanceAuxiliary.user_perp_data_stream.value
        else:
            self._listen_key_url = self._get_api_url() + BinanceAuxiliary.user_data_stream.value
        self.start_end_time_dict = start_end_time_dict
        self._send_params = send_params
        self._is_supplement = is_supplement
        self._bn_client = bn_client
        self._mm_symbol_list = mm_symbol_list
        self.verify_bookticker_duplicate = {}

    def _get_api_url(self) -> str:
        return self._api_url

    def _get_url(self) -> str:
        return self._url

    def _is_spot(self):
        return '/fapi/' not in self._listen_key_url

    def _ws_api_listen_key(self, method):
        """Use Binance WebSocket API to manage spot listen keys.
        Binance has deprecated the REST endpoint for spot userDataStream.
        """
        import websocket as _ws_mod
        ws_api_url = 'wss://ws-api.binance.com:443/ws-api/v3'
        request_id = str(int(time.time() * 1000))
        params = {'apiKey': self._api_key}
        if method in ('userDataStream.ping', 'userDataStream.stop'):
            listen_key = self._listen_key.get('SPOT')
            if listen_key:
                params['listenKey'] = listen_key
        msg = json.dumps({'id': request_id, 'method': method, 'params': params})

        ws = _ws_mod.create_connection(ws_api_url, timeout=10)
        try:
            ws.send(msg)
            raw = ws.recv()
            resp = json.loads(raw)
            if resp.get('status') != 200:
                raise RuntimeError(f"WebSocket API {method} failed: {resp}")
            return resp.get('result', {})
        finally:
            ws.close()

    def post_listen_key(self, api):
        if self._is_spot():
            # Use WebSocket API for spot (REST endpoint deprecated, returns 410)
            max_retries = 3
            last_error = None
            for attempt in range(max_retries):
                try:
                    result = self._ws_api_listen_key('userDataStream.start')
                    self._listen_key[api] = result['listenKey']
                    self.logger.debug(f"spot listen key obtained via WebSocket API")
                    return
                except Exception as e:
                    last_error = str(e)
                    self.logger.warning(f"post_listen_key ws-api attempt {attempt + 1} failed: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
            raise RuntimeError(f"Failed to obtain spot listen key via WebSocket API after {max_retries} attempts. Last error: {last_error}")
        else:
            # Perp still uses REST endpoint
            try:
                resp = requests.post(url=self._listen_key_url, headers={'X-MBX-APIKEY': self._api_key}, timeout=10)
                if resp.status_code == 200:
                    self._listen_key[api] = resp.json()['listenKey']
                    return
                raise RuntimeError(f"HTTP {resp.status_code} from {self._listen_key_url}: {resp.text[:200]}")
            except requests.RequestException as e:
                raise RuntimeError(f"post_listen_key perp error: {e}") from e

    def put_listen_key(self):
        if self._is_spot():
            try:
                self._ws_api_listen_key('userDataStream.ping')
            except Exception as e:
                self.logger.warning(f"put_listen_key ws-api failed, re-creating: {e}")
                self.post_listen_key('SPOT')
        else:
            try:
                resp = requests.put(url=self._listen_key_url, headers={'X-MBX-APIKEY': self._api_key}, timeout=10)
                if resp.status_code == 200:
                    return
                self.logger.warning(f"put_listen_key failed: HTTP {resp.status_code} from {self._listen_key_url}: {resp.text[:200]}")
                if resp.status_code in (410, 404, 400):
                    self.logger.info("put_listen_key: listenKey expired or endpoint gone, re-creating...")
                    self.post_listen_key('SPOT')
            except Exception as e:
                self.logger.warning(f"put_listen_key error: {e}")

    def delete_listen_key(self):
        if self._is_spot():
            try:
                self._ws_api_listen_key('userDataStream.stop')
            except Exception as e:
                self.logger.warning(f"delete_listen_key ws-api error: {e}")
        else:
            try:
                resp = requests.delete(url=self._listen_key_url, headers={'X-MBX-APIKEY': self._api_key}, timeout=10)
                if resp.status_code != 200:
                    self.logger.warning(f"delete_listen_key failed: HTTP {resp.status_code} {resp.text[:200]}")
            except Exception as e:
                self.logger.warning(f"delete_listen_key error: {e}")

    def _pong(self) -> None:
        self.send(json.dumps({"pong": get_timestamp_ms()}))

    def _ping(self, n_seconds, is_listen_key=True, reconnection_time=None) -> None:
        now_times = get_timestamp_s()
        while True:
            if is_listen_key:
                try:
                    self.put_listen_key()
                except Exception as e:
                    self.logger.info(f"binance ws order trade listen key error: {e} {AtUser.debug}")
                    time.sleep(BinanceAuxiliary.ws_listen_key_sleep.value)
                    continue
            if reconnection_time:
                if int(get_timestamp_s() - now_times) >= reconnection_time:
                    # self._pong()
                    break
            time.sleep(n_seconds)
        self.subscribe()

    def _ping_market(self, n_seconds, symbols) -> None:
        reconnect_timer = BinanceAuxiliary.ws_reconnect_interval.value
        while True:
            reconnect_timer -= n_seconds
            if reconnect_timer <= 0:
                self.logger.info('Reconnecting...')
                self.start_kline_stream(symbols)
                reconnect_timer = BinanceAuxiliary.ws_reconnect_interval.value
            time.sleep(n_seconds)

    def subscribe(self):
        try:
            self.post_listen_key('SPOT')
            listen_key = self._listen_key['SPOT']
            if not self._is_spot():
                # Binance perp userDataStream only delivers events when listenKey is
                # in the URL path; the SUBSCRIBE method silently drops them on fstream.
                self._url = f"{BinanceAuxiliary.url_perp_ws.value}/{listen_key}"
                self.logger.debug(
                    f"perp user data stream connect with listen_key in url path "
                    f"(len={len(listen_key)})"
                )
                self._reconnect_with_new_url()
                self._ping(BinanceAuxiliary.ws_ping_sleep.value,
                           reconnection_time=BinanceAuxiliary.reconnection_time_sleep.value)
                return
            params = [listen_key]
            if self._send_params:
                params += self._send_params
            self.logger.debug(f"subscribe: {params}, url: {self._url}, listen key url:{self._listen_key_url}")
            self.start_subscribe(params)
            self._ping(BinanceAuxiliary.ws_ping_sleep.value,
                       reconnection_time=BinanceAuxiliary.reconnection_time_sleep.value)
        except Exception as e:
            self.logger.debug(f"subscribe error: {e}", exc_info=True)

    def _reconnect_with_new_url(self):
        # Close any existing ws so connect() rebinds to the freshly built _url.
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        self.status = WebsocketStatus.INIT.name
        self.connect()

    def start_aggtrade_stream(self, symbols):
        params = []
        for symbol in symbols:
            params.append(f'{symbol.lower()}{BinanceAuxiliary.ws_aggtrade.value}')
        self.start_subscribe(params)
        self._ping(BinanceAuxiliary.ws_ping_sleep.value, is_listen_key=False)

    def start_book_ticker_stream(self, symbols):
        params = []
        for symbol in symbols:
            params.append(f'{symbol.lower()}{BinanceAuxiliary.ws_book_ticker.value}')
        self.start_subscribe(params)
        self._ping(n_seconds=BinanceAuxiliary.ws_ping_sleep.value,
                   reconnection_time=BinanceAuxiliary.reconnection_time_sleep.value, is_listen_key=False)

    def start_ticker(self):
        params = []
        params.append(f'{BinanceAuxiliary.ws_ticker.value}')
        self.start_subscribe(params)
        self._ping(BinanceAuxiliary.ws_ping_sleep.value, is_listen_key=False)

    def start_kline_stream(self, symbols):
        params = []
        for symbol in symbols:
            params.append(f'{symbol.lower()}{BinanceAuxiliary.ws_kline_interval.value}')
        self.start_subscribe(params)
        self._ping_market(BinanceAuxiliary.ws_ping_sleep.value, symbols)

    def start_order_book_stream(self, symbols):
        params = []
        for symbol in symbols:
            params.append(f'{symbol.lower()}{BinanceAuxiliary.ws_orderbook.value}')
        self.start_subscribe(params)
        self._ping(BinanceAuxiliary.ws_ping_sleep.value)

    def start_bookticker_stream(self, symbols):
        params = []
        for symbol in symbols:
            params.append(f'{symbol.lower()}{BinanceAuxiliary.ws_book_ticker.value}')
        self.start_subscribe(params)
        self._ping(BinanceAuxiliary.ws_ping_sleep.value, is_listen_key=False)

    def start_perp_lastprice_stream(self, symbols):
        params = []
        for symbol in symbols:
            params.append(f'{symbol.lower()}{BinanceAuxiliary.ws_lastprice.value}')
        self.start_subscribe(params)
        self._ping(BinanceAuxiliary.ws_ping_sleep.value)

    def start_subscribe(self, params):
        try:
            msg = {'method': BinanceWebSocket.subscribe.value, 'params': params}
            self._subs = [msg]
            self.send_json(msg)
        except Exception as e:
            self.logger.exception(e)

    def supplement_orders(self, times):
        if self.start_end_time_dict:
            connect_status = self.start_end_time_dict.get('connect_status')
            if connect_status is True:
                self.start_end_time_dict.update({'end_time': times, 'connect_status': False})
                start_time = self.start_end_time_dict.get('start_time')

                if start_time:
                    my_redis = get_redis(logger=self.logger, config=self.config, running_mode=self.running_mode)
                    time_span = TimeSpan(start=start_time, end=times)
                    self.logger.debug(
                        f"restful start time: {time_span.start}, end time: {time_span.end}, symbol list: {self._mm_symbol_list}")
                    for symbol in self._mm_symbol_list:
                        trade_res = get_binance_trade(
                            self.logger, self._bn_client, time_span, symbol,
                            self._account_id, self._strategy_id
                        )
                        if trade_res:
                            self.logger.debug(f"symbol: {symbol}, len trade: {len(trade_res)}")
                            self.logger.debug(f"trade res0: {trade_res[0]}")
                            self.logger.debug(f"trade res-1: {trade_res[-1]}")
                        for trade in trade_res:
                            my_redis.set_publish_trades(value=trade.to_dict(),
                                                        timestamp=trade['timestamp'])
            elif connect_status is False:
                self.start_end_time_dict['start_time'] = times

    def verify_spot_bookticker_duplicate(self, msg):
        if BinanceWebSocket.order_book_update_id.value not in msg or BinanceWebSocket.symbol.value not in msg or BinanceWebSocket.orderbook_asks.value not in msg or BinanceWebSocket.orderbook_bids.value not in msg:
            return False
        if msg[BinanceWebSocket.symbol.value] not in self.verify_bookticker_duplicate:
            self.verify_bookticker_duplicate[msg[BinanceWebSocket.symbol.value]] = msg[
                                                                                       BinanceWebSocket.orderbook_asks.value] + \
                                                                                   msg[
                                                                                       BinanceWebSocket.orderbook_bids.value]
        else:
            if self.verify_bookticker_duplicate[msg[BinanceWebSocket.symbol.value]] == msg[
                BinanceWebSocket.orderbook_asks.value] + msg[BinanceWebSocket.orderbook_bids.value]:
                return False
            else:
                self.verify_bookticker_duplicate[msg[BinanceWebSocket.symbol.value]] = msg[
                                                                                           BinanceWebSocket.orderbook_asks.value] + \
                                                                                       msg[
                                                                                           BinanceWebSocket.orderbook_bids.value]
        return True

    def verify_spot_order_trade(self, msg):
        if BinanceWebSocket.event_type.value in msg and msg[
            BinanceWebSocket.event_type.value] == BinanceWebSocket.execution_report.value:
            return True
        return False

    def verify_perp_order_trade(self, msg):
        if BinanceWebSocket.event_type.value in msg and msg[
            BinanceWebSocket.event_type.value] == BinanceWebSocket.perp_order_trade.value:
            return True
        return False

    def _on_open(self, ws, *args, **kwargs):
        market = 'perp' if self._is_perp else 'spot'
        url_preview = (self._url or '')[:80]
        self.logger.debug(f"[bn_ws on_open] market={market} url={url_preview}")

    def _on_close(self, ws, *args, **kwargs):
        market = 'perp' if self._is_perp else 'spot'
        self.logger.debug(
            f"[bn_ws on_close] market={market} msg_count_so_far={self._msg_count} "
            f"close_args={args} close_kwargs={list(kwargs)}"
        )
        # delegate to parent to trigger reconnect
        super()._on_close(ws, *args, **kwargs)

    def _on_error(self, ws, error, *args, **kwargs):
        market = 'perp' if self._is_perp else 'spot'
        self.logger.debug(
            f"[bn_ws on_error] market={market} error={error!r} msg_count_so_far={self._msg_count}"
        )
        super()._on_error(ws, error, *args, **kwargs)

    def _on_message(self, _ws, message):
        msg = json.loads(message)
        # raw-msg trace: first N msgs are logged in full, rest are summarized by top-level keys.
        # Use to diagnose missing perp/spot event delivery when verify_* keeps returning False.
        self._msg_count += 1
        market = 'perp' if self._is_perp else 'spot'
        if self._msg_count <= self._msg_log_full_n:
            preview = str(message)[:400]
            self.logger.debug(
                f"[bn_ws raw #{self._msg_count}] market={market} type={type(msg).__name__} raw={preview}"
            )
        try:
            # Ticker data from !ticker@arr is a list
            if isinstance(msg, list) and self._ticker_queue:
                self._ticker_queue.put_nowait(msg)
                return
            if isinstance(msg, dict) and BinanceAuxiliary.ws_ping.value in msg:
                self._pong()
            else:
                if self.verify_perp_order_trade(msg):
                    # DEBUG per-event log — leave at DEBUG so it's off by default but available
                    # for post-mortem when a perp order's WS event appears to be missing.
                    o = msg.get(BinanceWebSocket.perp_order_data.value, {}) or {}
                    self.logger.debug(
                        f"perp ORDER_TRADE_UPDATE: symbol={o.get(BinanceWebSocket.symbol.value)} "
                        f"clientOrderId={o.get(BinanceWebSocket.client_order_id_new.value)} "
                        f"status={o.get(BinanceWebSocket.status.value)} "
                        f"side={o.get(BinanceWebSocket.side.value)}"
                    )
                    self._queue.put_nowait(msg)
                    return
                if self.verify_spot_order_trade(msg):
                    self._queue.put_nowait(msg)
                    return
                if self.verify_spot_bookticker_duplicate(msg):
                    self._queue.put_nowait(msg)
                    return
        except Exception as e:
            self.logger.exception(e)
            self.logger.debug(f"binance message error {msg}")
