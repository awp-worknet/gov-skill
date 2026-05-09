#!/usr/bin/env python3
"""POST /v1/orders — submit an order.

    submit-order.py --market 6 --worknet 11 --side buy --kind limit \
                    --price 0.2200 --quantity 100 \
                    [--tif gtc|ioc|fok|gtt] [--expires-at <iso>] \
                    [--post-only] [--reduce-only] \
                    [--stp cancel_both|cancel_taker|cancel_maker|decrement_taker] \
                    [--visible-quantity Q] [--allow-synthesis BOOL] \
                    [--client-order-id TAG] [--idem-key UUID] [--yes]

A phase pre-check runs before signing (so the server never has to return
BUSINESS_PHASE_MISMATCH), and a [TX] confirmation block is printed;
non-interactive scenarios must pass `--yes` explicitly.

idem-key defaults to a fresh UUIDv4 per invocation — to retry the same
logical action, **explicitly pass** the previous key (the server caches
responses by (principal, key) for 24h).
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import (  # noqa: E402
    EmgError,
    confirm,
    emit_error,
    fetch_market,
    fmt_amount,
    fmt_price,
    normalize_phase,
    signed_request,
    wallet_address,
)


def _phase_check(market_id: int) -> dict:
    """Try `/markets/{id}` first (includes worknets[]); on failure, fall back to `/epochs/{id}`.

    EpochInfo uses the `phase` field; Market schema may use `status` — try both.
    """
    market = fetch_market(market_id)
    phase = normalize_phase(market.get("phase") or market.get("status", ""))
    if phase not in ("voting_and_trading", "trading_only"):
        raise EmgError(
            "BUSINESS_PHASE_MISMATCH",
            f"market {market_id} is in '{phase}' phase; cannot submit orders",
            status=409,
        )
    return market


def _power_check(principal: str, market_id: int) -> None:
    """Before submitting an order, verify the principal has AWP Power in this market.

    The production server returns 500 INTERNAL_UNEXPECTED_STATE
    (`principal not initialized before reserve_for_buy`) on orders from
    principals missing from the epoch's power snapshot — surface it as
    a clean 404 STATE_PRINCIPAL_NOT_IN_EPOCH up front to avoid burning
    a nonce on an inscrutable 500.

    /principals/{me}/power requires EMG-SIG-V1 (despite OpenAPI marking
    `security: []`); query param is `market_id` (canonical) — server now
    accepts `epoch_id` as an alias too.

    A 404 from this endpoint can mean any of:
      a) No veAWP position at all → stake via awp-skill
      b) Position exists but lock_end is too close to / earlier than the
         epoch's settlement window → extend lock via veAWP.addToPosition
      c) Position exists with adequate lock but server snapshot indexer
         missed it (rare race condition) → escalate to protocol team

    The skill can't reliably distinguish (a)/(b)/(c) from /power's response
    alone — it would need an on-chain veAWP read. Surface the ambiguity
    in the error message rather than asserting a specific remedy.
    """
    try:
        power = signed_request(
            "GET",
            sign_path=f"/principals/{principal}/power",
            full_path=f"/principals/{principal}/power",
            query_params={"market_id": market_id},
            principal=principal,
        )
    except EmgError as e:
        if e.status == 404:
            raise EmgError(
                "STATE_PRINCIPAL_NOT_IN_EPOCH",
                f"Principal not in market {market_id}'s AWP Power snapshot. "
                "Possible causes: (a) no veAWP position — stake via awp-skill; "
                "(b) lock_end too close to epoch settlement — extend lock via "
                "veAWP.addToPosition; (c) snapshot indexer missed the position "
                "(rare). Cross-check on-chain veAWP.getVotingPower(tokenId) "
                "before deciding remedy.",
                status=404,
            )
        raise
    total = power.get("total_voting_power") or "0"
    try:
        from decimal import Decimal
        if Decimal(str(total)) <= 0:
            raise EmgError(
                "STATE_PRINCIPAL_NOT_IN_EPOCH",
                f"AWP Power for market {market_id} is {total} — no chips this epoch.",
                status=404,
            )
    except (TypeError, ValueError, ArithmeticError):
        # If we can't parse, let the server decide (don't mask)
        pass


def _confirm_block(args, market: dict, idem_key: str) -> str:
    wn = next(
        (
            w for w in market.get("worknets", []) or market.get("worknet_ids", [])
            if (w.get("id") if isinstance(w, dict) else w) == args.worknet
        ),
        None,
    )
    wn_label = (wn.get("name") if isinstance(wn, dict) else None) or f"id {args.worknet}"
    price_line = f" @ {fmt_price(args.price)}" if args.kind == "limit" and args.price else ""
    return (
        "[TX] about to submit order:\n"
        f"     market:       №{args.market}\n"
        f"     worknet:      {wn_label}\n"
        f"     side:         {args.side}\n"
        f"     kind:         {args.kind}{price_line}\n"
        f"     quantity:     {fmt_amount(args.quantity)}\n"
        f"     tif:          {args.tif}\n"
        f"     post_only:    {args.post_only}\n"
        f"     reduce_only:  {args.reduce_only}\n"
        f"     stp_mode:     {args.stp}\n"
        f"     idem-key:     {idem_key}\n"
        "     proceed? (y/n) "
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", type=int, required=True)
    ap.add_argument("--worknet", type=int, required=True)
    ap.add_argument("--side", choices=("buy", "sell"), required=True)
    ap.add_argument("--kind", choices=("limit", "market"), required=True)
    ap.add_argument("--price", help="required when --kind limit")
    ap.add_argument("--quantity", required=True)
    ap.add_argument("--tif", default="gtc", choices=("gtc", "ioc", "fok", "gtt"))
    ap.add_argument("--expires-at", help="ISO-8601, required when tif=gtt")
    ap.add_argument("--post-only", action="store_true")
    ap.add_argument("--reduce-only", action="store_true")
    ap.add_argument(
        "--stp", default="cancel_both",
        choices=("cancel_taker", "cancel_maker", "cancel_both", "decrement_taker"),
    )
    ap.add_argument("--visible-quantity", help="iceberg: visible portion (< quantity)")
    ap.add_argument("--allow-synthesis", default="true", choices=("true", "false"))
    ap.add_argument("--client-order-id")
    ap.add_argument("--idem-key", default=str(uuid.uuid4()))
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    if args.kind == "limit" and not args.price:
        ap.error("--kind limit requires --price")
    if args.tif == "gtt" and not args.expires_at:
        ap.error("--tif gtt requires --expires-at")
    if args.kind == "market" and args.post_only:
        ap.error("--post-only is invalid with market kind")

    # OpenAPI's `CreateOrderRequest` does not mark market_id as required, but
    # the production server actually requires it (server-side validation is
    # stricter than the spec). Submitting without market_id goes straight to
    # 422 missing field.
    body = {
        "market_id": args.market,
        "worknet_id": args.worknet,
        "side": args.side,
        "kind": args.kind,
        "quantity": args.quantity,
        "time_in_force": args.tif,
        "post_only": args.post_only,
        "reduce_only": args.reduce_only,
        "stp_mode": args.stp,
        "allow_synthesis": args.allow_synthesis == "true",
    }
    if args.kind == "limit":
        body["limit_price"] = args.price
    if args.expires_at:
        body["expires_at"] = args.expires_at
    if args.visible_quantity:
        body["visible_quantity"] = args.visible_quantity
    if args.client_order_id:
        body["client_order_id"] = args.client_order_id

    try:
        market = _phase_check(args.market)
        principal = wallet_address()
        # Power pre-check — runs after phase check because the power-failure
        # remedy (go stake via awp-skill) only makes sense in the
        # before-window-opens window; if phase has already changed, surface the
        # phase issue first.
        _power_check(principal, args.market)
        if not confirm(_confirm_block(args, market, args.idem_key), yes=args.yes):
            print(json.dumps({"cancelled": True}))
            return 0
        data = signed_request(
            "POST",
            sign_path="/orders",
            full_path="/orders",
            body=body,
            principal=principal,
            idempotency_key=args.idem_key,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
