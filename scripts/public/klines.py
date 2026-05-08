#!/usr/bin/env python3
"""GET /v1/markets/{m}/worknets/{wn}/klines — OHLCV history.

    klines.py --market 6 --worknet 11 \
              [--interval 1m|5m|1h|4h|1d] [--from <iso>] [--to <iso>] [--limit 100]

Output is a bare array (matching the server schema); each element:
    { timestamp, open, high, low, close, volume, trade_count }

Prices / quantities are given as string-decimals with scale 18 — callers
should parse with Decimal themselves.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, fetch  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", type=int, required=True)
    ap.add_argument("--worknet", type=int, required=True)
    ap.add_argument("--interval", default="1h", choices=("1m", "5m", "1h", "4h", "1d"))
    ap.add_argument("--from", dest="from_", help="ISO-8601 lower bound")
    ap.add_argument("--to", help="ISO-8601 upper bound")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()

    params = {
        "interval": args.interval,
        "from": args.from_,
        "to": args.to,
        "limit": args.limit,
    }
    try:
        data = fetch(
            "GET",
            f"/markets/{args.market}/worknets/{args.worknet}/klines",
            params=params,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
