#!/usr/bin/env python3
"""epoch 信息 — current / by-id / phase / results / voters / merkle / proof / history。

    epochs.py current
    epochs.py get --id 6
    epochs.py phase --id 6
    epochs.py results --id 5
    epochs.py voters --id 6 [--limit 100] [--cursor <opaque>] [--all-pages]
    epochs.py merkle --id 5
    epochs.py proof --id 5 --principal 0xabc…
    epochs.py history --id 5 --principal 0xabc… [--all-pages]

`voters` 和 `history` 是 cursor-paginated 端点，加 `--all-pages` 自动
跟着 next_cursor 取完所有页面（默认 max-pages=100 防呆）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, fetch, paginate_all  # noqa: E402


def _add_paging(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--cursor", default=None)
    p.add_argument("--all-pages", action="store_true",
                   help="follow next_cursor until exhausted")
    p.add_argument("--max-pages", type=int, default=100,
                   help="safety cap when --all-pages")


def _paging_params(args) -> dict:
    p = {"limit": args.limit}
    if args.cursor:
        p["cursor"] = args.cursor
    return p


def _maybe_paginate(args, path):
    if args.all_pages:
        return paginate_all(
            lambda p: fetch("GET", path, params=p),
            initial_params=_paging_params(args),
            max_pages=args.max_pages,
        )
    return fetch("GET", path, params=_paging_params(args))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("current")

    p = sub.add_parser("get"); p.add_argument("--id", type=int, required=True)
    p = sub.add_parser("phase"); p.add_argument("--id", type=int, required=True)
    p = sub.add_parser("results"); p.add_argument("--id", type=int, required=True)

    p = sub.add_parser("voters"); p.add_argument("--id", type=int, required=True); _add_paging(p)
    p = sub.add_parser("merkle"); p.add_argument("--id", type=int, required=True)

    p = sub.add_parser("proof")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--principal", required=True)

    p = sub.add_parser("history")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--principal", required=True)
    _add_paging(p)

    args = ap.parse_args()

    try:
        if args.cmd == "current":
            data = fetch("GET", "/v1/epochs/current")
        elif args.cmd == "get":
            data = fetch("GET", f"/v1/epochs/{args.id}")
        elif args.cmd == "phase":
            data = fetch("GET", f"/v1/epochs/{args.id}/phase")
        elif args.cmd == "results":
            data = fetch("GET", f"/v1/epochs/{args.id}/results")
        elif args.cmd == "voters":
            data = _maybe_paginate(args, f"/v1/epochs/{args.id}/voters")
        elif args.cmd == "merkle":
            data = fetch("GET", f"/v1/epochs/{args.id}/merkle-root")
        elif args.cmd == "proof":
            data = fetch("GET", f"/v1/epochs/{args.id}/votes/{args.principal}/proof")
        elif args.cmd == "history":
            data = _maybe_paginate(args, f"/v1/epochs/{args.id}/votes/{args.principal}/history")
        else:
            ap.error(f"unknown subcommand {args.cmd}")
    except EmgError as e:
        return emit_error(e)

    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
