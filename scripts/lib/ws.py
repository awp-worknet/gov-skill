"""JSON-RPC over WebSocket — `subscribe` + `auth.hello` + event dispatch.

Usage:

    async with WSClient() as ws:
        await ws.auth_hello(principal=..., auth_info=...)   # only needed for private channels
        await ws.subscribe(["book.6.10", "klines.6.10.1m"])
        async for event in ws:
            json.dumps(event, ...)  # one event per line

Key conventions (these once blocked frontend for two months):

- The subscribe param key is `channels`, **not** `topics`.
- Server-pushed messages use `params.channel`, **not** `params.topic`.
- Private channels (`fills.me` / `orders.me`) require `auth.hello` first;
  otherwise the subscription returns `AUTH_SESSION_REQUIRED`.

`auth.hello`'s signing material has `path` set to `/v1/ws` (the `/v1` prefix
is *not* stripped) — the WS handler reads a hardcoded literal, unlike REST
where `Router::nest` strips the prefix.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
from typing import Any, AsyncIterator, Dict, List, Optional

import websockets

from .canonical import eip712_body_hash
from .sign import build_emg_request_typed_data, wallet_sign_typed_data


WS_URL = os.environ.get("GOVNET_WS_URL", "wss://api.gov.works/v1/ws")


def _enforce_wss(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "wss":
        raise RuntimeError(
            f"{url}: skill refuses plaintext ws:// — set GOVNET_WS_URL to a wss:// URL"
        )


class WSError(RuntimeError):
    pass


class WSClient:
    """Lightweight JSON-RPC client, designed for long-running subscriptions in scripts.

    - `id` is monotonically increasing.
    - `subscribe()` blocks waiting for the matching `id` ack; other messages go to an internal queue.
    - The iterator only yields server-pushed notifications (where `method` is present and `id` is not).
    """

    def __init__(self, url: Optional[str] = None) -> None:
        self.url = url or WS_URL
        _enforce_wss(self.url)
        self._ws = None
        self._next_id = 1
        self._pending: Dict[int, asyncio.Future] = {}
        self._notif_queue: asyncio.Queue = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        self._closed = False

    async def __aenter__(self) -> "WSClient":
        # `ping_interval` lets the websockets library send periodic control-frame
        # pings; the server disconnects after 60s of idle, so we keep alive
        # with a 25s interval.
        self._ws = await websockets.connect(self.url, ping_interval=25, ping_timeout=10)
        self._reader_task = asyncio.create_task(self._reader())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            # Wait for the reader to actually exit before closing the socket,
            # to avoid leaving an unjoined task in long-running async programs
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            await self._ws.close()

    async def _reader(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "id" in msg and msg["id"] in self._pending:
                    fut = self._pending.pop(msg["id"])
                    if not fut.done():
                        fut.set_result(msg)
                else:
                    await self._notif_queue.put(msg)
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(WSError("connection closed"))
            self._pending.clear()

    def _take_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    async def _call(self, method: str, params: Optional[Dict] = None, *, timeout: float = 10.0) -> Any:
        if self._closed or self._ws is None:
            raise WSError("ws not connected")
        rpc_id = self._take_id()
        envelope: Dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            envelope["params"] = params
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rpc_id] = fut
        await self._ws.send(json.dumps(envelope))
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(rpc_id, None)
            raise WSError(f"{method} timed out after {timeout}s") from e
        if "error" in resp:
            err = resp["error"]
            raise WSError(
                f"{method} → {err.get('code')}: {err.get('message', '')}"
            )
        return resp.get("result")

    # --- High-level API -----------------------------------------------------

    async def auth_hello(
        self,
        *,
        principal: str,
        auth_info: Dict,
        nonce: int,
        timestamp: Optional[int] = None,
        actor: Optional[str] = None,
    ) -> Dict:
        """Pre-handshake for private channels. Signing material has `method=WS_HELLO`, `path=/v1/ws`.

        params must include all seven fields of the signing material — asyncapi
        only marks `principal/nonce/timestamp/signature` as required, but
        MAIN-SPEC §4.3's example also shows `method/path/query/bodyHash`,
        which are necessary inputs the server uses to rebuild the EIP-712
        digest during ecrecover. If the server reads those four values from
        params instead of using hardcoded constants, omitting them will
        always produce `AUTH_SIGNATURE_INVALID`.
        """
        ts = timestamp if timestamp is not None else int(time.time())
        # WS handshake's path is the full `/v1/ws`, unlike REST POST-strip behavior
        typed_data = build_emg_request_typed_data(
            principal=principal,
            method="WS_HELLO",
            path="/v1/ws",
            query="",
            body=b"",
            nonce=nonce,
            timestamp=ts,
            auth_info=auth_info,
        )
        signature = wallet_sign_typed_data(typed_data)
        params: Dict[str, Any] = {
            "principal": principal,
            "method": "WS_HELLO",
            "path": "/v1/ws",
            "query": "",
            "bodyHash": "0x" + "00" * 32,
            "nonce": nonce,
            "timestamp": ts,
            "signature": signature,
        }
        if actor is not None and actor.lower() != principal.lower():
            params["actor"] = actor
        return await self._call("auth.hello", params)

    async def subscribe(
        self,
        channels: List[str],
        *,
        since_sequence: Optional[int] = None,
        allow_partial: bool = False,
    ) -> Dict:
        """Subscribe to a set of channels. **The param key is `channels`** — not `topics`.

        Important: the JSON-RPC top level always succeeds (no `error` field);
        per-channel rejections are reported via `result.failed[]`. Common codes:
            CHANNEL_NOT_FOUND       — channel string didn't match any of the 8 supported patterns
            CHANNEL_INVALID_FIELD   — pattern matched but a field was invalid
            AUTH_SESSION_REQUIRED   — subscribed to fills.me etc. without auth.hello
            SEQUENCE_TOO_OLD        — since_sequence is outside the WAL retention window
            RATE_LIMIT_EXCEEDED     — single-connection subscription rate limit

        By default any failed item triggers `WSError`; passing `allow_partial=True`
        when the caller only cares about successful subscriptions returns the
        failed list in the response so the caller can decide.
        """
        params: Dict[str, Any] = {"channels": list(channels)}
        if since_sequence is not None:
            params["since_sequence"] = int(since_sequence)
        result = await self._call("subscribe", params)
        failed = (result or {}).get("failed") or []
        if failed and not allow_partial:
            details = "; ".join(
                f"{f.get('channel', '?')}: "
                f"{f.get('error', {}).get('code', 'UNKNOWN')} "
                f"{f.get('error', {}).get('message', '')}".rstrip()
                for f in failed
            )
            raise WSError(f"subscribe failed for {len(failed)} channel(s): {details}")
        return result or {}

    async def unsubscribe(self, channels: List[str]) -> Dict:
        return await self._call("unsubscribe", {"channels": list(channels)})

    async def ping(self) -> Dict:
        return await self._call("ping", {}, timeout=5.0)

    async def __aiter__(self) -> AsyncIterator[Dict]:
        """Yields server-pushed notifications (those without an `id` field)."""
        while not self._closed:
            try:
                yield await self._notif_queue.get()
            except asyncio.CancelledError:
                break

    # --- Low-level send for arbitrary messages (for batch and similar extensions) ---

    async def send_raw(self, envelope: Dict) -> None:
        await self._ws.send(json.dumps(envelope))


def emit_event(event: Dict) -> None:
    """Serialize a subscription event as a single line of JSON to stdout (JSON-Lines stream).

    A script's entry point typically calls this directly inside `for event in ws:`
    — the calling agent can incrementally parse and doesn't need to wait for
    the stream to close.
    """
    print(json.dumps(event, separators=(",", ":")), flush=True)
