"""查询规范化 — 已知答案向量从 crates/emg-auth/src/canonical.rs::tests 移植。

任何漂移都会让 EIP-712 摘要与服务端不一致，所以这些测试 **必须** 始终
通过。失败 ≡ 客户端会被服务端 401 拒绝。
"""

import pytest

from lib.canonical import (
    CanonicalError,
    canonicalize_query,
    eip712_body_hash,
    idempotency_body_hash,
    keccak256,
)


# --- eip712_body_hash --------------------------------------------------------


def test_body_hash_empty_is_zero():
    assert eip712_body_hash(b"") == b"\x00" * 32


def test_body_hash_nonempty_matches_keccak():
    body = b'{"side":"buy"}'
    assert eip712_body_hash(body) == keccak256(body)


def test_body_hash_distinguishes_one_byte_change():
    assert eip712_body_hash(b"abc") != eip712_body_hash(b"abd")


# --- idempotency_body_hash (SHA-256) ----------------------------------------


def test_idempotency_uses_sha256_not_keccak():
    h = idempotency_body_hash(b"hello")
    # SHA-256("hello") = 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
    assert h.startswith("0x2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")


def test_idempotency_is_lowercase_66_chars():
    h = idempotency_body_hash(b"test body")
    assert h.startswith("0x") and len(h) == 66
    assert h[2:].islower()


def test_idempotency_empty_body():
    # SHA-256("") = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
    assert idempotency_body_hash(b"").startswith("0xe3b0c44298fc1c14")


# --- canonicalize_query: empty + leading-? ----------------------------------


def test_empty_query_returns_empty():
    assert canonicalize_query("") == ""
    assert canonicalize_query("?") == ""


def test_leading_question_stripped():
    assert canonicalize_query("?a=1") == "a=1"
    assert canonicalize_query("a=1") == "a=1"


# --- sort behaviour ----------------------------------------------------------


def test_pairs_sorted_ascending_by_key():
    assert canonicalize_query("?b=2&a=1") == "a=1&b=2"


def test_duplicate_keys_sorted_by_value():
    assert canonicalize_query("?a=2&a=1") == "a=1&a=2"


def test_spec_example_input_canonicalizes_correctly():
    # spec §9.3.4 example input — 与 Rust 测试同名，固定 ascending lex 排序
    assert canonicalize_query("?epoch=5&principal=0xabc") == "epoch=5&principal=0xabc"


# --- percent decoding/encoding equivalence ----------------------------------


def test_uppercase_percent_decodes_to_unreserved():
    # %41 == 'A' (unreserved) → 还原成裸 A
    assert canonicalize_query("?a=%41") == "a=A"


def test_lowercase_percent_round_trip():
    assert canonicalize_query("?a=%2A") == "a=%2a"
    assert canonicalize_query("?a=%2a") == "a=%2a"


def test_space_encodes_as_percent_20():
    assert canonicalize_query("?a=hi%20there") == "a=hi%20there"


def test_plus_is_literal_not_space():
    # 不套用 form-urlencoded 的 + 即空格语义
    assert canonicalize_query("?a=%2b") == "a=%2b"
    assert canonicalize_query("?a=+") == "a=%2b"


def test_unicode_percent_lowercases_hex():
    # 'ä' = U+00E4, UTF-8 = c3 a4
    assert canonicalize_query("?a=%C3%A4") == "a=%c3%a4"
    assert canonicalize_query("?a=%c3%a4") == "a=%c3%a4"


def test_raw_unicode_input_encodes_byte_by_byte_lowercase():
    assert canonicalize_query("?name=ä") == "name=%c3%a4"


def test_unreserved_chars_preserved():
    assert canonicalize_query("?k=A-z.0_9~") == "k=A-z.0_9~"


# --- missing/empty value -----------------------------------------------------


def test_missing_equals_treated_as_empty():
    assert canonicalize_query("?flag") == "flag="


def test_explicit_empty_value():
    assert canonicalize_query("?flag=") == "flag="


# --- malformed input rejected -----------------------------------------------


def test_truncated_percent_escape_rejected():
    with pytest.raises(CanonicalError):
        canonicalize_query("?a=%4")
    with pytest.raises(CanonicalError):
        canonicalize_query("?a=%")


def test_non_hex_percent_escape_rejected():
    with pytest.raises(CanonicalError):
        canonicalize_query("?a=%G0")


def test_invalid_utf8_rejected():
    with pytest.raises(CanonicalError):
        canonicalize_query("?a=%ff")


# --- idempotence -------------------------------------------------------------


@pytest.mark.parametrize(
    "input_str",
    [
        "?b=2&a=1",
        "?a=%41&b=%2a",
        "?a=2&a=1",
        "?name=café",
        "?epoch=5&principal=0xabc",
    ],
)
def test_canonicalization_is_idempotent(input_str):
    once = canonicalize_query(input_str)
    twice = canonicalize_query(once)
    assert once == twice


def test_semantically_equivalent_inputs_collapse():
    a = canonicalize_query("?a=A&b=B")
    b = canonicalize_query("?b=B&a=A")
    c = canonicalize_query("?a=%41&b=%42")
    assert a == b == c
