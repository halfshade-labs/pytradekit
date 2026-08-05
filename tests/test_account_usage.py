"""get_account_api must tolerate accounts without a passphrase.

Passphrase is exchange-specific (OKX has one; BN/HTX do not). The previous
`config.private[account_id + '_passphrase']` raised KeyError for BN/HTX, so
exchange_fees._create_rest_client returned None and those exchanges silently
fell back to static fees instead of live API fees.
"""
from types import SimpleNamespace

import pytest

import pytradekit.trading_setup.account_usage as account_usage
from pytradekit.utils.exceptions import DataTypeException


def _config(private):
    return SimpleNamespace(private=private)


def test_missing_passphrase_returns_none(monkeypatch):
    monkeypatch.setattr(account_usage, "encrypt_decrypt", lambda value, _mode: value)
    config = _config({"HTX_000_key": "k", "HTX_000_secret": "s"})

    key, secret, passphrase = account_usage.get_account_api(config, "HTX_000")

    assert (key, secret, passphrase) == ("k", "s", None)


def test_present_passphrase_is_decrypted(monkeypatch):
    monkeypatch.setattr(account_usage, "encrypt_decrypt", lambda value, _mode: value)
    config = _config({"OKX_000_key": "k", "OKX_000_secret": "s", "OKX_000_passphrase": "p"})

    assert account_usage.get_account_api(config, "OKX_000") == ("k", "s", "p")


def test_empty_passphrase_treated_as_none(monkeypatch):
    monkeypatch.setattr(account_usage, "encrypt_decrypt", lambda value, _mode: value)
    config = _config({"BN_000_key": "k", "BN_000_secret": "s", "BN_000_passphrase": ""})

    _key, _secret, passphrase = account_usage.get_account_api(config, "BN_000")

    assert passphrase is None


def test_missing_required_key_raises_data_type_exception(monkeypatch):
    # #141: a missing key/secret must surface a domain exception, not a raw
    # KeyError, consistent with the passphrase handling.
    monkeypatch.setattr(account_usage, "encrypt_decrypt", lambda value, _mode: value)
    config = _config({"BN_000_secret": "s"})  # no _key

    with pytest.raises(DataTypeException):
        account_usage.get_account_api(config, "BN_000")
