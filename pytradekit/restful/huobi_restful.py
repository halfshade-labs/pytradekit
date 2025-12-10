import json
import hashlib
import hmac
import base64
from urllib.parse import urlencode, urlparse
import requests
from pytradekit.utils.time_handler import get_now_time, DATETIME_FORMAT_HB
from pytradekit.utils.dynamic_types import HttpMmthod, HuobiAuxiliary, HuobiRestful
from pytradekit.utils.static_types import FeeStructureKey


class HuobiClient:
    def __init__(self, logger, key=None, secret=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.session = requests.session()
        self.logger = logger
        if is_swap:
            self._url = HuobiAuxiliary.swap_url.value
        else:
            self._url = HuobiAuxiliary.url.value

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, use_sign=True):
        def create_sign(Params):
            host_url = urlparse(self._url).netloc
            sorted_params = sorted(Params.items(), key=lambda d: d[0], reverse=False)
            paramurl = urlencode(sorted_params)
            payload = [method, host_url, api, paramurl]
            signatureraw = "\n".join(payload)
            digest = hmac.new(self.secret_key.encode(encoding='UTF8'), signatureraw.encode(encoding='UTF8'),
                              digestmod=hashlib.sha256).digest()
            signature = base64.b64encode(digest)
            signature = signature.decode()
            return signature

        def get_sign_params():
            return {
                'AccessKeyId': self.api_key,
                'SignatureMethod': 'HmacSHA256',
                'SignatureVersion': 2,
                'Timestamp': get_now_time(a_format=DATETIME_FORMAT_HB)
            }

        url = f'{self._url}{api}'
        headers = {
            "Accept": "application/json",
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:53.0) Gecko/20100101 Firefox/53.0'
        }
        params = params or {}

        try:
            if method == 'POST':
                if use_sign:
                    new_params = get_sign_params()
                    new_params['Signature'] = create_sign(new_params)
                    url = f'{url}?{urlencode(new_params)}'
                resp = requests.post(url=url, data=json.dumps(params), headers=headers)
            elif method == "GET":
                if use_sign:
                    params.update(get_sign_params())
                    params['Signature'] = create_sign(params)
                url = f'{url}?{urlencode(params)}'
                resp = requests.get(url=url, headers=headers)
            else:
                return 'request method err'
            return resp.json()
        except Exception as e:
            return e

    def get_swap_position(self, symbol):
        params = {'contract_code': symbol}
        url = HuobiAuxiliary.url_swap_position_info.value
        datas = self._send_request(url, method=HttpMmthod.POST.name, params=params)
        return datas

    def get_swap_cross_position(self, symbol):
        params = {'contract_code': symbol}
        url = HuobiAuxiliary.url_swap_cross_position_info.value
        datas = self._send_request(url, method=HttpMmthod.POST.name, params=params)
        return datas

    def get_swap_financial_record(self, symbol, start_time, end_time, types):
        params = {'mar_acct': symbol, 'start_time': start_time, 'end_time': end_time, 'type': types, 'direct': 'prev'}
        url = HuobiAuxiliary.url_swap_financial_record.value
        datas = self._send_request(url, method=HttpMmthod.POST.name, params=params)
        return datas

    def get_ticker_24hr(self):
        url = HuobiAuxiliary.url_ticker.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=False)
        return datas

    def get_exchange_information(self):
        url = HuobiAuxiliary.url_exchange.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=False)
        return datas

    def get_orderbook(self, symbol=None):
        params = {'symbol': symbol,
                  'type': 'step0'}
        url = HuobiAuxiliary.url_orderbook.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=False)
        return datas

    def get_accounts(self):
        url = HuobiAuxiliary.url_account.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_balances(self):
        data = self.get_accounts()
        hb_account_id = data[HuobiRestful.balance_data.value][0][HuobiRestful.account_id.value]
        url = HuobiAuxiliary.url_balance.value.format(hb_account_id)
        datas = self._send_request(url, method=HttpMmthod.GET.name, use_sign=True)
        return datas

    def get_transfer_history(self):
        data = self.get_accounts()
        hb_account_id = data[HuobiRestful.balance_data.value][0][HuobiRestful.account_id.value]
        url = HuobiAuxiliary.url_transfer.value
        params = {'account-id': hb_account_id,}
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas

    def get_deposit_withdraw_history(self, types):
        params = {'type': types}
        url = HuobiAuxiliary.url_deposit_withdraw.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas

    def get_orders(self, symbol, start_time, end_time, limit=1000):
        params = {
            'symbol': symbol,
            'start-time': start_time,
            'end-time': end_time,
            'size': limit,
        }
        url = HuobiAuxiliary.url_orders.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas

    def get_trades(self, symbol, start_time, end_time):
        params = {
            'symbol': symbol,
            'start-time': start_time,
            'end-time': end_time,
        }
        url = HuobiAuxiliary.url_trades.value
        datas = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
        return datas

    def get_swap_balances(self):
        params = {'valuation_asset': 'USDT'}
        url = HuobiAuxiliary.url_swap_balance.value
        datas = self._send_request(url, method=HttpMmthod.POST.name, params=params)
        return datas

    def get_commission_rate(self, symbol=None):
        """
        Get commission rate for a symbol.
        
        Args:
            symbol: Trading symbol (e.g., 'btcusdt'), optional
        
        Returns:
            dict: {FeeStructureKey.maker.name: float, FeeStructureKey.taker.name: float} or None if failed
        """
        try:
            params = {}
            if symbol:
                params['symbol'] = symbol.lower()
            url = HuobiAuxiliary.url_commission_rate.value
            result = self._send_request(url, method=HttpMmthod.GET.name, params=params, use_sign=True)
            
            if result and isinstance(result, dict):
                if result.get('code') == 200 and 'data' in result:
                    data = result['data']
                    if isinstance(data, list) and len(data) > 0:
                        fee_data = data[0]
                    elif isinstance(data, dict):
                        fee_data = data
                    else:
                        return None
                    
                    maker_rate = float(fee_data.get('maker-fee-rate', 0))
                    taker_rate = float(fee_data.get('taker-fee-rate', 0))
                    return {FeeStructureKey.maker.name: maker_rate, FeeStructureKey.taker.name: taker_rate}
                elif isinstance(result, dict) and 'maker-fee-rate' in result:
                    maker_rate = float(result.get('maker-fee-rate', 0))
                    taker_rate = float(result.get('taker-fee-rate', 0))
                    return {FeeStructureKey.maker.name: maker_rate, FeeStructureKey.taker.name: taker_rate}
            return None
        except Exception as e:
            if self.logger:
                self.logger.info(f"Failed to get commission rate for {symbol}: {e}")
            return None
