"""
Minimal OKX balance check (based on okex_restful.py signing).

No local pytradekit dependency. Only requires: requests

Run:
  OKX_API_KEY=xxx OKX_API_SECRET=yyy OKX_PASSPHRASE=zzz \
  python okex_balance_check.py

Optional:
  OKX_BASE_URL=https://www.okx.com
  OKX_CCY=USDT
"""

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

import requests

OKX_API_KEY = "a4a89592-c6c5-4685-8054-591c46e7a496"  # 直接填你的 key（不建议提交到 git）
OKX_API_SECRET = "21EB40D5C011CC52C66A4ED6BF07A95B"  # 直接填你的 secret（不建议提交到 git）
OKX_PASSPHRASE = "Goldwave112#"  # 直接填你的 passphrase（不建议提交到 git）


def okx_ts() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main() -> None:
    key = (OKX_API_KEY or os.getenv("OKX_API_KEY", "")).strip()
    secret = (OKX_API_SECRET or os.getenv("OKX_API_SECRET", "")).strip()
    passphrase = (OKX_PASSPHRASE or os.getenv("OKX_PASSPHRASE", "")).strip()
    base_url = os.getenv("OKX_BASE_URL", "https://www.okx.com").strip()
    ccy = os.getenv("OKX_CCY", "").strip()
    if not (key and secret and passphrase):
        raise SystemExit("Need OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE")

    api = "/api/v5/account/balance"  # same as OkexAuxiliary.url_balance in okex_restful.py
    params = {"ccy": ccy} if ccy else {}
    ts = okx_ts()
    path = api + (("?" + "&".join([f"{k}={v}" for k, v in params.items()])) if params else "")
    msg = f"{ts}GET{path}"
    sign = base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()

    headers = {
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }

    r = requests.get(base_url + api, headers=headers, params=params, timeout=15)
    try:
        data = r.json()
    except Exception:
        data = {"http_status": r.status_code, "raw": r.text}
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


