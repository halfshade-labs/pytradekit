"""Minimal environment-only OKX balance check.

Configure ``OKX_API_KEY``, ``OKX_API_SECRET``, and ``OKX_PASSPHRASE`` through
a secret manager or non-echoing shell input, then run::

    python -m pytradekit.restful.ceshi

Optional environment variables are ``OKX_BASE_URL`` and ``OKX_CCY``.
"""

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests


REQUEST_TIMEOUT_SECONDS = 15


def get_okx_timestamp() -> str:
    """Return an OKX-compatible UTC timestamp."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


def get_required_environment(name: str) -> str:
    """Read a required non-empty environment variable."""
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    key = get_required_environment("OKX_API_KEY")
    secret = get_required_environment("OKX_API_SECRET")
    passphrase = get_required_environment("OKX_PASSPHRASE")
    base_url = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
    currency = os.getenv("OKX_CCY", "").strip()

    api_path = "/api/v5/account/balance"
    params = {"ccy": currency} if currency else {}
    timestamp = get_okx_timestamp()
    request_path = api_path
    if params:
        request_path = f"{api_path}?{urlencode(params)}"
    message = f"{timestamp}GET{request_path}"
    signature = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")

    headers = {
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }
    response = requests.get(
        f"{base_url}{api_path}",
        headers=headers,
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
