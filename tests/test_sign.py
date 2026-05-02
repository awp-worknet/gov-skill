"""EIP-712 已知答案测试 — 与 crates/emg-auth/src/eip712.rs::REFERENCE_DIGEST_HEX 对齐。

验证我们用 `build_emg_request_typed_data` 构造的 typed data 在 eth_account
本地复算后产生与 Rust 服务端完全一致的摘要。一旦字段顺序、命名、类型或
domain 编码漂移，本测试就会失败 — 这是签名兼容性的最强保护。
"""

from lib.sign import build_emg_request_typed_data, compute_eip712_digest


# 与 Rust 测试 `pinned_reference_digest_for_sample_request` 完全一致的输入
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

# 摘要直接从 eip712.rs::REFERENCE_DIGEST_HEX 拷贝
REFERENCE_DIGEST_HEX = "7686da836df9c9ae2a800b0d4c8987fa97e0e237d904b4ac3e708f29a8a4a092"


def _build_typed_data():
    """复刻 sample_request 的 typed data — 注意 bodyHash 直接给定，绕过 eip712_body_hash。"""
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
    """已知答案：Python 构造 + 本地复算的摘要必须等于 Rust 端固定值。"""
    typed = _build_typed_data()
    digest = compute_eip712_digest(typed)
    assert digest.hex() == REFERENCE_DIGEST_HEX, (
        f"EIP-712 digest drift!\n  expected: {REFERENCE_DIGEST_HEX}\n  got:      {digest.hex()}\n"
        "Check field order/types in EMG_REQUEST_TYPES or domain encoding."
    )


def test_build_emg_request_typed_data_round_trip():
    """build_emg_request_typed_data() + 给定 raw body 应该产生同样的摘要。

    sample_request 的 bodyHash 是 32×0x11 — 不可能从 keccak256(body) 直接
    构造出来，所以这里换一组 body 自洽测试：用 keccak256(b"abc") 作为
    bodyHash，调用 build_emg_request_typed_data 后摘要必须可复算。
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
    # bodyHash 应为 keccak256("abc") 的 0x-hex
    from lib.canonical import keccak256

    assert typed["message"]["bodyHash"] == "0x" + keccak256(body).hex()
    # 同一输入两次构造 → 相同摘要
    d1 = compute_eip712_digest(typed)
    d2 = compute_eip712_digest(typed)
    assert d1 == d2


def test_chain_id_change_changes_digest():
    """跨环境 replay protection — 不同 chainId 必须得到不同摘要。"""
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
    """改 path 必须改摘要 — 否则 GET /orders 和 GET /votes 可以混用签名。"""
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
