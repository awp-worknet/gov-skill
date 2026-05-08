#!/usr/bin/env python3
"""Subscribe to book.{m}.{wn} (or book.{wn} — the server supports both naming conventions).

    watch-book.py --market 6 --worknet 11
    watch-book.py --channel "book.6.11"   # pass the raw channel id directly

Each event is emitted to stdout as JSON-Lines. In BookDelta, `new_quantity`
is the **absolute** amount; 0 = the price level has been cleared — do not
treat it as a delta.
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
