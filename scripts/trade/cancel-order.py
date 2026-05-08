#!/usr/bin/env python3
"""DELETE /v1/orders/{id} — 取消单个订单。

返回值是一份 cancel receipt：
    status ∈ { cancelled, partially_filled_then_cancelled,
               already_fully_filled, already_cancelled }

也就是说就算订单同时被 fill 了，HTTP 还是 200 — 客户端要看 `status`。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, confirm, emit_error, signed_request  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", required=True)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    prompt = f"[TX] cancel order {args.id}\n     proceed? (y/n) "
    if not confirm(prompt, yes=args.yes):
        print(json.dumps({"cancelled": True, "skipped": True}))
        return 0

    try:
        data = signed_request(
            "DELETE",
            sign_path=f"/orders/{args.id}",
            full_path=f"/orders/{args.id}",
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
