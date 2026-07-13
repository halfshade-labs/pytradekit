"""Binance wire-format enums must match the exchange API exactly and stay
distinct from the internal-storage enums (OrderSide 'B'/'S',
OrderStatus 'CANCELLED' double-L)."""
from pytradekit.utils.dynamic_types import (
    BinanceOrderSide,
    BinanceOrderType,
    BinanceOrderStatus,
    OrderSide,
    OrderStatus,
)


class TestBinanceWireValues:

    def test_side_wire_values(self):
        assert BinanceOrderSide.BUY.value == 'BUY'
        assert BinanceOrderSide.SELL.value == 'SELL'

    def test_type_wire_values(self):
        assert BinanceOrderType.MARKET.value == 'MARKET'
        assert BinanceOrderType.LIMIT.value == 'LIMIT'
        assert BinanceOrderType.LIMIT_MAKER.value == 'LIMIT_MAKER'

    def test_status_wire_values_single_l_canceled(self):
        # Binance spells it CANCELED; the internal enum spells CANCELLED.
        assert BinanceOrderStatus.CANCELED.value == 'CANCELED'
        assert OrderStatus.cancelled.value == 'CANCELLED'

    def test_wire_and_internal_side_values_differ(self):
        assert BinanceOrderSide.BUY.value != OrderSide.buy.value
        assert BinanceOrderSide.SELL.value != OrderSide.sell.value

    def test_status_members_cover_binance_lifecycle(self):
        assert {m.name for m in BinanceOrderStatus} == {
            'NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED',
        }
