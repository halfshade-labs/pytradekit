from decimal import Decimal
from pytradekit.utils.custom_types import Pair, TradingOrder, InstCode, CancelOrder, ClientOrderId


def test_pair_str_full():
    pair = Pair(base="BTC", quote="USDT")
    assert str(pair) == "BTC_USDT"


def test_pair_str_base_only():
    pair = Pair(base="BTC")
    assert str(pair) == "BTC_None"


def test_pair_str_quote_only():
    pair = Pair(quote="USDT")
    assert str(pair) == "None_USDT"


def test_pair_str_none():
    pair = Pair()
    assert str(pair) == "None_None"


def test_from_string_full():
    pair = Pair.from_string("BTC_USDT")
    assert pair.base == "BTC"
    assert pair.quote == "USDT"


def test_from_string_base_only():
    pair = Pair.from_string("BTC_None")
    assert pair.base == "BTC"
    assert pair.quote == "None"


def test_from_string_quote_only():
    pair = Pair.from_string("None_USDT")
    assert pair.base == "None"
    assert pair.quote == "USDT"


def test_trading_order_volume_calculation():
    # Create an instance of InstCode as required by TradingOrder
    inst_code = InstCode(symbol='BNBTUSD', exchange_id='BN', category='SPOT')
    a_error = None

    # Create the TradingOrder object
    order = TradingOrder(
        inst_code=inst_code,
        type='MARKET',
        side='B',
        raw_price=Decimal('0'),
        tick_price=Decimal('0'),
        raw_volume=Decimal('0.010011680293675955'),
        tick_volume=Decimal('0.001'),
        strategy_id='jwj_kline',
        error=a_error
    )
    print(str(order))
    assert order.error == a_error
    # Assert that the calculated volume is as expected
    assert len(str(order.volume)) <= 5, "The calculated volume should match the expected value."
    assert str(order.volume)[:4] == '0.01', "The calculated volume should match the expected value."


def test_cancel_order_delay_calculation():
    # Create an instance of CancelOrder with timestamps
    inst_code = InstCode(symbol='BNBTUSD', exchange_id='BN', category='SPOT')
    client_order_id = ClientOrderId(client_order_id='1231234', strategy_id='jwj_kline')
    order = CancelOrder(
        inst_code=inst_code,
        client_order_id=client_order_id,
        side='B',
        price=Decimal('1.1'),
        volume=Decimal('2'),
        request_timestamp=1632873600000,  # Example timestamp
        working_timestamp=1632873660000,  # Example timestamp
        error=None
    )

    # Assert that the delay is calculated correctly
    assert order.delay == 60000, "The delay should be 60 seconds."

    order_no_timestamps = CancelOrder(
        inst_code=inst_code,
        client_order_id=client_order_id,
        error=None,
        side='B',
        price=Decimal('1.1'),
        volume=Decimal('2'),
    )

    # Assert that the delay is None when timestamps are not provided
    assert order_no_timestamps.delay is None, "The delay should be None without timestamps."
