"""
Tests for exchange fees module.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from pytradekit.trading_setup.exchange_fees import FeeRateResolver, EXCHANGE_FEES
from pytradekit.utils.config_agent import ConfigAgent
from pytradekit.utils.exceptions import DependencyException


class TestFeeRateResolver:

    """Test FeeRateResolver class."""

    def test_init_without_config(self):
        """Test FeeRateResolver initialization without config."""
        logger = Mock()
        calculator = FeeRateResolver(logger=logger)
        assert calculator.config is None
        assert calculator.exchange_fees == EXCHANGE_FEES

    def test_init_with_config(self):
        """Test FeeRateResolver initialization with config."""
        logger = Mock()
        config = Mock(spec=ConfigAgent)
        calculator = FeeRateResolver(logger=logger, config=config)
        assert calculator.config == config
        assert calculator.exchange_fees == EXCHANGE_FEES

    def test_get_account_fee_config_no_config(self):
        """Test getting account fee config when no config provided."""
        logger = Mock()
        calculator = FeeRateResolver(logger=logger)
        config = calculator._get_account_fee_config('BN')
        assert config == {
            'vip_level': 0,
            'use_platform_token_discount': False,
            'holding_discount': False
        }

    def test_get_account_fee_config_with_config(self):
        """Test getting account fee config from config file."""
        logger = Mock()
        config = Mock(spec=ConfigAgent)
        config.outer = MagicMock()
        config.outer.sections.return_value = ['BN_ACCOUNT']
        config.outer.__getitem__ = lambda self, key: {
            'account_id': 'BN_000',
            'vip_level': '1',
            'use_platform_token_discount': 'true',
            'holding_discount': 'false'
        }[key]

        calculator = FeeRateResolver(logger=logger, config=config)
        
        # Mock ConfigAgent static methods
        with patch.object(ConfigAgent, 'get_str', return_value='BN_000'), \
             patch.object(ConfigAgent, 'get_int', return_value=1), \
             patch.object(ConfigAgent, 'get_boolean', side_effect=[True, False]):
            result = calculator._get_account_fee_config('BN')
            assert result['vip_level'] == 1
            assert result['use_platform_token_discount'] is True
            assert result['holding_discount'] is False

    def test_get_account_fee_config_wrong_account_id(self):
        """Test getting account fee config with wrong account_id."""
        logger = Mock()
        config = Mock(spec=ConfigAgent)
        config.outer = MagicMock()
        config.outer.sections.return_value = ['BN_ACCOUNT']
        
        calculator = FeeRateResolver(logger=logger, config=config)
        
        with patch.object(ConfigAgent, 'get_str', return_value='BN_001'):  # Different account_id
            result = calculator._get_account_fee_config('BN')
            # Should return default config
            assert result == {
                'vip_level': 0,
                'use_platform_token_discount': False,
                'holding_discount': False
            }

    def test_get_fee_rate_exchange_not_found(self):
        """Test get_fee_rate with non-existent exchange."""
        logger = Mock()
        calculator = FeeRateResolver(logger=logger)
        with pytest.raises(DependencyException) as exc_info:
            calculator.get_fee_rate('INVALID', 'spot', True)
        assert 'Exchange INVALID not found' in str(exc_info.value)

    def test_get_fee_rate_market_type_not_found(self):
        """Test get_fee_rate with non-existent market type."""
        # First, we need to add some test data to EXCHANGE_FEES
        test_exchange = {
            'spot': {
                'vip_levels': {0: {'maker': 0.001, 'taker': 0.001}},
                'discounts': {'platform_token_discount': 0.25, 'platform_token': 'TEST'}
            }
        }
        logger = Mock()
        calculator = FeeRateResolver(logger=logger)
        calculator.exchange_fees['TEST'] = test_exchange
        
        with pytest.raises(DependencyException) as exc_info:
            calculator.get_fee_rate('TEST', 'invalid', True)
        assert 'Market type invalid not found' in str(exc_info.value)

    def test_get_fee_rate_vip_level_not_found(self):
        """Test get_fee_rate with non-existent VIP level."""
        test_exchange = {
            'spot': {
                'vip_levels': {0: {'maker': 0.001, 'taker': 0.001}},
                'discounts': {'platform_token_discount': 0.25, 'platform_token': 'TEST'}
            }
        }
        logger = Mock()
        calculator = FeeRateResolver(logger=logger)
        calculator.exchange_fees['TEST'] = test_exchange
        
        # Should fallback to level 0 if VIP level not found
        rate = calculator.get_fee_rate('TEST', 'spot', True)
        assert rate == 0.001

    def test_get_fee_rate_maker(self):
        """Test get_fee_rate for maker order."""
        test_exchange = {
            'spot': {
                'vip_levels': {
                    0: {'maker': 0.001, 'taker': 0.001},
                    1: {'maker': 0.0009, 'taker': 0.001}
                },
                'discounts': {
                    'platform_token_discount': 0.25,
                    'platform_token': 'TEST',
                    'holding_discount': {}
                }
            }
        }
        logger = Mock()
        calculator = FeeRateResolver(logger=logger)
        calculator.exchange_fees['TEST'] = test_exchange
        
        rate = calculator.get_fee_rate('TEST', 'spot', True)
        assert rate == 0.001

    def test_get_fee_rate_taker(self):
        """Test get_fee_rate for taker order."""
        test_exchange = {
            'spot': {
                'vip_levels': {
                    0: {'maker': 0.001, 'taker': 0.001},
                    1: {'maker': 0.0009, 'taker': 0.001}
                },
                'discounts': {
                    'platform_token_discount': 0.25,
                    'platform_token': 'TEST',
                    'holding_discount': {}
                }
            }
        }
        logger = Mock()
        calculator = FeeRateResolver(logger=logger)
        calculator.exchange_fees['TEST'] = test_exchange
        
        rate = calculator.get_fee_rate('TEST', 'spot', False)
        assert rate == 0.001

    def test_get_fee_rate_with_platform_token_discount(self):
        """Test get_fee_rate with platform token discount."""
        test_exchange = {
            'spot': {
                'vip_levels': {
                    0: {'maker': 0.001, 'taker': 0.001}
                },
                'discounts': {
                    'platform_token_discount': 0.25,
                    'platform_token': 'TEST',
                    'holding_discount': {}
                }
            }
        }
        logger = Mock()
        calculator = FeeRateResolver(logger=logger)
        calculator.exchange_fees['TEST'] = test_exchange
        
        # Mock config to enable platform token discount
        config = Mock(spec=ConfigAgent)
        config.outer = MagicMock()
        config.outer.sections.return_value = ['TEST_ACCOUNT']
        calculator.config = config
        
        with patch.object(
            calculator,
            '_get_account_fee_config',
            return_value={
                'vip_level': 0,
                'use_platform_token_discount': True,
                'holding_discount': False
            }
        ):
            rate = calculator.get_fee_rate('TEST', 'spot', True)
            # 0.001 * (1 - 0.25) = 0.00075
            assert rate == 0.00075

    def test_get_fee_rate_with_holding_discount(self):
        """Test get_fee_rate with holding discount."""
        test_exchange = {
            'spot': {
                'vip_levels': {
                    0: {'maker': 0.001, 'taker': 0.001}
                },
                'discounts': {
                    'platform_token_discount': 0.25,
                    'platform_token': 'TEST',
                    'holding_discount': {'discount_rate': 0.1}
                }
            }
        }
        logger = Mock()
        calculator = FeeRateResolver(logger=logger)
        calculator.exchange_fees['TEST'] = test_exchange
        
        config = Mock(spec=ConfigAgent)
        config.outer = MagicMock()
        config.outer.sections.return_value = ['TEST_ACCOUNT']
        calculator.config = config
        
        with patch.object(
            calculator,
            '_get_account_fee_config',
            return_value={
                'vip_level': 0,
                'use_platform_token_discount': False,
                'holding_discount': True
            }
        ):
            rate = calculator.get_fee_rate('TEST', 'spot', True)
            # 0.001 * (1 - 0.1) = 0.0009
            assert rate == 0.0009

    def test_get_fee_rate_with_both_discounts(self):
        """Test get_fee_rate with both platform token and holding discounts."""
        test_exchange = {
            'spot': {
                'vip_levels': {
                    0: {'maker': 0.001, 'taker': 0.001}
                },
                'discounts': {
                    'platform_token_discount': 0.25,
                    'platform_token': 'TEST',
                    'holding_discount': {'discount_rate': 0.1}
                }
            }
        }
        logger = Mock()
        calculator = FeeRateResolver(logger=logger)
        calculator.exchange_fees['TEST'] = test_exchange
        
        config = Mock(spec=ConfigAgent)
        config.outer = MagicMock()
        config.outer.sections.return_value = ['TEST_ACCOUNT']
        calculator.config = config
        
        with patch.object(
            calculator,
            '_get_account_fee_config',
            return_value={
                'vip_level': 0,
                'use_platform_token_discount': True,
                'holding_discount': True
            }
        ):
            rate = calculator.get_fee_rate('TEST', 'spot', True)
            # 0.001 * (1 - 0.25) * (1 - 0.1) = 0.000675
            assert rate == pytest.approx(0.000675)

