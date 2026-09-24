from decimal import Decimal

import pytest
from pytradekit.utils.account_valuation import (
    ValuationError, build_quote_snapshot, collect_wallets, legacy_balance_value,
    normalize_wallet, select_quote, value_wallets,
)

NOW = 1790000000000


def bn_quote(asset="BTC", price="100", age=0):
    return build_quote_snapshot("BN", [{"symbol": asset + "USDT", "lastPrice": price,
                                        "closeTime": NOW - age}], NOW)


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity", None])
def test_invalid_prices_do_not_become_complete(price):
    batch = bn_quote(price=price)
    assert batch["status"] == "partial"
    assert not batch["quotes"]


@pytest.mark.parametrize("age", [600001, -5001])
def test_exchange_time_controls_validity(age):
    assert bn_quote(age=age)["status"] == "partial"


def test_duplicate_identity_removes_both_quotes():
    row = {"symbol": "BTCUSDT", "lastPrice": "1", "closeTime": NOW}
    batch = build_quote_snapshot("BN", [row, row], NOW)
    assert batch["status"] == "partial"
    assert not batch["quotes"]


def test_numeraire_fallback_and_stablecoins():
    context = {"now_ms": NOW, "quotes": {"BN": bn_quote()}}
    quote = select_quote("BTC", "OKX", context)
    assert quote["kind"] == "bn_fallback"
    assert quote["exchange_ms"] == NOW
    assert select_quote("USDT", "OKX", context)["price"] == "1"
    assert select_quote("USDC", "OKX", context) is None
    context["now_ms"] += 600001
    assert select_quote("BTC", "OKX", context) is None


def test_own_venue_wins_and_wrong_instrument_is_rejected():
    own = build_quote_snapshot("OKX", {"code": "0", "data": [
        {"instId": "BTC-USDT", "last": "101", "ts": NOW}]}, NOW)
    context = {"now_ms": NOW, "quotes": {"BN": bn_quote(), "OKX": own}}
    assert select_quote("BTC", "OKX", context)["price"] == "101"
    own["quotes"]["BTC"]["inst_code"] = "ETH-USDT_OKX.SPOT"
    assert select_quote("BTC", "OKX", context)["price"] == "100"


def test_htx_batch_timestamp_and_bad_outer_payload():
    batch = build_quote_snapshot("HTX", {"status": "ok", "ts": NOW,
        "data": [{"symbol": "btcusdt", "close": "1.1"}]}, NOW)
    assert batch["quotes"]["BTC"]["exchange_ms"] == NOW
    with pytest.raises(ValuationError):
        build_quote_snapshot("OKX", {"code": "500", "data": []}, NOW)


def test_okx_frozen_cash_liability_and_equity_are_distinct():
    rows = normalize_wallet("OKX", "trading", {"code": "0", "data": [{"details": [
        {"ccy": "BTC", "cashBal": "2", "eq": "1.5", "availBal": "0", "frozenBal": "2",
         "liab": ".7", "upl": ".2"}]}]})
    assert rows[0]["quantity"] == "2"
    assert rows[0]["equity"] == "1.5"
    assert rows[0]["liabilities"] == "0.7"


def test_bn_isolated_upl_and_negative_assets_are_retained():
    rows = normalize_wallet("BN", "usd_m", {"multiAssetsMargin": False, "assets": [
        {"asset": "USDT", "walletBalance": "0", "unrealizedProfit": "-5", "marginBalance": "-5"}]})
    assert rows[0]["equity"] == "-5"
    assert rows[0]["quantity"] == "0"


def test_funding_freeze_withdrawing_included():
    rows = normalize_wallet("BN", "funding", [{"asset": "USDT", "free": "0", "locked": "1",
                                                 "freeze": "2", "withdrawing": "3"}])
    assert rows[0]["quantity"] == "6"


def _fetchers(spot, funding):
    return {"spot": lambda: {"balances": [{"asset": "BTC", "free": str(spot), "locked": "1"}]},
            "funding": lambda: [{"asset": "BTC", "free": str(funding), "locked": "0", "freeze": "0", "withdrawing": "0"}],
            "usd_m": lambda: {"multiAssetsMargin": False, "assets": [{"asset": "USDT", "walletBalance": "5",
                                             "marginBalance": "7", "unrealizedProfit": "2"}]}}


def test_transfers_preserve_equity_and_upl_only_once():
    context = {"now_ms": NOW, "quotes": {"BN": bn_quote()}}
    values = [value_wallets(collect_wallets("BN", "test", _fetchers(*amounts)), context)
              for amounts in [(2, 3), (4, 1)]]
    assert values[0]["total_equity"] == values[1]["total_equity"] == "607"
    assert {r["wallet"] for r in values[0]["rows"]} == {"spot", "funding", "usd_m"}


def test_wallet_errors_and_missing_prices_remain_partial_with_quantities():
    fetchers = _fetchers(1, 2)
    fetchers["usd_m"] = lambda: {"code": -1, "msg": "sensitive raw error"}
    snapshot = value_wallets(collect_wallets("BN", "test", fetchers), {"now_ms": NOW, "quotes": {}})
    assert snapshot["total_equity"] is None
    assert snapshot["rows"][0]["quantity"] == "2"
    assert snapshot["rows"][0]["value"] is None
    assert snapshot["sources"][-1]["error"] == "wallet_fetch_or_validation_failed"
    assert legacy_balance_value(snapshot)["BTC"]["value_in_u"] is None
    assert snapshot["missing"]


@pytest.mark.parametrize("raw", [{"assets": []}, {"multiAssetsMargin": "false", "assets": []}])
def test_ambiguous_bn_mode_rejected(raw):
    with pytest.raises(ValuationError):
        normalize_wallet("BN", "usd_m", raw)


def test_legacy_json_emits_exact_numbers_and_inventory_excludes_perp(monkeypatch):
    import json
    import pytradekit.utils.account_valuation as valuation
    monkeypatch.setattr(valuation, "get_timestamp_ms", lambda: NOW)
    snapshot = value_wallets(collect_wallets("BN", "test", _fetchers(2, 3)),
                            {"now_ms": NOW, "quotes": {"BN": bn_quote()}})
    serialized = valuation.serialize_legacy_balance(snapshot)
    legacy = json.loads(serialized, parse_float=Decimal)
    assert legacy["BTC"]["volume"] == 6
    inventory = valuation.read_cash_inventory(snapshot, ("BN", "test"), NOW)
    assert inventory["BTC"]["quantity"] == Decimal("6")
    assert "USDT" not in inventory
    with pytest.raises(ValuationError):
        valuation.read_cash_inventory(snapshot, ("BN", "test"), NOW + 600001)


@pytest.mark.parametrize("asset", ["币1", "ÉCOIN"])
def test_unicode_asset_quote_wallet_inventory_and_duplicate_detection(monkeypatch, asset):
    import pytradekit.utils.account_valuation as valuation
    monkeypatch.setattr(valuation, "get_timestamp_ms", lambda: NOW)
    snapshot = collect_wallets("BN", "test", {
        "spot": lambda: {"balances": [{"asset": asset, "free": "2", "locked": "0"}]},
        "funding": lambda: [], "usd_m": lambda: {"multiAssetsMargin": False, "assets": []}})
    valued = value_wallets(snapshot, {"now_ms": NOW, "quotes": {"BN": bn_quote(asset)}})
    assert valued["status"] == "complete"
    assert valuation.read_cash_inventory(valued, ("BN", "test"), NOW)[asset]["quantity"] == Decimal("2")
    row = {"symbol": asset + "USDT", "lastPrice": "1", "closeTime": NOW}
    assert not build_quote_snapshot("BN", [row, row], NOW)["quotes"]
    with pytest.raises(ValuationError):
        normalize_wallet("BN", "spot", {"balances": [
            {"asset": asset, "free": "1", "locked": "0"},
            {"asset": asset, "free": "2", "locked": "0"}]})
