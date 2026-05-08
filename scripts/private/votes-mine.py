#!/usr/bin/env python3
"""GET /v1/principals/{me}/votes/{market_id} — 自己在某 market 的最终揭票。

    votes-mine.py --market 6
    votes-mine.py --market 6 --principal 0x…   # Manager 代查（需委托）

返回 `RevealedVote` —— vote 向量 + prediction 向量 + nonce + signature。
self-only：path principal 必须等于认证 principal，否则 401
`AUTH_UNAUTHORIZED_DELEGATE`。

reveal-gated：phase 1/2 期间返回 403 `STATE_VOTES_NOT_REVEALED`，要等
Phase 2→3 boundary 触发后（settlement 开始）才能读自己的 final 票面。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, signed_request, wallet_address  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", type=int, required=True)
    ap.add_argument("--principal", help="defaults to wallet address")
    args = ap.parse_args()
    try:
        principal = args.principal or wallet_address()
        path = f"/principals/{principal}/votes/{args.market}"
        data = signed_request(
            "GET",
            sign_path=path,
            full_path=f"/v1{path}",
            principal=principal,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
