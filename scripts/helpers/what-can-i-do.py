#!/usr/bin/env python3
"""按当前 phase 列出允许的操作 — agent 在不确定状态时优先调用本脚本。

    what-can-i-do.py [--epoch <id>]   # 不指定就读 /v1/epochs/current

输出：
    {
      "epoch_id": 6,
      "phase": "voting_and_trading",
      "next_transition_at": "2026-05-03T12:00:00Z",
      "available": [
        {"op": "list-markets", "script": "scripts/public/markets.py"},
        {"op": "submit-order", "script": "scripts/trade/submit-order.py"},
        ...
      ],
      "blocked": [
        {"op": "read-settlement", "reason": "epoch not yet completed"}
      ]
    }
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, fetch, normalize_phase  # noqa: E402


# 每个操作允许出现的 phase 集合 — 镜像 SKILL.md 的 phase × op 矩阵
_PHASE_MATRIX = {
    "list-markets":         {"pending", "voting_and_trading", "trading_only", "settling", "completed"},
    "list-worknets":        {"pending", "voting_and_trading", "trading_only", "settling", "completed"},
    "read-book":            {"pending", "voting_and_trading", "trading_only", "settling", "completed"},
    "read-klines":          {"pending", "voting_and_trading", "trading_only", "settling", "completed"},
    "read-private-state":   {"pending", "voting_and_trading", "trading_only", "settling", "completed"},
    "submit-order":         {"voting_and_trading", "trading_only"},
    "cancel-order":         {"voting_and_trading", "trading_only"},
    "submit-vote":          {"voting_and_trading"},
    "split-position":       {"voting_and_trading", "trading_only"},
    "merge-position":       {"voting_and_trading", "trading_only"},
    "read-settlement":      {"completed"},
    "post-comment":         {"pending", "voting_and_trading", "trading_only", "settling", "completed"},
    "post-report":          {"voting_and_trading", "trading_only"},
    "endorse-comment":      {"pending", "voting_and_trading", "trading_only", "settling", "completed"},
}

_SCRIPT_OF = {
    "list-markets": "scripts/public/markets.py",
    "list-worknets": "scripts/public/worknets.py",
    "read-book": "scripts/public/book.py",
    "read-klines": "scripts/public/klines.py",
    "read-private-state": "scripts/private/state.py",
    "submit-order": "scripts/trade/submit-order.py",
    "cancel-order": "scripts/trade/cancel-order.py",
    "submit-vote": "scripts/vote/submit-vote.py",
    "split-position": "scripts/positions/split.py",
    "merge-position": "scripts/positions/merge.py",
    "read-settlement": "scripts/public/epochs.py results --id <ID>",
    "post-comment": "scripts/content/post-comment.py",
    "post-report": "scripts/content/post-report.py",
    "endorse-comment": "scripts/content/endorse.py",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--epoch", type=int)
    args = ap.parse_args()

    try:
        if args.epoch is not None:
            data = fetch("GET", f"/epochs/{args.epoch}/phase")
        else:
            data = fetch("GET", "/epochs/current")
    except EmgError as e:
        return emit_error(e)

    phase = normalize_phase(data.get("phase") or data.get("status", ""))
    epoch_id = data.get("epoch_id") or data.get("id")
    next_at = data.get("next_transition_at") or data.get("trading_closes_at")

    available = []
    blocked = []
    for op, allowed in _PHASE_MATRIX.items():
        entry = {"op": op, "script": _SCRIPT_OF[op]}
        if phase in allowed:
            available.append(entry)
        else:
            blocked.append({**entry, "reason": f"current phase is '{phase}'"})

    print(json.dumps({
        "epoch_id": epoch_id,
        "phase": phase,
        "next_transition_at": next_at,
        "available": available,
        "blocked": blocked,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
