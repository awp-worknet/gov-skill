#!/usr/bin/env python3
"""认证 WebSocket 会话 + 订阅任意 asyncapi 频道 —— 不仅限私有流。

    watch-private.py                         # 默认 fills.me + orders.me
    watch-private.py --channels fills.me,orders.me,account
    watch-private.py --channels phase,book.6.11   # 也可以混订公开频道

asyncapi 支持的全部 8 个频道：
    public            book.{m}.{wn}
                      klines.{m}.{wn}.{interval}
                      phase
                      reports
                      comments
    auth required     fills.me
                      orders.me
                      account              # 自己的 PrincipalCurrentState 变更

虽然脚本叫 watch-private，本质上是「先 auth.hello 再订阅」—— 一旦 session
认证成功，订阅清单可以混入任意公开频道。如果只想订公开频道、不想出 nonce
和签名 wallet，请改用 watch-book / watch-klines / watch-phase 之一。

签名材料：method=WS_HELLO, path=/v1/ws — 注意 WS handshake 的 path **不**
去 `/v1` 前缀（与 REST POST-strip 不同），原因见 SKILL.md "Critical
contract gotchas"。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, get_auth_info, wallet_address  # noqa: E402
from lib import nonce as nonce_mod  # noqa: E402
from lib.ws import WSClient, emit_event  # noqa: E402


async def run(channels: list) -> int:
    auth_info = get_auth_info()
    principal = wallet_address()
    n = nonce_mod.next_nonce(principal)
    async with WSClient() as ws:
        await ws.auth_hello(principal=principal, auth_info=auth_info, nonce=n)
        await ws.subscribe(channels)
        async for event in ws:
            emit_event(event)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--channels",
        default="fills.me,orders.me",
        help=(
            "comma-separated; auth-required: fills.me, orders.me, account. "
            "public (allowed on authed sessions too): "
            "book.{m}.{wn}, klines.{m}.{wn}.{interval}, phase, reports, comments"
        ),
    )
    args = ap.parse_args()
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]

    try:
        return asyncio.run(run(channels))
    except EmgError as e:
        return emit_error(e)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
