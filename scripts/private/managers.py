#!/usr/bin/env python3
"""GET /v1/principals/{me}/managers — list of currently authorized Managers.

    managers.py [--principal 0x…]

Despite OpenAPI marking this `security: []` (public read), production
requires EMG-SIG-V1 — the script signs accordingly. Read sourced from
the AWPRegistry chain state via api.awp.sh, with `checked_at` showing
the freshness of that registry snapshot (a `1970-01-01T00:00:00Z` value
means the indexer has never resolved it for this principal).
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
    args = ap.parse_args()
    try:
        principal = args.principal or wallet_address()
        data = signed_request(
            "GET",
            sign_path=f"/principals/{principal}/managers",
            full_path=f"/principals/{principal}/managers",
            principal=principal,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
