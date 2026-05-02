#!/usr/bin/env python3
"""POST /v1/epochs/{id}/votes — 提交私密投票。

    submit-vote.py --epoch 6 \
                   --vote "0.5,0.3,0.2,0,0,0,0" \
                   --prediction "0.5,0.3,0.2,0,0,0,0" \
                   [--idem-key UUID] [--yes]

`--vote` 和 `--prediction` 都是 `,` 分隔的 string-decimal，按 worknet
position 顺序排列；总和必须严格等于 1（在精度容差内）。

需要两个签名：
  1. 内层 EMGVote（绑定 epoch + voteHash + predictionHash + nonce）
  2. 外层 EMGRequest（标准 transport）

⚠️ 投票一旦提交 **不可撤回**。要修改投票必须用更高的 `nonce` 重新提交，
仅最高 nonce 那一份会进入 Merkle 树（其它历史可在结算后通过
`/v1/epochs/{id}/votes/{principal}/history` 审计）。
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from decimal import Decimal, getcontext
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.canonical import canonical_decimal_vector, keccak256  # noqa: E402
from lib.govnet_lib import EmgError, confirm, emit_error, fetch, normalize_phase, signed_request, wallet_address  # noqa: E402
from lib.sign import sign_emg_vote  # noqa: E402

getcontext().prec = 40


def _parse_vector(s: str) -> List[Decimal]:
    return [Decimal(x.strip()) for x in s.split(",") if x.strip()]


def _phase_check(epoch_id: int) -> dict:
    epoch = fetch("GET", f"/v1/epochs/{epoch_id}")
    phase = normalize_phase(epoch.get("phase") or epoch.get("status", ""))
    if phase != "voting_and_trading":
        raise EmgError(
            "BUSINESS_PHASE_MISMATCH",
            f"epoch {epoch_id} is in '{phase}' phase; voting only open during voting_and_trading",
            status=409,
        )
    return epoch


def _validate_simplex(vec: List[Decimal], label: str) -> None:
    if any(v < 0 or v > 1 for v in vec):
        raise EmgError("VALIDATION_SIMPLEX_CONSTRAINT_VIOLATED",
                       f"{label}: each entry must be in [0, 1]", status=422)
    total = sum(vec)
    if abs(total - Decimal("1")) > Decimal("0.000001"):
        raise EmgError("VALIDATION_SIMPLEX_CONSTRAINT_VIOLATED",
                       f"{label}: Σ = {total}, expected 1", status=422)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--epoch", type=int, required=True)
    ap.add_argument("--vote", required=True, help="comma-separated probabilities")
    ap.add_argument("--prediction", required=True, help="comma-separated probabilities")
    # 默认用当前 Unix 秒数 — 与 EMG-SIG-V1 transport nonce 是两个独立计数器，
    # 但同样要求"严格大于上一次提交"；时间戳天然单调，省去用户手工跟踪历史
    # 的负担。如果 1 秒内重投两次（罕见），用 --vote-nonce 显式指定即可。
    ap.add_argument("--vote-nonce", type=int, default=None,
                    help="vote-level nonce (NOT EMG-SIG nonce); defaults to current Unix seconds")
    ap.add_argument("--idem-key", default=str(uuid.uuid4()))
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    if args.vote_nonce is None:
        import time
        args.vote_nonce = int(time.time())

    try:
        vote = _parse_vector(args.vote)
        pred = _parse_vector(args.prediction)
        _validate_simplex(vote, "vote")
        _validate_simplex(pred, "prediction")
        epoch = _phase_check(args.epoch)
        if len(vote) != len(pred):
            raise EmgError("VALIDATION_INVALID_VOTE_VECTOR",
                           f"vote ({len(vote)}) and prediction ({len(pred)}) length mismatch",
                           status=422)
        prompt = (
            "[VOTE] about to submit:\n"
            f"     epoch:      {args.epoch}\n"
            f"     vote:       [{', '.join(format(v, 'f') for v in vote)}]\n"
            f"     prediction: [{', '.join(format(v, 'f') for v in pred)}]\n"
            f"     nonce:      {args.vote_nonce}\n"
            f"     idem-key:   {args.idem_key}\n"
            "     ⚠️  Votes are FINAL — to change, resubmit with HIGHER nonce.\n"
            "     proceed? (y/n) "
        )
        if not confirm(prompt, yes=args.yes):
            print(json.dumps({"cancelled": True}))
            return 0

        principal = wallet_address()
        from lib.govnet_lib import get_auth_info  # 内部使用，避免循环导入污染
        auth_info = get_auth_info()

        vote_hash = keccak256(canonical_decimal_vector(vote))
        pred_hash = keccak256(canonical_decimal_vector(pred))
        inner_sig = sign_emg_vote(
            principal=principal,
            epoch=args.epoch,
            vote_hash=vote_hash,
            prediction_hash=pred_hash,
            nonce=args.vote_nonce,
            auth_info=auth_info,
        )
        body = {
            "vote": [format(v, "f") for v in vote],
            "prediction": [format(v, "f") for v in pred],
            "nonce": args.vote_nonce,
            "signature": inner_sig,
        }
        data = signed_request(
            "POST",
            sign_path=f"/epochs/{args.epoch}/votes",
            full_path=f"/v1/epochs/{args.epoch}/votes",
            body=body,
            principal=principal,
            idempotency_key=args.idem_key,
        )
    except EmgError as e:
        return emit_error(e)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
