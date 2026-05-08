#!/usr/bin/env python3
"""POST /v1/reports — worknet 周报（仅 worknet operator 可发）。

    post-report.py --worknet 11 \
                   --content-file weekly.md \
                   --metrics-file metrics.json \
                   [--requested-share 0.15] [--idem-key UUID] [--yes]

content 上限 50 000 字符。`metrics` 是结构化 JSON（如 active_miners、
tasks_completed 等）。同一 (epoch, worknet) 一周只能提交一次 — 重复提
交返回 409 `STATE_IDEMPOTENCY_KEY_MISMATCH`，`details.previous_hash =
"epoch-worknet-collision"`。
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, confirm, emit_error, signed_request  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worknet", type=int, required=True)
    ap.add_argument("--content")
    ap.add_argument("--content-file")
    ap.add_argument("--metrics", help="inline JSON")
    ap.add_argument("--metrics-file")
    ap.add_argument("--requested-share")
    ap.add_argument("--idem-key", default=str(uuid.uuid4()))
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    if not (args.content or args.content_file):
        ap.error("--content or --content-file required")
    content = args.content or Path(args.content_file).read_text("utf-8")
    if len(content) > 50_000:
        ap.error("content exceeds 50 000 chars")

    if args.metrics and args.metrics_file:
        ap.error("pass --metrics OR --metrics-file, not both")
    metrics_raw = args.metrics or (
        Path(args.metrics_file).read_text("utf-8") if args.metrics_file else "{}"
    )
    try:
        metrics = json.loads(metrics_raw)
    except json.JSONDecodeError as e:
        ap.error(f"--metrics is not valid JSON: {e}")

    body = {"worknet_id": args.worknet, "content": content, "metrics": metrics}
    if args.requested_share is not None:
        # 服务端 schema 是 string format: decimal，但参数本身代表概率/份额，
        # 必须在 [0, 1]。客户端先解析+范围检查，省去服务端 400 + 含糊错误信息。
        try:
            share = Decimal(args.requested_share)
        except InvalidOperation:
            ap.error(f"--requested-share {args.requested_share!r} is not a valid decimal")
        if not (Decimal("0") <= share <= Decimal("1")):
            ap.error(f"--requested-share must be in [0, 1]; got {share}")
        body["requested_share"] = args.requested_share  # 原样发，保留 scale

    prompt = (
        "[TX] post weekly report:\n"
        f"     worknet:        {args.worknet}\n"
        f"     content len:    {len(content)} chars\n"
        f"     metrics keys:   {list(metrics.keys())}\n"
        + (f"     requested_share: {args.requested_share}\n" if args.requested_share else "")
        + f"     idem-key:       {args.idem_key}\n"
        "     proceed? (y/n) "
    )
    if not confirm(prompt, yes=args.yes):
        print(json.dumps({"cancelled": True}))
        return 0

    try:
        data = signed_request(
            "POST",
            sign_path="/reports",
            full_path="/reports",
            body=body,
            idempotency_key=args.idem_key,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
