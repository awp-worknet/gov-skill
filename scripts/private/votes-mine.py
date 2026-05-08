#!/usr/bin/env python3
"""GET /v1/principals/{me}/votes/{market_id} — your own revealed vote for a given market.

    votes-mine.py --market 6
    votes-mine.py --market 6 --principal 0x…   # Manager-on-behalf-of (requires delegation)

Returns a `RevealedVote` — vote vector + prediction vector + nonce + signature.
self-only: the path principal must equal the authenticated principal, else
401 `AUTH_UNAUTHORIZED_DELEGATE`.

reveal-gated: during phase 1/2, returns 403 `STATE_VOTES_NOT_REVEALED`; you
have to wait for the Phase 2→3 boundary (settlement starting) before reading
your own final ballot.
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
