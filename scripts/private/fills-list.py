#!/usr/bin/env python3
"""GET /v1/fills — your own fill history (private, self-only).

    fills-list.py [--worknet <id>] [--since <iso>]
                  [--limit 100] [--cursor <opaque>] [--all-pages]

The server filters by X-EMG-Principal (no `principal` query parameter —
visibility is locked to the signature). Each fill includes price, quantity,
role (maker/taker), market/worknet.

Real-time fills go through the WS `fills.me` channel; this endpoint is for:
  - Cold start / catch-up
  - Backfilling between two `fills.me` subscriptions

The cursor is an opaque base64url (the server encodes a composite
`(filled_at, fill_id)` key); the client just relays the previous page's
`pagination.next_cursor` verbatim.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, paginate_all, signed_request, wallet_address  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worknet", type=int, help="filter to one worknet")
    ap.add_argument("--since", help="ISO-8601 lower bound on filled_at")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--cursor")
    ap.add_argument("--all-pages", action="store_true",
                    help="follow next_cursor; each page is one signed request")
    ap.add_argument("--max-pages", type=int, default=100)
    args = ap.parse_args()

    try:
        principal = wallet_address()
        base_params = {
            "worknet_id": args.worknet,
            "since": args.since,
            "limit": args.limit,
        }
        if args.cursor:
            base_params["cursor"] = args.cursor

        def fetch_page(p):
            return signed_request(
                "GET",
                sign_path="/fills",
                full_path="/fills",
                query_params=p,
                principal=principal,
            )

        if args.all_pages:
            data = paginate_all(fetch_page, initial_params=base_params, max_pages=args.max_pages)
        else:
            data = fetch_page(base_params)
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
