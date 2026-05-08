#!/usr/bin/env python3
"""GET /v1/leaderboard/epistemic — 按 epistemic score 排名的 Stakers。

    leaderboard.py                     # 当前 epoch 第一页
    leaderboard.py --epoch 5           # 指定 epoch
    leaderboard.py --epoch 5 --limit 50
    leaderboard.py --all-pages         # 自动翻完所有页面
    leaderboard.py --cursor <opaque>   # 接力上次的 next_cursor

`--all-pages` 模式下返回 `{ data: [...合并...], page_count: N }`；超过
`--max-pages`（默认 100）时加 `truncated_at_max_pages: true` + `next_cursor`
让调用方自己接力。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, fetch, paginate_all  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--epoch", type=int)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--cursor", default=None)
    ap.add_argument("--all-pages", action="store_true",
                    help="follow next_cursor until exhausted")
    ap.add_argument("--max-pages", type=int, default=100,
                    help="safety cap when --all-pages")
    args = ap.parse_args()

    params = {"epoch_id": args.epoch, "limit": args.limit}
    if args.cursor:
        params["cursor"] = args.cursor

    try:
        if args.all_pages:
            data = paginate_all(
                lambda p: fetch("GET", "/leaderboard/epistemic", params=p),
                initial_params=params,
                max_pages=args.max_pages,
            )
        else:
            data = fetch("GET", "/leaderboard/epistemic", params=params)
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
