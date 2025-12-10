"""
author: Willem Wang
date: 2024-10-15
description: 存每个交易所的需要做市的交易对，以及获取这些交易对的方法。获取做市目标的做法。
"""
from pandas import DataFrame
import pandas as pd
from pytradekit.utils.dynamic_types import MmTarget, ExchangeId, InstCodeType
from pytradekit.utils.static_types import InstcodeBasicAttribute
from pytradekit.utils.exceptions import DataTypeException
from pytradekit.utils.custom_types import Pair

VENDOR_EXCHANGE = [ExchangeId.MCO.name, ExchangeId.EMO.name, ExchangeId.BUL.name, ExchangeId.BCI.name,
                   ExchangeId.PNX.name, ExchangeId.HKG.name]


class MmInstCode:
    STABLE_COINS = ['USDT', 'TUSD', 'USDC', ' USDD']
    BN = ['SOL-FDUSD_BN.SPOT']  # Updated to pair format (BASE-QUOTE)
    KC = []
    MCO = []
    BMT = []
    MXC = []
    BFX = []
    BGT = []
    BBT = []
    GT = []
    HTX = []
    OKX = []
    EMO = []
    WOO = []
    BUL = []
    BCI = []
    HKG = []
    PNX = []
    KRK = []
    LBK = []

    @classmethod
    def get_exchange_ids(cls):
        return [key for key, value in cls.__dict__.items()
                if isinstance(value, list) and key not in ["TRON_COINS", "BCI", "STABLE_COINS"]]

    @classmethod
    def get_trade_exchange_ids(cls):
        return [key for key, value in cls.__dict__.items()
                if isinstance(value, list) and key not in ["TRON_COINS", "BCI", "BUL", "MCO", "PNX", "STABLE_COINS"]]

    @classmethod
    def get_order_depth_exchange_id(cls):
        return ["HTX", "BN", "BFX"]

    @classmethod
    def get_raw_order_exchange_ids(cls):
        return ["BFX"]

    @classmethod
    def get_compare_order_trade_exchange_ids(cls):
        return ['BN', 'HTX', 'OKX', 'BBT']


class DepthThreshold:
    orderbook_pct = 3
    depth_pct = 2
    normal = 5_000
    normal_usdt = 20_000
    binance = 60_000
    binance_usdt = 100_000
    woox = 10_000
    okex = 100_000
    bybit_usdd = 1_000_000


class RankPctThreshold:
    normal = 75
    binance_usdt = 80
    binance_no_usdt = 90


class Volume24hThreshold:
    binance_fdusd = 1_000_000


class SpreadThreshold:
    normal = 1
    tick_pct = 0.5
    big_tick = 2


def get_mm_target(exchange_id, quote=None) -> dict:
    target_dict = {MmTarget.spread.name: SpreadThreshold.normal,
                   MmTarget.average_depth.name: -1,
                   MmTarget.side_depth.name: -1,
                   MmTarget.exchange_rank.name: -1,
                   MmTarget.quote_rank.name: -1,
                   MmTarget.volume_24h.name: -1}

    if exchange_id == ExchangeId.BN.name:
        if quote == 'USDT':
            target_dict[MmTarget.side_depth.name] = DepthThreshold.binance_usdt
            target_dict[MmTarget.quote_rank.name] = RankPctThreshold.binance_usdt
        else:
            target_dict[MmTarget.average_depth.name] = DepthThreshold.binance
            target_dict[MmTarget.exchange_rank.name] = RankPctThreshold.binance_no_usdt
            if quote == 'FDUSD':
                target_dict[MmTarget.volume_24h.name] = Volume24hThreshold.binance_fdusd
            
    else:
        target_dict[MmTarget.exchange_rank.name] = RankPctThreshold.normal
        if exchange_id == ExchangeId.OKX.name:
            target_dict[MmTarget.average_depth.name] = DepthThreshold.okex
        elif exchange_id == ExchangeId.WOO.name:
            target_dict[MmTarget.average_depth.name] = DepthThreshold.woox
        elif exchange_id == ExchangeId.EMO.name:
            target_dict[MmTarget.average_depth.name] = DepthThreshold.normal
        elif quote == 'USDT' or exchange_id == ExchangeId.BFX.name:
            target_dict[MmTarget.average_depth.name] = DepthThreshold.normal_usdt
        else:
            target_dict[MmTarget.average_depth.name] = DepthThreshold.normal
    return target_dict


def get_pair_key_mm_target(exchange_id) -> dict:
    result = {str(Pair()): get_mm_target(exchange_id, quote=None)}

    if exchange_id == ExchangeId.BN.name:
        result.update({
            str(Pair(quote='USDT')): get_mm_target(exchange_id, quote='USDT'),
            str(Pair(quote='TUSD')): get_mm_target(exchange_id, quote='TUSD')
        })
    elif exchange_id not in [ExchangeId.OKX.name, ExchangeId.BFX.name]:
        result.update({
            str(Pair(quote='USDT')): get_mm_target(exchange_id, quote='USDT')
        })

    return result


def fetch_mm_inst_code(exchange_id) -> list:
    try:
        return MmInstCode.__dict__[exchange_id]
    except KeyError as e:
        raise DataTypeException(f"Exchange ID '{exchange_id}' not found in MmInstCode.") from e


def fetch_mm_pairs(mongo, exchange_id) -> list:
    inst_code_list = fetch_mm_inst_code(exchange_id)
    mm_pairs = mongo.read_pairs(inst_code_list=inst_code_list, exchange_id=exchange_id)
    return mm_pairs


def fetch_mm_symbols(exchange_id) -> list:
    inst_code_list = fetch_mm_inst_code(exchange_id)
    mm_symbols = [convert_inst_code_to_symbol(i) for i in inst_code_list]
    return mm_symbols


def get_related_inst_code(exchange_id: str, inst_code_basic: DataFrame) -> DataFrame:
    mm_inst_code = fetch_mm_inst_code(exchange_id)
    mm_df = inst_code_basic[inst_code_basic[InstcodeBasicAttribute.inst_code.name].isin(mm_inst_code)].copy()
    if mm_df.empty:
        return DataFrame(columns=inst_code_basic.columns)
    all_tokens = set(mm_df[InstcodeBasicAttribute.base.name].tolist() +
                     mm_df[InstcodeBasicAttribute.quote.name].tolist())
    related_df = inst_code_basic[
        (inst_code_basic[InstcodeBasicAttribute.base.name].isin(all_tokens) &
         inst_code_basic[InstcodeBasicAttribute.quote.name].isin(all_tokens))
    ]
    result = pd.concat([mm_df, related_df]).drop_duplicates(subset=InstcodeBasicAttribute.inst_code.name)
    return result


def convert_pair_to_inst_code(pair: str, exchange_id=ExchangeId.BN.name, types=InstCodeType.SPOT.name) -> str:
    """
    Convert pair to inst_code.
    
    Args:
        pair: Trading pair in format 'BTC-USDT' (hyphen-separated, uppercase)
        exchange_id: Exchange ID (e.g., 'BN')
        types: Instrument type (e.g., 'SPOT')
    
    Returns:
        inst_code: Instrument code in format 'BTC-USDT_BN.SPOT'
    
    Example:
        'BTC-USDT' -> 'BTC-USDT_BN.SPOT'
    """
    pair_upper = pair.upper().replace('_', '-')
    inst_code = f'{pair_upper}_{exchange_id}.{types}'
    return inst_code


def convert_coin_to_inst_code(coin: str, exchange_id=ExchangeId.BN.name, types=InstCodeType.SPOT.name) -> str:
    """
    Convert coin to inst_code (assumes USDT as quote).
    
    Args:
        coin: Base currency (e.g., 'BTC')
        exchange_id: Exchange ID (e.g., 'BN')
        types: Instrument type (e.g., 'SPOT')
    
    Returns:
        inst_code: Instrument code in format 'BTC-USDT_BN.SPOT'
    
    Example:
        'BTC' -> 'BTC-USDT_BN.SPOT'
    """
    pair = f'{coin.upper()}-USDT'
    inst_code = f'{pair}_{exchange_id}.{types}'
    return inst_code

def convert_symbol_to_inst_code(symbol: str, exchange_id=ExchangeId.BN.name, types=InstCodeType.SPOT.name) -> str:
    """
    Convert symbol to inst_code.
    
    Args:
        symbol: Trading symbol in format 'BTCUSDT' (no separator, uppercase)
        exchange_id: Exchange ID (e.g., 'BN')
        types: Instrument type (e.g., 'SPOT')
    
    Returns:
        inst_code: Instrument code in format 'BTC-USDT_BN.SPOT'
    
    Example:
        'BTCUSDT' -> 'BTC-USDT_BN.SPOT'
    """
    pair = convert_symbol_to_pair(symbol)
    inst_code = f'{pair}_{exchange_id}.{types}'
    return inst_code


def convert_base_quote_to_inst_code(base: str, quote: str, exchange_id=ExchangeId.BN.name, types=InstCodeType.SPOT.name) -> str:
    """
    Convert base and quote to inst_code.
    
    Args:
        base: Base currency (e.g., 'BTC')
        quote: Quote currency (e.g., 'USDT')
        exchange_id: Exchange ID (e.g., 'BN')
        types: Instrument type (e.g., 'SPOT')
    
    Returns:
        inst_code: Instrument code in format 'BTC-USDT_BN.SPOT'
    
    Example:
        'BTC', 'USDT' -> 'BTC-USDT_BN.SPOT'
    """
    return base.upper() + "-" + quote.upper() + "_" + exchange_id + "." + types


def convert_inst_code_to_pair(inst_code: str) -> str:
    """
    Convert inst_code to pair.
    
    Args:
        inst_code: Instrument code in format 'BTC-USDT_BN.SPOT'
    
    Returns:
        pair: Trading pair in format 'BTC-USDT' (hyphen-separated, uppercase)
    
    Example:
        'BTC-USDT_BN.SPOT' -> 'BTC-USDT'
    """
    pair = inst_code.split('_')[0].upper()
    return pair


def convert_inst_code_to_symbol(inst_code: str) -> str:
    """
    Convert inst_code to symbol.
    
    Args:
        inst_code: Instrument code in format 'BTC-USDT_BN.SPOT'
    
    Returns:
        symbol: Trading symbol in format 'BTCUSDT' (no separator, uppercase)
    
    Example:
        'BTC-USDT_BN.SPOT' -> 'BTCUSDT'
    """
    pair = inst_code.split('_')[0]
    symbol = pair.replace('-', '').upper()
    return symbol


def convert_pair_to_symbol(pair: str) -> str:
    """
    Convert pair to symbol.
    
    Args:
        pair: Trading pair in format 'BTC-USDT' (hyphen-separated, uppercase)
    
    Returns:
        symbol: Trading symbol in format 'BTCUSDT' (no separator, uppercase)
    
    Example:
        'BTC-USDT' -> 'BTCUSDT'
    """
    symbol = pair.replace('-', '').replace('_', '').upper()
    return symbol


def convert_symbol_to_pair(symbol: str) -> str:
    """
    Convert symbol to pair.
    
    Args:
        symbol: Trading symbol in format 'BTCUSDT' (no separator, uppercase)
    
    Returns:
        pair: Trading pair in format 'BTC-USDT' (hyphen-separated, uppercase)
    
    Example:
        'BTCUSDT' -> 'BTC-USDT'
    """
    symbol = symbol.upper().replace("-", "")

    # Common quote currencies (ordered by length to match longest first)
    quote_currencies = ["USDT", "USDC", "BUSD", "USD", "BTC", "ETH"]

    for quote in quote_currencies:
        if symbol.endswith(quote):
            base = symbol[: -len(quote)]
            if base:
                return f"{base}-{quote}"

    # If no known quote currency found, return as-is (for edge cases)
    return symbol


def extract_base_from_inst_code(inst_code: str) -> str:
    """
    Extract base currency from inst_code.
    
    Args:
        inst_code: Instrument code in format 'BTC-USDT_BN.PERP'
    
    Returns:
        str: Base currency, e.g., 'BTC'
    
    Example:
        'BTC-USDT_BN.PERP' -> 'BTC'
    """
    pair = convert_inst_code_to_pair(inst_code)
    if "-" in pair:
        return pair.split("-")[0]
    return pair
