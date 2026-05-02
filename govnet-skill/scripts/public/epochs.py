#!/usr/bin/env python3
"""epoch 信息 — current / by-id / phase / results / voters / merkle / proof / history。

    epochs.py current
    epochs.py get --id 6
    epochs.py phase --id 6
    epochs.py results --id 5
    epochs.py voters --id 6 [--limit 100] [--cursor <opaque>]
    epochs.py merkle --id 5
    epochs.py proof --id 5 --principal 0xabc…
    epochs.py history --id 5 --principal 0xabc…
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, fetch  # noqa: E402


def _add_paging(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--cursor", default=None)


def _paging_params(args) -> dict:
    return {"limit": args.limit, "cursor": args.cursor}


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
            data = fetch("GET", f"/v1/epochs/{args.id}/voters", params=_paging_params(args))
        elif args.cmd == "merkle":
            data = fetch("GET", f"/v1/epochs/{args.id}/merkle-root")
        elif args.cmd == "proof":
            data = fetch("GET", f"/v1/epochs/{args.id}/votes/{args.principal}/proof")
        elif args.cmd == "history":
            data = fetch(
                "GET",
                f"/v1/epochs/{args.id}/votes/{args.principal}/history",
                params=_paging_params(args),
            )
        else:
            ap.error(f"unknown subcommand {args.cmd}")
    except EmgError as e:
        return emit_error(e)

    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
