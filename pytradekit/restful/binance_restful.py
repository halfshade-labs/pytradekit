from urllib.parse import urlencode
import time

import httpx
import requests
import base64

from cryptography.hazmat.primitives import serialization
from nacl.signing import SigningKey

from pytradekit.utils import time_handler
from pytradekit.utils.dynamic_types import HttpMmthod, RestfulRequestsAttribute, BinanceAuxiliary, BinanceRestful, InstCodeType
from pytradekit.utils.exceptions import ExchangeException, MinNotionalException, InsufficientBalanceException
from pytradekit.utils.tools import async_retry_decorator
from pytradekit.utils.static_types import FeeStructureKey

RECVWINDOW = 6000
RETRY_TIMES = 10
RETRY_INTERVAL = 5


class BinanceClient:
    def __init__(self, logger, key=None, secret=None, passphrase=None, account_id=None, is_perp=False, is_alpha=False):
        self.api_key = key
        if secret is not None:
            self.secret_key = self._decrypt_private_key(secret, passphrase)
        self.passphrase = passphrase
        self.account_id = account_id
        self.session = requests.session()
        self.logger = logger
        if is_perp:
            self._url = BinanceAuxiliary.perp_url.value
        elif is_alpha:
            self._url = BinanceAuxiliary.alpha_url.value
        else:
            self._url = BinanceAuxiliary.url.value

    def _decrypt_private_key(self, private_pem: str, password: str = None) -> str:

        private_key = serialization.load_pem_private_key(
            private_pem.encode(),
            password=password.encode() if password else None,
        )

        raw_private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

        private_key_b64 = base64.b64encode(raw_private_bytes).decode("utf-8")
        return private_key_b64

    def request(self, method, url, params=None, use_sign=True):
        try:
            headers = {}
            if use_sign:
                headers["X-MBX-APIKEY"] = self.api_key

            if method == "GET":
                resp = self.session.get(url, headers=headers, params=params)
            elif method == "POST":
                resp = self.session.post(url, headers=headers, data=params)
            elif method == "DELETE":
                resp = self.session.delete(url, headers=headers, data=params)
            else:
                raise ExchangeException(f"Unsupported HTTP method: {method}")

            if resp.status_code in [418, 429]:
                retry_after = int(resp.headers.get("Retry-After", 1))
                self.logger.info(f"Request rate limited: {resp.text}, retry after {retry_after}s")
                time.sleep(retry_after)
                return None

            # Check for non-2xx status codes
            if not resp.ok:
                error_msg = f"HTTP {resp.status_code} error for {method} {url}"
                try:
                    error_body = resp.text[:500]  # Limit error message length
                    self.logger.error(f"{error_msg}, response: {error_body}")
                except Exception:
                    self.logger.error(f"{error_msg}, failed to read response body")
                raise ExchangeException(f"{error_msg}")

            # Try to parse JSON response
            try:
                result = resp.json()
                return result
            except ValueError as json_err:
                error_msg = f"Failed to parse JSON response for {method} {url}, status: {resp.status_code}"
                response_text = resp.text[:500]  # Limit error message length
                self.logger.error(f"{error_msg}, response body: {response_text}")
                raise ExchangeException(f"{error_msg}") from json_err
        except ExchangeException:
            raise
        except Exception as e:
            self.logger.exception(e)
            raise ExchangeException(f"Request error {method} {url}") from e

    @async_retry_decorator(max_retries=RETRY_TIMES, wait_time=RETRY_INTERVAL)
    async def async_request(self, method, url, use_sign=True, http_client=None, params=None):
        try:
            headers = {}
            if use_sign:
                headers.update({'X-MBX-APIKEY': self.api_key})
            if http_client:
                resp = await self.requests_result(method, url, headers, http_client, params=params)
            else:
                async with httpx.AsyncClient() as client:
                    resp = await self.requests_result(method, url, headers, client, params=params)
            if resp.status_code in [418, 429]:
                retry_after = int(resp.headers['Retry-After'])
                self.logger.info(f'Request failed {resp.content} sleep:{retry_after}')
                time.sleep(retry_after)
            result = resp.json()
            if 'code' in result:
                if result['code'] == -1013:
                    return None, MinNotionalException.__name__
                if result['code'] == -2010:
                    return None, InsufficientBalanceException.__name__
            if resp.status_code != 200:
                return None, f'http err:{resp.status_code} result:{resp.content}'
            return result, None
        except Exception as e:
            self.logger.exception(e)
            raise ExchangeException(f'request error {method} {url}') from e

    async def requests_result(self, method, url, headers, client, params=None):
        if method == 'GET':
            resp = await client.get(url, params=params, headers=headers, timeout=5)
        elif method == 'POST':
            resp = await client.post(url, params=params, headers=headers, timeout=5)
        elif method == 'DELETE':
            resp = await client.delete(url, params=params, headers=headers, timeout=5)
        else:
            return None
        return resp

    @staticmethod
    def get_timestamp():
        return time_handler.get_timestamp_ms()

    def _hashing(self, query_string):
        """
        使用 Ed25519 私钥签名（代替 HMAC-SHA256）
        返回 Base64 编码的签名字符串
        """
        try:
            # 私钥以 Base64 存储
            private_key_bytes = base64.b64decode(self.secret_key)
            signing_key = SigningKey(private_key_bytes)

            signed = signing_key.sign(query_string.encode("ASCII")).signature
            signature_b64 = base64.b64encode(signed).decode("ASCII")
            return signature_b64
        except Exception as e:
            self.logger.exception(e)
            raise ExchangeException("Ed25519 签名失败") from e

    def _make_public_url(self, url_path, params):
        query_string = urlencode(params, True)
        url = self._url + url_path
        if query_string:
            url = url + '?' + query_string
        return url

    def _make_private_url(self, url_path, params, use_sign=True, timestamp=True):
        timestamp1 = self.get_timestamp()
        params['timestamp'] = timestamp1
        payload = '&'.join([f'{param}={value}' for param, value in params.items()])
        if use_sign:
            signature = self._hashing(payload)
            params['signature'] = signature
        url = f"{self._url}{url_path}"
        return url, params, timestamp1

    def get_exchange_information(self):
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_exchange.value,
                                                params={}, use_sign=False, timestamp=False)
        exch_info = self.request(HttpMmthod.GET.name, url, use_sign=False)
        return exch_info

    def get_alpha_exchange_information(self):
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_alpha_exchange_info.value,
                                                params={}, use_sign=False, timestamp=False)
        exch_info = self.request(HttpMmthod.GET.name, url, use_sign=False)
        return exch_info

    def get_account_information(self):
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_balance.value,
                                                params={})
        account = self.request(HttpMmthod.GET.name, url, params=params)
        return account

    def get_account_trade_list(self, symbol, start_time=None, end_time=None):
        params = {'symbol': symbol}
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_trade.value,
                                                params=params)
        trade_list = self.request(HttpMmthod.GET.name, url, params=params)
        return trade_list

    def get_account_all_orders(self, symbol):
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_all_order.value,
                                                params={RestfulRequestsAttribute.symbol: symbol})

        all_orders = self.request(HttpMmthod.GET.name, url, params=params)
        return all_orders

    def get_account_open_orders(self, symbol=None):
        params = {}
        if symbol:
            params['symbol'] = symbol
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_open_order.value,
                                                params=params)

        open_orders = self.request(HttpMmthod.GET.name, url, params=params)
        return open_orders

    def get_ticker_24hr(self, symbol=None):
        params = {}
        if symbol is not None:
            params[RestfulRequestsAttribute.symbol.name] = symbol.upper()
        url = self._make_public_url(url_path=BinanceAuxiliary.url_ticker_24hr.value,
                                    params=params)
        price = self.request(HttpMmthod.GET.name, url, use_sign=False)
        return price

    def get_perp_ticker_24hr(self, symbol=None):
        params = {}
        if symbol is not None:
            params[RestfulRequestsAttribute.symbol.name] = symbol.upper()
        url = self._make_public_url(url_path=BinanceAuxiliary.url_perp_ticker_24hr.value,
                                    params=params)
        stats = self.request(HttpMmthod.GET.name, url, use_sign=False)
        return stats

    def get_orderbook(self, symbol, limit=None):
        params = {RestfulRequestsAttribute.symbol.name: symbol}
        if limit is not None:
            params[RestfulRequestsAttribute.limit.name] = limit
        url = self._make_public_url(url_path=BinanceAuxiliary.url_orderbook.value,
                                    params=params)
        ob = self.request(HttpMmthod.GET.name, url, use_sign=False)
        return ob

    def get_klines(self, symbol, interval, from_date=None, to_date=None):
        params = {RestfulRequestsAttribute.symbol.name: symbol,
                  RestfulRequestsAttribute.interval.name: interval}
        if from_date:
            params[RestfulRequestsAttribute.startTime.name] = from_date * 1000
        if to_date:
            params[RestfulRequestsAttribute.endTime.name] = to_date * 1000
        url = self._make_public_url(url_path=BinanceAuxiliary.url_kline.value,
                                    params=params)
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    def get_balances(self):
        params = {}
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_balance.value,
                                                params=params)
        balances = self.request(HttpMmthod.GET.name, url, params=params)
        return balances

    def get_wallet(self, quoteasset):
        params = {BinanceRestful.balance_wallet_quote_asset.value: quoteasset}
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_wallet.value,
                                                params=params)
        balances = self.request(HttpMmthod.GET.name, url, params=params)
        return balances

    def get_deposit_history(self):
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_deposit_history.value,
                                                params={'limit': 10})
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    def get_withdraw_history(self):
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_withdraw_history.value,
                                                params={'limit': 10})
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    def get_transfer_history(self):
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_transfer_history.value,
                                                params={
                                                    'limit': 10
                                                })
        try:
            datas = self.request(HttpMmthod.GET.name, url, params=params)
        except:
            return []
        return datas

    def get_transfer_history_sub(self):
        url_in, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_transfer_sub_history.value,
                                                   params={
                                                       'type': 1,
                                                       'limit': 10
                                                   })
        data_in = self.request(HttpMmthod.GET.name, url_in, params=params)
        url_out, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_transfer_sub_history.value,
                                                    params={
                                                        'type': 2,
                                                        'limit': 10
                                                    })
        data_out = self.request(HttpMmthod.GET.name, url_out, params=params)
        datas = data_in + data_out
        return datas

    def get_swap_funding_rate(self, symbol=None, start_time=None, end_time=None, limit=1000):
        params = {}
        if symbol:
            params['symbol'] = symbol
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time
        if limit:
            params['limit'] = limit
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_swap_funding_rate.value,
                                                params=params, use_sign=False)
        datas = self.request(HttpMmthod.GET.name, url, use_sign=False)
        return datas

    def get_swap_last_funding_rate(self, symbol=None):
        params = {}
        if symbol:
            params['symbol'] = symbol
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_swap_last_funding_rate.value,
                                                params=params, use_sign=False)
        datas = self.request(HttpMmthod.GET.name, url, use_sign=False)
        return datas

    def get_swap_last_funding_rate_info(self):
        params = {}
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_swap_last_funding_rate_info.value,
                                                params=params, use_sign=False)
        datas = self.request(HttpMmthod.GET.name, url, use_sign=False)
        return datas

    def get_swap_ticker_price(self, symbol=None):
        params = {}
        if symbol:
            params['symbol'] = symbol
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_swap_ticker_price.value,
                                                params=params, use_sign=False)
        datas = self.request(HttpMmthod.GET.name, url, use_sign=False)
        return datas

    def get_swap_open_interes_hist(self, symbol, period, limit=None, start_time=None, end_time=None):
        params = {}
        params['symbol'] = symbol
        params['period'] = period
        if limit:
            params['limit'] = limit
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time

        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_swap_open_interes_hist.value,
                                                params=params)
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    def get_swap_all_order(self, symbol, limit=None, start_time=None, end_time=None):
        params = {}
        params['symbol'] = symbol
        if limit:
            params['limit'] = limit
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time

        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_swap_all_order.value,
                                                params=params)
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    def get_swap_force_order(self, symbol, limit=None, start_time=None, end_time=None):
        params = {}
        params['symbol'] = symbol
        if limit:
            params['limit'] = limit
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time

        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_swap_force_order.value,
                                                params=params)
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    def get_swap_position_risk(self, symbol=None):
        params = {}
        if symbol:
            params['symbol'] = symbol
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_swap_position_risk.value,
                                                params=params)
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    def get_swap_income(self, symbol=None, start_time=None, end_time=None, income_type='FUNDING_FEE'):
        params = {}
        if symbol:
            params['symbol'] = symbol
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time
        params['incomeType'] = income_type
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_swap_income.value,
                                                params=params)
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    def get_swap_balance(self):
        params = {}
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_swap_balance.value,
                                                params=params)
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    def get_swap_account(self):
        params = {}
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_swap_account.value,
                                                params=params)
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    def get_swap_multi_margin(self):
        params = {}
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_swap_multi_margin.value,
                                                params=params)
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    def update_swap_margin_type(self, symbol, margin_type):
        params = {'symbol': symbol, 'marginType': margin_type}
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_swap_margin_type.value,
                                                params=params)
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    async def place_market_order(self, symbol, client_order_id, side, volume, http_client=None):
        params = {'symbol': symbol, 'side': side, 'type': 'MARKET',
                  'quantity': volume, 'newClientOrderId': client_order_id, 'selfTradePreventionMode': 'EXPIRE_MAKER'}

        url, params, timestamp = self._make_private_url(url_path=BinanceAuxiliary.url_order.value,
                                                        params=params)
        datas, err = await self.async_request(HttpMmthod.POST.name, url, http_client=http_client, params=params)
        return datas, err, timestamp

    async def place_maker_order(self, symbol, client_order_id, side, volume, price, http_client=None):
        params = {'symbol': symbol, 'side': side, 'type': 'LIMIT_MAKER', 'price': price,
                  'quantity': volume, 'newClientOrderId': client_order_id, 'selfTradePreventionMode': 'EXPIRE_MAKER'}

        url, params, timestamp = self._make_private_url(url_path=BinanceAuxiliary.url_order.value,
                                                        params=params)
        datas, err = await self.async_request(HttpMmthod.POST.name, url, http_client=http_client, params=params)
        return datas, err, timestamp

    async def place_limit_order(self, symbol, client_order_id, side, volume, price, http_client=None):
        params = {'symbol': symbol, 'side': side, 'type': 'LIMIT', 'price': price, 'timeInForce': 'GTC',
                  'quantity': volume, 'newClientOrderId': client_order_id, 'selfTradePreventionMode': 'EXPIRE_TAKER'}

        url, params, timestamp = self._make_private_url(url_path=BinanceAuxiliary.url_order.value,
                                                        params=params)
        datas, err = await self.async_request(HttpMmthod.POST.name, url, http_client=http_client, params=params)
        return datas, err, timestamp

    def get_trade(self, symbol, order_id=None, start_time=None, end_time=None, from_id=None):
        params = {'symbol': symbol, 'limit': BinanceAuxiliary.url_limit.value}
        if order_id:
            params['orderId'] = order_id
        if from_id:
            params['fromId'] = from_id
        else:
            if start_time:
                params['startTime'] = start_time
            if end_time:
                params['endTime'] = end_time
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_trade.value,
                                                params=params)
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    def get_order(self, symbol, order_id=None, start_time=None, end_time=None):
        params = {'symbol': symbol, 'limit': BinanceAuxiliary.url_limit.value}
        if order_id:
            params['orderId'] = order_id
        else:
            if start_time:
                params['startTime'] = start_time
            if end_time:
                params['endTime'] = end_time

        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_all_order.value,
                                                params=params)
        datas = self.request(HttpMmthod.GET.name, url, params=params)
        return datas

    async def cancel_order(self, symbol, order_id=None, client_order_id=None, http_client=None):
        params = {'symbol': symbol}
        if order_id:
            params['orderId'] = order_id
        if client_order_id:
            params['origClientOrderId'] = client_order_id
        url, params, timestamp = self._make_private_url(url_path=BinanceAuxiliary.url_order.value,
                                                        params=params)
        datas, err = await self.async_request(HttpMmthod.DELETE.name, url, http_client, params=params)
        return datas, err, timestamp

    def restful_place_market_order(self, symbol, client_order_id, side, volume):
        params = {'symbol': symbol, 'side': side, 'type': 'MARKET',
                  'quantity': volume, 'newClientOrderId': client_order_id, 'selfTradePreventionMode': 'EXPIRE_MAKER'}

        url, params, timestamp = self._make_private_url(url_path=BinanceAuxiliary.url_order.value,
                                                        params=params)
        datas = self.request(HttpMmthod.POST.name, url, params=params)
        return datas, timestamp

    def cancel_symbol_all_order(self, symbol):
        params = {'symbol': symbol}
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_cancel_all_order.value,
                                                params=params)
        datas = self.request(HttpMmthod.DELETE.name, url)
        return datas

    def restful_place_limit_order(self, symbol, client_order_id, side, volume, price):
        params = {'symbol': symbol, 'side': side, 'type': 'LIMIT', 'price': price, 'timeInForce': 'GTC',
                  'quantity': volume, 'newClientOrderId': client_order_id, 'selfTradePreventionMode': 'EXPIRE_TAKER'}

        url, params, timestamp = self._make_private_url(url_path=BinanceAuxiliary.url_order.value,
                                                        params=params)
        datas = self.request(HttpMmthod.POST.name, url, params=params)
        return datas, timestamp

    def get_funding_balance(self):
        params = {}
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_funding_balance.value,
                                                params=params)
        balances = self.request(HttpMmthod.POST.name, url, params=params)
        return balances

    def get_alpha_balance(self):
        params = {}
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_alpha_balance.value,
                                                params=params)
        balances = self.request(HttpMmthod.GET.name, url, params=params)
        return balances

    def get_alpha_coin(self):
        params = {}
        url, params, _ = self._make_private_url(url_path=BinanceAuxiliary.url_alpha_coin.value,
                                                params=params)
        balances = self.request(HttpMmthod.GET.name, url, params=params)
        return balances

    def get_commission_rate(self, symbol):
        """
        Get commission rate for a symbol.
        
        Args:
            symbol: Trading symbol (e.g., 'BTCUSDT')
        
        Returns:
            dict: {FeeStructureKey.maker.name: float, FeeStructureKey.taker.name: float} or None if failed
        """
        try:
            params = {'symbol': symbol}
            
            # Use different API endpoints for spot and futures
            if hasattr(self, '_url') and 'fapi' in self._url:
                # Futures API: /fapi/v1/commissionRate
                url, params, _ = self._make_private_url(
                    url_path=BinanceAuxiliary.url_commission_rate.value,
                    params=params
                )
                result = self.request(HttpMmthod.GET.name, url, params=params)
                
                if result and isinstance(result, dict):
                    maker_rate = float(result.get('makerCommissionRate', 0))
                    taker_rate = float(result.get('takerCommissionRate', 0))
                    return {FeeStructureKey.maker.name: maker_rate, FeeStructureKey.taker.name: taker_rate}
            else:
                # Spot API: /api/v3/account/commission
                url, params, _ = self._make_private_url(
                    url_path=BinanceAuxiliary.url_spot_commission_rate.value,
                    params=params
                )
                result = self.request(HttpMmthod.GET.name, url, params=params)
                
                if result and isinstance(result, dict):
                    # Spot API returns standardCommission with maker/taker rates
                    standard_commission = result.get('standardCommission', {})
                    if standard_commission:
                        maker_rate = float(standard_commission.get('maker', 0))
                        taker_rate = float(standard_commission.get('taker', 0))
                        return {FeeStructureKey.maker.name: maker_rate, FeeStructureKey.taker.name: taker_rate}
            
            return None
        except Exception as e:
            market_type = InstCodeType.PERP.name if hasattr(self, '_url') and 'fapi' in self._url else InstCodeType.SPOT.name
            if self.logger:
                self.logger.info(f"Failed to get commission rate from Binance {market_type} for {symbol}: {e}")
            return None


class BinanceSwapClient(BinanceClient):
    def __init__(self, logger, key=None, secret=None, passphrase=None, account_id=None):
        super().__init__(logger, key, secret, passphrase, account_id)
        self._url = BinanceAuxiliary.perp_url.value


class BinanceAlphaClient(BinanceClient):
    def __init__(self, logger, key=None, secret=None, passphrase=None, account_id=None):
        super().__init__(logger, key, secret, passphrase, account_id)
        self._url = BinanceAuxiliary.alpha_url.value
