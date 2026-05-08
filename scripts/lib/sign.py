"""EMG-SIG-V1 EIP-712 signing — typed-data construction + awp-wallet bridge.

`sign_emg_request()` is the single public entry point. It does three things:
1. Compute the bodyHash via keccak256 (empty body uses 32 bytes of zero).
2. Build a typed-data JSON exactly matching `crates/emg-auth/src/eip712.rs`.
3. Call `awp-wallet sign-typed-data --data <json>` to obtain the 65-byte signature.

We do **not** hold the private key in Python; signing ALWAYS goes through awp-wallet.
But to run known-answer digest tests under tests/, we also expose
`compute_eip712_digest()`, which computes the same digest locally with eth_account —
that path never touches the private key, it's purely for cross-checking the
typed-data construction.

# EMGVote variant switch

The actual on-the-wire shape of EMGVote on the server changed in the
2026-05-08 deployment (see `docs/SKILL_API_LATEST.md` §2.3). `docs/openapi.yaml`
hasn't caught up yet. So this module supports all three shapes simultaneously:

- `latest_2026_05` (default): 6 fields including `principal/market_id/vote_revision/
  vote_hash/prediction_hash/timestamp`, snake_case, matches the **current production server**.
- `main_spec`: 5 fields (principal/epoch/voteHash/predictionHash/nonce, uint256),
  matches the legacy `01-MAIN-SPEC.md` §3.
- `openapi`: 4 fields (no principal, epoch/nonce uint64, camelCase), matches
  `02-openapi.yaml`'s SignedVoteRequest.signature description (now superseded by LATEST).

`GOVNET_VOTE_TYPED_DATA_VARIANT` selects between them. Production should use the default.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Dict, Optional

from .canonical import eip712_body_hash


# Matches the sol! macro in crates/emg-auth/src/eip712.rs. Field order, names,
# and types are all part of the typed-data hash and **must not change**.
EMG_REQUEST_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "EMGRequest": [
        {"name": "principal", "type": "address"},
        {"name": "method", "type": "string"},
        {"name": "path", "type": "string"},
        {"name": "query", "type": "string"},
        {"name": "bodyHash", "type": "bytes32"},
        {"name": "nonce", "type": "uint256"},
        {"name": "timestamp", "type": "uint256"},
    ],
}

# Vote inner typed data — primaryType differs, domain is the same.
# The three variants are documented in the module docstring under "EMGVote variant switch".

# Current production shape (since the 2026-05-08 deployment, see SKILL_API_LATEST.md §2.3).
EMG_VOTE_TYPES_LATEST_2026_05 = {
    "EIP712Domain": EMG_REQUEST_TYPES["EIP712Domain"],
    "EMGVote": [
        {"name": "principal", "type": "address"},
        {"name": "market_id", "type": "uint64"},
        {"name": "vote_revision", "type": "uint64"},
        {"name": "vote_hash", "type": "bytes32"},
        {"name": "prediction_hash", "type": "bytes32"},
        {"name": "timestamp", "type": "uint256"},
    ],
}

EMG_VOTE_TYPES_MAIN_SPEC = {
    "EIP712Domain": EMG_REQUEST_TYPES["EIP712Domain"],
    "EMGVote": [
        {"name": "principal", "type": "address"},
        {"name": "epoch", "type": "uint256"},
        {"name": "voteHash", "type": "bytes32"},
        {"name": "predictionHash", "type": "bytes32"},
        {"name": "nonce", "type": "uint256"},
    ],
}

EMG_VOTE_TYPES_OPENAPI = {
    "EIP712Domain": EMG_REQUEST_TYPES["EIP712Domain"],
    "EMGVote": [
        {"name": "epoch", "type": "uint64"},
        {"name": "voteHash", "type": "bytes32"},
        {"name": "predictionHash", "type": "bytes32"},
        {"name": "nonce", "type": "uint64"},
    ],
}


def _vote_variant() -> str:
    return os.environ.get("GOVNET_VOTE_TYPED_DATA_VARIANT", "latest_2026_05").lower()


def _vote_types() -> Dict:
    v = _vote_variant()
    if v == "openapi":
        return EMG_VOTE_TYPES_OPENAPI
    if v == "main_spec":
        return EMG_VOTE_TYPES_MAIN_SPEC
    return EMG_VOTE_TYPES_LATEST_2026_05


# Legacy alias — early callers imported this; subsequent code switches to `_vote_types()`.
EMG_VOTE_TYPES = EMG_VOTE_TYPES_LATEST_2026_05


def _domain(auth_info: Dict) -> Dict:
    """Extract the EIP-712 domain four-tuple from the /v1/auth/info response.

    The server returns `eip712_domain: { name, version, chainId, verifyingContract }`,
    but older versions might flatten the fields at the top level. Both shapes
    are accepted.
    """
    if "eip712_domain" in auth_info:
        d = auth_info["eip712_domain"]
    else:
        d = auth_info
    return {
        "name": d.get("name", "EMG"),
        "version": d.get("version", "1"),
        "chainId": int(d["chainId"]),
        "verifyingContract": d["verifyingContract"],
    }


def build_emg_request_typed_data(
    *,
    principal: str,
    method: str,
    path: str,
    query: str,
    body: bytes,
    nonce: int,
    timestamp: int,
    auth_info: Dict,
) -> Dict:
    """Build the JSON structure that awp-wallet sign-typed-data expects.

    `path` must be the POST-strip path — the server's axum router uses
    `nest("/v1", …)` to strip the prefix, so the auth middleware sees `/orders`,
    not `/v1/orders`. Exception: WS handshake uses method `WS_HELLO` and path
    `/v1/ws` (the WS dispatcher reads the full URI literal).
    """
    body_hash_hex = "0x" + eip712_body_hash(body).hex()
    return {
        "domain": _domain(auth_info),
        "primaryType": "EMGRequest",
        "types": EMG_REQUEST_TYPES,
        "message": {
            "principal": principal,
            "method": method.upper(),
            "path": path,
            "query": query,
            "bodyHash": body_hash_hex,
            "nonce": str(int(nonce)),
            "timestamp": str(int(timestamp)),
        },
    }


def build_emg_vote_typed_data(
    *,
    principal: str,
    market_id: int,
    vote_hash: bytes,
    prediction_hash: bytes,
    auth_info: Dict,
    vote_revision: Optional[int] = None,
    timestamp: Optional[int] = None,
    # Legacy compatibility parameters
    epoch: Optional[int] = None,
    nonce: Optional[int] = None,
) -> Dict:
    """Build the inner EMGVote typed data for voting.

    A vote requires two signatures: the outer EMGRequest goes through the
    standard transport, the inner EMGVote contains the vote integrity
    binding and its signature goes into the POST body's `signature` field.

    The shape is determined by `GOVNET_VOTE_TYPED_DATA_VARIANT` (see module
    docstring):
      - `latest_2026_05` (default / current production): uses market_id +
        vote_revision + timestamp; `vote_revision` + `timestamp` are required
        and `epoch`/`nonce` are ignored.
      - `main_spec`: 5 fields, market_id is sent as epoch and vote_revision
        is sent as nonce; `timestamp` is ignored; if the caller passes only
        epoch/nonce, that's also accepted.
      - `openapi`: 4 fields, like main_spec but without principal.

    All variants accept `market_id` as the unified name for epoch/market —
    the legacy `epoch=` parameter still works as a backward-compatibility
    entry point.
    """
    types = _vote_types()
    field_names = {f["name"] for f in types["EMGVote"]}

    # Unified mapping: pick from epoch / market_id
    market = market_id if market_id is not None else epoch
    if market is None:
        raise ValueError("must pass market_id (or legacy epoch=)")
    # Pick from vote_revision / nonce
    revision = vote_revision if vote_revision is not None else nonce
    if revision is None:
        raise ValueError("must pass vote_revision (or legacy nonce=)")

    message: Dict[str, str] = {}
    if "principal" in field_names:
        message["principal"] = principal
    if "market_id" in field_names:
        message["market_id"] = str(int(market))
    if "epoch" in field_names:
        message["epoch"] = str(int(market))
    if "vote_revision" in field_names:
        message["vote_revision"] = str(int(revision))
    if "nonce" in field_names:
        message["nonce"] = str(int(revision))
    if "vote_hash" in field_names:
        message["vote_hash"] = "0x" + vote_hash.hex()
    if "voteHash" in field_names:
        message["voteHash"] = "0x" + vote_hash.hex()
    if "prediction_hash" in field_names:
        message["prediction_hash"] = "0x" + prediction_hash.hex()
    if "predictionHash" in field_names:
        message["predictionHash"] = "0x" + prediction_hash.hex()
    if "timestamp" in field_names:
        if timestamp is None:
            raise ValueError("latest_2026_05 EMGVote requires timestamp")
        message["timestamp"] = str(int(timestamp))

    return {
        "domain": _domain(auth_info),
        "primaryType": "EMGVote",
        "types": types,
        "message": message,
    }


# --- awp-wallet bridge -------------------------------------------------------


class WalletError(RuntimeError):
    """awp-wallet process returned non-zero or its output could not be parsed."""


def _run_wallet(args, *, stdin: Optional[str] = None) -> str:
    """Thin wrapper that uniformly invokes awp-wallet. The `AWP_WALLET` env var overrides the binary path."""
    bin_name = os.environ.get("AWP_WALLET", "awp-wallet")
    try:
        proc = subprocess.run(
            [bin_name, *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise WalletError(
            f"awp-wallet not on PATH (set AWP_WALLET or install from "
            f"https://github.com/awp-core/awp-wallet)"
        ) from e
    if proc.returncode != 0:
        raise WalletError(
            f"awp-wallet {' '.join(args)} exited {proc.returncode}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def wallet_address() -> str:
    """`awp-wallet receive` → 0x-hex checksummed address.

    Multi-version compatibility:
    - Try `--json` mode first (newer awp-wallet). The JSON field name may be
      `address` or `eoaAddress`; both are accepted.
    - Older versions lack `--json`: fall back to `receive` with no args and
      grep the first 0x-prefixed 40-hex token from stdout.
    """
    import re

    # First try --json
    try:
        out = _run_wallet(["receive", "--json"])
        try:
            data = json.loads(out)
            addr = data.get("address") or data.get("eoaAddress")
            if addr:
                return addr
        except json.JSONDecodeError:
            pass
    except WalletError:
        pass

    # Fallback: plain text mode, regex out the address
    out = _run_wallet(["receive"])
    match = re.search(r"0x[0-9a-fA-F]{40}", out)
    if match:
        return match.group(0)
    raise WalletError(
        f"could not parse wallet address from `awp-wallet receive` output: {out!r}"
    )


def wallet_sign_typed_data(typed_data: Dict) -> str:
    """`awp-wallet sign-typed-data --data <json>` → 65-byte 0x-hex signature."""
    out = _run_wallet(["sign-typed-data", "--data", json.dumps(typed_data)])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise WalletError(
            f"awp-wallet sign-typed-data returned non-JSON: {out!r}"
        ) from e
    sig = data.get("signature")
    if not sig or not sig.startswith("0x"):
        raise WalletError(f"awp-wallet sign-typed-data missing signature in {data}")
    return sig


def sign_emg_request(
    *,
    principal: str,
    method: str,
    path: str,
    query: str,
    body: bytes,
    nonce: int,
    timestamp: int,
    auth_info: Dict,
    actor: Optional[str] = None,
) -> Dict[str, str]:
    """End to end: build typed data → call awp-wallet to sign → return the five-tuple header.

    `actor` defaults to `principal`. When a Manager signs on behalf of someone,
    callers should pass the Manager's address explicitly — the server will
    look up `AWPRegistry.delegates`.
    """
    typed_data = build_emg_request_typed_data(
        principal=principal,
        method=method,
        path=path,
        query=query,
        body=body,
        nonce=nonce,
        timestamp=timestamp,
        auth_info=auth_info,
    )
    signature = wallet_sign_typed_data(typed_data)
    headers = {
        "X-EMG-Principal": principal,
        "X-EMG-Nonce": str(int(nonce)),
        "X-EMG-Timestamp": str(int(timestamp)),
        "X-EMG-Signature": signature,
    }
    if actor is not None and actor.lower() != principal.lower():
        headers["X-EMG-Actor"] = actor
    return headers


def sign_emg_vote(
    *,
    principal: str,
    market_id: int,
    vote_hash: bytes,
    prediction_hash: bytes,
    auth_info: Dict,
    vote_revision: Optional[int] = None,
    timestamp: Optional[int] = None,
    epoch: Optional[int] = None,
    nonce: Optional[int] = None,
) -> str:
    """Inner vote signature — returns 65-byte 0x-hex. Goes into the `signature` field of the POST body.

    The `latest_2026_05` shape (default) requires `vote_revision` + `timestamp`;
    legacy `main_spec` / `openapi` shapes treat vote_revision as nonce and
    ignore `timestamp`. `epoch` is the legacy alias for `market_id`, kept only
    as a backward-compatibility input.
    """
    typed_data = build_emg_vote_typed_data(
        principal=principal,
        market_id=market_id,
        vote_hash=vote_hash,
        prediction_hash=prediction_hash,
        vote_revision=vote_revision,
        timestamp=timestamp,
        epoch=epoch,
        nonce=nonce,
        auth_info=auth_info,
    )
    return wallet_sign_typed_data(typed_data)


# --- Local digest computation (for known-answer tests only; never touches the private key) ---


def compute_eip712_digest(typed_data: Dict) -> bytes:
    """Locally recompute the EIP-712 digest `keccak256("\\x19\\x01" || domainSeparator || hashStruct)`.

    Used in tests/test_sign.py to feed the typed data we built into
    eth_account's `encode_typed_data` — if the digest equals
    REFERENCE_DIGEST_HEX, that proves field order, types, and encoding
    line up with the Rust reference implementation. **Never** triggers signing,
    so no private-key access happens.

    Note: eth_account 0.13.x's `encode_typed_data` requires the
    `full_message=` keyword argument; the returned `SignableMessage` exposes
    `header` (domain separator) and `body` (hashStruct), and concatenating
    them gives the EIP-712 digest.
    """
    from eth_account.messages import encode_typed_data
    from .canonical import keccak256

    sm = encode_typed_data(full_message=typed_data)
    return keccak256(b"\x19\x01" + sm.header + sm.body)
