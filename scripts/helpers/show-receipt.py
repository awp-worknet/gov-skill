#!/usr/bin/env python3
"""把一个 fill / order / settlement payload 美化成人类可读的回执。

    show-receipt.py < fill.json                 # 从 stdin 读
    show-receipt.py --type fill --file fill.json
    show-receipt.py --type settlement --file results.json

输入是 JSON（单对象或 `{ items: [...] }`），输出是 stdout 的多行文本。
保留原始 JSON 也照样输出到 stderr 末尾，方便 agent 解析。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import fmt_amount, fmt_price  # noqa: E402


def _render_fill(o: dict) -> str:
    return (
        "[FILL] "
        f"order={o.get('order_id')}  fill={o.get('id') or o.get('fill_id')}\n"
        f"       worknet={o.get('worknet_id')}  side={o.get('side')}  role={o.get('role','')}\n"
        f"       price={fmt_price(o.get('price'))}  qty={fmt_amount(o.get('quantity'))}\n"
        f"       at={o.get('filled_at')}\n"
    )


def _render_order(o: dict) -> str:
    return (
        f"[ORDER] {o.get('id')}  status={o.get('status')}\n"
        f"        worknet={o.get('worknet_id')}  side={o.get('side')}  kind={o.get('kind')}\n"
        f"        qty={fmt_amount(o.get('total_quantity'))}  filled={fmt_amount(o.get('filled_quantity'))}\n"
        f"        avg_price={fmt_price(o.get('avg_fill_price'))}\n"
    )


def _render_settlement(o: dict) -> str:
    v = o.get("v_vector") or []
    w = o.get("w_vector") or []
    p0 = o.get("p_open_vector") or []
    n = max(len(v), len(w), len(p0))
    rows = "\n".join(
        f"     pos={i:>2}  V={fmt_price(v[i] if i<len(v) else None)}  "
        f"W={fmt_price(w[i] if i<len(w) else None)}  "
        f"P₀={fmt_price(p0[i] if i<len(p0) else None)}"
        for i in range(n)
    )
    return (
        f"[SETTLEMENT] epoch={o.get('epoch_id')}\n"
        f"     total_gov_tokens={fmt_amount(o.get('total_gov_tokens'))}\n"
        f"{rows}\n"
    )


_RENDERERS = {"fill": _render_fill, "order": _render_order, "settlement": _render_settlement}


def _autodetect(o: dict) -> str:
    if "v_vector" in o or "w_vector" in o:
        return "settlement"
    if "total_quantity" in o or "filled_quantity" in o:
        return "order"
    return "fill"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--type", choices=("fill", "order", "settlement"))
    ap.add_argument("--file", help="path to JSON; default stdin")
    args = ap.parse_args()

    raw = Path(args.file).read_text("utf-8") if args.file else sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": "MALFORMED_JSON", "detail": str(e)}))
        return 1

    items = data.get("items") if isinstance(data, dict) and "items" in data else [data]
    for o in items:
        kind = args.type or _autodetect(o)
        sys.stdout.write(_RENDERERS[kind](o))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
