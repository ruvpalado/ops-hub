"""Log redaction. The one gate every log line passes before anything reaches
the console or the log file: token values, authorization headers, OAuth codes
and states, secrets and passwords never survive into emitted records.

Over-redaction is deliberately preferred over a leak: masking an innocent
value costs nothing, leaking a refresh token costs a mailbox.

The redaction is a value-level scrub: the key name stays, the value becomes
[redacted]. Keeping the key makes the logs still greppable ("which call leaked
a token?") without ever exposing the secret itself.
"""

from __future__ import annotations

import logging
import re

# value characters that actually occur in tokens/codes: base64url, hex, dots,
# slashes, plus, minus, underscore, tilde
# The vocabulary of "this is a secret" — shared with the audit writer, which
# drops these keys from metadata before persisting.
SECRET_KEY_NAMES = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "password",
        "token_key",
        "code",
        "state",
        "authorization",
        "token",
        "secret",
        "credentials",
        "set-cookie",
    }
)

_TOKEN = r"[A-Za-z0-9._~+/=-]{6,}"
_QUOTE = r"[\"']?"
_KEYS = "|".join(sorted(SECRET_KEY_NAMES))

# key/value form: access_token=..., "access_token":"...", secret: "..."
_PATTERN_KV = re.compile(
    rf"(?P<key>{_KEYS})"
    rf"\s*(?P<keyquote>{_QUOTE})\s*"
    rf"(?P<sep>=|:{_QUOTE}\s*)\s*"
    rf"(?!Bearer\b)"
    rf"(?P<val>{_TOKEN})",
    re.IGNORECASE,
)

# header form: 'Authorization': 'Bearer <value>' / Authorization: Bearer <value>
_PATTERN_HDR = re.compile(
    rf"(?P<kw>Authorization|Bearer)\s*{_QUOTE}\s*[:=]\s*{_QUOTE}\s*"
    rf"(?:Bearer\s+)?(?P<val>{_TOKEN})",
    re.IGNORECASE,
)

_REDACTED = "[redacted]"


def redact(text: str) -> str:
    """Return text with every recognized secret value replaced."""

    def mask_kv(match: re.Match[str]) -> str:
        return match.group("key") + (match.group("keyquote") or "") + match.group("sep") + _REDACTED

    def mask_hdr(match: re.Match[str]) -> str:
        # keep the field name ("Authorization:"), kill the value
        prefix = match.string[: match.start("val")].rstrip()
        return prefix + " " + _REDACTED

    once = _PATTERN_KV.sub(mask_kv, text)
    return _PATTERN_HDR.sub(mask_hdr, once)


class RedactionFilter(logging.Filter):
    """logging.Filter that scrubs every emitted message, arguments and traceback.

    Attached in settings.LOGGING via a dictConfig class reference, so it covers
    every record regardless of which logger produced it — application loggers,
    Django internals, our exceptions. A record that survives with an unruly
    argument is still scrubbed here because we replace the message with its
    interpolated, redacted form.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # Unformattable record: still scrub raw fields, then let the
            # formatter fail the same way it would have anyway.
            message = record.msg if isinstance(record.msg, str) else ""
        record.msg = redact(message)
        record.args = ()
        for field in ("exc_text", "stack_info"):
            value = getattr(record, field, None)
            if isinstance(value, str):
                setattr(record, field, redact(value))
        return True