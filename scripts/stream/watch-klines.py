#!/usr/bin/env python3
"""订阅 klines.{m}.{wn}.{interval} — 实时 OHLCV bucket 更新。

    watch-klines.py --market 6 --worknet 11 --interval 1m
    watch-klines.py --channel "klines.6.11.5m"
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
    ap.add_argument("--interval", default="1m", choices=("1m", "5m", "15m", "1h", "4h", "1d"))
    ap.add_argument("--channel")
    args = ap.parse_args()

    if args.channel:
        channels = [args.channel]
    elif args.market is not None and args.worknet is not None:
        channels = [f"klines.{args.market}.{args.worknet}.{args.interval}"]
    elif args.worknet is not None:
        channels = [f"klines.{args.worknet}.{args.interval}"]
    else:
        ap.error("need --channel or worknet (+ optional market)")

    try:
        return asyncio.run(run(channels))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
