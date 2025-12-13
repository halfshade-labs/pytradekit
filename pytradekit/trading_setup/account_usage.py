from pytradekit.utils.dynamic_types import RunningMode
from pytradekit.utils.tools import encrypt_decrypt



class MonitorAccount:
    BN = ['BN_000']
    HTX = ['HTX_008', 'HTX_009', 'HTX_010', 'HTX_011', 'HTX_012', 'HTX_013', 'HTX_014', 'HTX_015', 'HTX_016', 'HTX_017',
           'HTX_018', 'HTX_019', 'HTX_020', 'HTX_021', 'HTX_022']
    OKX = ['OKX_026', 'OKX_028', 'OKX_027', 'OKX_024', 'OKX_025']
    BFX = ['BFX_003', 'BFX_004', 'BFX_005', 'BFX_006']
    KRK = ['KRK_001', 'KRK_002', 'KRK_003', 'KRK_004']
    KC = ['KC_001', 'KC_002', 'KC_003']
    GT = ['GT_001', 'GT_002', 'GT_003']
    BBT = ['BBT_001', 'BBT_002']
    MXC = ['MXC_005']
    BGT = ['BGT_001']
    BMT = ['BMT_002']
    MCO = ['MCO_001']
    EMO = ['EMO_001']
    WOO = ['WOO_001']
    HKG = ['HKG_001']


class ExchangeApi:

    def __init__(self, api_key, api_secret, passphrase, tag):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.tag = tag


def get_account_api(config, account_id):
    api_key = encrypt_decrypt(config.private[account_id + '_key'], 'decrypt')
    api_secret = encrypt_decrypt(config.private[account_id + '_secret'], 'decrypt')
    api_passphrase = encrypt_decrypt(config.private[account_id + '_passphrase'], 'decrypt')
    return api_key, api_secret, api_passphrase
