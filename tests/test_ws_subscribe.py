"""Tests for WSClient.subscribe's failed[] handling — does not actually open a socket; monkeypatches _call."""

import asyncio

import pytest

from lib.ws import WSClient, WSError


class _StubClient(WSClient):
    """Overrides only `_call`, bypassing the real WebSocket."""

    def __init__(self, response):
        # Skip super().__init__ to avoid triggering _enforce_wss
        self._response = response
        self._closed = False
        self._next_id = 1
        self._pending = {}
        self._notif_queue = None
        self._reader_task = None
        self._ws = "stub"
        self.url = "wss://test"

    async def _call(self, method, params=None, *, timeout=10.0):
        return self._response


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_subscribe_raises_on_any_failed_channel():
    stub = _StubClient(
        {
            "subscribed": ["book.6.11"],
            "failed": [
                {
                    "channel": "fills.me",
                    "error": {"code": "AUTH_SESSION_REQUIRED", "message": "send auth.hello first"},
                }
            ],
        }
    )

    async def go():
        await stub.subscribe(["book.6.11", "fills.me"])

    with pytest.raises(WSError, match=r"AUTH_SESSION_REQUIRED"):
        _run(go())


def test_subscribe_succeeds_when_all_channels_ok():
    stub = _StubClient({"subscribed": ["book.6.11", "phase"], "failed": []})

    async def go():
        return await stub.subscribe(["book.6.11", "phase"])

    result = _run(go())
    assert result["subscribed"] == ["book.6.11", "phase"]


def test_subscribe_allow_partial_returns_failed_inline():
    stub = _StubClient(
        {
            "subscribed": ["book.6.11"],
            "failed": [{"channel": "klines.x", "error": {"code": "CHANNEL_INVALID_FIELD", "message": ""}}],
        }
    )

    async def go():
        return await stub.subscribe(["book.6.11", "klines.x"], allow_partial=True)

    result = _run(go())
    # No raise — the caller inspects failed itself
    assert result["failed"][0]["error"]["code"] == "CHANNEL_INVALID_FIELD"
    assert result["subscribed"] == ["book.6.11"]


def test_subscribe_handles_missing_failed_field():
    # The server may omit the failed: [] field
    stub = _StubClient({"subscribed": ["phase"]})

    async def go():
        return await stub.subscribe(["phase"])

    result = _run(go())
    assert result["subscribed"] == ["phase"]
