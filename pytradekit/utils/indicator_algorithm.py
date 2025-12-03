from decimal import Decimal

from pytradekit.utils.number_tools import handle_pcs_decimal
from pytradekit.utils.static_types import OrderBookAttribute
from pytradekit.utils.number_tools import convert_to_decimal, convert_decimal_to_str
from pytradekit.utils.tools import get_coin_price
from pytradekit.utils.exceptions import DataTypeException


def get_median_price(bid1_price, ask1_price):
    return (bid1_price + ask1_price) / 2


def get_coin_price_pct(coin_price, pct):
    return coin_price * (1 - pct)


def get_fair_price(base_price, quote_price, tick_price):
    return handle_pcs_decimal(tick_price, base_price / quote_price)


def get_tick_pct_spread(bid1_price, ask1_price, tick_size):
    median_price = get_median_price(bid1_price, ask1_price)
    spread = round(100 * (ask1_price - bid1_price) / median_price, 2)
    tick_pct = round((tick_size / median_price) * 100, 2)
    return tick_pct, spread, median_price


def calculate_best_prices(bid1_price: Decimal, ask1_price: Decimal, price_tick: Decimal):
    median_price = get_median_price(bid1_price, ask1_price)
    if median_price - bid1_price < price_tick:
        best_bid = bid1_price
        best_ask = best_bid + price_tick
    elif ask1_price - median_price < price_tick:
        best_ask = ask1_price
        best_bid = best_ask - price_tick
    else:
        best_bid = handle_pcs_decimal(price_tick, median_price)
        best_ask = best_bid + price_tick
    return best_bid, best_ask, median_price


def calculate_depth_bounds(median_price: float, depth_pct: float) -> tuple:
    """
    计算深度上下边界
    
    Args:
        median_price: 中间价格
        depth_pct: 深度百分比 (例如: 1.0 表示1%)
    
    Returns:
        tuple: (depth_lower, depth_upper) 深度下边界和上边界

    """
    depth_upper = median_price * (1 + depth_pct / 100)
    depth_lower = median_price * (1 - depth_pct / 100)
    return depth_lower, depth_upper


def calculate_depth_bounds_with_best_prices(bid1_price: float, ask1_price: float, depth_pct: float) -> tuple:
    """
    基于最佳买卖价格计算深度上下边界
    
    Args:
        bid1_price: 最佳买价
        ask1_price: 最佳卖价
        depth_pct: 深度百分比
    
    Returns:
        tuple: (depth_lower, depth_upper, median_price) 深度下边界、上边界和中间价格
    """
    median_price = get_median_price(bid1_price, ask1_price)
    depth_lower, depth_upper = calculate_depth_bounds(median_price, depth_pct)
    return depth_lower, depth_upper, median_price


def compute_bid_ask_for_bn_bttcusdt(bids, asks, tick_size, bid_amount, ask_amount, bid_volume, ask_volume):
    depth_lower, depth_upper, _ = calculate_best_prices(bids[0][0], asks[0][0], tick_size)
    depth_upper += tick_size
    depth_lower -= tick_size
    for bid in bids:
        if bid[0] >= depth_lower:
            bid_amount += bid[1] * bid[0]
            bid_volume += bid[1]
    for ask in asks:
        if ask[0] <= depth_upper:
            ask_amount += ask[1] * ask[0]
            ask_volume += ask[1]
    return bid_amount, ask_amount, bid_volume, ask_volume, depth_upper, depth_lower


def compute_depth(logger, pair, orderbook, tick_size, depth_pct, ticker_price, exchange_id):
    try:
        bids = orderbook[OrderBookAttribute.bids.name]
        asks = orderbook[OrderBookAttribute.asks.name]
        tick_pct, spread, median_price = get_tick_pct_spread(bids[0][0], asks[0][0], tick_size)
        if tick_pct > spread:
            logger.info(
                f'tick pct:{tick_pct}, spread:{spread}, pair:{pair}, exchange_id:{exchange_id}')
        bid_amount = 0
        ask_amount = 0
        bid_volume = 0
        ask_volume = 0
        if orderbook[OrderBookAttribute.inst_code.name] == 'BTTCUSDT_BN.SPOT':
            bid_amount, ask_amount, bid_volume, ask_volume, depth_upper, depth_lower \
                = compute_bid_ask_for_bn_bttcusdt(bids, asks, tick_size, bid_amount, ask_amount, bid_volume, ask_volume)
        elif tick_pct <= depth_pct:
            depth_lower, depth_upper = calculate_depth_bounds(median_price, depth_pct)
            for bid in bids:
                if bid[0] >= depth_lower:
                    bid_amount += bid[1] * bid[0]
                    bid_volume += bid[1]
            for ask in asks:
                if ask[0] <= depth_upper:
                    ask_amount += ask[1] * ask[0]
                    ask_volume += ask[1]
        else:
            depth_lower, depth_upper, _ = calculate_best_prices(convert_to_decimal(bids[0][0]),
                                                                convert_to_decimal(asks[0][0]),
                                                                convert_to_decimal(tick_size))
            if spread == tick_pct:
                bid_amount = bids[0][1] * bids[0][0]
                ask_amount = asks[0][1] * asks[0][0]
                bid_volume = bids[0][1]
                ask_volume = asks[0][1]
        quote = pair.split('_')[1]
        quote_price = get_coin_price(logger, quote, ticker_price, exchange_id)
        bid_depth = int(bid_amount * quote_price)
        ask_depth = int(ask_amount * quote_price)
        return bid_depth, ask_depth, bid_volume, ask_volume, spread, tick_pct, float(
            convert_decimal_to_str(depth_lower)), float(convert_decimal_to_str(depth_upper))
    except Exception as e:
        raise DataTypeException('compute_depth error') from e
