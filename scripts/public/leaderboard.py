#!/usr/bin/env python3
"""GET /v1/leaderboard/epistemic — Stakers ranked by epistemic score.

    leaderboard.py                     # current epoch, first page
    leaderboard.py --epoch 5           # specific epoch
    leaderboard.py --epoch 5 --limit 50
    leaderboard.py --all-pages         # auto-walk every page
    leaderboard.py --cursor <opaque>   # relay from a previous next_cursor

In `--all-pages` mode the response is `{ data: [...merged...], page_count: N }`;
when `--max-pages` (default 100) is exceeded, `truncated_at_max_pages: true`
and `next_cursor` are added so the caller can relay.
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
