"""Retry-After header 解析 — delta-seconds + HTTP-date + 异常输入。

429 响应的自动重试要 honor 服务端的 Retry-After，但又不能信任 misconfig
的服务端把客户端挂到下个世纪 —— `cap` 上限保护。
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
    # 服务端瞎发 99999 秒，要被 cap=60 截断
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
    # 允许 ±2s 漂移（解析期间过的时间）
    assert 18.0 <= parsed <= 22.0


def test_http_date_in_past_clamped_to_zero():
    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    header = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert _parse_retry_after(header) == 0.0


def test_http_date_too_far_future_capped():
    far_future = datetime.now(timezone.utc) + timedelta(days=365)
    header = far_future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert _parse_retry_after(header) == 60.0
