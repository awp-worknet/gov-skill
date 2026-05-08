#!/usr/bin/env python3
"""GET /v1/principals/{me}/state — 当前 epoch 的 chips + per-worknet shares。

签名读取，自动通过 awp-wallet 解析 principal。

    state.py                  # 当前 epoch 自己的 state
    state.py --principal 0x…  # 替别人查（需要 Manager 委托）
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
    ap.add_argument("--principal", help="defaults to wallet address")
    args = ap.parse_args()
    try:
        principal = args.principal or wallet_address()
        data = signed_request(
            "GET",
            sign_path=f"/principals/{principal}/state",
            full_path=f"/principals/{principal}/state",
            principal=principal,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
