#!/usr/bin/env python3
"""GET /v1/leaderboard/epistemic — Stakers ranked by epistemic score.

    leaderboard.py                     # current market, first page
    leaderboard.py --market 5          # specific market
    leaderboard.py --market 5 --limit 50
    leaderboard.py --all-pages         # auto-walk every page
    leaderboard.py --cursor <opaque>   # relay from a previous next_cursor

The query param is `market_id` per `SKILL_API_LATEST.md` §1.1; `--epoch`
remains accepted as a legacy alias.

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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", type=int, help="market_id (preferred name)")
    ap.add_argument("--epoch", type=int, help="alias for --market (legacy)")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--cursor", default=None)
    ap.add_argument("--all-pages", action="store_true",
                    help="follow next_cursor until exhausted")
    ap.add_argument("--max-pages", type=int, default=100,
                    help="safety cap when --all-pages")
    args = ap.parse_args()
    market_id = args.market if args.market is not None else args.epoch

    params = {"limit": args.limit}
    if market_id is not None:
        params["market_id"] = market_id
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
