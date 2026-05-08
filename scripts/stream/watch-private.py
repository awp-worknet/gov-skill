#!/usr/bin/env python3
"""Authenticated WebSocket session + subscribe to any asyncapi channel — not just private streams.

    watch-private.py                         # defaults to fills.me + orders.me
    watch-private.py --channels fills.me,orders.me,account
    watch-private.py --channels phase,book.6.11   # public channels can be mixed in too

All 8 asyncapi-supported channels:
    public            book.{m}.{wn}
                      klines.{m}.{wn}.{interval}
                      phase
                      reports
                      comments
    auth required     fills.me
                      orders.me
                      account              # changes to your own PrincipalCurrentState

Despite the script being named watch-private, it is essentially "auth.hello
first then subscribe" — once a session authenticates, the subscribe list
can mix in any public channel. If you only want public channels and don't
want to spend a nonce and a wallet signature, use watch-book / watch-klines
/ watch-phase instead.

Signing material: method=WS_HELLO, path=/v1/ws — note that the WS handshake's
path **does not** strip the `/v1` prefix (unlike REST POST-strip); see
SKILL.md "Critical contract gotchas" for why.
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
