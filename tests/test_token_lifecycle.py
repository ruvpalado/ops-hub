"""Redaction: secrets never reach the logs. Covers the token lifecycle and the
OAuth handshake, the two places with something worth leaking."""

import logging

from core.redaction import redact, RedactionFilter


def test_redaction_access_token_value():
    line = 'POST /oauth2/v4/token 200 218ms body access_token=ya29.A0b1C2D3e4 client_id=ok'
    out = redact(line)
    assert "ya29.A0b1C2D3e4" not in out
    assert "access_token=[redacted]" in out


def test_redaction_bearer_header():
    line = "GET /gmail/v1/users/me/messages 200 41ms 'Authorization': 'Bearer ya29.zz99xx88AA'"
    out = redact(line)
    assert "ya29.zz99xx88AA" not in out
    assert "[redacted]" in out


def test_redaction_refresh_and_client_secret():
    line = '{"refresh_token":"1//0abcDEF","client_secret":"GOCSPX-aaaabbbbcccc"}'
    out = redact(line)
    assert "1//0abcDEF" not in out
    assert "GOCSPX-aaaabbbbcccc" not in out
    assert "refresh_token" in out
    assert "client_secret" in out
    assert "[redacted]" in out


def test_redaction_logging_filter_scrubs_records(caplog):
    logger = logging.getLogger("opshub.token_lifecycle_test")
    logger.addFilter(RedactionFilter())
    with caplog.at_level(logging.INFO, logger="opshub.token_lifecycle_test"):
        logger.info("refresh ok access_token=%s", "ya29.xXxXxXxXxXxXxXx")
        logger.info("code=%s", "4/0Aabc_DEfg-hIjK")
    out = caplog.text
    assert "ya29.xXxXxXxXxXxXxXx" not in out
    assert "4/0Aabc_DEfg-hIjK" not in out
    assert "access_token=[redacted]" in out
    assert "code=[redacted]" in out