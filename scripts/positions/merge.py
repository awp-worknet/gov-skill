#!/usr/bin/env python3
"""POST /v1/positions/merge — 把 N 份 shares 合并回 chips。

    merge.py --quantity 5 [--idem-key UUID] [--yes]

需要每个 worknet 都至少持有 `quantity` 份 shares — 否则服务端返回
`BUSINESS_INSUFFICIENT_SHARES`（HTTP 409）。
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
    ap.add_argument("--quantity", required=True)
    ap.add_argument("--idem-key", default=str(uuid.uuid4()))
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    prompt = (
        "[TX] merge shares → chips:\n"
        f"     quantity:   {fmt_amount(args.quantity)} shares per worknet\n"
        f"     idem-key:   {args.idem_key}\n"
        "     proceed? (y/n) "
    )
    if not confirm(prompt, yes=args.yes):
        print(json.dumps({"cancelled": True}))
        return 0

    try:
        data = signed_request(
            "POST",
            sign_path="/positions/merge",
            full_path="/positions/merge",
            body={"quantity": args.quantity},
            idempotency_key=args.idem_key,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
