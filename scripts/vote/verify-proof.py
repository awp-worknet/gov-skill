#!/usr/bin/env python3
"""Locally verify a Merkle inclusion proof — sends no request.

    verify-proof.py --proof-file proof.json
    verify-proof.py --epoch 5 --principal 0x… --fetch   # auto-GET the proof

Logic:
1. Take leaf_hash + siblings[] + leaf_index.
2. Rebuild the path bottom-up (the parity of `leaf_index` determines the sibling direction).
3. Compare the rebuilt root with the `merkle_root` field.

Only verifies the proof's internal consistency — comparing against the
on-chain RootNet requires reading the root from chain, which this script
does not do (the user can do it manually with cast call or with awp-wallet
outside of govnet-skill).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.govnet_lib import EmgError, emit_error, fetch  # noqa: E402


def _b64(s: str) -> bytes:
    # The server schema marks this base64 — Python's `base64.b64decode` requires padding.
    pad = "=" * (-len(s) % 4)
    return base64.b64decode(s + pad)


def _verify(leaf: bytes, siblings: list, leaf_index: int) -> bytes:
    h = leaf
    idx = leaf_index
    for sib in siblings:
        if idx & 1:
            # leaf on the right → sibling on the left
            h = hashlib.sha256(_b64(sib) + h).digest()
        else:
            h = hashlib.sha256(h + _b64(sib)).digest()
        idx >>= 1
    return h


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--proof-file", help="JSON file with the proof object")
    ap.add_argument("--epoch", type=int)
    ap.add_argument("--principal")
    ap.add_argument("--fetch", action="store_true",
                    help="GET /v1/epochs/{id}/votes/{principal}/proof first")
    args = ap.parse_args()

    try:
        if args.fetch:
            if args.epoch is None or not args.principal:
                ap.error("--fetch requires --epoch and --principal")
            proof = fetch(
                "GET",
                f"/epochs/{args.epoch}/votes/{args.principal}/proof",
            )
        elif args.proof_file:
            proof = json.loads(Path(args.proof_file).read_text("utf-8"))
        else:
            ap.error("need --proof-file or --fetch + --epoch + --principal")

        leaf = _b64(proof["leaf_hash"])
        computed = _verify(leaf, proof.get("siblings", []), int(proof["leaf_index"]))
        expected = _b64(proof["merkle_root"])
        ok = computed == expected
        out = {
            "ok": ok,
            "epoch_id": proof.get("epoch_id"),
            "principal": proof.get("principal"),
            "computed_root": base64.b64encode(computed).decode("ascii").rstrip("="),
            "expected_root": base64.b64encode(expected).decode("ascii").rstrip("="),
        }
        print(json.dumps(out, indent=2))
        return 0 if ok else 7
    except EmgError as e:
        return emit_error(e)
    except (KeyError, ValueError) as e:
        print(json.dumps({"error": "MALFORMED_PROOF", "detail": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
