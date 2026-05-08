#!/usr/bin/env python3
"""GET /v1/fills — 自己的成交历史（私有，self-only）。

    fills-list.py [--worknet <id>] [--since <iso>]
                  [--limit 100] [--cursor <opaque>] [--all-pages]

服务端按 X-EMG-Principal 过滤（无 `principal` 查询参数 —— 可见性由签名
自己锁定）。每条 fill 含价格、数量、role (maker/taker)、市场/worknet。

实时 fills 走 WS `fills.me` 频道；本端点用于：
  - 冷启动 / catch-up
  - 两次 `fills.me` 订阅之间的 backfill

cursor 是不透明的 base64url（服务端编码 `(filled_at, fill_id)` 复合键）；
客户端把上一页的 `pagination.next_cursor` 原样回传即可。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, paginate_all, signed_request, wallet_address  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worknet", type=int, help="filter to one worknet")
    ap.add_argument("--since", help="ISO-8601 lower bound on filled_at")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--cursor")
    ap.add_argument("--all-pages", action="store_true",
                    help="follow next_cursor; each page is one signed request")
    ap.add_argument("--max-pages", type=int, default=100)
    args = ap.parse_args()

    try:
        principal = wallet_address()
        base_params = {
            "worknet_id": args.worknet,
            "since": args.since,
            "limit": args.limit,
        }
        if args.cursor:
            base_params["cursor"] = args.cursor

        def fetch_page(p):
            return signed_request(
                "GET",
                sign_path="/fills",
                full_path="/v1/fills",
                query_params=p,
                principal=principal,
            )

        if args.all_pages:
            data = paginate_all(fetch_page, initial_params=base_params, max_pages=args.max_pages)
        else:
            data = fetch_page(base_params)
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
