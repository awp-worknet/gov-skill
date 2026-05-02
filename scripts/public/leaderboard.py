#!/usr/bin/env python3
"""GET /v1/leaderboard/epistemic — 按 epistemic score 排名的 Stakers。

    leaderboard.py                       # 当前 epoch
    leaderboard.py --epoch 5             # 指定 epoch
    leaderboard.py --epoch 5 --limit 50

输出 `{ data: [{ principal, epistemic_score, rank }], pagination: {...} }`。
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
    ap.add_argument("--epoch", type=int)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--cursor", default=None)
    args = ap.parse_args()

    params = {"epoch_id": args.epoch, "limit": args.limit, "cursor": args.cursor}
    try:
        data = fetch("GET", "/v1/leaderboard/epistemic", params=params)
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
