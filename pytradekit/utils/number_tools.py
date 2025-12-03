from decimal import Decimal, InvalidOperation
import math
import random
import uuid

import numpy as np

from pytradekit.utils.exceptions import DataTypeException


def convert_keep_two_places(figure: float) -> float:
    return round(figure, 2)


def reshape_report_number(number: [int, float, None]) -> str:
    if number in [None, np.nan, 'nan', 'None'] or math.isnan(number):
        return np.nan
    is_positive = 1 if number > 0 else 0
    number = abs(number)
    if number < 1e-6:
        return "0.0"
    if number >= 10_000:
        return format_number(int(number), is_positive)
    elif number >= 10:
        return format_number(round(number, 2), is_positive)
    elif number >= 1:
        return format_number(round(number, 3), is_positive)
    else:
        fraction_part_str = f"{number: .20f}".split('.')[1].rstrip('0')
        first_non_zero_index = next((i for i, ch in enumerate(fraction_part_str) if ch != '0'), None)
        if first_non_zero_index is not None:
            significant_digits = fraction_part_str[first_non_zero_index:first_non_zero_index + 4]
            format_fraction = f"{fraction_part_str[:first_non_zero_index]}{significant_digits}".rstrip('0')
            decimal_separator = "'"
            format_fraction = decimal_separator.join(
                [format_fraction[i:i + 3] for i in range(0, len(format_fraction), 3)])
            return f"0.{format_fraction}" if is_positive else f"-0.{format_fraction}"
        else:
            return f"{number:.4f}".rstrip('0') if is_positive else f"-{number:.4f}".rstrip('0')


def format_big_int(x):
    try:
        return f"{int(x):,}"
    except (ValueError, TypeError):
        return np.nan


def format_number(number, is_positive=1, decimal_separator="'"):
    if '.' in str(number):
        integer_part, decimal_part = str(number).split('.')
        formatted_integer = format_big_int(integer_part)
        formatted_decimal = decimal_separator.join([decimal_part[i:i + 3] for i in range(0, len(decimal_part), 3)])
        return f"{formatted_integer}.{formatted_decimal}" if is_positive else f"-{formatted_integer}.{formatted_decimal}"
    else:
        integer_part = str(number)
        return format_big_int(integer_part) if is_positive else f"-{format_big_int(integer_part)}"


def get_rank_pct(a_str):
    x, y = int(a_str.split('/')[0]), int(a_str.split('/')[1])
    return round((100 * x / y), 1)


def generate_client_order_id(strategy_id):
    ret = f'{str(uuid.uuid4())[2:-len(strategy_id) - 4]}_{strategy_id}'
    return ret


class BotCommonUtils:

    @staticmethod
    def letter_conversion(value: [int, float]) -> str:
        try:
            n = abs(value)
            symbols = ('', 'K', 'M', 'B', 'T', 'Q')
            suffix_idx = math.floor(math.log10(n) / 3)
            suffix_idx = max(min(suffix_idx, len(symbols) - 1), 0)
            return f'{value / 1000 ** suffix_idx:.3f}{symbols[suffix_idx]}'
        except:
            return str(value)

    @staticmethod
    def comma_conversion(value: [int, float]) -> str:
        status = True
        if int(value) < 0:
            value = 0 - int(value)
            status = False
        value = str(int(value))
        value_result = ''
        for i, s in enumerate(value[::-1]):
            value_result = value_result + s
            if (i + 1) % 3 == 0 and len(value) != (i + 1):
                value_result = value_result + ','
        value_result = value_result[::-1]
        if not status:
            value_result = "-" + value_result
        return value_result


def subtract_dict(a_dict, b_dict):
    # 将A中的所有键减去B中的对应值，如果B中没有则减去0
    result = {key: a_dict.get(key, 0) - b_dict.get(key, 0) for key in set(a_dict) | set(b_dict)}
    return result


def add_dict(a_dict, b_dict):
    result = {key: a_dict.get(key, 0) + b_dict.get(key, 0) for key in set(a_dict) | set(b_dict)}
    return result


def convert_to_decimal(x):
    """
    将输入 x 转换为 Decimal 类型。如果 x 已经是 Decimal 则直接返回。

    :param x: 可以是 int、float、str 或 Decimal 类型
    :return: Decimal 类型的值
    """
    try:
        if not isinstance(x, Decimal):
            x = Decimal(str(x))
        return x
    except (InvalidOperation, ValueError) as e:
        raise DataTypeException(f"Value {x} cannot be converted to Decimal") from e


def convert_decimal_to_str(decimal_value):
    """
    将 Decimal 转换为非科学计数法的字符串表示
    :param decimal_value: Decimal 类型的数值
    :return: 字符串形式，没有科学计数法
    """
    if 'E' in str(decimal_value):
        return format(decimal_value, 'f')
    else:
        return str(decimal_value)


def convert_decimal_to_float(decimal_value):
    try:
        return float(convert_decimal_to_str(decimal_value))
    except:
        return decimal_value


def handle_pcs_decimal(min_pcs, value) -> Decimal:
    """
    将输入值调整为 min_pcs 的整数倍。
    支持 min_pcs 和 value 为 Decimal 或 float 类型。

    :param min_pcs: 最小单位，可以是 Decimal 或 float
    :param value: 待调整的值，可以是 Decimal 或 float
    :return: 调整后的 Decimal 值
    """
    min_pcs, value = convert_to_decimal(min_pcs), convert_to_decimal(value)
    adjusted_value = (value // min_pcs) * min_pcs
    return adjusted_value


def get_random_num(origin_num, min_num):
    return origin_num + random.randint(1, 9) * min_num


def check_sign_consistency(num1: float, num2: float) -> bool:
    """
    检查两个 Decimal 数字的符号是否一致。

    :param num1: 第一个 Decimal 数字
    :param num2: 第二个 Decimal 数字
    :return: 如果符号一致返回 True，否则返回 False
    """
    return (num1 >= 0) == (num2 >= 0)
