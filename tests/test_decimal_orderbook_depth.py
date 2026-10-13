from decimal import Decimal
from unittest.mock import Mock

import pytest
from pytradekit.utils.dynamic_types import ExchangeId
from pytradekit.utils.exceptions import DataTypeException
from pytradekit.utils.indicator_algorithm import compute_depth
from pytradekit.utils.mongodb_operations import MongodbOperations
from pytradekit.utils.tools import get_coin_price


def test_stored_decimal_book_can_be_read_and_valued(mocker):
    mocker.patch.object(MongodbOperations, "_client", None)
    mocker.patch.object(MongodbOperations, "_indexes_ensured", False)
    client = mocker.patch("pytradekit.utils.mongodb_operations.MongoClient").return_value
    mocker.patch.object(MongodbOperations, "_ensure_indexes")
    ops = MongodbOperations("mongodb://localhost:27017")
    book = {"inst_code": "BTC-USDT_BN.SPOT", "bids": [[Decimal("100.000000000000000001"), Decimal("2")]],
            "asks": [[Decimal("100.1"), Decimal("3")]], "time_ms": 1791853200123}
    stored = ops.get_correct_dict(book)
    assert isinstance(stored["bids"][0][0], str)
    cursor = client["raw_market"]["BN_order_book"].find.return_value.sort.return_value.limit.return_value
    cursor.__iter__.return_value = iter([stored])
    read = ops.read_last_orderbook(book["inst_code"], ExchangeId.BN.name)
    assert read["bids"][0][0] == book["bids"][0][0]
    result = compute_depth(Mock(), "BTC-USDT", read, "0.01", "1", {}, ExchangeId.BN.name)
    assert result[:4] == (200, 300, Decimal("2"), Decimal("3"))
    assert all(isinstance(value, Decimal) for value in result[4:])


@pytest.mark.parametrize("pair,inst_code", [("BTTC-USDT", "BTTC-USDT_BN.SPOT"), ("BTTC_USDT", "BTTCUSDT_BN.SPOT")])
def test_legacy_and_canonical_bttc_use_same_depth_rule(pair, inst_code):
    book = {"inst_code": inst_code, "bids": [["0.00000091", "1000000"]],
            "asks": [["0.00000093", "1000000"]]}
    result = compute_depth(Mock(), pair, book, "0.00000001", "1", {}, ExchangeId.BN.name)
    assert result[2:4] == (Decimal("1000000"), Decimal("1000000"))


def test_decimal_quote_lookup_preserves_price():
    exact = "9007199254740993.000000001"
    assert get_coin_price(Mock(), "BTC", {"BTCUSDT": exact}, ExchangeId.BN.name, as_decimal=True) == Decimal(exact)
    assert isinstance(get_coin_price(Mock(), "BTC", {"BTCUSDT": exact}, ExchangeId.BN.name), float)
    assert get_coin_price(Mock(), "EUR", {"USDTEUR": "0.8"}, ExchangeId.BN.name, as_decimal=True) == Decimal("1.25")


def test_missing_quote_does_not_fabricate_zero_depth():
    book = {"inst_code": "BTC-EUR_BN.SPOT", "bids": [["100", "2"]], "asks": [["101", "3"]]}
    with pytest.raises(DataTypeException):
        compute_depth(Mock(), "BTC-EUR", book, "0.01", "1", {}, ExchangeId.BN.name)


@pytest.mark.parametrize("is_df", [False, True])
def test_depth_and_ratio_readers_restore_legacy_and_decimal_records(mocker, is_df):
    from pytradekit.utils.time_handler import TimeSpan
    mocker.patch.object(MongodbOperations, "_client", None)
    mocker.patch.object(MongodbOperations, "_indexes_ensured", False)
    client = mocker.patch("pytradekit.utils.mongodb_operations.MongoClient").return_value
    mocker.patch.object(MongodbOperations, "_ensure_indexes")
    ops = MongodbOperations("mongodb://localhost:27017")
    span = TimeSpan(1, 2)
    client["raw_metrics"]["depth"].find.return_value = [
        {"spread": "0.15", "bid_volume": "2.01", "bid_depth": 201}, {"spread": 0.2, "bid_volume": 3, "bid_depth": 300}]
    read = ops.read_depth(time_span=span, is_df=is_df)
    records = read.to_dict("records") if is_df else read
    assert [r["spread"] for r in records] == [Decimal("0.15"), Decimal("0.2")]
    assert sum(r["bid_volume"] for r in records) == Decimal("5.01")
    assert all(isinstance(r["bid_depth"], int) for r in records)
    client["metrics_order_depth"]["BN_order_depth_ratio"].find.return_value = [{"bid_ratio": "0.1", "depth_bid": 201}, {"bid_ratio": 0.2, "depth_bid": 300}]
    read = ops.read_order_depth_ratio(span, ExchangeId.BN.name, is_df=is_df)
    records = read.to_dict("records") if is_df else read
    assert [r["bid_ratio"] for r in records] == [Decimal("0.1"), Decimal("0.2")]
    assert all(isinstance(r["depth_bid"], int) for r in records)
