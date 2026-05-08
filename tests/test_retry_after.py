"""Retry-After header parsing — delta-seconds + HTTP-date + bad inputs.

The auto-retry on 429 must honor the server's Retry-After, but cannot trust
a misconfigured server to park the client in the next century — `cap`
provides an upper bound.
"""

from datetime import datetime, timedelta, timezone

import pytest

from lib.govnet_lib import _parse_retry_after


def test_delta_seconds_numeric():
    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after("0.5") == 0.5


def test_delta_seconds_with_whitespace():
    assert _parse_retry_after(" 10 ") == 10.0


def test_negative_clamped_to_zero():
    assert _parse_retry_after("-3") == 0.0


def test_above_cap_clamped():
    # The server emits a nonsense 99999 seconds; this must be capped at 60
    assert _parse_retry_after("99999") == 60.0
    assert _parse_retry_after("99999", cap=10.0) == 10.0


def test_missing_header_returns_default():
    assert _parse_retry_after(None) == 1.0
    assert _parse_retry_after("") == 1.0
    assert _parse_retry_after(None, default=5.0) == 5.0


def test_garbage_returns_default():
    assert _parse_retry_after("not-a-number") == 1.0
    assert _parse_retry_after("abc def", default=2.0) == 2.0


def test_http_date_in_future_parsed():
    future = datetime.now(timezone.utc) + timedelta(seconds=20)
    # RFC 7231 § 7.1.1.1 IMF-fixdate
    header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    parsed = _parse_retry_after(header)
    # Allow ±2s drift (time elapsed during parsing)
    assert 18.0 <= parsed <= 22.0


def test_http_date_in_past_clamped_to_zero():
    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    header = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert _parse_retry_after(header) == 0.0


def test_http_date_too_far_future_capped():
    far_future = datetime.now(timezone.utc) + timedelta(days=365)
    header = far_future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert _parse_retry_after(header) == 60.0
