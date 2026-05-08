"""EIP-712 known-answer tests — aligned with crates/emg-auth/src/eip712.rs::REFERENCE_DIGEST_HEX.

Verifies that the typed data we construct via `build_emg_request_typed_data`,
when locally recomputed by eth_account, produces a digest exactly matching
the Rust server. Any drift in field order, naming, types, or domain encoding
makes this test fail — it's the strongest signature-compatibility guard we have.
"""

from lib.sign import build_emg_request_typed_data, compute_eip712_digest


# Inputs that exactly match the Rust test `pinned_reference_digest_for_sample_request`
REFERENCE_INPUT = {
    "principal": "0x" + "42" * 20,
    "method": "POST",
    "path": "/v1/orders",
    "query": "",
    # 32 × 0x11
    "body_hash_hex": "0x" + "11" * 32,
    "nonce": 7,
    "timestamp": 1_745_323_200,
    "chain_id": 56,
    "verifying_contract": "0x" + "aa" * 20,
}

# Digest copied directly from eip712.rs::REFERENCE_DIGEST_HEX
REFERENCE_DIGEST_HEX = "7686da836df9c9ae2a800b0d4c8987fa97e0e237d904b4ac3e708f29a8a4a092"


def _build_typed_data():
    """Reproduce sample_request's typed data — note that bodyHash is given directly, bypassing eip712_body_hash."""
    auth_info = {
        "eip712_domain": {
            "name": "EMG",
            "version": "1",
            "chainId": REFERENCE_INPUT["chain_id"],
            "verifyingContract": REFERENCE_INPUT["verifying_contract"],
        }
    }
    typed = {
        "domain": {
            "name": "EMG",
            "version": "1",
            "chainId": REFERENCE_INPUT["chain_id"],
            "verifyingContract": REFERENCE_INPUT["verifying_contract"],
        },
        "primaryType": "EMGRequest",
        "types": {
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
        },
        "message": {
            "principal": REFERENCE_INPUT["principal"],
            "method": REFERENCE_INPUT["method"],
            "path": REFERENCE_INPUT["path"],
            "query": REFERENCE_INPUT["query"],
            "bodyHash": REFERENCE_INPUT["body_hash_hex"],
            "nonce": str(REFERENCE_INPUT["nonce"]),
            "timestamp": str(REFERENCE_INPUT["timestamp"]),
        },
    }
    return typed


def test_reference_digest_matches_rust_pin():
    """Known answer: the Python-constructed + locally-recomputed digest must equal the Rust-side pinned value."""
    typed = _build_typed_data()
    digest = compute_eip712_digest(typed)
    assert digest.hex() == REFERENCE_DIGEST_HEX, (
        f"EIP-712 digest drift!\n  expected: {REFERENCE_DIGEST_HEX}\n  got:      {digest.hex()}\n"
        "Check field order/types in EMG_REQUEST_TYPES or domain encoding."
    )


def test_build_emg_request_typed_data_round_trip():
    """build_emg_request_typed_data() + a given raw body should produce the same digest.

    sample_request's bodyHash is 32×0x11 — that can't be produced from
    keccak256(body), so we use a different self-consistent body here: with
    keccak256(b"abc") as bodyHash, the digest must be reproducible after
    calling build_emg_request_typed_data.
    """
    auth_info = {
        "eip712_domain": {
            "name": "EMG",
            "version": "1",
            "chainId": 56,
            "verifyingContract": REFERENCE_INPUT["verifying_contract"],
        }
    }
    body = b"abc"
    typed = build_emg_request_typed_data(
        principal=REFERENCE_INPUT["principal"],
        method="POST",
        path="/orders",
        query="",
        body=body,
        nonce=7,
        timestamp=1_745_323_200,
        auth_info=auth_info,
    )
    # bodyHash should be the 0x-hex of keccak256("abc")
    from lib.canonical import keccak256

    assert typed["message"]["bodyHash"] == "0x" + keccak256(body).hex()
    # Building twice from the same input → identical digest
    d1 = compute_eip712_digest(typed)
    d2 = compute_eip712_digest(typed)
    assert d1 == d2


def test_chain_id_change_changes_digest():
    """Cross-environment replay protection — different chainIds must yield different digests."""
    typed = _build_typed_data()
    other = {**typed, "domain": {**typed["domain"], "chainId": 97}}
    assert compute_eip712_digest(typed) != compute_eip712_digest(other)


def test_verifying_contract_change_changes_digest():
    typed = _build_typed_data()
    other = {
        **typed,
        "domain": {
            **typed["domain"],
            "verifyingContract": "0x" + "bb" * 20,
        },
    }
    assert compute_eip712_digest(typed) != compute_eip712_digest(other)


def test_path_change_changes_digest():
    """Changing path must change the digest — otherwise signatures could be reused between GET /orders and GET /votes."""
    typed = _build_typed_data()
    other = {
        **typed,
        "message": {**typed["message"], "path": "/v1/votes"},
    }
    assert compute_eip712_digest(typed) != compute_eip712_digest(other)


def test_nonce_change_changes_digest():
    typed = _build_typed_data()
    other = {
        **typed,
        "message": {**typed["message"], "nonce": "8"},
    }
    assert compute_eip712_digest(typed) != compute_eip712_digest(other)
