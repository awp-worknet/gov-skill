#!/usr/bin/env python3
"""订阅 book.{m}.{wn}（或 book.{wn} — 服务端两种命名都支持）。

    watch-book.py --market 6 --worknet 11
    watch-book.py --channel "book.6.11"   # 直接传 raw channel id

每条事件按 JSON-Lines 输出到 stdout。BookDelta 中 `new_quantity` 是
**绝对** 量，0 = 该 price level 已清空 — 不要把它当增量。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.ws import WSClient, emit_event  # noqa: E402


async def run(channels: list) -> int:
    async with WSClient() as ws:
        await ws.subscribe(channels)
        async for event in ws:
            emit_event(event)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", type=int)
    ap.add_argument("--worknet", type=int)
    ap.add_argument("--channel", help="raw channel id; overrides --market/--worknet")
    args = ap.parse_args()

    if args.channel:
        channels = [args.channel]
    elif args.market is not None and args.worknet is not None:
        channels = [f"book.{args.market}.{args.worknet}"]
    elif args.worknet is not None:
        channels = [f"book.{args.worknet}"]
    else:
        ap.error("need --channel or (--market + --worknet) or --worknet")

    try:
        return asyncio.run(run(channels))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
