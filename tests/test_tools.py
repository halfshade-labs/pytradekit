from pytradekit.utils.number_tools import reshape_report_number


def test_format_number():
    # 测试整数部分大于等于1的情况
    assert reshape_report_number(74.022380) == '74.02'
    assert reshape_report_number(598.155700) == '598.16'
    assert reshape_report_number(-598.155700) == '-598.16'
    assert reshape_report_number(5.284200) == '5.284'
    assert reshape_report_number(-39543.850000) == '-39,543'
    assert reshape_report_number(5.571238) == '5.571'
    assert reshape_report_number(15.193791) == '15.19'

    # 测试没有整数部分的情况
    assert reshape_report_number(0.000021) == "0.000'021"
    assert reshape_report_number(0.2348316) == "0.234'8"
    assert reshape_report_number(0.000003456) == "0.000'003'456"
    assert reshape_report_number(0.00123456789) == "0.001'234"

    # 测试整数部分为0的情况
    assert reshape_report_number(0) == "0.0"

    # 测试整数部分大于5的情况
    assert reshape_report_number(100000.1234) == "100,000"
    assert reshape_report_number(123456.789) == "123,456"

    # 测试其他情况
    assert reshape_report_number(21.867) == "21.87"
    assert reshape_report_number(719343) == "719,343"
    assert reshape_report_number(0.00456789) == "0.004'567"
