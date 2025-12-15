from enum import Enum, auto
from pytradekit.utils.static_types import TradeAttribute, OrderAttribute


class Env(Enum):
    MONGODB_URL = auto()
    REDIS_URL = auto()
    mongodb_url = auto()
    redis_url = auto()


class Api(Enum):
    api = auto()
    secret = auto()
    password = auto()
    tag = auto()


class AdsAttribute(Enum):
    access_key = 'accessKey'
    secret_key = 'secretKey'
    passphrase = 'passphrase'
    tag = 'tag'


class StrategyId:
    ALL = 'all'
    JWJ_KLINE = 'jwj_kline'
    JWJ_VOLUME = 'jwj_volume'
    JWJ_DEPTH = 'jwj_depth'
    JWJ_SPREAD = 'jwj_spread'
    JWJ_ADJUST = 'jwj_adjust'
    INVENTORY_SPLIT = 'inventory_split'
    INVENTORY_CLOSE = 'inventory_close'
    VENDOR = 'vendor'
    TRONMM = 'tronmm'
    GRID = 'GRID'


class InstCodeType(Enum):
    SPOT = auto()
    PERP = auto()
    FUTU = auto()


class TaskIdName(Enum):
    key = 'task_id'
    ws_001 = 'ws_bn_order_trade'
    rs_001 = 'rs_bn_order_trade'
    ws_002 = 'ws_bn_kline'
    ws_003 = 'ws_bn_aggtrade'
    ws_004 = 'ws_bn_kline_report'
    ws_005 = 'ws_rs_bn_orderbook_redis'
    ws_006 = 'ws_bn_orderbook'
    ws_007 = 'ws_bn_balance'
    rs_002 = 'rs_bn_balance'
    rs_003 = 'rs_bfx_balance'
    ws_008 = 'ws_bn_deposit_withdraw'
    ws_009 = 'fetch_ws_ticker'
    rs_swap_001 = 'ws_bn_swap_position'
    rs_swap_002 = 'push_slack_hedge_report'
    rs_swap_003 = 'fetch_restful_bn_swap_position'
    rs_swap_004 = 'fetch_restful_hb_swap_position'
    rs_swap_005 = 'fetch_restful_okex_swap_position'
    rs_swap_006 = 'fetch_restful_bit_swap_position'


class SlackUser(Enum):
    ABC = 'ABC'


class ExchangeId(Enum):
    ALL = 'all'
    BN = 'binance'
    HTX = 'huobi'
    OKX = 'okx'
    PNX = 'poloniex'
    BFX = 'bitfinex'
    GT = 'gate'
    KC = 'kucoin'
    MXC = 'mexc'
    BMT = 'bitmart'
    BTS = 'btse'
    BBT = 'bybit'
    BGT = 'bitget'
    WZX = 'wazirx'
    MCO = 'mercado'
    EMO = 'exmo'
    WOO = 'woox'
    BUL = 'bullish'
    BCI = 'bitci'
    HKG = 'hashkey_global'
    KRK = 'kraken'
    LBK = 'Lbank'


class ConfigSection(Enum):
    web_hook_url = 'WebHookUrl'
    foundational = 'foundational'
    inst_code = 'instCode'
    log = 'Log'
    account = 'account'
    inst_code_list = 'inst_code_list'
    report_minutes = 'report_minutes'
    inner_config_path = 'cfg'


class ConfigKey(Enum):
    log_name = 'LogName'
    report = 'Report'
    watch = 'Watch'
    channel = 'Channel'


class BinanceWebSocket(Enum):
    subscribe = 'SUBSCRIBE'
    request = 'REQUEST'
    run_time_ms = 'run_time_ms'
    deposit_withraw = 'deposit_withraw'
    agg_trade = 'aggTrade'
    kline_raw = 'kline'
    order_type = "o"
    side = "S"
    client_order_id_new = "c"
    client_order_id_cancel = "C"
    event_type = 'e'
    order_book_update_id = 'u'
    execution_report = 'executionReport'
    portfolio_id = 'portfolio_id'
    strategy_id = 'strategy_id'
    account_id = 'account_id'
    account_name = 'account_name'
    account_balance = 'outboundAccountPosition'
    balance_update = 'balanceUpdate'
    symbol = "s"
    order_volume = 'q'
    order_price = 'p'
    order_timestamp = 'O'
    order_id = 'i'
    fee_coin = "N"
    order_stop_price = 'P'
    order_time_in_force = 'f'
    status = "X"
    trade_is_maker = 'm'
    trade_price = "L"
    trade_volume = "l"
    traded_time_ms = "T"
    trade_id = "t"
    fee = "n"
    working_time = "W"
    cumulative_volume = 'z'
    cumulative_amount = 'Z'
    data_time = 'E'
    kline = 'k'
    kline_start_time = 't'
    kline_end_time = 'T'
    kline_frame = 'i'
    kline_open = 'o'
    kline_close = 'c'
    kline_high = 'h'
    kline_low = 'l'
    kline_volume = 'v'
    kline_amount = 'q'
    kline_completed = 'x'
    kline_trade_amount = 'n'
    kline_taker_buy_volume = 'V'
    kline_taker_buy_amount = 'Q'
    agg_trade_time = 'T'
    agg_trade_symbol = 's'
    orderbook_last_update_id = 'u'
    orderbook_first_update_id = 'U'
    orderbook_bids = 'b'
    orderbook_asks = 'a'
    BUY = 'BUY'
    lastUpdateId = 'lastUpdateId'
    balance_ws_time = 'E'
    balance_ws_coin = 'a'
    balance_ws_updata = 'd'
    order_limit_maker = 'LIMIT_MAKER'


class BinanceRestful(Enum):
    symbol = 'symbol'
    side = 'side'
    deposit_time = 'insertTime'
    id = 'id'
    coin = 'coin'
    quantity = 'amount'
    rank = 'rank'
    txid = 'txId'
    state = 'status'
    withdraw_time = 'completeTime'
    withdraw_fee = 'transactionFee'
    network = 'network'
    transfer_time = 'time'
    transfer_id = 'tranId'
    transfer_coin = 'asset'
    transfer_from = 'from'
    transfer_to = 'to'
    transfer_quantity = 'qty'
    transfer_state_comformed = 'SUCCESS'
    balacnes = 'balances'
    balance_asset = 'asset'
    balance_free = 'free'
    balance_locked = 'locked'
    balance_wallet_name = 'walletName'
    balance_wallet_balance = 'balance'
    balance_wallet_quote_asset = 'quoteAsset'
    ticker_lastPrice = 'lastPrice'
    ticker_quote_volume = 'quoteVolume'
    transfer_type = 'type'
    transfer_email = 'email'
    order_volume = 'origQty'
    order_price = 'price'
    order_time = 'time'
    order_update_time = 'updateTime'
    order_client_order_id = 'clientOrderId'
    order_id = 'orderId'
    order_fee_coin = 'commissionAsset'
    order_type = 'type'
    order_stop_price = 'stopPrice'
    order_status = 'status'
    order_time_in_force = 'timeInForce'
    order_working_time = 'workingTime'
    order_cumulative_volume = 'cummulativeQuoteQty'
    trade_price = 'price'
    trade_volume = 'qty'
    trade_time = 'time'
    trade_id = 'id'
    trade_fee = 'commission'
    trade_is_maker = 'isMaker'
    order_limit = 'LIMIT'
    order_side_buy = 'BUY'
    trade_is_buy = 'isBuyer'
    alpha_id = 'alphaId'


class DepositWithdrawAuxiliary(Enum):
    deposit_comformed = 'comformed'
    deposit_comforming = 'comforming'


class BitfinexRestful(Enum):
    balance_currency = 'currency'
    balance_amount = 'amount'
    ticker_lastprice = 'last_price'
    deposit_withdraw_comformed = 'COMPLETED'


class BitfinexWebSocket(Enum):
    order_type_new = 'on'
    order_type_update = 'ou'
    order_type_cannel = 'oc'
    order_type_limit = 'EXCHANGE LIMIT'
    portfolio_id = 'portfolio_id'
    strategy_id = 'strategy_id'
    account_id = 'account_id'
    run_time_ms = 'run_time_ms'


class HuobiWebSocket(Enum):
    order_type = 'type'
    order_buy = 'buy'
    order_client_order_id = 'clientOrderId'
    order_type_limit = 'limit'
    account_id = 'account_id'
    portfolio_id = 'portfolio_id'
    strategy_id = 'strategy_id'
    symbol = 'symbol'
    order_volume = 'orderSize'
    order_price = 'orderPrice'
    order_time_ms = 'orderCreateTime'
    order_id = 'orderId'
    order_status = 'orderStatus'
    cumulative_volume = 'execAmt'
    unfilled_volume = 'remainAmt'
    order_data_time_ms = 'lastActTime'
    run_time_ms = 'run_time_ms'
    is_taker = 'aggressor'
    trade_volume = 'tradeVolume'
    trade_price = 'tradePrice'
    traded_time_ms = 'tradeTime'
    trade_id = 'tradeId'
    tick = 'tick'
    orderbook_bid = 'bid'
    orderbook_ask = 'ask'
    orderbook_time = 'quoteTime'


class OkexWebSocket(Enum):
    symbol = 'instId'
    run_time_ms = 'run_time_ms'
    account_id = 'account_id'
    portfolio_id = 'portfolio_id'
    strategy_id = 'strategy_id'
    order_side = 'side'
    order_buy = 'buy'
    order_type = 'ordType'
    order_limit = 'limit'
    order_price = 'px'
    order_volume = 'sz'
    order_time_ms = 'cTime'
    order_update_time_ms = 'uTime'
    cumulative_volume = 'accFillSz'
    order_avg_price = 'avgPx'
    order_client_order_id = 'clOrdId'
    order_id = 'ordId'
    order_status = 'state'
    trade_exec_type = 'execType'
    trade_taker = 'T'
    trade_volume = 'fillSz'
    trade_price = 'fillPx'
    traded_time_ms = 'fillTime'
    trade_id = 'tradeId'
    trade_fee = 'fee'
    trade_fee_coin = 'feeCcy'
    orderbook_bid = 'bidPx'
    orderbook_ask = 'askPx'
    orderbook_time = 'ts'


class BybitWebSocket(Enum):
    run_time_ms = 'run_time_ms'
    account_id = 'account_id'
    portfolio_id = 'portfolio_id'
    strategy_id = 'strategy_id'
    order_side = 'side'
    order_buy = 'Buy'
    order_type = 'orderType'
    order_type_limit = 'limit'
    symbol = 'symbol'
    order_volume = 'qty'
    order_price = 'price'
    order_time_ms = 'createdTime'
    client_order_id = 'orderLinkId'
    order_id = 'orderId'
    order_status = 'orderStatus'
    unfilled_volume = 'leavesQty'
    unfilled_amount = 'leavesValue'
    cumulative_volume = 'cumExecQty'
    cumulative_amount = 'cumExecValue'
    data_time_ms = 'updatedTime'
    trade_is_maker = 'isMaker'
    trade_volume = 'execQty'
    trade_price = 'execPrice'
    traded_time_ms = 'execTime'
    trade_id = 'execId'
    fee = 'execFee'


class KucoinRestful(Enum):
    balance_data = 'data'
    balance_type = 'type'
    balance_type_trade = 'trade'
    balance_type_main = 'main'
    balacne_currency = 'currency'
    balacne_amount = 'balance'
    data = 'data'
    price = 'price'
    deposit_withdraw_state = 'status'
    deposit_withdraw_id = 'id'
    deposit_withdraw_coin = 'currency'
    deposit_withdraw_amount = 'amount'
    deposit_withdraw_fee = 'fee'
    deposit_withdraw_network = 'chain'
    deposit_withdraw_txid = 'walletTxId'
    deposit_withdraw_time = 'updatedAt'
    transfer_id = 'applyId'
    symbol = 'symbol'
    order_side = 'side'
    order_buy = 'buy'
    client_order_id = 'clientOid'
    order_id = 'id'
    order_fee_coin = 'feeCurrency'
    order_type = 'type'
    order_limit = 'limit'
    order_volume = 'size'
    order_price = 'price'
    order_time = 'createdAt'
    order_deal_size = 'dealSize'
    order_stop_price = 'stopPrice'
    order_status = 'isActive'
    order_time_in_force = 'timeInForce'
    trade_id = 'tradeId'
    trade_order_id = 'orderId'
    trade_liquidity = 'liquidity'
    trade_fee = 'fee'
    trade_taker = 'taker'


class HuobiRestful(Enum):
    balance_data = 'data'
    balance_list = 'list'
    balance_currency = 'currency'
    balance_amount = 'balance'
    account_id = 'id'
    deposit_type = 'deposit'
    withdraw_type = 'withdraw'
    deposit_withdraw_id = 'id'
    deposit_withdraw_coin = 'currency'
    deposit_withdraw_network = 'chain'
    deposit_withdraw_amount = 'amount'
    deposit_withdraw_state = 'state'
    deposit_withdraw_time = 'updated-at'
    deposit_withdraw_fee = 'fee'
    transfer_id = 'record-id'
    transfer_time = 'transact-time'
    transfer_amount = 'transact-amt'
    transfer_type = 'transact-type'
    symbol = 'symbol'
    order_type = 'type'
    order_buy = 'buy'
    order_limit = 'limit'
    order_maker = 'maker'
    order_volume = 'amount'
    order_price = 'price'
    order_time = 'created-at'
    client_order_id = 'client-order-id'
    order_id = 'id'
    order_status = 'state'
    order_cumulative_volume = 'field-amount'
    trade_liquidity = 'role'
    trade_maker = 'maker'
    trade_volume = 'filled-amount'
    trade_price = 'price'
    trade_time = 'created-at'
    trade_id = 'trade-id'
    trade_order_id = 'order-id'
    trade_fee_coin = 'fee-currency'
    trade_fee = 'filled-fees'


class MexcRestful(Enum):
    symbol = 'symbol'
    balance_data = 'balances'
    balance_currency = 'asset'
    balance_free = 'free'
    balance_locked = 'locked'
    state = 'status'
    coin = 'coin'
    deposit_amount = 'amount'
    txid = 'txId'
    deposit_time = 'insertTime'
    withdraw_time = 'updateTime'
    withdraw_id = 'id'
    network = 'network'
    order_side = 'side'
    order_buy = 'BUY'
    client_order_id = 'clientOrderId'
    order_id = 'orderId'
    order_type = 'type'
    order_limit = 'LIMIT'
    order_volume = 'origQty'
    order_price = 'price'
    order_time = 'time'
    order_stop_price = 'stopPrice'
    order_status = 'status'
    order_time_in_force = 'timeInForce'
    order_cumulative_volume = 'cummulativeQuoteQty'
    trade_liquidity = 'isMaker'
    trade_side = 'isBuyer'
    trade_volume = 'quoteQty'
    trade_price = 'price'
    trade_id = 'id'
    trade_fee_coin = 'commissionAsset'
    trade_fee = 'commission'


class OkexRestful(Enum):
    balance_data = 'data'
    balance_details = 'details'
    balance_currency = 'ccy'
    balance_amount = 'eq'
    balance_balance = 'bal'
    deposit_withdraw_data = 'data'
    deposit_withdraw_amount = 'amt'
    network = 'chain'
    coin = 'ccy'
    deposit_withdraw_time = 'ts'
    txid = 'txId'
    state = 'state'
    deposit_id = 'depId'
    withdraw_fee = 'fee'
    withdraw_id = 'wdId'
    transfer_id = 'billId'
    transfer_amount = 'balChg'
    symbol = 'instId'
    order_side = 'side'
    order_buy = 'buy'
    order_type = 'ordType'
    order_market = 'market'
    order_price = 'px'
    order_volume = 'sz'
    order_time = 'cTime'
    order_id = 'ordId'
    client_order_id = 'clOrdId'
    order_fee_coin = 'feeCcy'
    order_stop_price = 'slTriggerPx'
    order_cumulative_volume = 'accFillSz'
    order_status = 'state'
    trade_liquidity = 'execType'
    trade_maker = 'M'
    trade_volume = 'fillSz'
    trade_price = 'fillPx'
    trade_time = 'ts'
    trade_id = 'tradeId'
    trade_fee = 'fee'
    transfer_type = 'type'


class BitgetRestful(Enum):
    data = 'data'
    balance_coin = 'coin'
    balance_available = 'available'
    balance_frozen = 'frozen'
    balance_locked = 'locked'
    deposit_withdraw_state = 'status'
    deposit_withdraw_coin = 'coin'
    deposit_withdraw_amount = 'size'
    deposit_withdraw_id = 'orderId'
    deposit_withdraw_network = 'chain'
    deposit_withdraw_txid = 'tradeId'
    deposit_withdraw_time = 'uTime'
    deposit_withdraw_success = 'success'
    withdraw_fee = 'fee'
    symbol = 'symbol'
    order_id = 'orderId'
    client_order_id = 'clientOid'
    order_price = 'price'
    order_volume = 'size'
    order_side = 'side'
    order_buy = 'buy'
    order_type = 'orderType'
    order_market = 'market'
    order_time = 'cTime'
    order_fee_detail = 'feeDetail'
    order_new_fee = 'newFees'
    order_status = 'status'
    order_cumulative_volume = 'quoteVolume'
    trade_liquidity = 'tradeScope'
    trade_maker = 'maker'
    trade_price = 'priceAvg'
    trade_volume = 'size'
    trade_id = 'tradeId'
    trade_fee_coin = 'feeCoin'
    trade_fee_detail = 'feeDetail'
    trade_fee = 'totalFee'


class BybitRestful(Enum):
    symbol = 'symbol'
    balance_result = 'result'
    balance_list = 'list'
    balance_coin = 'coin'
    balance_walletbalance = 'walletBalance'
    order_buy = 'Buy'
    order_side = 'side'
    order_type = 'orderType'
    order_limit = 'Limit'
    order_price = 'price'
    order_volume = 'qty'
    order_id = 'orderId'
    client_order_id = 'orderLinkId'
    order_time = 'createdTime'
    order_status = 'orderStatus'
    order_stop_price = 'stopLoss'
    order_update_time = 'updatedTime'
    order_cumulative_volume = 'cumExecQty'
    order_cumulative_amount = 'cumExecValue'
    order_time_in_force = 'timeInForce'
    order_fee_coin = 'feeCurrency'
    order_is_maker = 'isMaker'
    trade_price = 'execPrice'
    trade_volume = 'execQty'
    trade_id = 'execId'
    trade_time = 'execTime'
    trade_fee = 'execFee'
    deposit_withdraw_coin = 'coin'
    deposit_withdraw_network = 'chain'
    deposit_withdraw_amount = 'amount'
    deposit_withdraw_txid = 'txID'
    deposit_withdraw_state = 'status'
    deposit_fee = 'depositFee'
    withdraw_fee = 'withdrawFee'
    withdraw_id = 'withdrawId'
    withdraw_time = 'updateTime'
    deposit_time = 'successAt'
    deposit_id = 'txIndex'
    transfer_id = 'transferId'
    transfer_from = 'fromMemberId'
    transfer_to = 'toMemberId'
    transfer_time = 'timestamp'
    transfer_to_account_type = 'toAccountType'
    transfer_from_account_type = 'fromAccountType'


class GateioRestful(Enum):
    pair = 'currency_pair'
    balance_coin = 'currency'
    balance_available = 'available'
    balance_locked = 'locked'
    deposit_withdraw_id = 'id'
    deposit_withdraw_txid = 'txid'
    deposit_withdraw_state = 'status'
    deposit_withdraw_fee = 'fee'
    deposit_withdraw_network = 'chain'
    deposit_withdraw_coin = 'currency'
    deposit_withdraw_amount = 'amount'
    deposit_withdraw_time = 'timestamp'
    transfer_time = 'timest'
    order_id = 'id'
    client_order_id = 'text'
    order_side = 'side'
    order_buy = 'buy'
    order_type = 'type'
    order_limit = 'limit'
    order_price = 'price'
    order_volume = 'amount'
    order_time = 'create_time_ms'
    order_fee_coin = 'fee_currency'
    order_status = 'status'
    order_time_in_force = 'time_in_force'
    order_cumulative_volume = 'filled_amount'
    trade_fee = 'fee'
    trade_gt_fee = 'gt_fee'
    trade_taker = 'taker'
    trade_liquidity = 'role'
    trade_id = 'id'
    trade_order_id = 'order_id'
    direction = 'direction'


class BitmartRestful(Enum):
    data = 'data'
    wallet = 'wallet'
    currency = 'currency'
    available = 'available'
    frozen = 'frozen'
    deposit_withdraw_records = 'records'
    withdraw_id = 'withdraw_id'
    deposit_id = 'deposit_id'
    deposit_withdraw_status = 'status'
    deposit_withdraw_time = 'apply_time'
    deposit_withdraw_coin = 'currency'
    deposit_withdraw_fee = 'fee'
    deposit_withdraw_amount = 'arrival_amount'
    deposit_withdraw_txid = 'tx_id'
    transfer_time = 'submissionTime'
    transfer_coin = 'currency'
    transfer_from = 'fromAccount'
    transfer_to = 'toAccount'
    transfer_quantity = 'amount'
    transfer_historylist = 'historyList'
    order_side = 'side'
    order_buy = 'buy'
    order_type = 'type'
    order_market = 'market'
    symbol = 'symbol'
    order_volume = 'size'
    order_price = 'price'
    order_time = 'createTime'
    order_id = 'orderId'
    client_order_id = 'clientOrderId'
    order_status = 'state'
    order_cumulative_volume = 'filledSize'
    order_update_time = 'updateTime'
    trade_liquidity = 'tradeRole'
    trade_maker = 'maker'
    trade_volume = 'size'
    trade_price = 'price'
    trade_id = 'tradeId'
    trade_fee_coin = 'feeCoinName'
    trade_fee = 'fee'


class ExmoRestful(Enum):
    order_side = 'type'
    order_buy = 'buy'
    pair = 'pair'
    order_volume = 'quantity'
    order_price = 'price'
    order_time = 'created'
    client_order_id = 'client_id'
    order_id = 'order_id'
    trade_liquidity = 'exec_type'
    trade_maker = 'maker'
    traded_time = 'date'
    trade_fee_coin = 'commission_currency'
    trade_fee = 'commission_amount'
    trade_id = 'trade_id'
    balance_result = 'balances'
    balance_reserved = 'reserved'
    deposit_withdraw_list = 'items'
    deposit_withdraw_time = 'updated'
    deposit_withdraw_type = 'type'
    deposit_withdraw_coin = 'currency'
    deposit_withdraw_volume = 'amount'
    deposit_withdraw_status = 'status'
    deposit_withdraw_txid = 'txid'
    deposit_withdraw_extra = 'extra'
    deposit_withdraw_id = 'order_id'
    kline_data = 'candles'
    kline_open = 'o'
    kline_close = 'c'
    kline_volume = 'v'
    kline_high = 'h'
    kline_low = 'l'
    kline_open_time = 't'


class MercadoRestful(Enum):
    order_side = 'side'
    order_buy = 'buy'
    symbol = 'instrument'
    order_volume = 'qty'
    order_price = 'limitPrice'
    order_type = 'type'
    order_market = 'market'
    order_time = 'created_at'
    order_update_time = 'updated_at'
    order_id = 'id'
    trade_liquidity = 'liquidity'
    trade_maker = 'maker'
    trade_volume = 'qty'
    trade_price = 'price'
    trade_time = 'executed_at'
    trade_id = 'id'
    trade_fee = 'fee'
    order_status = 'status'
    balance_symbol = 'symbol'
    balance_total = 'total'
    kline_open = 'o'
    kline_close = 'c'
    kline_volume = 'v'
    kline_high = 'h'
    kline_low = 'l'
    kline_open_time = 't'


class WooxRestful(Enum):
    result_type = 'success'
    balance_data = 'data'
    balance_all = 'totalAccountValue'
    order_side = 'side'
    order_buy = 'BUY'
    order_type = 'type'
    order_limit = 'LIMIT'
    symbol = 'symbol'
    order_volume = 'quantity'
    order_price = 'price'
    order_time = 'created_time'
    order_id = 'order_id'
    client_order_id = 'client_order_id'
    order_status = 'status'
    order_update_time = 'updated_time'
    trade_volume = 'executed_quantity'
    trade_price = 'executed_price'
    trade_time = 'executed_timestamp'
    trade_id = 'id'
    trade_fee = 'fee'
    fee_coin = 'fee_asset'
    trade_liquidity = 'is_maker'


class HashkeyRestful(Enum):
    order_side = 'side'
    order_buy = 'BUY'
    order_type = 'type'
    order_limit = 'LIMIT'
    client_order_id = 'clientOrderId'
    order_id = 'orderId'
    symbol = 'symbol'
    order_volume = 'origQty'
    order_price = 'price'
    order_time = 'time'
    stop_price = 'stopPrice'
    order_status = 'status'
    order_time_in_force = 'timeInForce'
    order_data_time = 'updateTime'
    order_cumulative_volume = 'cummulativeQuoteQty'
    fee = 'feeAmount'
    fee_coin = 'feeCoin'
    trade_volume = 'executedQty'
    trade_price = 'avgPrice'
    balance = 'balances'


class KranKenRestful(Enum):
    symbol = 'pair'
    order_side = 'type'
    order_buy = 'buy'
    order_id = 'refid'
    client_order_id = 'cl_ord_id'
    trade_order_id = 'ordertxid'
    order_type = 'ordertype'
    order_limit = 'limit'
    order_volume = 'vol'
    order_price = 'price'
    order_stop_price = 'stopprice'
    order_status = 'status'
    order_time = 'opentm'
    order_update_time = 'starttm'
    trade_id = 'trade_id'
    order_is_maker = 'maker'
    trade_volume = 'vol'
    trade_price = 'price'
    trade_time = 'time'
    trade_fee = 'fee'


class BalanceAuxiliary(Enum):
    balance_volume = 'volume'
    balance_price = 'price'
    balance_usdt = 'value_in_u'


class OrderStatus(Enum):
    active = 'ACTIVE'
    fully_filled = 'FULLY_FILLED'
    partial_filled = 'PARTIAL_FILLED'
    cancelled = 'CANCELLED'
    expired = 'EXPIRED'


class OrderType(Enum):
    limit = 'LIMIT'
    market = 'MARKET'
    maker = 'MAKER'
    taker = 'TAKER'


class HttpMmthod(Enum):
    GET = auto()
    POST = auto()
    DELETE = auto()
    PUT = auto()


class OrderSide(Enum):
    buy = 'B'
    sell = 'S'


class WebsocketStatus(Enum):
    ACTIVE = auto()
    SUSPENDED = auto()
    INACTIVE = auto()
    STOP = auto()
    RECOVERY = auto()
    INIT = auto()
    STANDBY = auto()


class AdsAuxiliary(Enum):
    pro_url = 'http://liquid-account.prd8.apne-general.huobiapps.com'
    url_key = '/api/account/query'


class BinanceAuxiliary(Enum):
    url = 'https://api.binance.com'
    perp_url = 'https://fapi.binance.com'
    alpha_url = 'https://www.binance.com'
    url_ws = 'wss://stream.binance.com:9443/ws'
    url_ws_base = 'wss://stream.binance.com:9443'
    url_perp_ws = 'wss://fstream.binance.com/ws'
    user_data_stream = '/api/v3/userDataStream'
    user_swap_data_stream = '/fapi/v1/listenKey'
    url_ticker_24hr = '/api/v3/ticker/24hr'
    url_perp_ticker_24hr = '/fapi/v1/ticker/24hr'
    url_balance = '/api/v3/account'
    url_wallet = '/sapi/v1/asset/wallet/balance'
    url_funding_balance = '/sapi/v1/asset/get-funding-asset'
    url_alpha_balance = '/sapi/v1/asset/get-alpha-asset'
    url_alpha_coin = '/sapi/v1/alpha-trade/token/all/list'
    url_order = '/api/v3/order'
    url_cancel_all_order = '/api/v3/openOrders'
    url_exchange = '/api/v3/exchangeInfo'
    url_all_order = '/api/v3/allOrders'
    url_open_order = '/api/v3/openOrders'
    url_trade = '/api/v3/myTrades'
    url_kline = '/api/v3/klines'
    url_orderbook = '/api/v3/depth'
    url_deposit_history = '/sapi/v1/capital/deposit/hisrec'
    url_withdraw_history = '/sapi/v1/capital/withdraw/history'
    url_transfer_history = '/sapi/v1/sub-account/sub/transfer/history'
    url_transfer_sub_history = '/sapi/v1/sub-account/transfer/subUserHistory'
    url_swap_funding_rate = '/fapi/v1/fundingRate'
    url_swap_last_funding_rate = "/fapi/v1/premiumIndex"
    url_swap_last_funding_rate_info = "/fapi/v1/fundingInfo"
    url_swap_ticker_price = '/fapi/v2/ticker/price'
    url_swap_all_order = '/fapi/v1/allOrders'
    url_swap_force_order = '/fapi/v1/forceOrders'
    url_swap_position_risk = '/fapi/v2/positionRisk'
    url_swap_income = '/fapi/v1/income'
    url_swap_balance = '/fapi/v2/balance'
    url_swap_account = '/fapi/v2/account'
    url_swap_multi_margin = '/fapi/v1/multiAssetsMargin'
    url_swap_margin_type = '/fapi/v1/marginType'
    url_swap_open_interes_hist = '/futures/data/openInterestHist'
    url_alpha_exchange_info = "/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
    url_commission_rate = '/fapi/v1/commissionRate'
    ws_aggtrade = "@aggTrade"
    ws_ticker = "!ticker@arr"
    ws_kline_interval = '@kline_15m'
    ws_book_ticker = '@bookTicker'
    ws_orderbook = '@depth'
    ws_lastprice = '@markPrice@1s'
    ws_ping_sleep = 1800
    ws_reconnect_interval = 60 * 60 * 24
    ws_ping = 'ping'
    url_limit = 1000
    reconnection_time_sleep = 60 * 60 * 3
    ws_listen_key_sleep = 60


class BitfinexAuxiliary(Enum):
    url = 'https://api-pub.bitfinex.com/'
    url_ws = 'wss://api.bitfinex.com/ws/2'
    url_private = 'https://api.bitfinex.com/'
    url_orderbook = 'v2/book'
    url_kline = 'v2/candles'
    url_ticker = 'v2/tickers'
    url_orders = 'v2/auth/r/orders/hist'
    url_trades = 'v2/auth/r/trades/hist'
    url_balance = 'v2/auth/r/wallets'
    url_deposit = 'v2/auth/r/movements/hist'
    url_transfer = 'v2/auth/r/ledgers/hist'
    limit = 2500
    ws_ping_sleep = 1800
    reconnection_time_sleep = 60 * 60 * 2


class HuobiAuxiliary(Enum):
    url = 'https://api.huobi.pro'
    url_ws = 'wss://api.huobi.pro/ws'
    url_exchange = '/v2/settings/common/symbols'
    url_ticker = '/market/tickers'
    swap_url = 'https://api.hbdm.com'
    url_swap_cross_position_info = '/linear-swap-api/v1/swap_cross_position_info'
    url_swap_position_info = '/linear-swap-api/v1/swap_position_info'
    url_swap_financial_record = '/linear-swap-api/v3/swap_financial_record'
    url_orderbook = '/market/depth'
    url_account = '/v1/account/accounts'
    url_balance = '/v1/account/accounts/{}/balance'
    url_deposit_withdraw = '/v1/query/deposit-withdraw'
    url_transfer = '/v1/account/history'
    url_orders = '/v1/order/history'
    url_trades = '/v1/order/matchresults'
    url_swap_balance = '/linear-swap-api/v1/swap_balance_valuation'
    url_commission_rate = '/v2/reference/transact-fee-rate/get'
    ws_ping_sleep = 1800
    reconnection_time_sleep = 60 * 60 * 2


class OkexAuxiliary(Enum):
    url = 'https://www.okx.com'
    url_ws = 'wss://ws.okx.com:8443/ws/v5/public'
    swap_url = 'https://www.okx.com'
    url_swap_position = '/api/v5/account/positions'
    url_swap_interest = '/api/v5/account/interest-accrued'
    url_exchange = '/api/v5/public/instruments'
    url_ticker = '/api/v5/market/tickers'
    url_orderbook = '/api/v5/market/books-full'
    url_balance = '/api/v5/account/balance'
    url_asset_balance = '/api/v5/asset/balances'
    url_deposit_history = '/api/v5/asset/deposit-history'
    url_withdraw_history = '/api/v5/asset/withdrawal-history'
    url_transfer_history = '/api/v5/asset/bills-history'
    url_orders = '/api/v5/trade/orders-history'
    url_trades = '/api/v5/trade/fills-history'
    url_commission_rate = '/api/v5/account/trade-fee'
    ws_orders = '/ws/v5/private'
    ws_ping_sleep = 1800
    reconnection_time_sleep = 60 * 60 * 2


class BybitAuxiliary(Enum):
    url = 'https://api.bybit.com'
    url_ws = 'wss://stream.bybit.com/v5/private'
    swap_url = 'https://api.bybit.com'
    url_swap_position = '/v5/position/list'
    url_swap_interest = '/v5/account/borrow-history'
    url_exchange = '/v5/market/instruments-info'
    url_ticker = '/v5/market/tickers'
    url_orderbook = '/v5/market/orderbook'
    url_balance = '/v5/account/wallet-balance'
    url_deposit = '/v5/asset/deposit/query-record'
    url_deposit_sub = '/v5/asset/deposit/query-sub-member-record'
    url_deposit_internal = '/v5/asset/deposit/query-internal-record'
    url_withdraw = '/v5/asset/withdraw/query-record'
    url_transfer = '/v5/asset/transfer/query-universal-transfer-list'
    url_order = '/v5/order/history'
    url_trade = '/v5/execution/list'
    ws_ping_sleep = 1800
    reconnection_time_sleep = 60 * 60 * 2


class KucoinAuxiliary(Enum):
    url = 'https://api.kucoin.com'
    url_ws = 'wss://ws-api-spot.kucoin.com'
    swap_url = 'https://api.kucoin.com'
    url_tickers = '/api/v1/market/allTickers'
    url_ticker = '/api/v1/market/orderbook/level1'
    url_exchange = '/api/v2/symbols'
    url_orderbook = '/api/v1/market/orderbook/level2_'
    url_balance = '/api/v1/accounts'
    url_deposits_history = '/api/v1/deposits'
    url_orders = '/api/v1/orders'
    url_trades = '/api/v1/fills'
    url_withdraw_history = '/api/v1/withdrawals'
    url_ws_token = '/api/v1/bullet-public'
    url_transfer_history = '/api/v1/transfer-list'
    ws_ping_sleep = 1800
    reconnection_time_sleep = 60 * 60 * 2


class MexcAuxiliary(Enum):
    url = 'https://api.mexc.com'
    swap_url = 'https://api.mexc.com'
    url_ticker = '/api/v3/ticker/24hr'
    url_exchange = '/api/v3/exchangeInfo'
    url_orderbook = '/api/v3/depth'
    url_balance = '/api/v3/account'
    url_deposit_history = '/api/v3/capital/deposit/hisrec'
    url_withdraw_history = '/api/v3/capital/withdraw/history'
    url_transfer_history = '/api/v3/capital/transfer'
    url_internal_transfer_history = '/api/v3/capital/transfer/internal'
    url_orders = '/api/v3/allOrders'
    url_trades = '/api/v3/myTrades'


class GateioAuxiliary(Enum):
    url = 'https://api.gateio.ws'
    swap_url = 'https://api.gateio.ws'
    url_ticker = '/api/v4/spot/tickers'
    url_exchange = '/api/v4/spot/currency_pairs'
    url_orderbook = '/api/v4/spot/order_book'
    url_balance = '/api/v4/spot/accounts'
    url_deposit_history = '/api/v4/wallet/deposits'
    url_withdraw_history = '/api/v4/wallet/withdrawals'
    url_transfer_history = '/api/v4/wallet/sub_account_transfers'
    url_orders = '/api/v4/spot/orders'
    url_trades = '/api/v4/spot/my_trades'
    url_swap_position = '/api/v4/futures/'
    url_swap_position_risk = '/positions'
    url_swap_position_accounts = '/accounts'
    url_swap_position_income = '/account_book'


class BitgetAuxiliary(Enum):
    url = 'https://api.bitget.com'
    swap_url = 'https://api.bitget.com'
    url_exchange = '/api/v2/spot/public/symbols'
    url_ticker = '/api/v2/spot/market/tickers'
    url_orderbook = '/api/v2/spot/market/orderbook'
    url_balances = '/api/v2/spot/account/assets'
    url_deposit_history = '/api/v2/spot/wallet/deposit-records'
    url_withdraw_history = '/api/v2/spot/wallet/withdrawal-records'
    url_transfer_history = '/api/v2/spot/account/transferRecords'
    url_orders = '/api/v2/spot/trade/history-orders'
    url_trades = '/api/v2/spot/trade/fills'


class WazirxAuxiliary(Enum):
    url = 'https://api.wazirx.com'
    swap_url = 'https://api.wazirx.com'
    url_exchange = '/sapi/v1/exchangeInfo'
    url_ticker = '/sapi/v1/tickers/24hr'
    url_orderbook = '/sapi/v1/depth'
    url_kline = '/sapi/v1/klines'


class BullishAuxiliary(Enum):
    url = 'https://api.exchange.bullish.com/trading-api'
    swap_url = 'https://api.exchange.bullish.com/trading-api'
    url_exchange = '/v1/markets'
    url_ticker = '/tick'
    url_orderbook = '/orderbook/hybrid'


class HashkeyAuxiliary(Enum):
    url = 'https://api-glb.hashkey.com'
    swap_url = 'https://api-glb.hashkey.com'
    url_exchange = '/api/v1/exchangeInfo'
    url_ticker = '/quote/v1/ticker/24hr'
    url_orderbook = '/quote/v1/depth'
    url_orders = '/api/v1/spot/tradeOrders'
    url_trade = '/api/v1/spot/order'
    url_balances = '/api/v1/account'


class MercadoAuxiliary(Enum):
    url = 'https://api.mercadobitcoin.net/api/v4'
    swap_url = 'https://api.mercadobitcoin.net/api/v4'
    url_exchange = '/symbols'
    url_orderbook = '/orderbook'
    url_ticker = '/tickers'
    url_accounts = '/accounts'
    url_orders = '/orders'
    url_auth = '/authorize'
    url_balance = '/balances'
    url_kline = '/candles'


class BitmartAuxiliary(Enum):
    url = 'https://api-cloud.bitmart.com'
    swap_url = 'https://api-cloud.bitmart.com'
    url_exchange = '/spot/v1/symbols/details'
    url_tickers = '/spot/quotation/v3/tickers'
    url_ticker = '/spot/quotation/v3/ticker'
    url_ticker_trades = '/spot/quotation/v3/trades'
    url_orderbook = '/spot/quotation/v3/books'
    url_balance = '/account/v1/wallet'
    url_deposit_withdraw_history = '/account/v2/deposit-withdraw/history'
    url_transfer = '/account/sub-account/v1/transfer-history'
    url_subaccount_list = '/account/sub-account/main/v1/subaccount-list'
    url_orders = '/spot/v4/query/history-orders'
    url_trades = '/spot/v4/query/trades'


class ExmoAuxiliary(Enum):
    url = 'https://api.exmo.com/v1.1'
    swap_url = 'https://api.exmo.com/v1.1'
    url_exchange = '/pair_settings'
    url_orderbook = '/order_book'
    url_ticker = '/ticker'
    url_balance = '/user_info'
    url_orders = '/user_open_orders'
    url_trades = '/user_trades'
    url_deposit_withdraw_history = '/wallet_operations'
    url_kline = '/candles_history'


class WooxAuxiliary(Enum):
    url = 'https://api.woox.io'
    swap_url = 'https://api.woox.io'
    url_version_v1 = '/v1'
    url_version_v3 = '/v3'
    url_exchange = '/public/info'
    url_balance = '/accountinfo'
    url_orderbook = '/public/orderbook'
    url_kline = '/public/kline'
    url_orders = '/orders'
    url_trades = '/client/trades'


class KrakenAuxiliary(Enum):
    url = 'https://api.kraken.com'
    url_exchange = '/0/public/AssetPairs'
    url_balance = '/0/private/Balance'
    url_orderbook = '/0/public/Depth'
    url_trade = '/0/public/Trades'
    url_ticker = '/0/public/Ticker'
    url_orders = '/0/private/ClosedOrders'
    url_trades = '/0/private/TradesHistory'


class PoloniexAuxiliary(Enum):
    url = "https://api.poloniex.com"
    url_ticker = "/markets/ticker24h"
    url_exchange = "/markets"


class LbankAuxiliary(Enum):
    url = "https://api.lbkex.com"
    swap_url = "https://api.lbkex.com"
    url_ticker = "/v2/ticker/24hr.do"
    url_ticker_price = '/v2/supplement/ticker/price.do'
    url_exchange = "/v2/currencyPairs.do"
    url_orderbook = "/v2/depth.do"


class ConfluenceApi(Enum):
    confluence_api_url = 'https://mvid.atlassian.net/wiki'
    confluence_space_key = 'process'


class BinanceSwapRestful(Enum):
    symbol = auto()
    positionAmt = auto()
    entryPrice = auto()
    liquidationPrice = auto()
    markPrice = auto()
    marginType = auto()
    isolatedMargin = auto()
    totalMargin = auto()
    unRealizedProfit = auto()
    leverage = auto()
    updateTime = auto()
    time = auto
    incomeType = auto()
    income = auto()
    asset = auto()
    tradeId = auto()
    tranId = auto()
    cross = auto()
    crossWalletBalance = auto()
    marginAvailable = auto()
    totalMarginBalance = auto()
    totalWalletBalance = auto()
    multiAssetsMargin = auto()


class KlineExtraAttribute(Enum):
    number_of_trades = auto()
    taker_buy_base_asset_volume = auto()
    taker_buy_quote_asset_volume = auto()
    ignored = auto()


class RestfulRequestsAttribute(Enum):
    symbol = auto()
    interval = auto()
    limit = auto()
    start_time = auto()
    end_time = auto()
    startTime = auto()
    endTime = auto()


class DepositState(Enum):
    comformed = auto()
    comforming = auto()


class TransferAbstract(Enum):
    fromemail = auto()
    toemail = auto()
    coin = auto()
    quantity = auto()
    event_time = auto()
    tranid = auto()


class RunningMode(Enum):
    development_flag = auto()  # 测试服务器 + 测试频道 + 单次运行
    testing_flag = auto()  # 测试服务器 + 测试频道 + 定时运行
    production_flag = auto()  # 正式服务器 + 正式频道 + 定时运行


class HuobiSwapRestful(Enum):
    lever_rate = auto()
    symbol = auto()
    direction = auto()
    buy = auto()
    cost_open = auto()
    liquidation_price = auto()
    last_price = auto()
    margin_mode = auto()
    profit = auto()
    position_margin = auto()
    contract_code = auto()
    amount = auto()
    asset = auto()
    id = auto()
    query_id = auto()
    volume = auto()


class OkexSwapRestful(Enum):
    posSide = auto()
    long = auto()
    lever = auto()
    instId = auto()
    pos = auto()
    avgPx = auto()
    liqPx = auto()
    last = auto()
    mgnMode = auto()
    upl = auto()
    imr = auto()
    margin = auto()
    cross = auto()
    interest = auto()
    ccy = auto()


class BybitSwapRestful(Enum):
    side = auto()
    Buy = auto()
    leverage = auto()
    symbol = auto()
    positionValue = auto()
    avgPrice = auto()
    markPrice = auto()
    positionBalance = auto()
    liqPrice = auto()
    tradeMode = auto()
    cross = auto()
    isolated = auto()
    cumRealisedPnl = auto()
    currency = auto()
    borrowCost = auto()


class GateSwapRestful(Enum):
    symbol = auto()
    settle = auto()
    leverage = auto()
    size = auto()
    update_time = auto()
    value = auto()
    entry_price = auto()
    liq_price = auto()
    mark_price = auto()
    initial_margin = auto()
    unrealised_pnl = auto()
    total = auto()
    time = auto()
    trade_id = auto()
    contract = auto()
    change = auto()
    cross_leverage_limit = auto()


class RedisFields(Enum):
    ticker_price = auto()
    book_ticker = auto()
    orderbook = auto()
    orders = auto()
    trades = auto()
    inventory = auto()
    inventory_close = auto()
    trading_proposal = auto()
    non_compliant_inst_code = auto()
    depth_order_theoretical = auto()


class DuplicateFields(Enum):
    trade = [TradeAttribute.trade_id.name, TradeAttribute.side.name]
    order = [OrderAttribute.order_id.name, OrderAttribute.data_time_ms.name,
             OrderAttribute.cumulative_volume.name, OrderAttribute.status.name]
    order_bfx_raw = [OrderAttribute.order_id.name, OrderAttribute.data_time_ms.name,
                     OrderAttribute.cumulative_volume.name, OrderAttribute.status.name, OrderAttribute.other.name]
    order_id = [OrderAttribute.order_id.name]


class ConfigField:
    def __init__(self, section, key):
        self.section = section
        self.key = key


class MmTarget(Enum):
    average_depth = auto()
    side_depth = auto()
    exchange_rank = auto()
    quote_rank = auto()
    spread = auto()
    kline_frame = auto()
    volume_24h = auto()


class TickerPriceFields(Enum):
    event_time_ms = auto()


class TradingProposalType(Enum):
    buy_sell = auto()
    buy_only = auto()
    sell_only = auto()
    no_trade = auto()
    trading_proposal = auto()
    wake_up_time = auto()
