"""Mail sender identity comes from explicit or deployment configuration."""
import pytest

from pytradekit.notifiers.mail_util import SendMail


def test_explicit_sender_overrides_environment(monkeypatch):
    monkeypatch.setenv('SMTP_FROM_EMAIL', 'environment@example.com')
    mail = SendMail('test-secret', from_email='sender@example.com')
    assert mail.FROM_EMAIL == 'sender@example.com'


def test_sender_is_read_from_environment(monkeypatch):
    monkeypatch.setenv('SMTP_FROM_EMAIL', 'sender@example.com')
    assert SendMail('test-secret').FROM_EMAIL == 'sender@example.com'


def test_missing_sender_fails_before_sending(monkeypatch):
    monkeypatch.delenv('SMTP_FROM_EMAIL', raising=False)
    with pytest.raises(KeyError, match='SMTP_FROM_EMAIL'):
        SendMail('test-secret')
