#!/usr/bin/env python3
"""POST /v1/positions/split — split chips into N worknet shares.

    split.py --market 6 --quantity 10 [--idem-key UUID] [--yes]

Polymarket-style: 1 chip → N shares (one per worknet). No counterparty.
Returns the updated `StakerEpochState`.

`market_id` is per-market (both chips and shares are accounted per market),
so the body explicitly includes market. OpenAPI does not mark market_id
required, but the server actually validates it (same spec-lag pattern as
submit-order).
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, confirm, emit_error, signed_request, fmt_amount  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", type=int, required=True)
    ap.add_argument("--quantity", required=True)
    ap.add_argument("--idem-key", default=str(uuid.uuid4()))
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    prompt = (
        "[TX] split chips → shares:\n"
        f"     market:     {args.market}\n"
        f"     quantity:   {fmt_amount(args.quantity)} chips\n"
        f"     idem-key:   {args.idem_key}\n"
        "     proceed? (y/n) "
    )
    if not confirm(prompt, yes=args.yes):
        print(json.dumps({"cancelled": True}))
        return 0

    try:
        data = signed_request(
            "POST",
            sign_path="/positions/split",
            full_path="/positions/split",
            body={"market_id": args.market, "quantity": args.quantity},
            idempotency_key=args.idem_key,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
