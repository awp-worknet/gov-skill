#!/usr/bin/env python3
"""GET /v1/worknets — worknet directory (id ↔ name resolution).

    worknets.py                  # all worknets
    worknets.py --name aGOV      # reverse-lookup id by name
    worknets.py --id 11          # fetch a single worknet's detail

worknet names (e.g. aMINE / aGOV / aPRED) change with each epoch — do not
hardcode any mapping in the skill; call this script each time you need one.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, fetch  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", help="resolve worknet by symbolic name")
    ap.add_argument("--id", type=int, help="single worknet by id")
    args = ap.parse_args()

    try:
        data = fetch("GET", "/worknets")
    except EmgError as e:
        return emit_error(e)

    items = data.get("items", []) if isinstance(data, dict) else data
    if args.name:
        match = [w for w in items if w.get("name", "").lower() == args.name.lower()]
        print(json.dumps({"items": match}, indent=2))
        return 0 if match else 6
    if args.id is not None:
        match = next((w for w in items if w.get("id") == args.id), None)
        if match is None:
            print(json.dumps({"error": "WORKNET_NOT_FOUND", "id": args.id}))
            return 6
        print(json.dumps(match, indent=2))
        return 0
    print(json.dumps({"items": items}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
