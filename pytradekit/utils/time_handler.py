from datetime import datetime, timedelta
import functools
import time
from enum import Enum, auto

import pytz
import pandas as pd

DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'
DATETIME_FORMAT_MS = '%Y-%m-%d %H:%M:%S.%f'
DATETIME_FORMAT_DAY = '%Y-%m-%d'
DATETIME_FORMAT_DAY2 = '%Y-%m-%d 00:00:00'
DATETIME_FORMAT_HOUR = '%Y-%m-%d %H:00:00'
DATETIME_FORMAT_HMS = '%H-%M-%S'
DATETIME_FORMAT_ADS = '%Y-%m-%dT%H:%M:%S'
DATETIME_FORMAT_HB = '%Y-%m-%dT%H:%M:%S'
DATETIME_FORMAT_OKEX = '%Y-%m-%d %H:%M:%S.%fZ'
TIME_ZONE = pytz.timezone('UTC')


class TimeFrame(Enum):
    minute = 'm'
    hour = 'h'
    day = 'd'


class TimeUnits:
    MS = 'MS'
    SECOND = 'S'
    MINUTE = 'M'
    HOUR = 'H'
    DAY = 'd'
    WEEK = 'w'
    MONTH = 'm'
    YEAR = 'Y'


class TimeSpan:
    def __init__(self, start, end):
        self.start = start
        self.end = end


class TimeConvert:
    DAY_TO_HOUR = 24
    HOUR_TO_MIN = 60
    MIN_TO_S = 60
    S_TO_MS = 1000
    MINUTE_TO_MS = 60 * 1000
    HOUR_TO_MS = 60 * 60 * 1000
    HOUR_TO_S = 60 * 60
    DAY_TO_S = 60 * 60 * 24
    DAT_TO_MS = 60 * 60 * 24 * 1000


class CronKey(Enum):
    month = auto()
    day = auto()
    day_of_week = auto()
    hour = auto()
    minute = auto()
    second = auto()


class DayWeek(Enum):
    mon = auto()
    tue = auto()
    wed = auto()
    thu = auto()
    fri = auto()
    sat = auto()
    sun = auto()


def parse_backup_cron(backup_time):
    """
    解析 backup_time 字典并生成 cron_kwargs。

    Args:
    backup_time (dict): 包含定时任务配置的字典。

    Returns:
    dict: cron 表达式的参数字典。
    """
    cron_kwargs = {
        CronKey.month.name: backup_time.get(CronKey.month.name),
        CronKey.day.name: backup_time.get(CronKey.day.name),
        CronKey.day_of_week.name: backup_time.get(CronKey.day_of_week.name),
        CronKey.hour.name: backup_time.get(CronKey.hour.name),
        CronKey.minute.name: backup_time.get(CronKey.minute.name),
        CronKey.second.name: backup_time.get(CronKey.second.name, '0'),
    }
    cron_kwargs = {k: v for k, v in cron_kwargs.items() if v is not None}
    return cron_kwargs


def timer(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        end = time.time()
        time_spend = end - start
        print(f'{func.__name__} cost seconds: {time_spend}')
        return result

    return wrapper


def get_rounded_time_interval(back_days=None, back_hours=None, back_minutes=None, end_time=None):
    """
    获取从当前时间往回指定小时数或分钟数的时间间隔，并将其四舍五入到最近的整点或整分。

    参数:
    - back_hours: 往回的小时数。
    - back_minutes: 往回的分钟数。
    - end_time: datetime

    返回:
    - tuple: 包含两个四舍五入到整点或整分的时间字符串（起始时间，结束时间）。

    异常:
    - ValueError: 如果没有提供 back_hours 或 back_minutes 参数。
    """
    if end_time is None:
        end_time = get_datetime()
    if back_days is not None:
        start_time = end_time - timedelta(days=back_days)
        end_time = end_time.replace(hour=0, minute=0, second=0, microsecond=0)
        start_time = start_time.replace(hour=0, minute=0, second=0, microsecond=0)
    elif back_hours is not None:
        start_time = end_time - timedelta(hours=back_hours)
        end_time = end_time.replace(minute=0, second=0, microsecond=0)
        start_time = start_time.replace(minute=0, second=0, microsecond=0)
    elif back_minutes is not None:
        start_time = end_time - timedelta(minutes=back_minutes)
        end_time = end_time.replace(second=0, microsecond=0)
        start_time = start_time.replace(second=0, microsecond=0)
    else:
        # 如果没有提供 back_hours 或 back_minutes 参数，则抛出异常
        raise ValueError("Must provide either 'back_hours' or 'back_minutes' parameter.")

    # 格式化时间为字符串
    start_time_str = start_time.strftime(DATETIME_FORMAT)
    end_time_str = end_time.strftime(DATETIME_FORMAT)
    time_span = TimeSpan(start=start_time_str,
                         end=end_time_str)
    return time_span


def get_rounded_date_list(back_days: int) -> list:
    """
    获取从当前日期往回指定天数的日期字符串列表，并将每个日期四舍五入到最近的整天。

    参数:
    - back_days: 往回的天数。

    返回:
    - list[str]: 包含从当前日期往回指定天数的日期字符串列表。

    异常:
    - ValueError: 如果 back_days 参数不是正整数。
    """
    if back_days <= 0:
        raise ValueError("back_days must be a positive integer.")

    date_list = []
    today = get_datetime().replace(hour=0, minute=0, second=0, microsecond=0)  # 获取今天的日期并四舍五入到最近的整天
    for i in range(1, back_days + 1):
        day = today - timedelta(days=i)
        date_list.append(day.strftime(DATETIME_FORMAT_DAY))
    return date_list


def get_millisecond_str(datatime):
    return datatime.strftime(DATETIME_FORMAT_MS)[:-3]


def get_seconds_str(datatime):
    return datatime.strftime(DATETIME_FORMAT)


def get_next_min_ms(timestamp) -> int:
    return (
            int(timestamp / TimeConvert.S_TO_MS / TimeConvert.MIN_TO_S) + 1) * TimeConvert.MIN_TO_S * TimeConvert.S_TO_MS


def get_now_time(a_format=DATETIME_FORMAT) -> str:
    return get_datetime().strftime(a_format)


def get_hour_time(time_str):
    dt = datetime.strptime(time_str, DATETIME_FORMAT_MS)
    formatted_time_str = dt.strftime(DATETIME_FORMAT_HOUR)
    return formatted_time_str


def get_day_time(time_str, date_format=DATETIME_FORMAT):
    dt = datetime.strptime(time_str, date_format)
    formatted_time_str = dt.strftime(DATETIME_FORMAT_DAY2)
    return formatted_time_str


def get_now_hour(a_format=DATETIME_FORMAT) -> str:
    return get_current_hour_datetime().strftime(a_format)


def get_now_hour_utc8(a_format=DATETIME_FORMAT) -> str:
    return (get_current_hour_datetime() + timedelta(hours=8)).strftime(a_format)


def get_timestamp_ms() -> int:
    return int(round(time.time() * TimeConvert.S_TO_MS))


def get_timestamp_s() -> int:
    return round(time.time())


def convert_str_to_timestamp(date_string):
    if len(date_string) == 19:
        date_format = DATETIME_FORMAT
    elif len(date_string) == 23:
        date_format = DATETIME_FORMAT_MS
    else:
        raise ValueError("unsupported date format")
    utc_zone = pytz.utc
    naive_datetime = datetime.strptime(date_string, date_format)
    timestamp = naive_datetime.replace(tzinfo=utc_zone).timestamp()
    if date_format == DATETIME_FORMAT_MS:
        return int(timestamp * TimeConvert.S_TO_MS)
    else:
        return int(timestamp)


def check_time_within_threshold(data_time, threshold_minutes):
    data_ts = convert_str_to_timestamp(data_time)

    if len(data_time) == 19:
        now_ts = get_timestamp_s()
    elif len(data_time) == 23:
        now_ts = get_timestamp_ms()
    else:
        raise ValueError("unsupported date format")

    time_difference_minutes = (now_ts - data_ts) / (
        TimeConvert.MIN_TO_S * TimeConvert.S_TO_MS if len(data_time) == 23 else TimeConvert.MIN_TO_S)

    return time_difference_minutes < threshold_minutes


def convert_timestamp_to_hour_str(timestamp):
    dt_object = datetime.fromtimestamp(timestamp / TimeConvert.S_TO_MS)
    hour = dt_object.strftime("%Y-%m-%d %H:00:00")
    return hour


def get_today_start_timestamp(unit=TimeUnits.MS):
    today_start_str = get_datetime().strftime('%Y-%m-%d 00:00:00')
    today_start_timestamp = int(time.mktime(time.strptime(today_start_str, DATETIME_FORMAT)))

    if unit == TimeUnits.MS:
        return today_start_timestamp * TimeConvert.S_TO_MS
    elif unit == TimeUnits.SECOND:
        return today_start_timestamp
    else:
        raise ValueError("Unsupported time unit")


def get_hours_start_timestamp(unit=TimeUnits.MS):
    hours_start_str = get_datetime().strftime('%Y-%m-%d %H:00:00')
    hours_start_timestamp = int(time.mktime(time.strptime(hours_start_str, DATETIME_FORMAT)))

    if unit == TimeUnits.MS:
        return hours_start_timestamp * TimeConvert.S_TO_MS
    elif unit == TimeUnits.SECOND:
        return hours_start_timestamp
    else:
        raise ValueError("Unsupported time unit")


def get_hours_start_end_timestamp():
    now = get_datetime()
    current_hour = int(
        datetime(year=now.year, month=now.month, day=now.day, hour=now.hour, minute=0, second=0).timestamp())
    previous_hour_timestamp = current_hour - TimeConvert.MIN_TO_S * TimeConvert.MIN_TO_S
    time_span = TimeSpan(start=previous_hour_timestamp * TimeConvert.S_TO_MS, end=current_hour * TimeConvert.S_TO_MS)
    return time_span


def convert_str_to_datetime(date_str, date_format=None):
    if date_format:
        return datetime.strptime(date_str, date_format)
    else:
        try:
            if len(date_str) > 19:
                return datetime.strptime(date_str, DATETIME_FORMAT_MS)
            else:
                return datetime.strptime(date_str, DATETIME_FORMAT)
        except ValueError as e:
            raise ValueError(f"wrong format'{date_str}'，reason: {e}")


def convert_date_str_timezone(date_str, hours=8, date_format=DATETIME_FORMAT):
    return (convert_str_to_datetime(date_str) + timedelta(hours=hours)).strftime(date_format)


def convert_timestamp_to_str(timestamp, unit=TimeUnits.MS, a_format=DATETIME_FORMAT):
    if unit == TimeUnits.MS:
        timestamp = int(timestamp) / TimeConvert.S_TO_MS
    elif unit == TimeUnits.SECOND:
        timestamp = int(timestamp)
    else:
        raise ValueError("Unsupported unit. Please use 'ms' for milliseconds or 's' for seconds.")
    time_str = time.strftime(a_format, time.localtime(timestamp))
    return time_str


def convert_datatime_to_timestamp(datatime, a_format=DATETIME_FORMAT):
    time_str = datatime.strftime(a_format)
    timestamp = convert_str_to_timestamp(time_str)
    return timestamp


def convert_timestamp_to_datetime(timestamp, unit=TimeUnits.MS):
    if unit == TimeUnits.MS:
        timestamp = int(timestamp) / TimeConvert.S_TO_MS
    elif unit == TimeUnits.SECOND:
        timestamp = int(timestamp)
    else:
        raise ValueError("Unsupported unit. Please use 'ms' for milliseconds or 's' for seconds.")
    dt = datetime.utcfromtimestamp(timestamp)
    return dt


def get_datetime() -> datetime:
    return datetime.utcnow()


def get_current_hour_datetime() -> datetime:
    now_time = get_datetime()
    today = now_time.replace(minute=0, second=0, microsecond=0)
    return today


def get_today_datetime() -> datetime:
    now_time = get_datetime()
    today = now_time.replace(hour=0, minute=0, second=0, microsecond=0)
    return today


def get_today_str(date_format=DATETIME_FORMAT_DAY) -> str:
    return get_today_datetime().strftime(date_format)


def get_tomorrow_datetime() -> datetime:
    now_time = get_datetime()
    tomorrow = (now_time + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow


def get_yesterday_datetime() -> datetime:
    now_time = get_datetime()
    yesterday = (now_time - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return yesterday


def get_yesterday_str(date_format=DATETIME_FORMAT_DAY):
    return get_yesterday_datetime().strftime(date_format)


def get_last_complete_week_utc(now_utc=None) -> TimeSpan:
    if not now_utc:
        now_utc = get_datetime()
    last_day_of_last_week = now_utc - timedelta(days=now_utc.weekday() + 1)
    end_of_last_week = last_day_of_last_week.replace(hour=23, minute=59, second=59, microsecond=999000)
    end_of_last_week = get_millisecond_str(end_of_last_week)
    start_of_last_week = last_day_of_last_week - timedelta(days=6)
    start_of_last_week = start_of_last_week.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_last_week = get_millisecond_str(start_of_last_week)
    return TimeSpan(start_of_last_week, end_of_last_week)


def get_last_complete_month_utc(now_utc=None) -> TimeSpan:
    if not now_utc:
        now_utc = get_datetime()
    first_day_of_this_month = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day_of_last_month = first_day_of_this_month - timedelta(days=1)
    end_of_last_month = last_day_of_last_month.replace(hour=23, minute=59, second=59, microsecond=999000)
    end_of_last_month = get_millisecond_str(end_of_last_month)
    start_of_last_month = last_day_of_last_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_of_last_month = get_millisecond_str(start_of_last_month)
    return TimeSpan(start_of_last_month, end_of_last_month)


def get_last_complete_quarter_utc(now_utc=None) -> TimeSpan:
    if not now_utc:
        now_utc = get_datetime()
    current_quarter = (now_utc.month - 1) // 3 + 1
    first_month_of_this_quarter = (current_quarter - 1) * 3 + 1
    first_day_of_this_quarter = now_utc.replace(month=first_month_of_this_quarter, day=1, hour=0, minute=0, second=0,
                                                microsecond=0)
    last_day_of_last_quarter = first_day_of_this_quarter - timedelta(days=1)
    last_quarter = (last_day_of_last_quarter.month - 1) // 3 + 1
    first_month_of_last_quarter = (last_quarter - 1) * 3 + 1
    first_day_of_last_quarter = last_day_of_last_quarter.replace(month=first_month_of_last_quarter, day=1, hour=0,
                                                                 minute=0, second=0, microsecond=0)
    end_of_last_quarter = last_day_of_last_quarter.replace(hour=23, minute=59, second=59, microsecond=999000)
    end_of_last_quarter_str = get_millisecond_str(end_of_last_quarter)
    start_of_last_quarter_str = get_millisecond_str(first_day_of_last_quarter)
    return TimeSpan(start_of_last_quarter_str, end_of_last_quarter_str)


def get_last_quarter_day_range(now_utc=None) -> TimeSpan:
    quarter_span = get_last_complete_quarter_utc(now_utc)
    start_dt_plus_one = convert_str_to_datetime(quarter_span.start)
    end_dt = convert_str_to_datetime(quarter_span.end)
    start_str_day = start_dt_plus_one.strftime(DATETIME_FORMAT_DAY)
    end_str_day = end_dt.strftime(DATETIME_FORMAT_DAY)
    return TimeSpan(start=start_str_day, end=end_str_day)


def get_since_2000_utc(now_utc=None) -> TimeSpan:
    if not now_utc:
        now_utc = get_datetime()
    start_date = datetime(2000, 1, 1, 0, 0, 0, 0)
    end_date = now_utc - timedelta(days=2)
    end_date = end_date.replace(hour=23, minute=59, second=59, microsecond=999000)

    start_millisecond_str = get_millisecond_str(start_date)
    end_millisecond_str = get_millisecond_str(end_date)

    return TimeSpan(start_millisecond_str, end_millisecond_str)


def get_before_yesterday_utc(now_utc=None) -> TimeSpan:
    if not now_utc:
        now_utc = get_datetime()
    end_date = now_utc - timedelta(days=2)
    end_date = end_date.replace(hour=23, minute=59, second=59, microsecond=999000)

    start_date = now_utc - timedelta(days=300)
    start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)

    start_millisecond_str = get_millisecond_str(start_date)
    end_millisecond_str = get_millisecond_str(end_date)

    return TimeSpan(start_millisecond_str, end_millisecond_str)


def get_yesterday_today_hour_utc(now_utc=None) -> TimeSpan:
    if not now_utc:
        now_utc = get_datetime()

    start_date = now_utc - timedelta(days=1)
    start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)

    end_date = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    start_millisecond_str = get_seconds_str(start_date)
    end_millisecond_str = get_seconds_str(end_date)

    return TimeSpan(start_millisecond_str, end_millisecond_str)


def convert_kline_frame_to_ms(kline_frame, logger=None):
    units = {TimeFrame.hour.value: TimeConvert.HOUR_TO_MS,
             TimeFrame.minute.value: TimeConvert.MIN_TO_S * TimeConvert.S_TO_MS,
             TimeFrame.day.value: TimeConvert.DAY_TO_HOUR * TimeConvert.HOUR_TO_MS}
    unit = kline_frame.unit
    if unit in units:
        number = kline_frame.number
        return number * units[unit]
    else:
        msg = f"Unrecognized time unit: {unit}"
        if logger:
            logger.info(msg)
        raise ValueError(msg)


def get_datatime_difference(datatime1: str, datatime2: str) -> int:
    time1 = datetime.strptime(datatime1, DATETIME_FORMAT)
    time2 = datetime.strptime(datatime2, DATETIME_FORMAT)
    time_difference_seconds = abs((time1 - time2).total_seconds())
    return int(time_difference_seconds)


def get_ms_time_diff(datatime1: str, datatime2: str) -> int:
    time1 = datetime.strptime(datatime1, DATETIME_FORMAT_MS)
    time2 = datetime.strptime(datatime2, DATETIME_FORMAT_MS)
    time_difference_seconds = (time1 - time2).total_seconds() * TimeConvert.S_TO_MS
    return int(time_difference_seconds)


def get_ok_timestamp():
    return get_datetime().isoformat()[:23] + 'Z'


def get_htx_timestamp():
    return get_datetime().isoformat()[:19]


def get_bybit_timestamp():
    return int((time.time() + 1) * TimeConvert.S_TO_MS)


def get_time_span_by_hour(raw_cron):
    if raw_cron['hour'][0] == '*':
        hours = int(raw_cron['hour'].split('/')[1])
        back_hours = hours - 1
        time_span = get_rounded_time_interval(back_hours=back_hours)
    else:
        hour_now = get_datetime().hour
        hours_list = list(map(int, raw_cron['hour'].split(',')))
        now_hour_index = hours_list.index(hour_now)
        hours = hour_now - hours_list[now_hour_index - 1]
        if hours < 0:
            hours += 24
        back_hours = hours - 1
        time_span = get_rounded_time_interval(back_hours=back_hours)
    return time_span, hours


def get_ms_time_span_by_hour(raw_cron):
    assert raw_cron['hour'][0] == '*'
    hours = int(raw_cron['hour'].split('/')[1])
    time_span = get_rounded_time_interval(back_hours=hours)
    start = pd.to_datetime(time_span.start).replace(minute=0, second=0, microsecond=0)

    end = (pd.to_datetime(time_span.end) - timedelta(hours=1)).replace(minute=59, second=59, microsecond=999000)

    time_span.start = get_millisecond_str(start)
    time_span.end = get_millisecond_str(end)

    return time_span


def check_str_format(time_str, date_format=DATETIME_FORMAT):
    try:
        datetime.strptime(time_str, date_format)
        return True
    except ValueError:
        return False


def get_df_time_diff(df, start_time, end_time):
    df[start_time] = pd.to_datetime(df[start_time])
    df[end_time] = pd.to_datetime(df[end_time])
    df['time_diff'] = (df[end_time] - df[start_time]).dt.total_seconds() * TimeConvert.S_TO_MS
    return df


def adjust_time_span(time_span, adjust_hour, is_add=True):
    adjust_delta = timedelta(hours=adjust_hour)
    if not is_add:
        adjust_delta = -adjust_delta
    time_span.start = str(convert_str_to_datetime(time_span.start) + adjust_delta)
    time_span.end = str(convert_str_to_datetime(time_span.end) + adjust_delta)
    return time_span


def get_next_hour_time(hour: int) -> datetime:
    next_hour_time = get_datetime() + timedelta(hours=hour)
    next_hour_time = next_hour_time.replace(minute=1, second=0, microsecond=0)
    return next_hour_time


def check_if_time_exceeded(times: datetime) -> bool:
    return get_datetime() >= times


def sleep_min_time(min_second=0.0001):
    time.sleep(min_second)


def check_timeout(start_time, limit_seconds):
    if get_timestamp_s() - start_time > limit_seconds:
        return True
    return False
