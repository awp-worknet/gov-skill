#!/usr/bin/env python3
"""GET /v1/principals/{me}/managers — 当前授权的 Manager 列表（公开读，OpenAPI security []）。

    managers.py [--principal 0x…]
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
    args = ap.parse_args()
    try:
        principal = args.principal or wallet_address()
        data = fetch("GET", f"/principals/{principal}/managers")
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
