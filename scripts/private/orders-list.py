#!/usr/bin/env python3
"""GET /v1/orders — list my orders (cursor-paginated).

    orders-list.py [--status active|partially_filled|filled|cancelled|expired|rejected]
                   [--worknet <id>] [--epoch <id>]
                   [--limit 100] [--cursor <opaque>] [--all-pages]

The server filters by X-EMG-Principal. A `--principal` query parameter that
disagrees with the header gets a 400 from the server (defensive validation).

`--all-pages` auto-follows `pagination.next_cursor` to drain every page; each
page is one signed request (each signature consumes a nonce), so don't add
it blindly for huge sets.
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
    ap.add_argument(
        "--status",
        choices=("active", "partially_filled", "filled", "cancelled", "expired", "rejected"),
    )
    ap.add_argument("--worknet", type=int)
    ap.add_argument("--epoch", type=int)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--cursor")
    ap.add_argument("--all-pages", action="store_true",
                    help="follow next_cursor; each page is one signed request")
    ap.add_argument("--max-pages", type=int, default=100)
    args = ap.parse_args()

    try:
        principal = wallet_address()
        base_params = {
            "principal": principal,
            "status": args.status,
            "worknet_id": args.worknet,
            "epoch_id": args.epoch,
            "limit": args.limit,
        }
        if args.cursor:
            base_params["cursor"] = args.cursor

        def fetch_page(p):
            return signed_request(
                "GET",
                sign_path="/orders",
                full_path="/orders",
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
