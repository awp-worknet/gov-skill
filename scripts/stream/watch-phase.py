#!/usr/bin/env python3
"""Subscribe to the phase channel — live notifications of epoch state-machine transitions.

    watch-phase.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.ws import WSClient, emit_event  # noqa: E402


async def run() -> int:
    async with WSClient() as ws:
        await ws.subscribe(["phase"])
        async for event in ws:
            emit_event(event)
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
