"""Tests for arbitrage data storage types and MongoDB CRUD methods."""
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from pytradekit.utils.custom_types import InstCode
from pytradekit.utils.dynamic_types import ExchangeId
from pytradekit.utils.static_types import (
    Database,
    TradeRecordAttribute, LegAttribute, HedgeStatusAttribute, PnlSummaryAttribute,
    PremiumSnapshotAttribute, FundingRateHistoryAttribute,
    PremiumSnapshot, FundingRateHistory,
)
from pytradekit.utils.mongodb_operations import MongodbOperations


# === Data Type Tests ===

class TestDatabaseEnum:
    def test_new_database_entries_exist(self):
        assert Database.arbitrage.name == "arbitrage"
        assert Database.trade_records.name == "trade_records"
        assert Database.premium_snapshots.name == "premium_snapshots"
        assert Database.funding_rate_history.name == "funding_rate_history"


class TestTradeRecordAttribute:
    def test_all_fields_present(self):
        expected = [
            "trade_id", "strategy_type", "strategy_id", "coin", "status",
            "created_time_ms", "updated_time_ms", "closed_time_ms",
            "liq_hedge_checked", "perp_client_order_id",
            "legs", "hedge_status", "pnl_summary",
        ]
        actual = [a.name for a in TradeRecordAttribute]
        assert actual == expected


class TestLegAttribute:
    def test_inst_type_field_exists(self):
        names = [a.name for a in LegAttribute]
        assert "inst_type" in names
        assert "inst_code" in names
        assert "delivery_time_ms" in names
        assert "roll_plan" in names


class TestPremiumSnapshot:
    def test_to_dict(self):
        ps = PremiumSnapshot(
            time_ms=1000, coin="BTC",
            perp_exchange=ExchangeId.BN.name, spot_exchange=ExchangeId.OKX.name,
            perp_price=Decimal("70200.0"), spot_price=Decimal("70100.0"),
            premium_pct=Decimal("0.00143"), premium_usdt=Decimal("100.0"),
        )
        d = ps.to_dict()
        assert d["coin"] == "BTC"
        assert d["perp_exchange"] == ExchangeId.BN.name
        assert d["spot_exchange"] == ExchangeId.OKX.name
        assert d["perp_price"] == Decimal("70200.0")
        assert d["spot_price"] == Decimal("70100.0")
        assert d["premium_pct"] == Decimal("0.00143")
        assert d["premium_usdt"] == Decimal("100.0")

    def test_slots_match_attribute_enum(self):
        assert PremiumSnapshot.__slots__ == [a.name for a in PremiumSnapshotAttribute]


class TestFundingRateHistory:
    def test_to_dict(self):
        inst_code = InstCode.from_string("BTC-USDT_BN.PERP")
        fr = FundingRateHistory(
            time_ms=1000, exchange_id=ExchangeId.BN.name, inst_code=inst_code,
            funding_rate=Decimal("0.0002"), funding_rate_annualized=Decimal("0.219"),
            mark_price=Decimal("70200.0"), next_funding_time_ms=2000,
        )
        d = fr.to_dict()
        assert d["exchange_id"] == ExchangeId.BN.name
        assert d["inst_code"] == inst_code
        assert d["funding_rate"] == Decimal("0.0002")
        assert d["mark_price"] == Decimal("70200.0")
        assert d["next_funding_time_ms"] == 2000

    def test_slots_match_attribute_enum(self):
        assert FundingRateHistory.__slots__ == [a.name for a in FundingRateHistoryAttribute]


# === MongoDB CRUD Tests ===

def _make_mongo(mocker):
    """Create a MongodbOperations instance with mocked client."""
    mocked_client = MagicMock()
    mocker.patch('pytradekit.utils.mongodb_operations.MongoClient', return_value=mocked_client)
    MongodbOperations._client = None
    mongo = MongodbOperations("mongodb://test:27017")
    return mongo, mocked_client


class TestTradeRecordCRUD:
    def test_insert_trade_record_sets_id(self, mocker):
        mongo, client = _make_mongo(mocker)
        doc = {"trade_id": "abc-123", "status": "open"}
        mongo.insert_trade_record(doc)

        assert doc["_id"] == "abc-123"
        collection = client["arbitrage"]["trade_records"]
        collection.insert_one.assert_called_once()

    def test_update_trade_record(self, mocker):
        mongo, client = _make_mongo(mocker)
        mongo.update_trade_record("abc-123", {"status": "close"})

        collection = client["arbitrage"]["trade_records"]
        collection.update_one.assert_called_once_with(
            {"trade_id": "abc-123"},
            {"$set": {"status": "close"}},
        )

    def test_read_trade_records_by_status(self, mocker):
        mongo, client = _make_mongo(mocker)
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.__iter__ = MagicMock(return_value=iter([{"trade_id": "1"}]))
        client["arbitrage"]["trade_records"].find.return_value = mock_cursor

        result = mongo.read_trade_records(status="open")

        client["arbitrage"]["trade_records"].find.assert_called_once_with({"status": "open"})

    def test_read_trade_record_by_id(self, mocker):
        mongo, client = _make_mongo(mocker)
        client["arbitrage"]["trade_records"].find_one.return_value = {"trade_id": "abc"}

        result = mongo.read_trade_record_by_id("abc")

        client["arbitrage"]["trade_records"].find_one.assert_called_once_with({"trade_id": "abc"})
        assert result["trade_id"] == "abc"


class TestPremiumSnapshotCRUD:
    def test_insert_premium_snapshot(self, mocker):
        mongo, client = _make_mongo(mocker)
        data = {"time_ms": 1000, "coin": "BTC"}
        mongo.insert_premium_snapshot(data)

        collection = client["arbitrage"]["premium_snapshots"]
        collection.insert_one.assert_called_once()

    def test_read_premium_snapshots_with_coin_filter(self, mocker):
        mongo, client = _make_mongo(mocker)
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.__iter__ = MagicMock(return_value=iter([]))
        client["arbitrage"]["premium_snapshots"].find.return_value = mock_cursor

        mongo.read_premium_snapshots(coin="BTC")

        client["arbitrage"]["premium_snapshots"].find.assert_called_once_with({"coin": "BTC"})


class TestFundingRateHistoryCRUD:
    def test_insert_funding_rate_history(self, mocker):
        mongo, client = _make_mongo(mocker)
        data = [{
            "time_ms": 1000,
            "exchange_id": ExchangeId.BN.name,
            "inst_code": InstCode.from_string("BTC-USDT_BN.PERP"),
        }]
        mongo.insert_funding_rate_history(data)

        collection = client["arbitrage"]["funding_rate_history"]
        collection.bulk_write.assert_called_once()

    def test_read_funding_rate_history_with_inst_code(self, mocker):
        mongo, client = _make_mongo(mocker)
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.limit.return_value = mock_cursor
        mock_cursor.__iter__ = MagicMock(return_value=iter([]))
        client["arbitrage"]["funding_rate_history"].find.return_value = mock_cursor

        # Production passes the serialized inst_code string read back from mongo,
        # not the InstCode object — keep test aligned with that call path.
        inst_code_str = str(InstCode.from_string("BTC-USDT_BN.PERP"))
        mongo.read_funding_rate_history(inst_code=inst_code_str, limit=1)

        client["arbitrage"]["funding_rate_history"].find.assert_called_once_with(
            {"inst_code": inst_code_str}
        )
        mock_cursor.sort.return_value.limit.assert_called_once_with(1)
