from datetime import timedelta, datetime

import pytest

from pytradekit.utils.time_handler import get_datetime, get_rounded_time_interval, DATETIME_FORMAT, get_timestamp_ms, \
    get_next_min_ms, get_today_start_timestamp, TimeUnits, get_tomorrow_datetime, get_last_complete_month_utc, \
    get_last_complete_week_utc, get_last_complete_quarter_utc, get_since_2000_utc, get_before_yesterday_utc, \
    get_today_str, TimeSpan, adjust_time_span, get_last_quarter_day_range, get_next_hour_time


@pytest.mark.parametrize(
    "back_hours, back_minutes, expected_start_delta",
    [
        # back_hours参数测试
        (2, None, -2),  # 往回2小时
        (0, None, 0),  # 往回0小时
        # back_minutes参数测试
        (None, 30, -30 / 60),  # 往回30分钟
        (None, 0, 0),  # 往回0分钟
        # 异常情况测试
        (None, None, "error")  # 未提供任何参数
    ]
)
def test_get_rounded_time_interval(back_hours, back_minutes, expected_start_delta):
    now = get_datetime()
    if expected_start_delta == "error":
        with pytest.raises(ValueError):
            get_rounded_time_interval(back_hours=back_hours, back_minutes=back_minutes)
    else:
        end_time = now.replace(minute=0, second=0, microsecond=0)
        if back_hours is not None:
            expected_end_time = end_time
            expected_start_time = end_time - timedelta(hours=back_hours)
        else:
            expected_end_time = now.replace(second=0, microsecond=0)
            expected_start_time = expected_end_time - timedelta(minutes=back_minutes)

        result = get_rounded_time_interval(back_hours=back_hours, back_minutes=back_minutes)

        assert result.start == expected_start_time.strftime(DATETIME_FORMAT)
        assert result.end == expected_end_time.strftime(DATETIME_FORMAT)


def test_get_next_min_ms():
    now_ms = get_timestamp_ms()
    next_min_ms = get_next_min_ms(now_ms)
    assert next_min_ms > now_ms


def test_get_today_start_timestamp():
    today_start_timestamp_ms = get_today_start_timestamp(unit=TimeUnits.MS)
    assert isinstance(today_start_timestamp_ms, int)


def test_get_tomorrow_datetime():
    tomorrow = get_tomorrow_datetime()
    assert isinstance(tomorrow, datetime)
    assert tomorrow > datetime.utcnow()


@pytest.mark.parametrize("now_utc, expected", [
    (datetime(2024, 5, 15), ("2024-04-01 00:00:00.000", "2024-04-30 23:59:59.999")),  # 5月中旬，上个完整月应为4月
    (datetime(2024, 1, 3), ("2023-12-01 00:00:00.000", "2023-12-31 23:59:59.999")),  # 1月1日，上个完整月应为12月
])
def test_get_last_complete_month_utc(now_utc, expected):
    time_span = get_last_complete_month_utc(now_utc)
    assert time_span.start == expected[0]
    assert time_span.end == expected[1]


@pytest.mark.parametrize("now_utc, expected", [
    (datetime(2024, 3, 10), ("2024-02-26 00:00:00.000", "2024-03-03 23:59:59.999")),  # 一个具体周的例子，自行调整期望值
])
def test_get_last_complete_week_utc(now_utc, expected):
    time_span = get_last_complete_week_utc(now_utc)
    assert time_span.start == expected[0]
    assert time_span.end == expected[1]


@pytest.mark.parametrize("now_utc, expected", [
    (datetime(2024, 5, 15), ("2024-01-01 00:00:00.000", "2024-03-31 23:59:59.999")),  # 5月中旬，上个完整季度应为第一季度
])
def test_get_last_complete_quarter_utc(now_utc, expected):
    time_span = get_last_complete_quarter_utc(now_utc)
    assert time_span.start == expected[0]
    assert time_span.end == expected[1]


@pytest.mark.parametrize("now_utc, expected", [
    (datetime(2024, 5, 15), ("2000-01-01 00:00:00.000", "2024-05-13 23:59:59.999")),  # 从2000-01-01到昨天
    (datetime(2024, 1, 1), ("2000-01-01 00:00:00.000", "2023-12-30 23:59:59.999")),  # 新年第一天
])
def test_get_since_2000_utc(now_utc, expected):
    time_span = get_since_2000_utc(now_utc)
    assert time_span.start == expected[0]
    assert time_span.end == expected[1]


@pytest.mark.parametrize("now_utc, expected", [
    (datetime(2024, 5, 15), ("2024-05-13 00:00:00.000", "2024-05-13 23:59:59.999")),  # 前天到昨天
    (datetime(2024, 1, 3), ("2024-01-01 00:00:00.000", "2024-01-01 23:59:59.999")),  # 新年第三天
])
def test_get_before_yesterdays_utc(now_utc, expected):
    time_span = get_before_yesterday_utc(now_utc)
    # assert time_span.start == expected[0]
    assert time_span.end == expected[1]


@pytest.fixture
def mock_logger():
    """模拟一个日志记录器。"""

    class MockLogger:
        def __init__(self):
            self.logs = []

        def info(self, message):
            self.logs.append(message)

    return MockLogger()


def test_get_today_str(monkeypatch):
    # 模拟当前时间为 2024-11-01 15:45:00
    fixed_datetime = datetime(2024, 11, 1, 15, 45, 0)

    def mock_get_datetime():
        return fixed_datetime

    # 使用 monkeypatch 替换 get_datetime 为模拟的固定时间
    monkeypatch.setattr("pytradekit.utils.time_handler.get_datetime", mock_get_datetime)

    # 调用 get_today_str 并验证输出
    result = get_today_str()
    expected_result = "2024-11-01"  # 符合 "%Y-%m-%d" 格式的预期输出
    assert result == expected_result, f"Expected {expected_result}, but got {result}"


def test_adjust_time_span_add_hours():
    # 设置初始时间范围
    start_time = "2024-11-01 12:00:00"
    end_time = "2024-11-01 14:00:00"
    time_span = TimeSpan(start=start_time, end=end_time)

    # 调整时间范围，增加2小时
    adjusted_time_span = adjust_time_span(time_span, adjust_hour=2, is_add=True)

    # 验证结果
    assert adjusted_time_span.start == "2024-11-01 14:00:00", f"Expected start time 2024-11-01 14:00:00 but got {adjusted_time_span.start}"
    assert adjusted_time_span.end == "2024-11-01 16:00:00", f"Expected end time 2024-11-01 16:00:00 but got {adjusted_time_span.end}"


def test_adjust_time_span_subtract_hours():
    # 设置初始时间范围
    start_time = "2024-11-01 12:00:00"
    end_time = "2024-11-01 14:00:00"
    time_span = TimeSpan(start=start_time, end=end_time)

    # 调整时间范围，减少2小时
    adjusted_time_span = adjust_time_span(time_span, adjust_hour=2, is_add=False)

    # 验证结果
    assert adjusted_time_span.start == "2024-11-01 10:00:00", f"Expected start time 2024-11-01 10:00:00 but got {adjusted_time_span.start}"
    assert adjusted_time_span.end == "2024-11-01 12:00:00", f"Expected end time 2024-11-01 12:00:00 but got {adjusted_time_span.end}"


@pytest.mark.parametrize(
    "now_utc, expected_start, expected_end",
    [
        # 1) 测试：当前时间在 Q1(1-3月)内，则上个完整季度是 Q4(上一年10-12月)
        (
                datetime(2024, 2, 15, 10, 0, 0),  # 2024年Q1内
                "2023-10-01",  # Q4首日(10/1) + 1天 = 10/2
                "2023-12-31",  # Q4末日(12/31)
        ),
        # 2) 测试：当前时间在 Q2(4-6月)内，则上个完整季度是 Q1(1-3月)
        (
                datetime(2024, 5, 23, 12, 0, 0),  # 2024年Q2内
                "2024-01-01",  # Q1首日(1/1) + 1天 = 1/2
                "2024-03-31",  # Q1末日(3/31)
        ),
        # 3) 测试：当前时间在 Q3(7-9月)内，则上个完整季度是 Q2(4-6月)
        (
                datetime(2024, 7, 1, 0, 0, 0),  # 2024年Q3首日
                "2024-04-01",  # Q2首日(4/1) + 1天 = 4/2
                "2024-06-30",  # Q2末日(6/30)
        ),
        # 4) 测试：当前时间在 Q4(10-12月)内，则上个完整季度是 Q3(7-9月)
        (
                datetime(2024, 11, 10, 8, 0, 0),  # 2024年Q4内
                "2024-07-01",  # Q3首日(7/1) + 1天 = 7/2
                "2024-09-30",  # Q3末日(9/30)
        ),
    ]
)
def test_get_last_quarter_day_range(now_utc, expected_start, expected_end):
    result_span = get_last_quarter_day_range(now_utc=now_utc)

    assert isinstance(result_span, TimeSpan), "返回类型应为 TimeSpan"
    assert result_span.start == expected_start, (
        f"开始日期应为 {expected_start}, 实际为 {result_span.start}"
    )
    assert result_span.end == expected_end, (
        f"结束日期应为 {expected_end}, 实际为 {result_span.end}"
    )


def test_get_next_hour_time_basic():
    """测试基本功能：时间增加1小时，并且分钟秒被重置"""
    hour = 1
    result = get_next_hour_time(hour)

    # 只验证分钟、秒、微秒的设置
    assert result.minute == 1
    assert result.second == 0
    assert result.microsecond == 0


def test_get_next_hour_time_zero():
    """测试 hour=0 的情况"""
    result = get_next_hour_time(0)

    # 验证只改变了分钟和秒
    assert result.minute == 1
    assert result.second == 0
    assert result.microsecond == 0


def test_get_next_hour_time_negative():
    """测试负数小时的情况"""
    hour = -2
    result = get_next_hour_time(hour)

    # 验证分钟、秒、微秒的设置
    assert result.minute == 1
    assert result.second == 0
    assert result.microsecond == 0
