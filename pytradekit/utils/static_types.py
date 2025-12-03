from enum import Enum, auto


class Database(Enum):
    raw_accounts = auto()
    all_accounts = auto()
    deposit_withdraw = auto()
    balance = auto()
    raw_orders = auto()
    orders = auto()
    trades = auto()
    orders_other = auto()
    trades_other = auto()
    trades_inventory = auto()
    orders_raw = auto()
    raw_kline = auto()
    raw_market = auto()
    rank = auto()
    order_book = auto()
    order_book_ws = auto()
    order_depth_ratio = auto()
    metrics_volume = auto()
    metrics_order_depth = auto()
    account_daily_traded = auto()
    account_daily_ordered = auto()
    raw_task = auto()
    run_task = auto()
    mm_pairs = auto()
    kline_pairs = auto()
    mvid_official = auto()
    last_agg_trade_time = auto()
    perp_position = auto()
    perp_income = auto()
    raw_metrics = auto()
    indicator = auto()
    inst_code_basic = auto()
    pnl = auto()
    volume_fee = auto()
    depth = auto()
    budget = auto()
    inventory = auto()
    inventory_management = auto()
    inventory_quantity = auto()
    unrealized_profit_close_point = auto()
    daily_order = auto()
    loan = auto()
    max_inventory = auto()


class OrderAttribute(Enum):
    event_time_ms = auto()
    run_time_ms = auto()
    account_id = auto()
    inst_code = auto()
    side = auto()
    volume = auto()
    price = auto()
    portfolio_id = auto()
    strategy_id = auto()
    exchange_id = auto()
    order_time_ms = auto()
    client_order_id = auto()
    order_id = auto()
    fee_coin = auto()
    order_type = auto()
    stop_price = auto()
    status = auto()
    time_in_force = auto()
    cumulative_volume = auto()
    cumulative_amount = auto()
    unfilled_volume = auto()
    unfilled_amount = auto()
    working_time_ms = auto()
    data_time_ms = auto()
    day = auto()
    other = auto()


class Order:
    __slots__ = [attr.name for attr in OrderAttribute]

    def __init__(self, event_time_ms, run_time_ms, account_id, inst_code, side, volume, price, portfolio_id,
                 strategy_id,
                 exchange_id, order_time_ms, client_order_id, order_id, fee_coin, order_type, stop_price, status,
                 time_in_force, cumulative_volume, cumulative_amount, unfilled_volume, unfilled_amount, working_time_ms,
                 data_time_ms, day, other=None):
        self.event_time_ms = event_time_ms
        self.run_time_ms = run_time_ms
        self.account_id = account_id
        self.inst_code = inst_code
        self.side = side
        self.volume = volume
        self.price = price
        self.portfolio_id = portfolio_id
        self.strategy_id = strategy_id
        self.exchange_id = exchange_id
        self.order_time_ms = order_time_ms
        self.client_order_id = client_order_id
        self.order_id = order_id
        self.fee_coin = fee_coin
        self.order_type = order_type
        self.stop_price = stop_price
        self.status = status
        self.time_in_force = time_in_force
        self.cumulative_volume = cumulative_volume
        self.cumulative_amount = cumulative_amount
        self.unfilled_volume = unfilled_volume
        self.unfilled_amount = unfilled_amount
        self.working_time_ms = working_time_ms
        self.data_time_ms = data_time_ms
        self.day = day
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class TradeAttribute(Enum):
    event_time_ms = auto()
    run_time_ms = auto()
    account_id = auto()
    inst_code = auto()
    side = auto()
    filled_volume = auto()
    executed_price = auto()
    portfolio_id = auto()
    strategy_id = auto()
    exchange_id = auto()
    traded_time_ms = auto()
    client_order_id = auto()
    order_id = auto()
    trade_id = auto()
    fee_coin = auto()
    day = auto()
    fee = auto()
    filled_status = auto()
    triggered_source = auto()
    other = auto()


class Trade:
    __slots__ = [attr.name for attr in TradeAttribute]

    def __init__(self, event_time_ms, run_time_ms, account_id, inst_code, side, filled_volume, executed_price,
                 portfolio_id, strategy_id, exchange_id, traded_time_ms, client_order_id,
                 order_id, trade_id, fee_coin, day, fee,
                 filled_status, triggered_source, other=None):
        self.event_time_ms = event_time_ms
        self.run_time_ms = run_time_ms
        self.account_id = account_id
        self.inst_code = inst_code
        self.side = side
        self.filled_volume = filled_volume
        self.executed_price = executed_price
        self.portfolio_id = portfolio_id
        self.strategy_id = strategy_id
        self.exchange_id = exchange_id
        self.traded_time_ms = traded_time_ms
        self.client_order_id = client_order_id
        self.order_id = order_id
        self.trade_id = trade_id
        self.fee_coin = fee_coin
        self.day = day
        self.fee = fee
        self.filled_status = filled_status
        self.triggered_source = triggered_source
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class KlineAttribute(Enum):
    event_time_ms = auto()
    run_time_ms = auto()
    inst_code = auto()
    open_time_s = auto()
    close_time_s = auto()
    frame = auto()
    open = auto()
    high = auto()
    low = auto()
    close = auto()
    volume = auto()
    amount = auto()
    other = auto()


class Kline:
    __slots__ = [attr.name for attr in KlineAttribute]

    def __init__(self, event_time_ms, run_time_ms, inst_code, open_time_s, close_time_s, frame,
                 open, high, low, close, volume, amount, other=None):
        self.event_time_ms = event_time_ms
        self.run_time_ms = run_time_ms
        self.inst_code = inst_code
        self.open_time_s = open_time_s
        self.close_time_s = close_time_s
        self.frame = frame
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.amount = amount
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class BalanceAttribute(Enum):
    event_time_ms = auto()
    run_time_ms = auto()
    hour = auto()
    account_id = auto()
    value = auto()
    other = auto()


class Balance:
    __slots__ = [attr.name for attr in BalanceAttribute]

    def __init__(self, event_time_ms, run_time_ms, hour, account_id, value, other=None):
        self.event_time_ms = event_time_ms
        self.run_time_ms = run_time_ms
        self.hour = hour
        self.account_id = account_id
        self.value = value  # Expect value to be a dictionary with balance info
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class AccountAttribute(Enum):
    event_time_ms = auto()
    email = auto()
    uid = auto()
    account_name = auto()
    account_id = auto()
    exchange_id = auto()
    user = auto()
    is_mm = auto()
    description = auto()
    symbol = auto()
    strategy_id = auto()
    is_subaccount = auto()
    main_account = auto()
    other = auto()


class Account:
    __slots__ = [attr.name for attr in AccountAttribute]

    def __init__(self, event_time_ms, email, uid, account_name, account_id, exchange_id, user, is_mm, description,
                 symbol, strategy_id, is_subaccount, main_account, other=None):
        self.event_time_ms = event_time_ms
        self.email = email
        self.uid = uid
        self.account_name = account_name
        self.account_id = account_id
        self.exchange_id = exchange_id
        self.user = user
        self.is_mm = is_mm
        self.description = description
        self.symbol = symbol  # Expect symbol to be a list of symbols
        self.strategy_id = strategy_id
        self.is_subaccount = is_subaccount
        self.main_account = main_account
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class DepositWithdrawAttribute(Enum):
    event_time_ms = auto()
    run_time_ms = auto()
    time_ms = auto()
    account_id = auto()
    id = auto()
    coin = auto()
    is_inner = auto()
    txid = auto()
    quantity = auto()
    fee = auto()
    network = auto()
    state = auto()
    other = auto()


class DepositWithdraw:
    __slots__ = [attr.name for attr in DepositWithdrawAttribute]

    def __init__(
            self, event_time_ms, run_time_ms, time_ms, account_id, id, coin, is_inner,
            txid, quantity, fee, network, state, other=None):
        self.event_time_ms = event_time_ms
        self.run_time_ms = run_time_ms
        self.time_ms = time_ms
        self.account_id = account_id
        self.id = id
        self.coin = coin
        self.is_inner = is_inner  # Expect inner_transfer to be a dict with 'from_account' and 'to_account'
        self.txid = txid  # Expect outer_transfer to be a dict with 'tx_id' and 'address'
        self.quantity = quantity
        self.fee = fee
        self.network = network
        self.state = state
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class RankAttribute(Enum):
    event_time_ms = auto()
    run_time_ms = auto()
    inst_code = auto()
    hour = auto()
    exchange_rank = auto()
    seq_rank = auto()
    volume_24h = auto()
    exchange_id = auto()
    other = auto()


class Rank:
    __slots__ = [attr.name for attr in RankAttribute]

    def __init__(self, event_time_ms, run_time_ms, inst_code, hour, exchange_rank, seq_rank, volume_24h, exchange_id,
                 other=None):
        self.event_time_ms = event_time_ms
        self.run_time_ms = run_time_ms
        self.inst_code = inst_code
        self.hour = hour
        self.exchange_rank = exchange_rank
        self.seq_rank = seq_rank
        self.volume_24h = volume_24h
        self.exchange_id = exchange_id
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class RunTaskAttribute(Enum):
    task_id = auto()
    task_name = auto()
    producer = auto
    update_time_ms = auto()
    file_name = auto()
    status = auto()
    enable_status = auto()
    expiration_time_ms = auto()


class RunTask:
    __slots__ = [attr.name for attr in RunTaskAttribute]

    def __init__(self, task_id, task_name, producer, update_time_ms, file_name, status, enable_status,
                 expiration_time_ms):
        self.task_id = task_id
        self.task_name = task_name
        self.producer = producer
        self.update_time_ms = update_time_ms
        self.file_name = file_name
        self.status = status
        self.enable_status = enable_status
        self.expiration_time_ms = expiration_time_ms

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class IndexAttribute(Enum):
    index_id = auto()
    index_class = auto()
    index_name = auto()
    security_level = auto()
    data_source = auto()
    output_range = auto()
    event_time_ms = auto()


class Index:
    __slots__ = [attr.name for attr in IndexAttribute]

    def __init__(self, index_id, index_class, index_name, security_level, data_source, output_range, event_time_ms):
        self.index_id = index_id
        self.index_class = index_class
        self.index_name = index_name
        self.security_level = security_level
        self.data_source = data_source
        self.output_range = output_range
        self.event_time_ms = event_time_ms

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class OrderBookAttribute(Enum):
    event_time_ms = auto()
    run_time_ms = auto()
    time_ms = auto()
    inst_code = auto()
    bids = auto()
    asks = auto()
    threshold = auto()
    insufficient_flag = auto()
    other = auto()


class OrderBook:
    __slots__ = [attr.name for attr in OrderBookAttribute]

    def __init__(self, event_time_ms, run_time_ms, time_ms, inst_code, bids, asks, threshold, insufficient_flag,
                 other=None):
        self.event_time_ms = event_time_ms
        self.run_time_ms = run_time_ms
        self.time_ms = time_ms
        self.inst_code = inst_code
        self.bids = bids  # Expect bids to be a list of [price, volume]
        self.asks = asks  # Expect asks to be a list of [price, volume]
        self.threshold = threshold
        self.insufficient_flag = insufficient_flag
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class OrderBookWsAttribute(Enum):
    bids = auto()
    asks = auto()
    lastupdateid = auto()
    changed_within_threshold = auto()

class OrderBookWs:
    __slots__ = [attr.name for attr in OrderBookWsAttribute]

    def __init__(self, bids, asks, lastupdateid, changed_within_threshold):
        self.bids = bids  # Expect bids to be a list of [price, volume]
        self.asks = asks  # Expect asks to be a list of [price, volume]
        self.lastupdateid = lastupdateid
        self.changed_within_threshold = changed_within_threshold

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class LastAggtradeAttribute(Enum):
    update_time_ms = auto()
    inst_code = auto()
    time_interval_s = auto()
    sleep_time_s = auto()
    exchange_id = auto()


class LastAggtrade:
    __slots__ = [attr.name for attr in LastAggtradeAttribute]

    def __init__(self, update_time_ms, inst_code, time_interval_s, sleep_time_s, exchange_id):
        self.update_time_ms = update_time_ms
        self.inst_code = inst_code
        self.time_interval_s = time_interval_s
        self.sleep_time_s = sleep_time_s
        self.exchange_id = exchange_id

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class MMPairsAttribute(Enum):
    exchange_id = auto()
    update_time_ms = auto()
    mm_pairs = auto()
    hedge_pairs = auto()
    kline_pairs = auto()
    test_pairs = auto()


class MMPair:
    __slots__ = [attr.name for attr in MMPairsAttribute]

    def __init__(self, exchange_id, update_time_ms, mm_pairs, hedge_pairs, kline_pairs, test_pairs):
        self.exchange_id = exchange_id
        self.update_time_ms = update_time_ms
        self.mm_pairs = mm_pairs
        self.hedge_pairs = hedge_pairs
        self.kline_pairs = kline_pairs
        self.test_pairs = test_pairs

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class PerpPositionAttribute(Enum):
    event_time_ms = auto()
    time_ms = auto()
    leverage = auto()
    portfolio_id = auto()
    strategy_id = auto()
    exchange_id = auto()
    account_id = auto()
    inst_code = auto()
    side = auto()
    amount = auto()
    entry_price = auto()
    liquidation_price = auto()
    mark_price = auto()
    margin_type = auto()
    margin = auto()
    margin_coin = auto()
    pnl = auto()
    pnl_coin = auto()
    balance = auto()
    multi_margin = auto()
    other = auto()


class PerpPosition:
    __slots__ = [attr.name for attr in PerpPositionAttribute]

    def __init__(self, event_time_ms, time_ms, leverage, portfolio_id, strategy_id, exchange_id, account_id, inst_code,
                 side, amount,
                 entry_price, liquidation_price, mark_price, margin_type, margin, margin_coin, pnl, pnl_coin, balance,
                 multi_margin, other=None):
        self.event_time_ms = event_time_ms
        self.time_ms = time_ms
        self.leverage = leverage
        self.portfolio_id = portfolio_id
        self.strategy_id = strategy_id
        self.exchange_id = exchange_id
        self.account_id = account_id
        self.inst_code = inst_code
        self.side = side
        self.amount = amount
        self.entry_price = entry_price
        self.liquidation_price = liquidation_price
        self.mark_price = mark_price
        self.margin_type = margin_type
        self.margin = margin
        self.margin_coin = margin_coin
        self.pnl = pnl
        self.pnl_coin = pnl_coin
        self.balance = balance
        self.multi_margin = multi_margin
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class PerpIncomeAttribute(Enum):
    event_time_ms = auto()
    time_ms = auto()
    account_id = auto()
    inst_code = auto()
    income_type = auto()
    income = auto()
    income_coin = auto()
    trade_id = auto()
    tran_id = auto()
    other = auto()


class PerpIncome:
    __slots__ = [attr.name for attr in PerpIncomeAttribute]

    def __init__(self, event_time_ms, time_ms, account_id, inst_code, income_type, income, income_coin, trade_id,
                 tran_id, other=None):
        self.event_time_ms = event_time_ms
        self.time_ms = time_ms
        self.account_id = account_id
        self.inst_code = inst_code
        self.income_type = income_type
        self.income = income
        self.income_coin = income_coin
        self.trade_id = trade_id
        self.tran_id = tran_id
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class PnlAttribute(Enum):
    event_time_ms = auto()
    run_time_ms = auto()
    day = auto()
    inst_code = auto()
    strategy_id = auto()
    base_diff = auto()
    quote_diff = auto()
    base_price = auto()
    quote_price = auto()
    pnl = auto()
    other = auto()


class Pnl:
    __slots__ = [attr.name for attr in PnlAttribute]

    def __init__(self, event_time_ms, run_time_ms, day, pnl, inst_code, strategy_id, base_diff, quote_diff,
                 base_price, quote_price, other=None):
        self.event_time_ms = event_time_ms
        self.run_time_ms = run_time_ms
        self.day = day
        self.pnl = pnl
        self.inst_code = inst_code
        self.strategy_id = strategy_id
        self.base_diff = base_diff
        self.quote_diff = quote_diff
        self.base_price = base_price
        self.quote_price = quote_price
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class VolumeFeeAttribute(Enum):
    event_time_ms = auto()
    run_time_ms = auto()
    day = auto()
    strategy_id = auto()
    inst_code = auto()
    volume = auto()
    inner_volume = auto()
    maker_volume = auto()
    taker_volume = auto()
    bps_maker = auto()
    bps_taker = auto()
    fee = auto()
    order_traded_amount = auto()
    other = auto()


class VolumeFee:
    __slots__ = [attr.name for attr in VolumeFeeAttribute]

    def __init__(self, event_time_ms, run_time_ms, day, strategy_id, inst_code, volume, inner_volume, maker_volume,
                 taker_volume,
                 bps_maker,
                 bps_taker, fee, order_traded_amount, other=None):
        self.event_time_ms = event_time_ms
        self.run_time_ms = run_time_ms
        self.day = day
        self.strategy_id = strategy_id
        self.inst_code = inst_code
        self.volume = volume
        self.inner_volume = inner_volume
        self.maker_volume = maker_volume
        self.taker_volume = taker_volume
        self.bps_maker = bps_maker
        self.bps_taker = bps_taker
        self.fee = fee
        self.order_traded_amount = order_traded_amount
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class OrderDailyStatsAttribute(Enum):
    event_time_ms = auto()
    run_time_ms = auto()
    day = auto()
    strategy_id = auto()
    inst_code = auto()
    volume = auto()
    count = auto()
    high_price = auto()
    low_price = auto()
    markup_factor = auto()
    other = auto()


class OrderDailyStats:
    __slots__ = [attr.name for attr in OrderDailyStatsAttribute]

    def __init__(self, event_time_ms, run_time_ms, day, strategy_id, inst_code, volume, count,
                 high_price, low_price, markup_factor, other=None):
        self.event_time_ms = event_time_ms
        self.run_time_ms = run_time_ms
        self.day = day
        self.strategy_id = strategy_id
        self.inst_code = inst_code
        self.volume = volume
        self.count = count
        self.high_price = high_price
        self.low_price = low_price
        self.markup_factor = markup_factor
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class InstcodeBasicAttribute(Enum):
    event_time_ms = auto()
    inst_code = auto()
    base = auto()
    quote = auto()
    pair = auto()
    symbol = auto()
    tick_price = auto()
    tick_vol = auto()
    min_notional = auto()
    exchange_id = auto()
    other = auto()


class InstcodeBasic:
    __slots__ = [attr.name for attr in InstcodeBasicAttribute]

    def __init__(self, event_time_ms, inst_code, base, quote, pair, symbol, tick_price, tick_vol, min_notional,
                 exchange_id, other=None):
        self.event_time_ms = event_time_ms
        self.inst_code = inst_code
        self.base = base
        self.quote = quote
        self.pair = pair
        self.symbol = symbol
        self.tick_price = tick_price
        self.tick_vol = tick_vol
        self.min_notional = min_notional
        self.exchange_id = exchange_id
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class OrderDepthRatioAttribute(Enum):
    event_time_ms = auto()
    run_time_ms = auto()
    time_ms = auto()
    strategy_id = auto()
    inst_code = auto()
    depth_bid = auto()
    depth_ask = auto()
    order_bid = auto()
    order_ask = auto()
    bid_ratio = auto()
    ask_ratio = auto()
    depth_ratio = auto()
    other = auto()


class OrderDepthRatio:
    __slots__ = [attr.name for attr in OrderDepthRatioAttribute]

    def __init__(self, event_time_ms, run_time_ms, time_ms, strategy_id, inst_code, depth_bid, depth_ask, order_bid,
                 order_ask,
                 bid_ratio, ask_ratio, depth_ratio, other=None):
        self.event_time_ms = event_time_ms
        self.run_time_ms = run_time_ms
        self.time_ms = time_ms
        self.strategy_id = strategy_id
        self.inst_code = inst_code
        self.depth_bid = depth_bid
        self.depth_ask = depth_ask
        self.order_bid = order_bid
        self.order_ask = order_ask
        self.bid_ratio = bid_ratio
        self.ask_ratio = ask_ratio
        self.depth_ratio = depth_ratio
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class DepthAttribute(Enum):
    event_time_ms = auto()
    run_time_ms = auto()
    time_ms = auto()
    hour = auto()
    inst_code = auto()
    bid_depth = auto()
    ask_depth = auto()
    bid_volume = auto()
    ask_volume = auto()
    spread = auto()
    tick_pct = auto()
    price_lower = auto()
    price_upper = auto()
    other = auto()


class Depth:
    __slots__ = [attr.name for attr in DepthAttribute]

    def __init__(self, event_time_ms, run_time_ms, time_ms, hour, inst_code, bid_depth, ask_depth, bid_volume,
                 ask_volume, spread,
                 tick_pct, price_lower,
                 price_upper, other=None):
        self.event_time_ms = event_time_ms
        self.run_time_ms = run_time_ms
        self.time_ms = time_ms
        self.hour = hour
        self.inst_code = inst_code
        self.bid_depth = bid_depth
        self.ask_depth = ask_depth
        self.bid_volume = bid_volume
        self.ask_volume = ask_volume
        self.spread = spread
        self.tick_pct = tick_pct
        self.price_lower = price_lower
        self.price_upper = price_upper
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class BudgetAttribute(Enum):
    event_time_ms = auto()
    day = auto()
    budget_num = auto()
    account_id = auto()
    strategy_id = auto()
    exchange_id = auto()
    is_least = auto()
    other = auto()


class Budget:
    __slots__ = [attr.name for attr in BudgetAttribute]

    def __init__(self, event_time_ms, day, budget_num, account_id, strategy_id, exchange_id, is_least, other=None):
        self.event_time_ms = event_time_ms
        self.day = day
        self.budget_num = budget_num
        self.account_id = account_id
        self.strategy_id = strategy_id
        self.exchange_id = exchange_id
        self.is_least = is_least
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class InventoryAttribute(Enum):
    event_time_ms = auto()
    inst_code = auto()
    trade_update_time_ms = auto()
    inventory_quantity = auto()
    cost_price = auto()
    cumulative_profit = auto()
    unrealized_profit = auto()
    market_price = auto()
    price_update_time_ms = auto()
    other = auto()
    cumulative_volume = auto()


class Inventory:
    __slots__ = [attr.name for attr in InventoryAttribute]

    def __init__(self, event_time_ms, inst_code, trade_update_time_ms, inventory_quantity, cost_price,
                 cumulative_profit, unrealized_profit, market_price, price_update_time_ms, cumulative_volume=None,
                 other=None):
        self.event_time_ms = event_time_ms
        self.inst_code = inst_code
        self.trade_update_time_ms = trade_update_time_ms
        self.inventory_quantity = inventory_quantity
        self.cost_price = cost_price
        self.cumulative_profit = cumulative_profit
        self.unrealized_profit = unrealized_profit
        self.market_price = market_price
        self.price_update_time_ms = price_update_time_ms
        self.cumulative_volume = cumulative_volume
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class MaxInventoryQuantityAttribute(Enum):
    event_time_ms = auto()
    inst_code = auto()
    max_inventory_quantity = auto()
    day = auto()
    other = auto()


class MaxInventoryQuantity:
    __slots__ = [attr.name for attr in MaxInventoryQuantityAttribute]

    def __init__(self, event_time_ms, inst_code, max_inventory_quantity, day, other=None):
        self.event_time_ms = event_time_ms
        self.inst_code = inst_code
        self.max_inventory_quantity = max_inventory_quantity
        self.day = day
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class UnrealizedProfitClosePointAttribute(Enum):
    event_time_ms = auto()
    inst_code = auto()
    day = auto()
    take_profit_point = auto()
    take_profit_count = auto()
    stop_loss_point = auto()
    stop_loss_count = auto()
    total_profit = auto()
    other = auto()


class UnrealizedProfitClosePoint:
    __slots__ = [attr.name for attr in UnrealizedProfitClosePointAttribute]

    def __init__(self, event_time_ms, inst_code, day, take_profit_point, take_profit_count, stop_loss_point,
                 stop_loss_count, total_profit, other=None):
        self.event_time_ms = event_time_ms
        self.inst_code = inst_code
        self.day = day
        self.take_profit_point = take_profit_point
        self.take_profit_count = take_profit_count
        self.stop_loss_point = stop_loss_point
        self.stop_loss_count = stop_loss_count
        self.total_profit = total_profit
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class TotalProfitAttribute(Enum):
    inst_code = auto
    data = auto()
    time = auto()
    wake_up_time = auto()


class TotalProfit:
    __slots__ = [attr.name for attr in TotalProfitAttribute]

    def __init__(self, inst_code, data, time, wake_up_time):
        self.inst_code = inst_code
        self.data = data
        self.time = time
        self.wake_up_time = wake_up_time

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class InventoryCloseParamsAttribute(Enum):
    event_time_ms = auto()
    inst_code = auto()
    close_type = auto()
    price = auto()
    quantity = auto()


class InventoryCloseParams:
    __slots__ = [attr.name for attr in InventoryCloseParamsAttribute]

    def __init__(self, event_time_ms, inst_code, close_type, price, quantity):
        self.event_time_ms = event_time_ms
        self.inst_code = inst_code
        self.close_type = close_type
        self.price = price
        self.quantity = quantity

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class InventoryTradingProposalAttribute(Enum):
    trading_proposal = auto()
    wake_up_time = auto()


class InventoryTradingProposal:
    __slots__ = [attr.name for attr in InventoryTradingProposalAttribute]

    def __init__(self, trading_proposal, wake_up_time):
        self.trading_proposal = trading_proposal
        self.wake_up_time = wake_up_time

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class UserLoanAttribute(Enum):
    event_time_ms = auto()
    operation_write_time = auto()
    exchange_id = auto()
    user = auto()
    coin = auto()
    quantity = auto()
    other = auto()


class UserLoan:
    __slots__ = [attr.name for attr in UserLoanAttribute]

    def __init__(self, event_time_ms, operation_write_time, exchange_id, user, coin, quantity, other=None):
        self.event_time_ms = event_time_ms
        self.operation_write_time = operation_write_time
        self.exchange_id = exchange_id
        self.user = user
        self.coin = coin
        self.quantity = quantity
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class MaxInventoryAttribute(Enum):
    event_time_ms = auto()
    coin = auto()
    max_inventory = auto()
    strategy_id = auto()
    exchange_id = auto()
    day = auto()
    other = auto()


class MaxInventory:
    __slots__ = [attr.name for attr in MaxInventoryAttribute]

    def __init__(self, event_time_ms, coin, max_inventory, strategy_id, exchange_id, day, other=None):
        self.event_time_ms = event_time_ms
        self.coin = coin
        self.max_inventory = max_inventory
        self.strategy_id = strategy_id
        self.exchange_id = exchange_id
        self.day = day
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}
        


class ArbitragePoolsReportAttribute(Enum):
    event_time_ms = auto()
    open_time = auto()
    close_time = auto()
    day = auto()
    report = auto() # [{inst_code: amount}] amount是24h成交额 单位是USDT 降序排序
    other = auto()


class ArbitragePoolsReport:
    __slots__ = [attr.name for attr in ArbitragePoolsReportAttribute]

    def __init__(self, event_time_ms, open_time, close_time, day, report, other=None):
        self.event_time_ms = event_time_ms
        self.open_time = open_time  
        self.close_time = close_time
        self.day = day  
        self.report = report
        self.other = other if other is not None else {}

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}
