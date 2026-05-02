"""JSON-RPC over WebSocket — `subscribe` + `auth.hello` + 事件分发。

使用方式：

    async with WSClient() as ws:
        await ws.auth_hello(principal=..., auth_info=...)   # 私有频道才需要
        await ws.subscribe(["book.6.10", "klines.6.10.1m"])
        async for event in ws:
            json.dumps(event, ...)  # 每行一个事件

关键约定（曾经把 frontend 卡了两个月）：

- subscribe 的 param 键是 `channels`，**不是** `topics`。
- 服务端推送的字段是 `params.channel`，**不是** `params.topic`。
- 私有频道 (`fills.me` / `orders.me`) 必须先发 `auth.hello`，否则订阅
  返回 `AUTH_SESSION_REQUIRED`。

`auth.hello` 的签名 `path` 字段是 `/v1/ws`（不去 `/v1` 前缀）— WS handler
读取的是 hardcoded literal，与 REST 的 `Router::nest` strip 行为不同。
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
    """轻量 JSON-RPC 客户端，专为脚本里 long-running 订阅设计。

    - `id` 单调递增。
    - `subscribe()` 阻塞等待对应 `id` 的 ack；其它消息暂存到内部队列。
    - 迭代器只产出 server-pushed notification（`method` 存在且 `id` 不存在）。
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
        # `ping_interval` 让 websockets 库定期发 control-frame ping，
        # 服务端 60s 空闲会断开 — 我们用 25s 间隔保活。
        self._ws = await websockets.connect(self.url, ping_interval=25, ping_timeout=10)
        self._reader_task = asyncio.create_task(self._reader())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            # 等 reader 真正退出再关 socket，避免长 async 程序里留 unjoined task
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

    # --- 高层 API -----------------------------------------------------------

    async def auth_hello(
        self,
        *,
        principal: str,
        auth_info: Dict,
        nonce: int,
        timestamp: Optional[int] = None,
        actor: Optional[str] = None,
    ) -> Dict:
        """私有频道前置握手。签名材料里 `method=WS_HELLO`、`path=/v1/ws`。

        params 必须把签名材料的全部七个字段都带上 — asyncapi 只把
        `principal/nonce/timestamp/signature` 标 required，但 MAIN-SPEC §4.3
        的示例同时给出 `method/path/query/bodyHash`，这是服务端做 ecrecover
        时重建 EIP-712 摘要的必要输入。如果服务端是从 params 读这四个值
        而不是硬编码常量，缺字段就会一律 `AUTH_SIGNATURE_INVALID`。
        """
        ts = timestamp if timestamp is not None else int(time.time())
        # WS handshake 的 path 走完整 `/v1/ws`，与 REST POST-strip 行为不同
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
        """订阅一组频道。**param 键是 `channels`** — 不是 `topics`。

        重要：JSON-RPC 顶层永远成功（无 `error` 字段），per-channel 的拒绝
        通过 `result.failed[]` 上报，常见 code:
            CHANNEL_NOT_FOUND       — 频道字符串不匹配任何 8 个支持模式
            CHANNEL_INVALID_FIELD   — 模式对了但字段非法
            AUTH_SESSION_REQUIRED   — 未做 auth.hello 就订阅 fills.me 等
            SEQUENCE_TOO_OLD        — since_sequence 超出 WAL 保留窗口
            RATE_LIMIT_EXCEEDED     — 单连接订阅速率限制

        默认所有 failed 都触发 `WSError`；调用方传 `allow_partial=True` 只
        关心成功订阅时，则把 failed 放到返回 dict 让上层自己决定。
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
        """产出 server-pushed notifications（无 `id` 字段）。"""
        while not self._closed:
            try:
                yield await self._notif_queue.get()
            except asyncio.CancelledError:
                break

    # --- 底层送任意消息（用于 batch 等扩展） --------------------------------

    async def send_raw(self, envelope: Dict) -> None:
        await self._ws.send(json.dumps(envelope))


def emit_event(event: Dict) -> None:
    """把订阅事件序列化成单行 JSON 写到 stdout（JSON-Lines 流）。

    脚本入口 `for event in ws:` 之后直接调用本函数 — 调用方 agent 可
    以增量解析，不必等流结束。
    """
    print(json.dumps(event, separators=(",", ":")), flush=True)
