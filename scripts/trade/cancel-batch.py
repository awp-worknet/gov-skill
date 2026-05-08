#!/usr/bin/env python3
"""POST /v1/orders/cancel-batch — 一次取消最多 500 个订单。

    cancel-batch.py --ids 018f-aa,018f-bb,018f-cc [--yes]
    cancel-batch.py --ids-file orders.txt   # 一行一个 UUID

返回 `{ results: [...] }`，每个元素要么是 `CancelReceipt`（成功），要么
是 `CancelBatchError {order_id, code, detail}` — 用 `code` 字段区分。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, confirm, emit_error, signed_request  # noqa: E402


def _parse_ids(args) -> list:
    if args.ids:
        return [s.strip() for s in args.ids.split(",") if s.strip()]
    if args.ids_file:
        return [
            line.strip()
            for line in Path(args.ids_file).read_text("utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", help="comma-separated UUIDs")
    ap.add_argument("--ids-file", help="path to newline-separated UUID file")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    ids = _parse_ids(args)
    if not ids:
        ap.error("need --ids or --ids-file")
    if len(ids) > 500:
        ap.error("server caps batch at 500 ids; split your call")

    prompt = f"[TX] cancel-batch ({len(ids)} orders)\n     proceed? (y/n) "
    if not confirm(prompt, yes=args.yes):
        print(json.dumps({"cancelled": True, "skipped": True}))
        return 0

    try:
        data = signed_request(
            "POST",
            sign_path="/orders/cancel-batch",
            full_path="/orders/cancel-batch",
            body={"order_ids": ids},
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
