#!/usr/bin/env python3
"""距离下一个 phase boundary 还有多久 — 纯本地，不发请求。

    countdown.py
    countdown.py --epoch 6

输出：
    {
      "epoch_id": 6,
      "phase": "voting_and_trading",
      "next_transition_at": "2026-05-03T12:00:00Z",
      "next_phase": "trading_only",
      "remaining_seconds": 67200,
      "remaining_human": "18h 40m 0s"
    }
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, fetch, normalize_phase  # noqa: E402


def _human(seconds: int) -> str:
    if seconds < 0:
        return "0s (passed)"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


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

    next_at = data.get("next_transition_at") or data.get("trading_closes_at")
    if not next_at:
        print(json.dumps({"error": "NEXT_TRANSITION_UNKNOWN", "raw": data}))
        return 1

    # ISO-8601 — Python 3.11+ 自带 fromisoformat 支持 Z
    s = next_at.replace("Z", "+00:00")
    target = datetime.fromisoformat(s).astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    remaining = int((target - now).total_seconds())

    print(json.dumps({
        "epoch_id": data.get("epoch_id") or data.get("id"),
        "phase": normalize_phase(data.get("phase") or data.get("status", "")),
        "next_transition_at": next_at,
        "next_phase": data.get("next_phase"),
        "remaining_seconds": remaining,
        "remaining_human": _human(remaining),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
