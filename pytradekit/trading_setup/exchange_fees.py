"""
Exchange fee data and fee rate resolver module.

This module contains complete fee structures for major exchanges (Binance, Huobi, OKX)
and provides FeeRateResolver for retrieving trading fee rates based on account configurations.
"""

from typing import Dict, Optional, Any
from pytradekit.utils.config_agent import ConfigAgent
from pytradekit.utils.exceptions import DependencyException
from pytradekit.utils.dynamic_types import ExchangeId, InstCodeType
from pytradekit.utils.static_types import FeeConfigAttribute, FeeStructureKey
from pytradekit.restful.binance_restful import BinanceClient
from pytradekit.restful.huobi_restful import HuobiClient
from pytradekit.restful.okex_restful import OkexClient
from pytradekit.trading_setup.inst_code_usage import (
    convert_inst_code_to_symbol,
    convert_inst_code_to_pair,
    convert_pair_to_symbol,
    convert_symbol_to_pair,
)
from pytradekit.trading_setup.account_usage import get_account_api


EXCHANGE_FEES: Dict[str, Dict[str, Any]] = {
    ExchangeId.BN.name: {  # Binance
        InstCodeType.SPOT.name: {
            FeeStructureKey.vip_levels.name: {
                # VIP 0: 30-day trading volume < 1,000,000 USD
                0: {FeeStructureKey.maker.name: 0.001, FeeStructureKey.taker.name: 0.001},
                # VIP 1: 30-day trading volume >= 1,000,000 USD and hold >= 25 BNB
                1: {FeeStructureKey.maker.name: 0.0009, FeeStructureKey.taker.name: 0.001},
                # VIP 2: 30-day trading volume >= 5,000,000 USD and hold >= 100 BNB
                2: {FeeStructureKey.maker.name: 0.0008, FeeStructureKey.taker.name: 0.001},
                # VIP 3: 30-day trading volume >= 20,000,000 USD and hold >= 250 BNB
                3: {FeeStructureKey.maker.name: 0.0004, FeeStructureKey.taker.name: 0.0006},
                # VIP 4: 30-day trading volume >= 50,000,000 USD and hold >= 500 BNB
                4: {FeeStructureKey.maker.name: 0.0002, FeeStructureKey.taker.name: 0.0004},
                # VIP 5: 30-day trading volume >= 100,000,000 USD and hold >= 1,000 BNB
                5: {FeeStructureKey.maker.name: 0.0001, FeeStructureKey.taker.name: 0.0003},
                # VIP 6: 30-day trading volume >= 200,000,000 USD and hold >= 2,500 BNB
                6: {FeeStructureKey.maker.name: 0.00008, FeeStructureKey.taker.name: 0.00025},
                # VIP 7: 30-day trading volume >= 500,000,000 USD and hold >= 5,000 BNB
                7: {FeeStructureKey.maker.name: 0.00005, FeeStructureKey.taker.name: 0.0002},
                # VIP 8: 30-day trading volume >= 1,000,000,000 USD and hold >= 10,000 BNB
                8: {FeeStructureKey.maker.name: 0.00002, FeeStructureKey.taker.name: 0.00015},
                # VIP 9: 30-day trading volume >= 2,000,000,000 USD and hold >= 20,000 BNB
                9: {FeeStructureKey.maker.name: 0.0, FeeStructureKey.taker.name: 0.0001},
            },
            FeeStructureKey.discounts.name: {
                FeeStructureKey.platform_token_discount.name: 0.25, 
                FeeStructureKey.platform_token.name: 'BNB',  # Platform token name
                FeeStructureKey.holding_discount.name: {}
            }
        },
        InstCodeType.PERP.name: {
            FeeStructureKey.vip_levels.name: {
                # Perpetual futures fees (similar structure to spot)
                0: {FeeStructureKey.maker.name: 0.0002, FeeStructureKey.taker.name: 0.0005},
                1: {FeeStructureKey.maker.name: 0.00016, FeeStructureKey.taker.name: 0.0004},
                2: {FeeStructureKey.maker.name: 0.00014, FeeStructureKey.taker.name: 0.00035},
                3: {FeeStructureKey.maker.name: 0.00012, FeeStructureKey.taker.name: 0.00032},
                4: {FeeStructureKey.maker.name: 0.0001, FeeStructureKey.taker.name: 0.0003},
                5: {FeeStructureKey.maker.name: 0.00008, FeeStructureKey.taker.name: 0.00025},
                6: {FeeStructureKey.maker.name: 0.00006, FeeStructureKey.taker.name: 0.00022},
                7: {FeeStructureKey.maker.name: 0.00004, FeeStructureKey.taker.name: 0.0002},
                8: {FeeStructureKey.maker.name: 0.00002, FeeStructureKey.taker.name: 0.00018},
                9: {FeeStructureKey.maker.name: 0.0, FeeStructureKey.taker.name: 0.00016},
            },
            FeeStructureKey.discounts.name: {
                FeeStructureKey.platform_token_discount.name: 0.1, 
                FeeStructureKey.platform_token.name: 'BNB',
                FeeStructureKey.holding_discount.name: {}
            }
        }
    },
    ExchangeId.HTX.name: {  # Huobi (HTX)
        InstCodeType.SPOT.name: {
            FeeStructureKey.vip_levels.name: {
                # VIP 0: Regular user
                0: {FeeStructureKey.maker.name: 0.002, FeeStructureKey.taker.name: 0.002},
                # VIP 1
                1: {FeeStructureKey.maker.name: 0.0016, FeeStructureKey.taker.name: 0.0017},
                # VIP 2
                2: {FeeStructureKey.maker.name: 0.0014, FeeStructureKey.taker.name: 0.0015},
                # VIP 3
                3: {FeeStructureKey.maker.name: 0.001, FeeStructureKey.taker.name: 0.001},
                # VIP 4
                4: {FeeStructureKey.maker.name: 0.0008, FeeStructureKey.taker.name: 0.001},
                # VIP 5
                5: {FeeStructureKey.maker.name: 0.0006, FeeStructureKey.taker.name: 0.0009},
            },
            FeeStructureKey.discounts.name: {
                FeeStructureKey.platform_token_discount.name: 0.25,  # 20% discount when using HT to pay fees
                FeeStructureKey.platform_token.name: 'HTX',  # Platform token name
                FeeStructureKey.holding_discount.name: {}
            }
        },
        InstCodeType.PERP.name: {
            FeeStructureKey.vip_levels.name: {
                # Perpetual futures fees
                0: {FeeStructureKey.maker.name: 0.0002, FeeStructureKey.taker.name: 0.0006},
                1: {FeeStructureKey.maker.name: 0.00018, FeeStructureKey.taker.name: 0.00055},
                2: {FeeStructureKey.maker.name: 0.00016, FeeStructureKey.taker.name: 0.0005},
                3: {FeeStructureKey.maker.name: 0.00014, FeeStructureKey.taker.name: 0.00045},
                4: {FeeStructureKey.maker.name: 0.00012, FeeStructureKey.taker.name: 0.0004},
                5: {FeeStructureKey.maker.name: 0.0001, FeeStructureKey.taker.name: 0.00038},
            },
            FeeStructureKey.discounts.name: {
                FeeStructureKey.platform_token_discount.name: 0.05,  # 20% discount when using HT to pay fees
                FeeStructureKey.platform_token.name: 'HT',
                FeeStructureKey.holding_discount.name: {}
            }
        }
    },
    ExchangeId.OKX.name: {  # OKX
        InstCodeType.SPOT.name: {
            FeeStructureKey.vip_levels.name: {
                # VIP 0: Regular user
                0: {FeeStructureKey.maker.name: 0.0008, FeeStructureKey.taker.name: 0.001},
                # VIP 1
                1: {FeeStructureKey.maker.name: 0.00045, FeeStructureKey.taker.name: 0.0005},
                # VIP 2
                2: {FeeStructureKey.maker.name: 0.0004, FeeStructureKey.taker.name: 0.00045},
                # VIP 3
                3: {FeeStructureKey.maker.name: 0.0003, FeeStructureKey.taker.name: 0.0004},
                # VIP 4
                4: {FeeStructureKey.maker.name: 0.0002, FeeStructureKey.taker.name: 0.00035},
                # VIP 5
                5: {FeeStructureKey.maker.name: 0.00000, FeeStructureKey.taker.name: 0.0003},
            },
            FeeStructureKey.discounts.name: {
                FeeStructureKey.platform_token_discount.name: 0,  # 20% discount when using OKB to pay fees
                FeeStructureKey.platform_token.name: None,  # Platform token name
                FeeStructureKey.holding_discount.name: {}
            }
        },
        InstCodeType.PERP.name: {
            FeeStructureKey.vip_levels.name: {
                # Perpetual futures fees
                0: {FeeStructureKey.maker.name: 0.0002, FeeStructureKey.taker.name: 0.0005},
                1: {FeeStructureKey.maker.name: 0.00015, FeeStructureKey.taker.name: 0.0004},
                2: {FeeStructureKey.maker.name: 0.0001, FeeStructureKey.taker.name: 0.00035},
                3: {FeeStructureKey.maker.name: 0.00008, FeeStructureKey.taker.name: 0.0003},
                4: {FeeStructureKey.maker.name: 0.00005, FeeStructureKey.taker.name: 0.00025},
                5: {FeeStructureKey.maker.name: 0.00002, FeeStructureKey.taker.name: 0.0002},
            },
            FeeStructureKey.discounts.name: {
                FeeStructureKey.platform_token_discount.name: 0 , # 20% discount when using OKB to pay fees
                FeeStructureKey.platform_token.name: None,
                FeeStructureKey.holding_discount.name: {}
            }
        }
    }
}


class FeeRateResolver:
    """
    Resolve trading fee rates based on exchange fee structure and account configuration.
    Only returns fee rates (no fee amount calculation).
    """

    def __init__(self, logger, config: Optional[ConfigAgent] = None):
        """
        Initialize resolver.

        Args:
            logger: logger instance for reporting issues
            config: ConfigAgent instance for reading account fee configurations
        """
        self.logger = logger
        self.config = config
        self.exchange_fees = EXCHANGE_FEES

    def _get_account_fee_config(
        self,
        exchange_id: str
    ) -> Dict[str, Any]:
        """
        Get account-specific fee configuration from config file.

        Args:
            exchange_id: Exchange ID (BN, HTX, OKX, etc.)

        Returns:
            Dictionary containing account fee configuration:
            {
                FeeConfigAttribute.vip_level.name: int,
                FeeConfigAttribute.use_platform_token_discount.name: bool,
                FeeConfigAttribute.holding_discount.name: bool
            }
        """
        default_config = {
            FeeConfigAttribute.VIP_LEVEL.name: 0,
            FeeConfigAttribute.USE_PLATFORM_TOKEN_DISCOUNT.name: False,
            FeeConfigAttribute.HOLDING_DISCOUNT.name: False
        }

        if self.config is None or self.config.outer is None:
            if self.logger:
                self.logger.info(
                    f"Fee config not provided, using default config for {exchange_id}"
                )
            return default_config

        # Section name format: {EXCHANGE_ID}_ACCOUNT
        section_name = f"{exchange_id}_ACCOUNT"

        try:
            # Check if section exists
            if section_name not in self.config.outer.sections():
                if self.logger:
                    self.logger.info(
                        f"Config section {section_name} not found, using default fee config"
                    )
                return default_config

            # Get account_id from config (used for identification/logging)
            config_account_id = ConfigAgent.get_str(
                self.config.outer,
                section_name,
                FeeConfigAttribute.ACCOUNT_ID.name
            )

            # Read configuration
            vip_level = ConfigAgent.get_int(
                self.config.outer,
                section_name,
                FeeConfigAttribute.VIP_LEVEL.name
            )
            use_platform_token_discount = ConfigAgent.get_boolean(
                self.config.outer,
                section_name,
                FeeConfigAttribute.USE_PLATFORM_TOKEN_DISCOUNT.name
            )
            holding_discount = ConfigAgent.get_boolean(
                self.config.outer,
                section_name,
                FeeConfigAttribute.HOLDING_DISCOUNT.name
            )

            result = {
                FeeConfigAttribute.VIP_LEVEL.name: vip_level,
                FeeConfigAttribute.USE_PLATFORM_TOKEN_DISCOUNT.name: use_platform_token_discount,
                FeeConfigAttribute.HOLDING_DISCOUNT.name: holding_discount
            }
            if self.logger:
                self.logger.debug(
                    f"Loaded fee config for {exchange_id} account {config_account_id}: {result}"
                )
            return result
        except Exception as exc:
            if self.logger:
                self.logger.info(
                    f"Failed to read fee config for {exchange_id}, using default config: {exc}"
                )
            return default_config

    def get_fee_rate(
        self,
        exchange_id: str,
        market_type: str,
        is_maker: bool
    ) -> float:
        """
        Get fee rate for given account and trading parameters.

        Args:
            exchange_id: Exchange ID (BN, HTX, OKX, etc.)
            market_type: Market type ('spot' or 'perp')
            is_maker: True for maker order, False for taker order

        Returns:
            Fee rate as float (e.g., 0.001 for 0.1%)

        Raises:
            DependencyException: If exchange or market type not found
        """
        # Get exchange fee structure
        if exchange_id not in self.exchange_fees:
            raise DependencyException(
                f"Exchange {exchange_id} not found in fee structure"
            )

        exchange_data = self.exchange_fees[exchange_id]

        if market_type not in exchange_data:
            raise DependencyException(
                f"Market type {market_type} not found for exchange {exchange_id}"
            )

        market_data = exchange_data[market_type]

        # Get account configuration
        account_config = self._get_account_fee_config(exchange_id)
        vip_level = account_config[FeeConfigAttribute.VIP_LEVEL.name]

        # Get base fee rate from VIP level
        vip_levels_key = FeeStructureKey.vip_levels.name
        if vip_level not in market_data[vip_levels_key]:
            # Use level 0 as default if VIP level not found
            vip_level = 0
            if vip_level not in market_data[vip_levels_key]:
                raise DependencyException(
                    f"VIP level {vip_level} not found for {exchange_id} {market_type}"
                )

        fee_type = FeeStructureKey.maker.name if is_maker else FeeStructureKey.taker.name
        base_fee_rate = market_data[vip_levels_key][vip_level][fee_type]

        # Apply discounts
        final_fee_rate = base_fee_rate

        # Apply platform token discount
        discounts_key = FeeStructureKey.discounts.name
        if account_config[FeeConfigAttribute.USE_PLATFORM_TOKEN_DISCOUNT.name]:
            platform_token_discount = market_data[discounts_key].get(
                FeeStructureKey.platform_token_discount.name,
                0.0
            )
            final_fee_rate = final_fee_rate * (1 - platform_token_discount)

        # Apply holding discount (if applicable)
        if account_config[FeeConfigAttribute.HOLDING_DISCOUNT.name]:
            holding_discount_data = market_data[discounts_key].get(
                FeeStructureKey.holding_discount.name,
                {}
            )
            if holding_discount_data:
                holding_discount_rate = holding_discount_data.get(
                    'discount_rate',
                    0.0
                )
                final_fee_rate = final_fee_rate * (1 - holding_discount_rate)

        return float(final_fee_rate)

    def _create_rest_client(self, exchange_id: str, market_type: str):
        """
        Create REST client for the exchange.
        
        Args:
            exchange_id: Exchange ID (BN, HTX, OKX, etc.)
            market_type: Market type ('spot' or 'perp')
        
        Returns:
            REST client instance or None if failed
        """
        if self.config is None:
            return None
        
        try:
            # Get account_id from config
            section_name = f"{exchange_id}_ACCOUNT"
            if self.config.outer is None or section_name not in self.config.outer.sections():
                return None
            
            account_id = ConfigAgent.get_str(
                self.config.outer,
                section_name,
                FeeConfigAttribute.ACCOUNT_ID.name
            )
            
            # Get API credentials
            try:
                api_key, api_secret, api_passphrase = get_account_api(self.config, account_id)
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"Failed to get API credentials for {account_id}: {e}")
                return None
            
            # Create client based on exchange
            if exchange_id == ExchangeId.BN.name:
                is_perp = (market_type == InstCodeType.PERP.name.lower())
                return BinanceClient(
                    logger=self.logger,
                    key=api_key,
                    secret=api_secret,
                    passphrase=api_passphrase,
                    account_id=account_id,
                    is_perp=is_perp
                )
            elif exchange_id == ExchangeId.HTX.name:
                is_swap = (market_type == InstCodeType.PERP.name.lower())
                return HuobiClient(
                    logger=self.logger,
                    key=api_key,
                    secret=api_secret,
                    account_id=account_id,
                    is_swap=is_swap
                )
            elif exchange_id == ExchangeId.OKX.name:
                is_swap = (market_type == InstCodeType.PERP.name.lower())
                return OkexClient(
                    logger=self.logger,
                    key=api_key,
                    secret=api_secret,
                    passphrase=api_passphrase,
                    account_id=account_id,
                    is_swap=is_swap
                )
            return None
        except Exception as e:
            if self.logger:
                self.logger.debug(f"Failed to create REST client for {exchange_id}: {e}")
            return None

    def get_fee_rate_from_api(
        self,
        exchange_id: str,
        inst_code: str,
        market_type: str
    ) -> Optional[Dict[str, float]]:
        """
        Get fee rate from API, fallback to static data if API fails.
        
        Args:
            exchange_id: Exchange ID (BN, HTX, OKX, etc.)
            inst_code: Instrument code (e.g., 'BTC-USDT_BN.PERP')
            market_type: Market type ('spot' or 'perp')
        
        Returns:
            Dict with FeeStructureKey.maker.name and FeeStructureKey.taker.name fee rates, or None if both API and fallback fail
        """
        # Normalize market_type to match EXCHANGE_FEES keys (SPOT or PERP)
        market_type_lower = market_type.lower()
        if market_type_lower == InstCodeType.PERP.name.lower():
            market_type_normalized = InstCodeType.PERP.name
        elif market_type_lower == InstCodeType.SPOT.name.lower():
            market_type_normalized = InstCodeType.SPOT.name
        else:
            if self.logger:
                self.logger.info(f"Unknown market type: {market_type} for {inst_code}")
            # Still try to use static data as fallback
            market_type_normalized = InstCodeType.SPOT.name if InstCodeType.SPOT.name.lower() in market_type_lower else InstCodeType.PERP.name
        
        # Try to get from API first
        client = self._create_rest_client(exchange_id, market_type)
        if client is not None:
            try:
                if exchange_id == ExchangeId.BN.name:
                    # Binance needs symbol format (BTCUSDT)
                    symbol = convert_inst_code_to_symbol(inst_code)
                    result = client.get_commission_rate(symbol)
                elif exchange_id == ExchangeId.HTX.name:
                    # HTX needs symbol format in lowercase (btcusdt)
                    symbol = convert_inst_code_to_symbol(inst_code)
                    result = client.get_commission_rate(symbol.lower())
                elif exchange_id == ExchangeId.OKX.name:
                    # OKX needs pair format (BTC-USDT)
                    pair = convert_inst_code_to_pair(inst_code)
                    inst_type = InstCodeType.PERP.name if market_type_lower == InstCodeType.PERP.name.lower() else InstCodeType.SPOT.name
                    result = client.get_commission_rate(inst_type=inst_type, inst_id=pair)
                    # OKX API returns fee rates with opposite sign, need to negate
                    if result and isinstance(result, dict):
                        if FeeStructureKey.maker.name in result:
                            result[FeeStructureKey.maker.name] = -result[FeeStructureKey.maker.name]
                        if FeeStructureKey.taker.name in result:
                            result[FeeStructureKey.taker.name] = -result[FeeStructureKey.taker.name]
                else:
                    result = None
                
                if result and isinstance(result, dict) and FeeStructureKey.maker.name in result and FeeStructureKey.taker.name in result:
                    if self.logger:
                        self.logger.debug(f"Got fee rate from API for {inst_code}: {result}")
                    return result
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"API call failed for {inst_code}, using fallback: {e}")
        
        # Fallback to static data
        try:
            maker_rate = self.get_fee_rate(exchange_id, market_type_normalized, is_maker=True)
            taker_rate = self.get_fee_rate(exchange_id, market_type_normalized, is_maker=False)
            result = {FeeStructureKey.maker.name: maker_rate, FeeStructureKey.taker.name: taker_rate}
            if self.logger:
                self.logger.debug(f"Using static fee rate for {inst_code}: {result}")
            return result
        except Exception as e:
            if self.logger:
                self.logger.info(f"Failed to get fee rate for {inst_code} from both API and static data: {e}")
            # Last resort: use VIP level from config file
            try:
                account_config = self._get_account_fee_config(exchange_id)
                vip_level = account_config[FeeConfigAttribute.VIP_LEVEL.name]
                
                if exchange_id in self.exchange_fees and market_type_normalized in self.exchange_fees[exchange_id]:
                    vip_levels = self.exchange_fees[exchange_id][market_type_normalized][FeeStructureKey.vip_levels.name]
                    # Use the configured VIP level, or fallback to 0 if not found
                    if vip_level not in vip_levels:
                        if self.logger:
                            self.logger.info(f"VIP level {vip_level} not found for {exchange_id} {market_type_normalized}, using VIP 0")
                        vip_level = 0
                    
                    vip_rates = vip_levels[vip_level]
                    result = {
                        FeeStructureKey.maker.name: vip_rates[FeeStructureKey.maker.name],
                        FeeStructureKey.taker.name: vip_rates[FeeStructureKey.taker.name]
                    }
                    if self.logger:
                        self.logger.info(f"Using fee rate from config (VIP {vip_level}) for {inst_code}: {result}")
                    return result
            except Exception as e2:
                if self.logger:
                    self.logger.info(f"Failed to get fee rate from config for {inst_code}: {e2}")
            return None
