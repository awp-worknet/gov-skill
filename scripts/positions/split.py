#!/usr/bin/env python3
"""POST /v1/positions/split — 把 chips 切成 N 份 worknet shares。

    split.py --market 6 --quantity 10 [--idem-key UUID] [--yes]

Polymarket-style: 1 chip → N 份 shares（每个 worknet 一份）。无对手方。
返回更新后的 `StakerEpochState`。

`market_id` 是 per-market 操作（chips 与 shares 都按 market 分账），所以
body 显式带 market。OpenAPI 没把 market_id 标 required，但服务端实际验
证（与 submit-order 一致的 spec-lag 模式）。
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
