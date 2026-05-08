#!/usr/bin/env python3
"""POST /v1/epochs/{market_id}/votes — 提交私密投票（latest_2026_05 形态）。

    submit-vote.py --market 6 \
                   --vote "0.5,0.3,0.2,0,0,0,0" \
                   --prediction "0.5,0.3,0.2,0,0,0,0" \
                   [--vote-revision 1] [--idem-key UUID] [--yes]

`--vote` 和 `--prediction` 都是 `,` 分隔的 string-decimal，按 worknet
position 顺序排列。简单形约束 `|Σ − 1| ≤ 1e-9`（D2 / mig 0047）。

需要两个签名：
  1. 内层 EMGVote — `latest_2026_05` 形态：
        principal, market_id, vote_revision, vote_hash, prediction_hash, timestamp
     `GOVNET_VOTE_TYPED_DATA_VARIANT` 可切到 main_spec / openapi 旧形态。
  2. 外层 EMGRequest — 标准 transport 信封。

`--vote-revision` 在每个 (principal, market) 范围内严格递增；首次提交 1，
重投递增 +1。生产服务端用 vote_revision 取代旧的 vote-level `nonce` 字段
(2026-05-08 deployment 后)。`/v1/principals/{p}/votes/{m}` 仅 reveal 后
可读，无 in-progress 查询接口，所以 vote_revision 必须客户端自跟。

⚠️ 投票一旦提交不可撤回。要修改投票必须用更高的 vote_revision 重新提
交，仅最高 revision 那一份会进入 Merkle 树（其它历史可在结算后通过
`/v1/epochs/{id}/votes/{principal}/history` 审计）。

旧 `--epoch` / `--vote-nonce` 仍可用作 alias，向后兼容已有调用方；
新调用方应该用 `--market` / `--vote-revision`。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from decimal import Decimal, getcontext
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.canonical import canonical_decimal_vector, keccak256  # noqa: E402
from lib.govnet_lib import EmgError, confirm, emit_error, fetch, normalize_phase, signed_request, wallet_address  # noqa: E402
from lib.sign import sign_emg_vote  # noqa: E402

getcontext().prec = 40

# 服务端 D2 / mig 0047: 简单形容差 |Σ − 1| ≤ 1e-9（应用层 + DB CHECK 都校验）
_SIMPLEX_TOLERANCE = Decimal("0.000000001")


def _parse_vector(s: str) -> List[Decimal]:
    return [Decimal(x.strip()) for x in s.split(",") if x.strip()]


def _phase_check(market_id: int) -> dict:
    epoch = fetch("GET", f"/epochs/{market_id}")
    phase = normalize_phase(epoch.get("phase") or epoch.get("status", ""))
    if phase != "voting_and_trading":
        raise EmgError(
            "BUSINESS_PHASE_MISMATCH",
            f"market {market_id} is in '{phase}' phase; voting only open during voting_and_trading",
            status=409,
        )
    return epoch


def _validate_simplex(vec: List[Decimal], label: str) -> None:
    if any(v < 0 or v > 1 for v in vec):
        raise EmgError("VALIDATION_SIMPLEX_CONSTRAINT_VIOLATED",
                       f"{label}: each entry must be in [0, 1]", status=422)
    total = sum(vec)
    if abs(total - Decimal("1")) > _SIMPLEX_TOLERANCE:
        raise EmgError("VALIDATION_SIMPLEX_CONSTRAINT_VIOLATED",
                       f"{label}: |Σ − 1| = {abs(total - Decimal('1'))}, exceeds 1e-9 tolerance",
                       status=422)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # market_id / epoch 都接受，至少给一个
    ap.add_argument("--market", type=int, help="market_id (preferred name)")
    ap.add_argument("--epoch", type=int, help="alias for --market (legacy)")
    ap.add_argument("--vote", required=True, help="comma-separated probabilities")
    ap.add_argument("--prediction", required=True, help="comma-separated probabilities")
    # vote_revision: 每个 (principal, market) 范围内严格递增；服务端 EMGVote
    # typed-data 字段 vote_revision (uint64)。旧 --vote-nonce 是 alias。
    ap.add_argument("--vote-revision", type=int, default=None,
                    help="per-market revision counter (1, 2, ... strictly ascending); default 1")
    ap.add_argument("--vote-nonce", type=int, default=None,
                    help="alias for --vote-revision (legacy name)")
    ap.add_argument("--idem-key", default=str(uuid.uuid4()))
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    market_id = args.market if args.market is not None else args.epoch
    if market_id is None:
        ap.error("must pass --market (or --epoch alias)")
    revision = (
        args.vote_revision if args.vote_revision is not None
        else args.vote_nonce if args.vote_nonce is not None
        else 1
    )

    try:
        vote = _parse_vector(args.vote)
        pred = _parse_vector(args.prediction)
        _validate_simplex(vote, "vote")
        _validate_simplex(pred, "prediction")
        _phase_check(market_id)
        if len(vote) != len(pred):
            raise EmgError("VALIDATION_INVALID_VOTE_VECTOR",
                           f"vote ({len(vote)}) and prediction ({len(pred)}) length mismatch",
                           status=422)
        prompt = (
            "[VOTE] about to submit:\n"
            f"     market:        {market_id}\n"
            f"     vote:          [{', '.join(format(v, 'f') for v in vote)}]\n"
            f"     prediction:    [{', '.join(format(v, 'f') for v in pred)}]\n"
            f"     vote_revision: {revision}\n"
            f"     idem-key:      {args.idem_key}\n"
            "     ⚠️  Votes are FINAL — to change, resubmit with HIGHER vote_revision.\n"
            "     proceed? (y/n) "
        )
        if not confirm(prompt, yes=args.yes):
            print(json.dumps({"cancelled": True}))
            return 0

        principal = wallet_address()
        from lib.govnet_lib import get_auth_info
        auth_info = get_auth_info()

        # latest_2026_05 EMGVote 必传 timestamp；旧形态会忽略
        timestamp = int(time.time())

        vote_hash = keccak256(canonical_decimal_vector(vote))
        pred_hash = keccak256(canonical_decimal_vector(pred))
        inner_sig = sign_emg_vote(
            principal=principal,
            market_id=market_id,
            vote_hash=vote_hash,
            prediction_hash=pred_hash,
            vote_revision=revision,
            timestamp=timestamp,
            auth_info=auth_info,
        )
        # 同时发 nonce + vote_revision 字段：openapi.yaml 仍把 nonce 标 required，
        # SKILL_API_LATEST 提到 vote_revision 是新名字。两个都带保险，多发的字段
        # 服务端会忽略；少发会被 schema 拒。
        body = {
            "vote": [format(v, "f") for v in vote],
            "prediction": [format(v, "f") for v in pred],
            "nonce": revision,
            "vote_revision": revision,
            "signature": inner_sig,
        }
        data = signed_request(
            "POST",
            sign_path=f"/epochs/{market_id}/votes",
            full_path=f"/epochs/{market_id}/votes",
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
