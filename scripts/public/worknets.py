#!/usr/bin/env python3
"""GET /v1/worknets — worknet 目录（id ↔ name 解析）。

    worknets.py                  # 全部 worknets
    worknets.py --name aGOV      # 按名字反查 id
    worknets.py --id 11          # 按 id 取详情

worknet 名字（如 aMINE / aGOV / aPRED）会随 epoch 变化 — 不要在 skill
里硬编码任何映射，每次需要时调用本脚本。
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
