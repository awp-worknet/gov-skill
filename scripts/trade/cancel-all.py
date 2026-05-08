#!/usr/bin/env python3
"""POST /v1/orders/cancel-all — single-shot cancel of every active / partially_filled order.

    cancel-all.py [--worknet <id>] [--yes]

Optional `--worknet` scopes to a single worknet. Returns `{ cancelled_count,
not_cancellable_count, processed_at, partial_error? }`. Even if a matcher
dispatch errors mid-run, HTTP stays at 200 — the client checks the
`partial_error` field.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, confirm, emit_error, signed_request  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worknet", type=int)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    scope = f" in worknet {args.worknet}" if args.worknet else ""
    prompt = f"[TX] cancel ALL active orders{scope}\n     proceed? (y/n) "
    if not confirm(prompt, yes=args.yes):
        print(json.dumps({"cancelled": True, "skipped": True}))
        return 0

    params = {"worknet_id": args.worknet} if args.worknet else None
    try:
        data = signed_request(
            "POST",
            sign_path="/orders/cancel-all",
            full_path="/orders/cancel-all",
            query_params=params,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
