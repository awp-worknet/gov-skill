#!/usr/bin/env python3
"""GET /v1/principals/{me}/power — AWP Power snapshot for a given market.

    power.py [--principal 0x…] [--market <id>]

The endpoint is documented in OpenAPI as `security: []` (public read) but
production actually requires EMG-SIG-V1 — this script signs accordingly.
The query param's canonical name is `market_id` (server now also accepts
`epoch_id` as an alias post-9387e78, but `market_id` is the spec-correct
form).

A 404 STATE_PRINCIPAL_NOT_IN_EPOCH does NOT necessarily mean "no stake".
Per protocol-side filtering it can also fire when the principal has a
veAWP position whose `lock_end` is too close to (or earlier than) the
epoch's settlement window — the lock must outlast the full epoch lifecycle
to count toward voting power. Don't assume "go stake" — first cross-check
the on-chain veAWP position before suggesting that remedy.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, signed_request, wallet_address  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--principal")
    ap.add_argument("--market", type=int, help="market_id (preferred name)")
    ap.add_argument("--epoch", type=int, help="alias for --market (legacy)")
    args = ap.parse_args()
    market_id = args.market if args.market is not None else args.epoch

    try:
        principal = args.principal or wallet_address()
        params = {"market_id": market_id} if market_id is not None else None
        data = signed_request(
            "GET",
            sign_path=f"/principals/{principal}/power",
            full_path=f"/principals/{principal}/power",
            query_params=params,
            principal=principal,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
