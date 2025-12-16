import asyncio
import base64
import functools
import io
import os
import re
import time
import subprocess
import zipfile
import importlib
from urllib.parse import quote_plus

from cryptography.fernet import Fernet
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import pandas as pd

from pytradekit.utils.dynamic_types import OrderStatus, ExchangeId, InstCodeType, Env, RunningMode, TradeAttribute, \
    SlackUser
from pytradekit.utils.custom_types import InstCode, KlineFrame, ReportTitle
from pytradekit.utils.static_types import InstcodeBasicAttribute
from pytradekit.utils.time_handler import get_now_time, DATETIME_FORMAT_DAY, DATETIME_FORMAT_HMS
from pytradekit.utils.mongodb_operations import MongodbOperations
from pytradekit.utils.redis_operations import RedisOperations
from pytradekit.utils.exceptions import DependencyException, ExchangeException


RETRY_TIMES = 3
RETRY_INTERVAL = 2
REPORT_FILE_PATH = '/home/report_data_csv/'


def optional_import(name):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None

line_profiler = optional_import("line_profiler")
memory_profiler = optional_import("memory_profiler")


# Import conversion functions from inst_code_usage to avoid duplication
from pytradekit.trading_setup.inst_code_usage import (
    convert_pair_to_inst_code,
    convert_coin_to_inst_code,
    convert_base_quote_to_inst_code,
    convert_inst_code_to_pair,
    convert_pair_to_symbol,
    convert_symbol_to_pair,
    convert_symbol_to_inst_code,
    convert_inst_code_to_symbol,
)


def synchronized(func):
    def guarded(self, *args, **kwargs):
        self._semaphore.acquire()
        try:
            return func(self, *args, **kwargs)
        finally:
            self._semaphore.release()

    return guarded


def retry(try_times=RETRY_TIMES, interval=RETRY_INTERVAL):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempts_left = try_times
            initial_attempts = attempts_left
            last_exception = None
            retry_logs = []
            logger = None

            for arg in args:
                if hasattr(arg, 'info') and callable(arg.info):
                    logger = arg
                    break
            if not logger:
                for value in kwargs.values():
                    if hasattr(value, 'info') and callable(value.info):
                        logger = value
                        break

            while attempts_left:
                try:
                    result = func(*args, **kwargs)
                    if attempts_left < initial_attempts:
                        retry_logs.append(f'Success after {initial_attempts - attempts_left} retry attempts')
                    if logger and retry_logs:
                        logger.info("\n".join(retry_logs))
                    return result
                except Exception as e:
                    last_exception = e
                    attempts_left -= 1
                    retry_logs.append(
                        f'Attempt failed ({initial_attempts - attempts_left}/{initial_attempts}), '
                        f'retrying in {interval} seconds. Error: {str(e)}'
                    )

                    if attempts_left == 0:
                        retry_logs.append(
                            f'All retry attempts failed. Last error: {str(last_exception)} {AtUser.debug}')
                        if logger:
                            logger.info("\n".join(retry_logs))
                        raise ExchangeException('try times exceeded') from last_exception

                    time.sleep(interval)

        return wrapper

    return decorator


def async_retry_decorator(max_retries, wait_time):
    def outer_wrapper(func):
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            for i in range(max_retries):
                try:
                    return await func(self, *args, **kwargs)
                except Exception as e:
                    self.logger.debug(f"Failed attempt {i + 1}: {e}")
                    await asyncio.sleep(wait_time)
            return await func(self, *args, **kwargs)

        return wrapper

    return outer_wrapper


def convert_class_to_dict(obj) -> dict:
    return obj.__dict__


def find_project_root(start_directory):
    """
    从给定的开始目录回溯查找，直到找到含有.git目录的目录作为项目的根目录。
    :param start_directory: 开始搜索的目录，通常是文件的__file__属性。
    :return: 项目根目录的路径。
    """
    current_directory = os.path.abspath(start_directory)
    while current_directory != os.path.sep:
        if os.path.isdir(os.path.join(current_directory, 'cfg')):
            return current_directory
        current_directory = os.path.dirname(current_directory)
    raise FileNotFoundError("未能找到含有/cfg的项目根目录")


def add_time_suffix(file_name: str) -> str:
    datetime_str = get_now_time("%Y%m%d_%H%M%S")
    return f'{file_name}_{datetime_str}'


def get_bn_status_conversion(status):
    ret = status
    if status == 'NEW':
        ret = OrderStatus.active.value
    elif status == 'FILLED':
        ret = OrderStatus.fully_filled.value
    elif status == 'EXPIRED':
        ret = OrderStatus.expired.value
    elif status == 'PARTIALLY_FILLED':
        ret = OrderStatus.partial_filled.value
    elif status == 'CANCELED':
        ret = OrderStatus.cancelled.value
    return ret


def get_bn_status_conversion_fix(status):
    ret = status
    if status == 0:
        ret = OrderStatus.active.value
    elif status == 2:
        ret = OrderStatus.fully_filled.value
    elif status == 'C':
        ret = OrderStatus.expired.value
    elif status == 1:
        ret = OrderStatus.partial_filled.value
    elif status in [4, 6]:
        ret = OrderStatus.cancelled.value
    return ret


def get_bfx_status_conversion(status):
    ret = status
    if status == 'ACTIVE':
        ret = OrderStatus.active.value
    elif 'EXECUTED' in status:
        ret = OrderStatus.fully_filled.value
    elif 'RSN_SELFMATCH' in status:
        ret = OrderStatus.expired.value
    elif 'PARTIALLY FILLED' in status:
        ret = OrderStatus.partial_filled.value
    elif status == 'CANCELED':
        ret = OrderStatus.cancelled.value
    return ret


def get_bbt_status_conversion(status):
    ret = status
    if status == 'new':
        ret = OrderStatus.active.value
    elif status == 'Filled':
        ret = OrderStatus.fully_filled.value
    elif status == 'PartiallyFilled':
        ret = OrderStatus.partial_filled.value
    elif status in ['Cancelled', 'PartiallyFilledCancelled']:
        ret = OrderStatus.cancelled.value
    return ret


def get_kc_status_conversion(status):
    if status:
        ret = OrderStatus.active.value
    else:
        ret = OrderStatus.cancelled.value
    return ret


def get_gt_status_conversion(status):
    ret = status
    if status == 'open':
        ret = OrderStatus.active.value
    elif status == 'FILLED':
        ret = OrderStatus.fully_filled.value
    elif status == 'cancelled':
        ret = OrderStatus.cancelled.value
    return ret


def get_hkg_status_conversion(status):
    ret = status
    if status == 'NEW':
        ret = OrderStatus.active.value
    elif status == 'PARTIALLY_FILLED':
        ret = OrderStatus.partial_filled.value
    elif status == 'FILLED':
        ret = OrderStatus.fully_filled.value
    elif status in ['CANCELED', 'PARTIALLY_CANCELED']:
        ret = OrderStatus.cancelled.value
    return ret


def get_mxc_status_conversion(status):
    ret = status
    if status == 'NEW':
        ret = OrderStatus.active.value
    elif status == 'FILLED':
        ret = OrderStatus.fully_filled.value
    elif status in ['PARTIALLY_FILLED', 'PARTIALLY_CANCELED']:
        ret = OrderStatus.partial_filled.value
    elif status == 'CANCELED':
        ret = OrderStatus.cancelled.value
    return ret


def get_bgt_status_conversion(status):
    ret = status
    if status == 'filled':
        ret = OrderStatus.fully_filled.value
    elif status == 'partially_filled':
        ret = OrderStatus.partial_filled.value
    elif status == 'canceled':
        ret = OrderStatus.cancelled.value
    return ret


def get_okx_status_conversion(status):
    ret = status
    if status == 'filled':
        ret = OrderStatus.fully_filled.value
    elif status == 'canceled':
        ret = OrderStatus.cancelled.value
    return ret


def get_htx_status_conversion(status):
    ret = status
    if status == 'submitted':
        ret = OrderStatus.active.value
    elif status == 'filled':
        ret = OrderStatus.fully_filled.value
    elif status in ['partial-canceled', 'partial-filled']:
        ret = OrderStatus.partial_filled.value
    elif status == 'canceled':
        ret = OrderStatus.cancelled.value
    return ret


def get_bmt_status_conversion(status):
    ret = status
    if status == 'filled':
        ret = OrderStatus.fully_filled.value
    elif status == 'partially_canceled':
        ret = OrderStatus.partial_filled.value
    elif status == 'canceled':
        ret = OrderStatus.cancelled.value
    return ret


def get_htx_ws_status_conversion(status):
    ret = status
    if status == 'submitted':
        ret = OrderStatus.active.value
    elif status in ['partial-filled', 'partial-canceled']:
        ret = OrderStatus.partial_filled.value
    elif status == 'filled':
        ret = OrderStatus.fully_filled.value
    elif status == 'canceled':
        ret = OrderStatus.cancelled.value
    return ret


def get_okx_ws_status_conversion(status):
    ret = status
    if status == 'live':
        ret = OrderStatus.active.value
    elif status == 'partially_filled':
        ret = OrderStatus.partial_filled.value
    elif status == 'filled':
        ret = OrderStatus.fully_filled.value
    elif status in ['canceled', 'mmp_canceled']:
        ret = OrderStatus.cancelled.value
    return ret


def get_bbt_ws_status_conversion(status):
    ret = status
    if status == 'new':
        ret = OrderStatus.active.value
    elif status == 'Filled':
        ret = OrderStatus.fully_filled.value
    elif status == 'PartiallyFilled':
        ret = OrderStatus.partial_filled.value
    elif status in ['Cancelled', 'PartiallyFilledCancelled']:
        ret = OrderStatus.cancelled.value
    return ret


def get_mco_ws_status_conversion(status):
    ret = status
    if status == 'created':
        ret = OrderStatus.active.value
    elif status == 'filled':
        ret = OrderStatus.fully_filled.value
    elif status == 'working':
        ret = OrderStatus.partial_filled.value
    elif status == 'cancelled':
        ret = OrderStatus.cancelled.value
    return ret


def get_woo_ws_status_conversion(status):
    ret = status
    if status == 'NEW':
        ret = OrderStatus.active.value
    elif status == 'FILLED':
        ret = OrderStatus.fully_filled.value
    elif status == 'PARTIAL_FILLED':
        ret = OrderStatus.partial_filled.value
    elif status == 'CANCELLED':
        ret = OrderStatus.cancelled.value
    return ret


def get_krk_status_conversion(status):
    ret = status
    if status == 'open':
        ret = OrderStatus.active.value
    elif status == 'closed':
        ret = OrderStatus.fully_filled.value
    elif status == 'canceled':
        ret = OrderStatus.cancelled.value
    return ret


def split_inst_code_kline_frame(inst_code_interval_kline_frame: str):
    parts = inst_code_interval_kline_frame.rsplit('_', 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid format: {inst_code_interval_kline_frame}")

    inst_code_str, interval_str = parts
    try:
        inst_code = InstCode.from_string(inst_code_str)
        kline_frame = KlineFrame.from_string(interval_str)
        return inst_code, kline_frame
    except ValueError as e:
        print(f"Error parsing string: {e}")
        return e


def find_run_file_name(file_path):
    return os.path.basename(file_path).split(".")[0]


def generate_key(password: str) -> bytes:
    """
    从给定的密码生成Fernet密钥。
    """
    # 使用静态盐是不安全的，请在实际应用中生成随机盐
    salt = b'\x00' * 16
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
        backend=default_backend()
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def encrypt_decrypt(data: str, operation: str, key: str = 'trading_system') -> str:
    """
    根据操作加密或解密数据。

    参数:
    data: 要加密或解密的字符串。
    operation: 'encrypt' 或 'decrypt' 指定操作类型。
    key: 加密和解密的密钥
    """
    fernet_key = generate_key(key)
    f = Fernet(fernet_key)

    if operation == 'encrypt':
        # 加密数据并返回
        return f.encrypt(data.encode()).decode()
    elif operation == 'decrypt':
        # 解密数据并返回
        return f.decrypt(data.encode()).decode()
    else:
        raise ValueError("Operation must be 'encrypt' or 'decrypt'.")


def unzip_to_df(zip_file_path: str):
    try:
        with zipfile.ZipFile(zip_file_path, 'r') as zipf:
            csv_filename = zipf.namelist()[0]
            with zipf.open(csv_filename) as csvfile:
                df = pd.read_csv(csvfile)
        return df
    except Exception as e:
        print(e)
        raise DependencyException(f'Cannot unzip to dataframe: {zip_file_path}') from e


def get_mongo(config, logger, running_mode):
    def get_mongodb_url(config, running_mode=RunningMode.testing_flag.name):
        mongodb_url_key = Env.MONGODB_URL.name
        if mongodb_url_key in config.private and config.private[mongodb_url_key]:
            return encrypt_decrypt(config.private[mongodb_url_key], 'decrypt')
        
        # Build MongoDB URL from environment variables if MONGODB_URL is not set
        mongo_host = config.private.get(Env.MONGO_HOST.name ) 
        mongo_port = config.private.get(Env.MONGO_PORT.name)
        mongo_username = config.private.get(Env.MONGO_USERNAME.name)
        mongo_password = config.private.get(Env.MONGO_PASSWORD.name)
        
        if logger:
            logger.debug(f"MongoDB config: host={mongo_host}, port={mongo_port}, username={mongo_username}, password={'***' if mongo_password else None}")
        
        if mongo_username and mongo_password:
            username = quote_plus(mongo_username)
            password = quote_plus(mongo_password)
            # 添加 authSource=admin 用于 root 用户认证
            mongodb_url = f"mongodb://{username}:{password}@{mongo_host}:{mongo_port}/?authSource=admin"
            if logger:
                logger.debug(f"Built MongoDB URL: mongodb://{username}:***@{mongo_host}:{mongo_port}/?authSource=admin")
            return mongodb_url
        else:
            mongodb_url = f"mongodb://{mongo_host}:{mongo_port}/"
            if logger:
                logger.debug(f"Built MongoDB URL without auth: {mongodb_url}")
            return mongodb_url

    mongodb_url = get_mongodb_url(config, running_mode=running_mode)
    mongo = MongodbOperations(mongodb_url, logger=logger)
    return mongo


def get_redis(logger, config, running_mode):
    def get_redis_url(config, running_mode=RunningMode.testing_flag.name):
        redis_url_key = Env.REDIS_URL.name
        if redis_url_key in config.private and config.private[redis_url_key]:
            return encrypt_decrypt(config.private[redis_url_key], 'decrypt')
        
        # Build Redis URL from environment variables if REDIS_URL is not set
        redis_host = config.private.get(Env.REDIS_HOST.name) 
        redis_port = config.private.get(Env.REDIS_PORT.name)
        redis_username = config.private.get(Env.REDIS_USERNAME.name)
        redis_password = config.private.get(Env.REDIS_PASSWORD.name)
        
        if redis_username and redis_password:
            username = quote_plus(redis_username)
            password = quote_plus(redis_password)
            return f"redis://{username}:{password}@{redis_host}:{redis_port}/"
        elif redis_password:
            password = quote_plus(redis_password)
            return f"redis://:{password}@{redis_host}:{redis_port}/"
        else:
            return f"redis://{redis_host}:{redis_port}/"
    
    redis_url = get_redis_url(config, running_mode=running_mode)
    return RedisOperations(logger, redis_url)


def get_coin_price(logger, coin, exchange_ticker_price, exchange_id):
    if coin in ['USDT', 'USD']:
        return 1.
    tickers = [f'{coin}USDT', f'{coin}USD', f'USDT{coin}', f'USD{coin}']
    for ticker in tickers:
        if ticker in exchange_ticker_price:
            price = exchange_ticker_price[ticker]
            price_float = float(price)
            if price_float == 0:
                logger.debug(f'{coin} price is 0 in {exchange_id} ticker_price')
                return None
            return price_float if ticker.startswith(coin) else 1 / price_float
    try:
        if exchange_id == ExchangeId.MCO.name:
            if coin in ['MBRL']:
                return None
            return float(exchange_ticker_price[f'{coin}BRL']) / float(exchange_ticker_price['USDTBRL'])
        elif exchange_id == ExchangeId.BMT.name:
            return float(exchange_ticker_price[f'{coin}USDC']) * float(exchange_ticker_price['USDCUSDT'])
        return float(exchange_ticker_price['BTCUSDT']) / float(exchange_ticker_price[f'BTC{coin}'])
    except KeyError:
        logger.debug(f'{coin} can not get price in {exchange_id} ticker_price')
        return None


def get_symbol_quote_dict(inst_code_basic) -> dict:
    return {i[InstcodeBasicAttribute.symbol.name]: i[InstcodeBasicAttribute.quote.name] for i in inst_code_basic}


def get_pairs_tick_size_price_dict(inst_code_basic) -> dict:
    return {i[InstcodeBasicAttribute.pair.name]: i[InstcodeBasicAttribute.tick_price.name] for i in inst_code_basic}


def get_title(exchange_id, report_name, a_time):
    title = ReportTitle(exchange_id=exchange_id,
                        report_name=report_name,
                        a_time=a_time)
    return str(title)


def get_git_branch():
    try:
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"]).decode().strip()
        return branch
    except subprocess.CalledProcessError:
        return "unknown branch"


def zip_df(logger, logs_list, df, path, collection_path, time_span=None, part_num=0):
    day_time = get_now_time(DATETIME_FORMAT_DAY)
    hms_time = get_now_time(DATETIME_FORMAT_HMS)
    path = os.path.join(path, day_time)
    os.makedirs(path, exist_ok=True)
    csv_name = f'{collection_path.db_name}_{collection_path.collection_name}_{hms_time}_{part_num}'
    if time_span:
        start_time = time_span.start.replace(' ', '-').replace(':', '-')
        end_time = time_span.end.replace(' ', '-').replace(':', '-')
        csv_name += f'_{start_time}_{end_time}'
    csv_name += '.csv'
    zip_name = f'{path}/{csv_name}.zip'
    try:
        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)
        csv_buffer.seek(0)
        with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr(csv_name, csv_buffer.getvalue())
        logs_list.append(f"DataFrame successfully compressed and saved as {zip_name}")
        return zip_name, logs_list
    except DependencyException as e:
        logger.exception(e)
        raise DependencyException('Failed to zip dataframe') from e


def profile_function(func, *args, **kwargs):
    """
    使用 line_profiler 对单个函数进行性能分析。

    Args:
        func (function): 需要分析的目标函数。
        *args: 调用目标函数时传递的位置参数。
        **kwargs: 调用目标函数时传递的关键字参数。

    Returns:
        Any: 目标函数的返回结果。
    """
    if line_profiler is None:
        raise DependencyException('line_profiler is not installed. Please install it with: pip install line_profiler')
    # 创建 LineProfiler 实例
    profiler = line_profiler.LineProfiler()

    # 添加需要分析的单个函数
    profiler.add_function(func)

    # 启动分析
    profiler.enable()

    # 执行目标函数并记录返回结果
    result = func(*args, **kwargs)

    # 停止分析
    profiler.disable()

    # 输出分析结果
    profiler.print_stats()

    # 返回结果
    return result


def analyze_function_memory(func, *args, **kwargs):
    """
    分析指定函数的逐行内存使用情况。

    Args:
        func (callable): 需要分析的函数对象。
        *args: 传递给 `func` 的位置参数。
        **kwargs: 传递给 `func` 的关键字参数。

    Returns:
        None: 打印函数逐行内存使用情况。
    """
    # 创建一个 memory_profiler 的 LineProfiler 实例
    profiler = memory_profiler.LineProfiler()

    # 将目标函数添加到 profiler 中
    profiler.add_function(func)

    # 启动 profiler
    profiler.enable()

    # 运行目标函数
    print(f"Running memory analysis for {func.__name__}...")
    result = func(*args, **kwargs)

    # 关闭 profiler
    profiler.disable()

    # 打印结果
    memory_profiler.show_results(profiler)

    return result


def start_and_join_processes(process_list):
    for p in process_list:
        p.start()
    for p in process_list:
        p.join()


def save_report_df_csv(file_name, df):
    os.makedirs(REPORT_FILE_PATH, exist_ok=True)
    file_name = re.sub(r'[()<>|,\'"\s]', '-', file_name)
    df.to_csv(f"{REPORT_FILE_PATH}{file_name}.csv", index=False)


def read_report_df_csv(file_name):
    return pd.read_csv(file_name)


def filter_duplicate_trade(trade):
    result = []
    if isinstance(trade, pd.DataFrame):
        result = trade.drop_duplicates(subset=[TradeAttribute.trade_id.name, TradeAttribute.side.name])
    elif isinstance(trade, list):
        trade_side_list = []
        for i in trade:
            trade_id_side = i[TradeAttribute.trade_id.name] + i[TradeAttribute.side.name]
            if trade_id_side in trade_side_list:
                continue
            result.append(i)
            trade_side_list.append(trade_id_side)
    else:
        result = trade
    return result
