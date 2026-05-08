#!/usr/bin/env python3
"""POST /v1/orders — 提交订单。

    submit-order.py --market 6 --worknet 11 --side buy --kind limit \
                    --price 0.2200 --quantity 100 \
                    [--tif gtc|ioc|fok|gtt] [--expires-at <iso>] \
                    [--post-only] [--reduce-only] \
                    [--stp cancel_both|cancel_taker|cancel_maker|decrement_taker] \
                    [--visible-quantity Q] [--allow-synthesis BOOL] \
                    [--client-order-id TAG] [--idem-key UUID] [--yes]

签名前会做 phase 预检查（避免服务端再返回 BUSINESS_PHASE_MISMATCH），
并打印 [TX] 确认块；非交互场景必须显式 `--yes`。

idem-key 默认每次生成新的 UUIDv4 — 如果你想重试同一个逻辑动作，**显式
传入** 上次的 key（服务端按 (principal, key) 缓存响应 24h）。
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
    fetch,
    fetch_market,
    fmt_amount,
    fmt_price,
    normalize_phase,
    signed_request,
    wallet_address,
)


def _phase_check(market_id: int) -> dict:
    """先尝试 `/markets/{id}`（含 worknets[]），失败时退到 `/epochs/{id}`。

    EpochInfo 用 `phase` 字段，Market schema 可能用 `status` —— 都试。
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
    """下单前先确认 principal 在本 epoch 有 AWP Power。

    生产服务端对 zero-power 用户下单当前会返回 500
    `INTERNAL_UNEXPECTED_STATE: principal not initialized before reserve_for_buy`
    （server-side bug，应该是 404 STATE_PRINCIPAL_NOT_IN_EPOCH）。客户端
    先 GET /principals/{me}/power 拦下来，给用户友好提示，避免烧 nonce
    + 看到看不懂的 500。
    """
    try:
        power = fetch("GET", f"/principals/{principal}/power",
                     params={"epoch_id": market_id})
    except EmgError as e:
        # 404 直接转译；其它错误透传不要遮蔽
        if e.status == 404:
            raise EmgError(
                "STATE_PRINCIPAL_NOT_IN_EPOCH",
                f"No AWP Power for principal in market {market_id}. "
                "AWP Power is snapshotted every Wednesday 12:00 UTC. "
                "Stake AWP into a veAWP position via awp-skill, then wait "
                "for the next epoch open for the snapshot to take effect.",
                status=404,
            )
        raise
    total = power.get("total_voting_power") or "0"
    try:
        from decimal import Decimal
        if Decimal(str(total)) <= 0:
            raise EmgError(
                "STATE_PRINCIPAL_NOT_IN_EPOCH",
                f"AWP Power for market {market_id} is {total} — no chips this epoch. "
                "Stake veAWP via awp-skill before next Wednesday's snapshot.",
                status=404,
            )
    except (TypeError, ValueError, ArithmeticError):
        # 不能解析就当成 0，让 server 自己判
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

    # OpenAPI `CreateOrderRequest` 没把 market_id 标 required，但生产服务端
    # 实际要求（server-side validation 比 spec 严）。带上 market_id 之前
    # 提交直接 422 missing field。
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
        # power 预检 —— 比 phase check 后做，因为 power 失败的引导（去 awp-skill
        # 质押）只在 epoch 的"开窗前"这段时间有意义；phase 已挂就先报 phase。
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
