import hashlib
import hmac
import json
import base64
from binascii import a2b_hex
from urllib import parse
import requests
from Cryptodome.Cipher import AES
from pytradekit.utils.time_handler import get_now_time, DATETIME_FORMAT_ADS
from pytradekit.utils.tools import encrypt_decrypt
from pytradekit.utils.dynamic_types import AdsAuxiliary
from pytradekit.utils.exceptions import DependencyException


class AdsApi:
    def __init__(self, ads_key, ads_secret):
        self._api_key = encrypt_decrypt(ads_key, 'decrypt')
        self._secret_key = encrypt_decrypt(ads_secret, 'decrypt')
        self.pro_env = AdsAuxiliary.pro_url.value

    def get_request(self, url, params=None):
        if params is None:
            params = {}
        params.update({
            "AccessKeyId": self._api_key,
            "SignatureMethod": "HmacSHA256",
            "SignatureVersion": "2",
            "Timestamp": get_now_time(a_format=DATETIME_FORMAT_ADS)
        })
        params["Signature"] = self.create_signature("GET", url, params)
        headers = {
            "Accept": "application/json",
            "Content-type": "application/json",
            "User-agent": "Mozilla 5.10"
        }
        response = requests.get(url, params=params, headers=headers, timeout=10)
        if response.status_code == 200:
            result = response.json()
            if result.get("status") == "ok" or result.get("success") or result.get("code") == 200:
                return result.get("data", result)
            else:
                raise DependencyException(response)
        else:
            raise DependencyException("Get Func Fail")

    def create_signature(self, method, url, params):
        sorted_params = sorted(params.items(), key=lambda x: x[0])
        query_string = '&'.join([f"{key}={parse.quote(str(value))}" for key, value in sorted_params])
        payload = f"{method}\n{parse.urlparse(url).hostname}\n{parse.urlparse(url).path}\n{query_string}"
        dig = hmac.new(self._secret_key.encode("utf-8"), msg=payload.encode("utf-8"), digestmod=hashlib.sha256).digest()
        return base64.b64encode(dig).decode()

    def aes_msg(self, en_text):
        aes = AES.new(self._secret_key.encode(), AES.MODE_ECB)
        den_text = aes.decrypt(a2b_hex(en_text))
        return json.loads(den_text.decode("utf-8").split(',"extend"')[0] + "}")

    def get_keys(self, account_code: str):
        res_text = self.get_request(url=f"{self.pro_env}{AdsAuxiliary.url_key.value}",
                                    params={"accountCode": account_code})
        if res_text:
            return self.aes_msg(res_text)
        else:
            return {}
