"""
author: Willem Wang
date: 2024-10-15
description: 存放一些业务中需要的，自定义的数据类型
"""
from decimal import Decimal
from dataclasses import dataclass, field
from typing import Optional, Union
import re
import subprocess

from pytradekit.utils.time_handler import TimeFrame, TimeSpan, convert_date_str_timezone, check_str_format, \
    convert_timestamp_to_str
from pytradekit.utils.dynamic_types import ExchangeId, StrategyId, OrderSide, OrderType, InstCodeType
from pytradekit.utils.exceptions import DataTypeException
from pytradekit.utils.number_tools import get_random_num, handle_pcs_decimal, generate_client_order_id


@dataclass
class InstCode:
    symbol: str
    exchange_id: ExchangeId
    category: str

    def __str__(self) -> str:
        return f"{self.symbol}_{self.exchange_id}.{self.category}"

    @staticmethod
    def from_string(inst_code: str):
        pattern = r"([A-Za-z0-9]+)_([A-Za-z0-9]+)\.([A-Za-z0-9]+)"
        match = re.match(pattern, inst_code)
        if match:
            symbol, exchange_id, category = match.groups()
            return InstCode(symbol, exchange_id, category)
        else:
            raise DataTypeException(f"Invalid inst code format: {inst_code}")

    def get_exchange_type_suffix(self) -> str:
        return f"_{self.exchange_id}.{self.category}"

    def get_report_symbol(self) -> str:
        if self.category == InstCodeType.SPOT.name:
            return self.symbol
        else:
            return f"{self.symbol}_{self.category}"


@dataclass
class Pair:
    base: Optional[str] = field(default=None)
    quote: Optional[str] = field(default=None)

    def __str__(self) -> str:
        if self.base and self.quote:
            return f"{self.base}_{self.quote}"
        elif self.base:
            return f"{self.base}_None"
        elif self.quote:
            return f"None_{self.quote}"
        else:
            return "None_None"

    @staticmethod
    def from_string(pair_str: str):
        """
        从字符串解析 Pair 对象，格式为 <base>_<quote>
        """
        pattern = r"([A-Za-z0-9]+)?_([A-Za-z0-9]+)?"
        match = re.match(pattern, pair_str)
        if match:
            base, quote = match.groups()
            return Pair(base, quote)
        else:
            raise DataTypeException(f"Invalid pair format: {pair_str}")


@dataclass
class KlineFrame:
    number: int
    unit: str

    def __post_init__(self):
        valid_units = [TimeFrame.minute.value, TimeFrame.hour.value, TimeFrame.day.value]
        if self.unit not in valid_units:
            raise ValueError(f"Invalid unit '{self.unit}'. Unit must be one of {valid_units}.")

    def __str__(self) -> str:
        return f"{self.number}{self.unit}"

    @staticmethod
    def from_string(interval_str: str):
        valid_units_pattern = '|'.join([TimeFrame.minute.value, TimeFrame.hour.value, TimeFrame.day.value])
        regex_pattern = rf"(\d+)({valid_units_pattern})"
        match = re.match(regex_pattern, interval_str)
        if match:
            number, unit = match.groups()
            return KlineFrame(int(number), unit)
        else:
            raise ValueError(f"Invalid frequency format: {interval_str}")


@dataclass
class ReportTitle:
    exchange_id: str
    report_name: str
    a_time: (TimeSpan, str, list)

    @staticmethod
    def get_git_branch():
        try:
            branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"]).decode().strip()
            if branch == 'main':
                branch = ''
            else:
                branch = f'{branch[0]}'
            return branch
        except subprocess.CalledProcessError:
            return "unknown branch"

    def __str__(self) -> str:
        exchange = ExchangeId[self.exchange_id].value
        git_branch = self.get_git_branch()
        if isinstance(self.a_time, TimeSpan):
            start = convert_date_str_timezone(self.a_time.start.split('.')[0])
            end = convert_date_str_timezone(self.a_time.end.split('.')[0])
            time_str = f'{start}---{end}'
        elif isinstance(self.a_time, list):
            time_str = f'{str(self.a_time[0])}---{str(self.a_time[-1])}'
        elif check_str_format(self.a_time):
            time_str = convert_date_str_timezone(self.a_time.split('.')[0])
        else:
            time_str = self.a_time
        return f'{exchange.title()} {self.report_name}  {time_str} {git_branch}'


@dataclass
class ClientOrderId:
    client_order_id: str
    strategy_id: str

    def __str__(self) -> str:
        """创建一个复合字符串，格式为 client_order_id_order_trigger_strategy_id."""
        return f"{self.client_order_id}_{self.strategy_id}"

    @staticmethod
    def from_string(strategy_order_id: str):
        parts = strategy_order_id.split('_')
        if len(parts) != 3:
            raise ValueError("Invalid compound string format!")
        client_order_id, strategy_id_a, strategy_id_b = parts
        strategy_id = f'{strategy_id_a}_{strategy_id_b}'
        # 检查strategy_id是否符合StrategyId中定义的值
        if strategy_id not in StrategyId.__dict__.values():
            raise ValueError(f"Invalid strategy_id: {strategy_id}")
        return ClientOrderId(client_order_id, strategy_id)


@dataclass
class TradingOrder:
    """
    用于trading engine里下单和发送log使用
    """
    inst_code: InstCode
    type: Union[str, OrderType]
    side: Union[str, OrderSide]
    raw_price: Decimal
    tick_price: Decimal
    raw_volume: Decimal
    tick_volume: Decimal
    strategy_id: str
    order_id: [str, None] = None
    error: [str, None] = None
    request_timestamp: Optional[int] = None  # 发送请求的时间 (十三位时间戳)
    working_timestamp: Optional[int] = None  # 请求生效的时间 (十三位时间戳)

    def __post_init__(self):
        if not isinstance(self.inst_code, InstCode):
            raise TypeError(f"inst_code should be of type InstCode, got {type(self.inst_code).__name__}")
        if isinstance(self.side, str):
            if self.side not in [side.value for side in OrderSide]:
                raise ValueError(f"Invalid side: {self.side}. Must be one of {[side.value for side in OrderSide]}")

        default_init_price = -1

        random_num = get_random_num(self.raw_volume, self.tick_volume)
        self.volume = handle_pcs_decimal(self.tick_volume, random_num)
        self.price = handle_pcs_decimal(self.tick_price,
                                        self.raw_price) if self.type != OrderType.market.value else default_init_price
        self.client_order_id = generate_client_order_id(self.strategy_id)

    @property
    def delay(self):
        if self.request_timestamp and self.working_timestamp:
            return self.working_timestamp - self.request_timestamp
        return None

    def __str__(self):
        request_time_str = f"request:{convert_timestamp_to_str(self.request_timestamp)} " if self.request_timestamp else ""
        delay_str = f"delay:{self.delay}ms" if self.delay is not None else ""
        error_str = f" {self.error}" if self.error else ""
        msg = f'{str(self.inst_code)}, side:{self.side}, price:{self.price}, volume:{self.volume} {request_time_str} {delay_str} {error_str}'
        return msg


@dataclass
class CancelOrder:
    """
    用于trading engine里撤单
    """
    inst_code: InstCode
    client_order_id: ClientOrderId
    side: Union[str, OrderSide]
    price: [Decimal, None]
    volume: [Decimal, None]
    error: [str, None] = None
    request_timestamp: Optional[int] = None
    working_timestamp: Optional[int] = None

    @property
    def delay(self):
        if self.request_timestamp and self.working_timestamp:
            return self.working_timestamp - self.request_timestamp
        return None

    def __str__(self):
        request_time_str = f"request:{convert_timestamp_to_str(self.request_timestamp)} " if self.request_timestamp else ""
        price_str = f'price:{self.price}' if self.price else ''
        volume_str = f'volume:{self.volume}' if self.volume else ''
        side_str = f'side:{self.side}' if self.side else ''
        delay_str = f"delay:{self.delay}ms" if self.delay is not None else ""
        error_str = f" {self.error}" if self.error else ""
        msg = f'Cancel order, {str(self.inst_code)}, {side_str}, {price_str}, {volume_str} {request_time_str} {delay_str} {error_str}'
        return msg


@dataclass
class AccountId:
    exchange_id: ExchangeId
    id: str

    def __str__(self) -> str:
        return f"{self.exchange_id}_{self.id}"

    @staticmethod
    def from_string(account_id: str):
        pattern = r"([A-Za-z0-9]+)?_([A-Za-z0-9]+)?"
        match = re.match(pattern, account_id)
        if match:
            exchange_id, ids = match.groups()
            return AccountId(exchange_id, ids)
        else:
            raise DataTypeException(f"Invalid account id format: {account_id}")
