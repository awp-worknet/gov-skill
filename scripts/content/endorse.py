#!/usr/bin/env python3
"""POST /v1/comments/{id}/endorse — 给评论点赞（重复点赞不报错）。

    endorse.py --id 018f-…  [--yes]
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
    ap.add_argument("--id", required=True, help="UUID comment id")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    prompt = f"[TX] endorse comment {args.id}\n     proceed? (y/n) "
    if not confirm(prompt, yes=args.yes):
        print(json.dumps({"cancelled": True}))
        return 0

    try:
        data = signed_request(
            "POST",
            sign_path=f"/comments/{args.id}/endorse",
            full_path=f"/comments/{args.id}/endorse",
            body=None,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
