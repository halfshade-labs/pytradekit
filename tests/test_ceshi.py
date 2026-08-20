import os
from unittest.mock import Mock, patch

from pytradekit.restful import ceshi


def test_balance_check_omits_currency_filter_by_default():
    response = Mock()
    response.json.return_value = {"code": "0", "data": []}

    with patch.dict(
        os.environ,
        {
            "OKX_API_KEY": "test_api_key",
            "OKX_API_SECRET": "test_api_secret",
            "OKX_PASSPHRASE": "test_passphrase",
        },
        clear=True,
    ), patch.object(
        ceshi,
        "get_okx_timestamp",
        return_value="2026-08-20T00:00:00.000Z",
    ), patch.object(
        ceshi.requests,
        "get",
        return_value=response,
    ) as request_get, patch("builtins.print"):
        ceshi.main()

    assert request_get.call_args.kwargs["params"] == {}
    response.raise_for_status.assert_called_once_with()
