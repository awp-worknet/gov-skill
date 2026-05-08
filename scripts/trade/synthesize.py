#!/usr/bin/env python3
"""POST /v1/orders/synthesize — Smart Order Router buy (R8-D).

    synthesize.py --market 6 --worknet 11 --quantity 100 --max-price 0.25 \
                  [--idem-key UUID] [--yes]

Acquire `--quantity` shares of the target worknet — the server's Smart Order
Router picks the cheaper of two paths:

  1. Hit the target worknet's ask book directly.
  2. Decompose 1 chip → N shares, sell the non-target worknet portions at
     best-bid; net synthesis cost = 1 - Σ best_bid(non-target).

`--max-price` is the maximum acceptable per-share cost and must be strictly
in (0, 1). The server uses `max_price * quantity` as the planner's total
cost cap; exceeding it returns `409 BUSINESS_INSUFFICIENT_BALANCE`.

The response includes `actual_quantity` (the actual filled amount across
both legs) and `slippage_quantity` (the difference between plan and the
live book at execution time).

Idempotency policy is the same as `POST /v1/orders` (`X-Idempotency-Key`
24h cache).
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import (  # noqa: E402
    EmgError, confirm, emit_error, fmt_amount, fmt_price,
    signed_request,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", type=int, required=True)
    ap.add_argument("--worknet", type=int, required=True)
    ap.add_argument("--quantity", required=True, help="target shares (decimal > 0)")
    ap.add_argument("--max-price", required=True, help="max per-share cost in chips, (0, 1)")
    ap.add_argument("--idem-key", default=str(uuid.uuid4()))
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    # Client-side pre-validation, saves a vague 400 from the server
    try:
        qty = Decimal(args.quantity)
    except InvalidOperation:
        ap.error(f"--quantity {args.quantity!r} not a valid decimal")
    if qty <= 0:
        ap.error(f"--quantity must be > 0; got {qty}")
    try:
        max_p = Decimal(args.max_price)
    except InvalidOperation:
        ap.error(f"--max-price {args.max_price!r} not a valid decimal")
    if not (Decimal("0") < max_p < Decimal("1")):
        ap.error(f"--max-price must be strictly in (0, 1); got {max_p}")

    body = {
        "market_id": args.market,
        "worknet_id": args.worknet,
        "quantity": args.quantity,
        "max_price": args.max_price,
    }

    prompt = (
        "[TX] synthesize buy via SOR:\n"
        f"     market:        {args.market}\n"
        f"     worknet:       {args.worknet}\n"
        f"     quantity:      {fmt_amount(qty)} shares\n"
        f"     max_price:     {fmt_price(max_p)} per share\n"
        f"     budget cap:    {fmt_price(qty * max_p)} chips total\n"
        f"     idem-key:      {args.idem_key}\n"
        "     proceed? (y/n) "
    )
    if not confirm(prompt, yes=args.yes):
        print(json.dumps({"cancelled": True}))
        return 0

    try:
        data = signed_request(
            "POST",
            sign_path="/orders/synthesize",
            full_path="/orders/synthesize",
            body=body,
            idempotency_key=args.idem_key,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
