#!/usr/bin/env python3
"""POST /v1/comments — 发表评论。

    post-comment.py --content "..." [--worknet <id>] [--idem-key UUID] [--yes]
    post-comment.py --content-file note.md   # 内容从文件读

`--worknet` 可省（论坛级评论）。content 上限 10 000 字符（服务端 enforce）。
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, confirm, emit_error, signed_request  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--content")
    ap.add_argument("--content-file")
    ap.add_argument("--worknet", type=int)
    ap.add_argument("--idem-key", default=str(uuid.uuid4()))
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    if args.content_file and args.content:
        ap.error("pass --content OR --content-file, not both")
    content = args.content
    if args.content_file:
        content = Path(args.content_file).read_text("utf-8")
    if not content:
        ap.error("--content (or --content-file) is required")
    if len(content) > 10_000:
        ap.error("comment content exceeds 10 000 chars")

    body = {"content": content}
    if args.worknet is not None:
        body["worknet_id"] = args.worknet

    preview = content[:240] + ("…" if len(content) > 240 else "")
    prompt = (
        "[TX] post comment:\n"
        f"     worknet:   {args.worknet if args.worknet is not None else '(global)'}\n"
        f"     content:   {preview!r}\n"
        f"     idem-key:  {args.idem_key}\n"
        "     proceed? (y/n) "
    )
    if not confirm(prompt, yes=args.yes):
        print(json.dumps({"cancelled": True}))
        return 0

    try:
        data = signed_request(
            "POST",
            sign_path="/comments",
            full_path="/v1/comments",
            body=body,
            idempotency_key=args.idem_key,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
