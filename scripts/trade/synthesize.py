#!/usr/bin/env python3
"""POST /v1/orders/synthesize — Smart Order Router buy (R8-D)。

    synthesize.py --market 6 --worknet 11 --quantity 100 --max-price 0.25 \
                  [--idem-key UUID] [--yes]

获取 `--quantity` 份目标 worknet 的 shares —— 服务端 Smart Order Router
在两条路径里选最便宜：

  1. 直接吃目标 worknet 的卖单簿
  2. 拆 1 chip → N 份 shares，把非目标 worknet 的份额按 best-bid 卖掉，
     合成净成本 = 1 - Σ best_bid(non-target)

`--max-price` 是单 share 的最高可接受成本，必须严格在 (0, 1)。服务端
把 `max_price * quantity` 作为 planner 的总成本上限；超额返回
`409 BUSINESS_INSUFFICIENT_BALANCE`。

返回值含 `actual_quantity`（综合两条腿实际成交数量）和 `slippage_quantity`
（相对计划与执行时活簿的差额）。

幂等性策略与 `POST /v1/orders` 相同（`X-Idempotency-Key` 24h 缓存）。
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

    # 客户端预校验，省去服务端 400 含糊错误
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
