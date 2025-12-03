import time

from pytradekit.trading_setup.inst_code_usage import convert_symbol_to_inst_code
from pytradekit.utils.dynamic_types import OrderType, OrderSide, ExchangeId, BinanceRestful
from pytradekit.utils.static_types import Trade
from pytradekit.utils.time_handler import convert_timestamp_to_str, convert_timestamp_to_datetime, \
    get_millisecond_str, get_datetime, DATETIME_FORMAT_DAY


def handle_restful_trade_data(trade_data, account_id, strategy_id):
    triggered_source = OrderType.maker.value if trade_data[
        BinanceRestful.trade_is_maker.value] else OrderType.taker.value
    side = OrderSide.buy.value if trade_data[BinanceRestful.trade_is_buy.value] else OrderSide.sell.value
    timestamp = trade_data[BinanceRestful.trade_time.value]
    trade = Trade(event_time_ms=get_millisecond_str(get_datetime()),
                  run_time_ms=None,
                  account_id=account_id,
                  inst_code=convert_symbol_to_inst_code(trade_data[BinanceRestful.symbol.value]),
                  side=side,
                  filled_volume=float(trade_data[BinanceRestful.trade_volume.value]),
                  executed_price=float(trade_data[BinanceRestful.trade_price.value]),
                  portfolio_id=strategy_id,
                  strategy_id=strategy_id,
                  exchange_id=ExchangeId.BN.name,
                  traded_time_ms=get_millisecond_str(
                      convert_timestamp_to_datetime(trade_data[BinanceRestful.trade_time.value])),
                  client_order_id=None,
                  order_id=trade_data[BinanceRestful.order_id.value],
                  trade_id=str(trade_data[BinanceRestful.trade_id.value]) + "-" + trade_data[
                      BinanceRestful.symbol.value],
                  fee_coin=trade_data[BinanceRestful.order_fee_coin.value],
                  day=convert_timestamp_to_str(trade_data[BinanceRestful.trade_time.value],
                                               a_format=DATETIME_FORMAT_DAY),
                  fee=float(trade_data[BinanceRestful.trade_fee.value]),
                  filled_status=None,
                  triggered_source=triggered_source)
    return trade, timestamp


def get_binance_trade(logger, bn_client, time_span, symbol, account_id, strategy_id=None):
    start_tmp = time_span.start
    end_tmp = time_span.end
    result = []
    while True:
        try:
            trade_res = bn_client.get_trade(symbol, start_time=start_tmp, end_time=end_tmp)
            time.sleep(1)
        except Exception as e:
            logger.exception(e)
            time.sleep(3)
            break
        if not trade_res or 'code' in trade_res or trade_res[-1]['time'] == start_tmp:
            break
        if trade_res[0]['time'] == start_tmp:
            trade_res = trade_res[1:]
        start_tmp = trade_res[-1]['time']
        for trade_data in trade_res:
            trade, timestamp = handle_restful_trade_data(trade_data, account_id, strategy_id)
            if timestamp in [time_span.start, time_span.end]:
                continue
            res = trade.to_dict()
            res['timestamp'] = timestamp
            result.append(res)
    return result
