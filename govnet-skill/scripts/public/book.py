#!/usr/bin/env python3
"""GET /v1/markets/{m}/worknets/{wn}/book — 订单簿 snapshot。

    book.py --market 6 --worknet 11 [--depth 20]

输出按服务端原样转发：`{ market_id, worknet_id, timestamp, bids[], asks[] }`
其中每个 level 是 `{ price, total_quantity }`。

实时变化请用 `stream/watch-book.py` 订阅 `book.{m}.{wn}` 频道。
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
    ap.add_argument("--depth", type=int, default=20, help="levels per side (max 200)")
    args = ap.parse_args()
    try:
        data = fetch(
            "GET",
            f"/v1/markets/{args.market}/worknets/{args.worknet}/book",
            params={"depth": args.depth},
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
