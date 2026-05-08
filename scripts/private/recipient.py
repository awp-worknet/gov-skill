#!/usr/bin/env python3
"""GET /v1/principals/{me}/recipient — 结算时谁实际收到 Gov Tokens。

    recipient.py [--principal 0x…] [--epoch <id>]

仅在该 epoch 已结算后可用，未结算返回 404。
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
        data = fetch(
            "GET",
            f"/principals/{principal}/recipient",
            params={"epoch_id": args.epoch},
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
