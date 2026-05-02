#!/usr/bin/env python3
"""列出 markets / 取单个 market 详情。

EMG 把每周 emission market 称为 epoch — 因为 OpenAPI 把列表入口放在
`/v1/epochs/...`。本脚本提供两种用法：

    markets.py                 # 列出所有 markets（最近 N 个 epoch）
    markets.py --id 6          # 单个 market 的 worknets[] 详情
    markets.py --status open   # 过滤当前可下单的 market

输出统一是 `{ items: [...] }`（list 模式）或单个对象（--id 模式）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, fetch, fetch_market, normalize_phase  # noqa: E402


def _list_open_markets() -> dict:
    """没有 `/v1/markets` 列表端点时的回落 — 用 `/v1/epochs/current` 给出当前 market。"""
    cur = fetch("GET", "/v1/epochs/current")
    return {"items": [cur]} if cur else {"items": []}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", type=int, help="single market by id (epoch_id)")
    ap.add_argument("--status", help="filter to phase (e.g. voting_and_trading)")
    args = ap.parse_args()

    try:
        if args.id is not None:
            data = fetch_market(args.id)
        else:
            try:
                data = fetch("GET", "/v1/markets")
            except EmgError as e:
                if e.status == 404:
                    data = _list_open_markets()
                else:
                    raise
    except EmgError as e:
        return emit_error(e)

    if args.status and isinstance(data, dict) and "items" in data:
        wanted = normalize_phase(args.status)
        data["items"] = [
            m for m in data["items"]
            if normalize_phase(m.get("phase", m.get("status", ""))) == wanted
        ]

    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
