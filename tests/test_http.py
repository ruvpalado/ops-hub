"""The one HTTP primitive, and what it is obliged to carry out of a failure.

Four tests. They exist because `_http` is the single place this project touches
the network, and the two things it added recently are both easy to drop without
a symptom in any other test:

* **`json_body`.** The token endpoint speaks form encoding and the Gmail/Calendar
  REST APIs speak JSON. Sending a Gmail POST form-encoded does not raise; it
  returns a 400 with a body nobody reads, which reads as "Gmail is down" for an
  afternoon. `test_a_json_body_is_sent_as_json` asserts the header and the bytes.

* **`Retry-After` on a 429.** §6.2 requires the mailbox deferral to be Google's
  own number, and that number exists only in a response header. If `_http` drops
  it, `google_sync._defer` falls back to a constant — every test that checks a
  429 still passes, because the fake raises `RateLimited` directly and never
  goes through HTTP at all. This is the gap that made the whole quota path
  unprovable: the deferral was tested against a value no production code could
  produce.

* **410 must not be swept up with it.** Only a 410 nulls the history cursor, so
  the translation keys on the status code and not on "Google said no".

Nothing here touches the network: `urlopen` is patched. The patch target is
`urllib.request.urlopen` rather than `_http` itself, because a test that mocks
the thing it is asserting about proves nothing.
"""

import io
import json
import urllib.error
from unittest import mock

import pytest

from accounts.services import google_oauth
from accounts.services.google_oauth import OAuthError


def _response(payload, *, response_headers=None):
    """A urlopen result with the two attributes `_http` reads."""

    class _R:
        headers = response_headers or {}
        status = 200

        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _R()


def _http_error(status, *, headers=None, body=b""):
    return urllib.error.HTTPError(
        url="https://example.test/x",
        code=status,
        msg="",
        hdrs=headers or {},
        fp=io.BytesIO(body),
    )


def test_a_json_body_is_sent_as_json_not_form_encoded():
    """Gmail rejects a form-encoded POST; it does not say so usefully."""
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["headers"] = dict(request.headers)
        captured["data"] = request.data
        return _response({"id": "m1"})

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        google_oauth._http(
            "POST",
            "https://gmail.googleapis.com/x",
            json_body={"raw": "abc", "threadId": "t1"},
            access_token="tok",
        )

    assert "application/json" in captured["headers"]["Content-type"]
    assert json.loads(captured["data"]) == {"raw": "abc", "threadId": "t1"}
    assert captured["headers"]["Authorization"] == "Bearer tok"


def test_a_429_carries_the_retry_after_header_on_the_exception():
    """The one value §6.2's deferral depends on, and it exists nowhere else."""
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=_http_error(429, headers={"Retry-After": "900"}),
    ):
        with pytest.raises(OAuthError) as caught:
            google_oauth._http("GET", "https://gmail.googleapis.com/x", access_token="tok")

    assert caught.value.code == "http_429"
    assert caught.value.retry_after == 900


@pytest.mark.parametrize("raw, expected", [("900", 900), ("0", 0), (None, None), ("Wed, 21 Oct 2026 07:28:00 GMT", None)])
def test_only_the_delta_seconds_form_of_retry_after_is_honoured(raw, expected):
    """A date form needs a clock this process does not share, so it is None.

    Returning a number for the date form would be a fabricated answer; None makes
    the caller fall back to its own bounded backoff, which is defensible and a
    wrong number here is not.
    """
    headers = {"Retry-After": raw} if raw else {}
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(429, headers=headers)):
        with pytest.raises(OAuthError) as caught:
            google_oauth._http("GET", "https://gmail.googleapis.com/x")

    assert caught.value.retry_after == expected


def test_a_410_does_not_become_a_rate_limit_and_a_404_does_not_become_a_410():
    """Only the retention-window status nulls the cursor. Same family, different
    consequences, so the translation keys on the status and not on the shape of
    the failure."""
    for status, expected_retry in ((410, None), (404, None)):
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(status)):
            with pytest.raises(OAuthError) as caught:
                google_oauth._http("GET", "https://gmail.googleapis.com/x")

        assert caught.value.code == f"http_{status}"
        assert caught.value.retry_after == expected_retry


def test_a_json_error_body_still_names_the_reason():
    """The token leg reads `error` to tell invalid_grant from http_5xx."""
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=_http_error(400, body=json.dumps({"error": "invalid_grant"}).encode()),
    ):
        with pytest.raises(OAuthError) as caught:
            google_oauth._http("POST", "https://oauth2.googleapis.com/token", data={"a": "b"})

    assert caught.value.code == "invalid_grant"


def test_passing_both_encodings_is_a_programming_error():
    """Silent precedence here would be a body that is not what the caller meant."""
    with pytest.raises(ValueError):
        google_oauth._http("POST", "https://example.test", data={"a": "b"}, json_body={"a": "b"})