import pytest
import pandas as pd
from pandas import DataFrame
from pytradekit.utils.static_types import InstcodeBasicAttribute
from pytradekit.trading_setup.inst_code_usage import get_mm_target, DepthThreshold, RankPctThreshold, SpreadThreshold, \
    MmInstCode, \
    fetch_mm_inst_code, get_pair_key_mm_target, get_related_inst_code
from pytradekit.utils.dynamic_types import MmTarget, ExchangeId
from pytradekit.utils.custom_types import Pair


@pytest.mark.parametrize(
    "exchange_id, quote, expected",
    [
        # Case 1: Exchange is BN, quote is USDT
        (
                ExchangeId.BN.name,
                'USDT',
                {
                    MmTarget.spread.name: SpreadThreshold.normal,
                    MmTarget.average_depth.name: -1,
                    MmTarget.side_depth.name: DepthThreshold.binance_usdt,
                    MmTarget.exchange_rank.name: -1,
                    MmTarget.quote_rank.name: RankPctThreshold.binance_usdt,
                    MmTarget.volume_24h.name: -1,
                },
        ),
        # Case 2: Exchange is BN, quote is TUSD
        (
                ExchangeId.BN.name,
                'TUSD',
                {
                    MmTarget.spread.name: SpreadThreshold.normal,
                    MmTarget.average_depth.name: DepthThreshold.binance,
                    MmTarget.side_depth.name: -1,
                    MmTarget.exchange_rank.name: RankPctThreshold.binance_no_usdt,
                    MmTarget.quote_rank.name: -1,
                    MmTarget.volume_24h.name: -1,
                },
        ),
        # Case 3: Exchange is BN, other quote
        (
                ExchangeId.BN.name,
                'BTC',
                {
                    MmTarget.spread.name: SpreadThreshold.normal,
                    MmTarget.average_depth.name: DepthThreshold.binance,
                    MmTarget.side_depth.name: -1,
                    MmTarget.exchange_rank.name: RankPctThreshold.binance_no_usdt,
                    MmTarget.quote_rank.name: -1,
                    MmTarget.volume_24h.name: -1,
                },
        ),
        # Case 4: Other exchange, quote is USDT
        (
                ExchangeId.BFX.name,
                'USDT',
                {
                    MmTarget.spread.name: SpreadThreshold.normal,
                    MmTarget.average_depth.name: DepthThreshold.normal_usdt,
                    MmTarget.side_depth.name: -1,
                    MmTarget.exchange_rank.name: RankPctThreshold.normal,
                    MmTarget.quote_rank.name: -1,
                    MmTarget.volume_24h.name: -1,
                },
        ),
        # Case 5: Other exchange, other quote
        (
                'OTHER',
                'BTC',
                {
                    MmTarget.spread.name: SpreadThreshold.normal,
                    MmTarget.average_depth.name: DepthThreshold.normal,
                    MmTarget.side_depth.name: -1,
                    MmTarget.exchange_rank.name: RankPctThreshold.normal,
                    MmTarget.quote_rank.name: -1,
                    MmTarget.volume_24h.name: -1,
                },
        ),
    ],
)
def test_get_mm_target(exchange_id, quote, expected):
    result = get_mm_target(exchange_id, quote)
    assert result == expected


@pytest.mark.parametrize(
    "exchange_id, expected_inst_code",
    [
        ("KC", MmInstCode.KC),
        ("MCO", MmInstCode.MCO),
        ("BMT", MmInstCode.BMT),
        ("MXC", MmInstCode.MXC),
        ("BN", MmInstCode.BN),
        ("BFX", MmInstCode.BFX),
        ("BGT", MmInstCode.BGT),
        ("BBT", MmInstCode.BBT),
        ("GT", MmInstCode.GT),
        ("HTX", MmInstCode.HTX),
        ("OKX", MmInstCode.OKX),
        ("BUL", MmInstCode.BUL),
        ("BCI", MmInstCode.BCI)
    ],
)
def test_fetch_mm_inst_code_valid(exchange_id, expected_inst_code):
    # 测试有效的 exchange_id
    assert fetch_mm_inst_code(exchange_id) == expected_inst_code


def test_get_pair_key_mm_target_bn():
    result = get_pair_key_mm_target(ExchangeId.BN.name)

    # 检查结果是否包含 Pair() 键，并验证返回目标是否正确
    assert str(Pair()) in result
    assert str(Pair(quote='USDT')) in result
    assert str(Pair(quote='TUSD')) in result

    # 检查返回的目标值是否符合预期
    assert result[str(Pair())][MmTarget.average_depth.name] == DepthThreshold.binance
    assert result[str(Pair(quote='USDT'))][MmTarget.side_depth.name] == DepthThreshold.binance_usdt


def test_get_pair_key_mm_target_okx():
    result = get_pair_key_mm_target(ExchangeId.OKX.name)

    # 检查 OKX 是否只包含 Pair() 的情况
    assert str(Pair()) in result
    assert str(Pair(quote='USDT')) not in result


def test_get_pair_key_mm_target_bfx():
    result = get_pair_key_mm_target(ExchangeId.BFX.name)

    # 检查 BFX 是否只包含 Pair() 的情况
    assert str(Pair()) in result
    assert str(Pair(quote='USDT')) not in result  # BFX 无特殊 USDT 配置


def test_get_pair_key_mm_target_default():
    result = get_pair_key_mm_target("UNKNOWN_EXCHANGE")

    # 对于未知的 exchange_id，应该只包含 Pair() 和 USDT
    assert str(Pair()) in result
    assert str(Pair(quote='USDT')) in result


@pytest.fixture
def sample_inst_code_basic():
    data = {
        InstcodeBasicAttribute.inst_code.name: [
            'TRX-USDT_BN.SPOT', 'TUSD-USDT_BN.SPOT', 'TRX-TUSD_BN.SPOT',
            'BTC-USDT_BN.SPOT', 'ETH-USDT_BN.SPOT', 'XRP-USDT_BN.SPOT', 'XRP-TUSD_BN.SPOT',
        ],
        InstcodeBasicAttribute.base.name: ['TRX', 'TUSD', 'TRX', 'BTC', 'ETH', 'XRP', 'XRP'],
        InstcodeBasicAttribute.quote.name: ['USDT', 'USDT', 'TUSD', 'USDT', 'USDT', 'USDT', 'TUSD']
    }
    return DataFrame(data)


def test_get_related_inst_code_normal(mocker, sample_inst_code_basic):
    # 模拟fetch_mm_inst_code返回值，包含测试数据中的交易对
    # 返回多个做市交易对，以便找到更多相关交易对
    mocker.patch(
        'pytradekit.trading_setup.inst_code_usage.fetch_mm_inst_code',
        return_value=['TRX-TUSD_BN.SPOT', 'TRX-USDT_BN.SPOT', 'TUSD-USDT_BN.SPOT', 'XRP-TUSD_BN.SPOT']
    )
    result = get_related_inst_code('BN', sample_inst_code_basic)

    # 验证结果包含做市交易对和相关交易对
    # 做市交易对: TRX-TUSD, TRX-USDT, TUSD-USDT, XRP-TUSD
    # all_tokens = {TRX, TUSD, USDT, XRP}
    # 相关交易对: base 和 quote 都在 all_tokens 中的交易对
    # 应该包含: TRX-TUSD, TRX-USDT, TUSD-USDT, XRP-TUSD, XRP-USDT
    assert len(result) >= 5  # 至少包含做市交易对和相关交易对
    assert 'TRX-TUSD_BN.SPOT' in result[InstcodeBasicAttribute.inst_code.name].values
    assert 'TRX-USDT_BN.SPOT' in result[InstcodeBasicAttribute.inst_code.name].values
    assert 'TUSD-USDT_BN.SPOT' in result[InstcodeBasicAttribute.inst_code.name].values
    assert 'XRP-TUSD_BN.SPOT' in result[InstcodeBasicAttribute.inst_code.name].values
    assert 'XRP-USDT_BN.SPOT' in result[InstcodeBasicAttribute.inst_code.name].values


def test_get_related_inst_code_empty(mocker, sample_inst_code_basic):
    # 模拟fetch_mm_inst_code返回值
    mocker.patch(
        'pytradekit.trading_setup.inst_code_usage.fetch_mm_inst_code',
        return_value=['NON-EXIST_BN.SPOT']
    )

    result = get_related_inst_code('BN', sample_inst_code_basic)

    # 验证返回空DataFrame
    assert result.empty
    assert list(result.columns) == list(sample_inst_code_basic.columns)


def test_get_related_inst_code_no_related(mocker, sample_inst_code_basic):
    # 模拟fetch_mm_inst_code返回值
    mocker.patch(
        'pytradekit.trading_setup.inst_code_usage.fetch_mm_inst_code',
        return_value=['BTC-USDT_BN.SPOT']
    )

    result = get_related_inst_code('BN', sample_inst_code_basic)

    # 验证只返回做市交易对
    assert len(result) == 1
    assert 'BTC-USDT_BN.SPOT' in result[InstcodeBasicAttribute.inst_code.name].values
