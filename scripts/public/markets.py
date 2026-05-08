#!/usr/bin/env python3
"""List markets / fetch a single market's detail.

`/v1/markets` has been live in production since the 2026-05-08 deployment;
the `/v1/epochs/current` fallback is no longer needed.

    markets.py                 # list all markets
    markets.py --id 6          # single market's worknets[] detail
    markets.py --status voting_and_trading   # filter by phase

Output is uniformly `{ items: [...] }` (list mode) or a single object
(--id mode).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, fetch, fetch_market, normalize_phase  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", type=int, help="single market by id")
    ap.add_argument("--status", help="filter to phase (e.g. voting_and_trading)")
    args = ap.parse_args()

    try:
        if args.id is not None:
            data = fetch_market(args.id)
        else:
            data = fetch("GET", "/markets")
    except EmgError as e:
        return emit_error(e)

    if args.status and isinstance(data, dict) and "items" in data:
        wanted = normalize_phase(args.status)
        data["items"] = [
            m for m in data["items"]
            if normalize_phase(m.get("phase", m.get("status", ""))) == wanted
        ]

    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
