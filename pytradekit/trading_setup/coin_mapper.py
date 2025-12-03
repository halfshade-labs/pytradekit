"""
author: Willem Wang
data: 2025-04-08
description: Coin mapper, 存放交易所对coin的特殊命名，其中key为常规命名，value为交易所特殊命名
"""
from pytradekit.utils.dynamic_types import ExchangeId


class CoinMapper:
    coin_mapping = {
        ExchangeId.BN.name: {'BTTC': 'BTT'},
        ExchangeId.BFX.name: {'APENFT': 'NFT', 'HTXDAO': 'HTX'},
        ExchangeId.KRK.name: {'APENFT': 'NFT', 'XBT': 'BTC'}
    }

    @classmethod
    def convert_to_standard(cls, exchange_id: str, exchange_coin: str) -> str:
        if exchange_id in cls.coin_mapping and exchange_coin in cls.coin_mapping[exchange_id]:
            return cls.coin_mapping[exchange_id][exchange_coin]
        return exchange_coin

    @classmethod
    def convert_to_exchange(cls, exchange_id: str, standard_coin: str) -> str:
        if exchange_id not in cls.coin_mapping:
            return standard_coin
        for exchange_coin, standard_coin0 in cls.coin_mapping[exchange_id].items():
            if standard_coin == standard_coin0:
                return exchange_coin
        return standard_coin

    @classmethod
    def convert_batch_to_standard(cls, exchange_id: str, exchange_coins: list) -> list:
        return [cls.convert_to_standard(exchange_id, coin) for coin in exchange_coins]

    @classmethod
    def convert_batch_to_exchange(cls, exchange_id: str, standard_coins: list) -> list:
        return [cls.convert_to_exchange(exchange_id, coin) for coin in standard_coins]
