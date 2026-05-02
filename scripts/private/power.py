#!/usr/bin/env python3
"""GET /v1/principals/{me}/power — AWP Power 在指定 epoch 的快照。

    power.py [--principal 0x…] [--epoch <id>]

无 power 时服务端返回 404 — 提示用户先经 awp-skill 质押 veAWP。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, fetch, wallet_address  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--principal")
    ap.add_argument("--epoch", type=int)
    args = ap.parse_args()
    try:
        principal = args.principal or wallet_address()
        # 该端点在 OpenAPI 里标记 `security: []`（公开读），所以不签名
        data = fetch(
            "GET",
            f"/v1/principals/{principal}/power",
            params={"epoch_id": args.epoch},
        )
    except EmgError as e:
        if e.code == "STATE_PRINCIPAL_NOT_IN_EPOCH" or e.status == 404:
            print(json.dumps({
                "error": "STATE_PRINCIPAL_NOT_IN_EPOCH",
                "title": "No AWP Power in this epoch",
                "detail": "Stake veAWP via awp-skill before next Wednesday's epoch open.",
                "hint": "awp positions",
            }))
            return 6
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
