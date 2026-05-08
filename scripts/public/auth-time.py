#!/usr/bin/env python3
"""GET /v1/auth/time — lightweight server clock probe.

    auth-time.py            # print server Unix seconds
    auth-time.py --check    # also compute local drift and exit non-zero (when > 30s)

When signing, the server validates |now - signed_timestamp| ≤ 30s (spec §9.3.4).
A locally desynced NTP gets you persistently rejected with
`AUTH_TIMESTAMP_OUT_OF_WINDOW`, but the error message can't tell you whether
the issue is client-side or server-side. Run this first for a direct
comparison.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, fetch_server_time  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if drift > 30s")
    args = ap.parse_args()
    try:
        srv = fetch_server_time()
    except EmgError as e:
        return emit_error(e)
    local = int(time.time())
    drift = srv - local
    out = {
        "server_time_unix": srv,
        "local_time_unix": local,
        "drift_seconds": drift,
        "within_signing_window": abs(drift) <= 30,
    }
    print(json.dumps(out, indent=2))
    if args.check and abs(drift) > 30:
        sys.stderr.write(
            f"WARNING: Local clock is {drift:+d}s off from server. "
            "Run NTP sync (e.g. `sudo systemctl restart systemd-timesyncd`) "
            "before signing — your requests will be rejected with "
            "AUTH_TIMESTAMP_OUT_OF_WINDOW.\n"
        )
        return 8
    return 0


if __name__ == "__main__":
    sys.exit(main())
