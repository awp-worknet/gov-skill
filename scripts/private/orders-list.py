#!/usr/bin/env python3
"""GET /v1/orders — 列出我的订单（cursor 分页）。

    orders-list.py [--status active|partially_filled|filled|cancelled|expired|rejected]
                   [--worknet <id>] [--epoch <id>]
                   [--limit 100] [--cursor <opaque>]

服务端按 X-EMG-Principal 过滤。`--principal` 查询参数与 header 不一致
会被服务端 400（防御性校验）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.canonical import build_query  # noqa: E402
from lib.govnet_lib import EmgError, emit_error, signed_request, wallet_address  # noqa: E402


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
    args = ap.parse_args()

    try:
        principal = wallet_address()
        params = {
            "principal": principal,
            "status": args.status,
            "worknet_id": args.worknet,
            "epoch_id": args.epoch,
            "limit": args.limit,
            "cursor": args.cursor,
        }
        data = signed_request(
            "GET",
            sign_path="/orders",
            full_path="/v1/orders",
            query_params=params,
            principal=principal,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
