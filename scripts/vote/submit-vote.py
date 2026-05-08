#!/usr/bin/env python3
"""POST /v1/epochs/{market_id}/votes — submit a private vote (latest_2026_05 shape).

    submit-vote.py --market 6 \
                   --vote "0.5,0.3,0.2,0,0,0,0" \
                   --prediction "0.5,0.3,0.2,0,0,0,0" \
                   [--vote-revision 1] [--idem-key UUID] [--yes]

`--vote` and `--prediction` are both `,`-separated string-decimal values
arranged in worknet position order. Simplex constraint: `|Σ − 1| ≤ 1e-9`
(D2 / mig 0047).

Two signatures are required:
  1. Inner EMGVote — `latest_2026_05` shape:
        principal, market_id, vote_revision, vote_hash, prediction_hash, timestamp
     `GOVNET_VOTE_TYPED_DATA_VARIANT` can switch to the legacy main_spec / openapi shapes.
  2. Outer EMGRequest — standard transport envelope.

`--vote-revision` is strictly ascending within each (principal, market) scope;
first submit is 1, re-vote increments by +1. The production server replaces
the legacy vote-level `nonce` field with vote_revision (post-2026-05-08
deployment). `/v1/principals/{p}/votes/{m}` is only readable after reveal —
there is no in-progress query endpoint — so vote_revision must be tracked
client-side.

Warning: a vote, once submitted, cannot be retracted. To change a vote, you
must resubmit with a HIGHER vote_revision; only the highest revision enters
the Merkle tree (other history can be audited after settlement via
`/v1/epochs/{id}/votes/{principal}/history`).

Legacy `--epoch` / `--vote-nonce` are still accepted as aliases for
backward compatibility with existing callers; new callers should use
`--market` / `--vote-revision`.
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

# Server D2 / mig 0047: simplex tolerance |Σ − 1| ≤ 1e-9 (validated at both the application layer and DB CHECK)
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
    # Both market_id / epoch are accepted; at least one is required
    ap.add_argument("--market", type=int, help="market_id (preferred name)")
    ap.add_argument("--epoch", type=int, help="alias for --market (legacy)")
    ap.add_argument("--vote", required=True, help="comma-separated probabilities")
    ap.add_argument("--prediction", required=True, help="comma-separated probabilities")
    # vote_revision: strictly ascending within each (principal, market) scope; the
    # server's EMGVote typed-data field is vote_revision (uint64). --vote-nonce is a legacy alias.
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
            "     WARNING: Votes are FINAL — to change, resubmit with HIGHER vote_revision.\n"
            "     proceed? (y/n) "
        )
        if not confirm(prompt, yes=args.yes):
            print(json.dumps({"cancelled": True}))
            return 0

        principal = wallet_address()
        from lib.govnet_lib import get_auth_info
        auth_info = get_auth_info()

        # latest_2026_05 EMGVote requires timestamp; legacy shapes ignore it
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
        # The fields OpenAPI's SignedVoteRequest explicitly lists: vote / prediction /
        # nonce / signature. The server internally maps body.nonce to the new model's
        # vote_revision; the client only sends spec-declared fields to prevent a
        # strict-validation server from rejecting the unknown `vote_revision` field.
        # `vote_revision` still appears in the EMGVote typed-data — but that's
        # signing material, not a body field.
        body = {
            "vote": [format(v, "f") for v in vote],
            "prediction": [format(v, "f") for v in pred],
            "nonce": revision,
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
