#!/usr/bin/env python3
"""GET /v1/auth/info — bootstrap the EIP-712 domain (chainId + verifyingContract).

The first script the skill calls on startup. The result is cached by
`govnet_lib.get_auth_info()` to `~/.govnet/auth-info.json`; subsequent
signatures read from the cache directly.

Usage:
    auth-info.py [--refresh]   # --refresh forces bypassing the local cache
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, get_auth_info  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true", help="ignore local cache")
    args = ap.parse_args()
    try:
        info = get_auth_info(force_refresh=args.refresh)
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
