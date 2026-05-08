#!/usr/bin/env python3
"""GET /v1/orders/{id} — 单个订单详情 + fills[] + avg_fill_price。

    orders-get.py --id 018f-….
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, signed_request  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", required=True, help="UUID order id")
    args = ap.parse_args()
    try:
        data = signed_request(
            "GET",
            sign_path=f"/orders/{args.id}",
            full_path=f"/orders/{args.id}",
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
