"""rust_decimal 二进制兼容序列化的已知答案测试。

每条 KAT 都是按 `rust_decimal::Decimal::serialize` 的 16 字节内存 layout
手算出来的。测试失败 ≡ 投票 voteHash 与服务端不一致 ≡ 所有 vote 提交
被服务端 ecrecover 拒掉。
"""

from decimal import Decimal

import pytest

from lib.canonical import (
    canonical_decimal_vector,
    keccak256,
    rust_decimal_serialize,
)


# 实际 layout（已用 Rust cargo 1.94 + rust_decimal 1.x 真程序交叉验证）：
# bytes[0..4]   = flags (u32 LE)：bits 16..23 = scale (0..28)，bit 31 = sign
# bytes[4..8]   = lo    (mantissa 低 32 位)
# bytes[8..12]  = mid   (mantissa 中 32 位)
# bytes[12..16] = hi    (mantissa 高 32 位)


@pytest.mark.parametrize(
    "value, expected_hex",
    [
        # 0.5 → flags=scale1, lo=5
        ("0.5",     "00000100" "05000000" "00000000" "00000000"),
        # 1 → flags=0, lo=1
        ("1",       "00000000" "01000000" "00000000" "00000000"),
        # 0 → all zero
        ("0",       "00000000" "00000000" "00000000" "00000000"),
        # -1 → flags 带 sign bit (0x80000000)，lo=1
        ("-1",      "00000080" "01000000" "00000000" "00000000"),
        # 0.000000000000000001 → flags=scale18 (0x00120000)，lo=1
        ("0.000000000000000001",
                    "00001200" "01000000" "00000000" "00000000"),
        # 100 → flags=0，lo=0x64
        ("100",     "00000000" "64000000" "00000000" "00000000"),
        # 0.0001 → flags=scale4 (0x00040000)，lo=1
        ("0.0001",  "00000400" "01000000" "00000000" "00000000"),
        # -0.5 → flags=scale1+sign (0x80010000)，lo=5
        ("-0.5",    "00000180" "05000000" "00000000" "00000000"),
        # 大数：mantissa=2^32 跨越 lo→mid 边界 → lo=0, mid=1
        ("4294967296",
                    "00000000" "00000000" "01000000" "00000000"),
        # mantissa=2^64 → hi=1
        ("18446744073709551616",
                    "00000000" "00000000" "00000000" "01000000"),
    ],
)
def test_known_answer_serialize(value, expected_hex):
    got = rust_decimal_serialize(Decimal(value)).hex()
    assert got == expected_hex, (
        f"\n  Decimal({value!r})\n  expected: {expected_hex}\n  got:      {got}"
    )


def test_serialize_is_16_bytes():
    for s in ("0", "1", "100", "0.5", "-3.14159265358979", "0.000000000000000001"):
        assert len(rust_decimal_serialize(Decimal(s))) == 16


def test_scale_above_28_rejected():
    # rust_decimal 只支持 scale 0..28
    with pytest.raises(ValueError, match=r"scale 29"):
        rust_decimal_serialize(Decimal("0." + "0" * 28 + "1"))  # scale=29


def test_mantissa_above_96_bits_rejected():
    # 2^96 — 1 是合法的，2^96 不行
    just_fits = (1 << 96) - 1
    rust_decimal_serialize(Decimal(just_fits))  # 不抛
    with pytest.raises(ValueError, match=r"mantissa exceeds 96 bits"):
        rust_decimal_serialize(Decimal(1 << 96))


def test_canonical_decimal_vector_layout():
    # 4-byte LE u32 length + N × 16 bytes (flags-first layout)
    vec = [Decimal("0.5"), Decimal("0.3"), Decimal("0.2")]
    out = canonical_decimal_vector(vec)
    assert len(out) == 4 + 3 * 16
    # 长度前缀 = 3
    assert out[:4] == b"\x03\x00\x00\x00"
    # 第一个 entry: 0.5 → flags=scale1, lo=5
    assert out[4:20] == bytes.fromhex("00000100" "05000000" "00000000" "00000000")
    # 第二个 entry: 0.3 → flags=scale1, lo=3
    assert out[20:36] == bytes.fromhex("00000100" "03000000" "00000000" "00000000")
    # 第三个 entry: 0.2 → flags=scale1, lo=2
    assert out[36:52] == bytes.fromhex("00000100" "02000000" "00000000" "00000000")


def test_empty_vector_is_just_length_prefix():
    assert canonical_decimal_vector([]) == b"\x00\x00\x00\x00"


def test_keccak_of_canonical_vector_is_deterministic():
    # 同一输入两次必须哈希到同一摘要 — 否则签名一致性丧失
    vec = [Decimal("0.5"), Decimal("0.3"), Decimal("0.2")]
    h1 = keccak256(canonical_decimal_vector(vec))
    h2 = keccak256(canonical_decimal_vector(vec))
    assert h1 == h2
    # 改一位元素 → 摘要必须不同
    vec2 = [Decimal("0.5"), Decimal("0.3"), Decimal("0.20000000000001")]
    assert keccak256(canonical_decimal_vector(vec2)) != h1


def test_negative_zero_normalizes_to_positive_zero():
    """rust_decimal 把 -0 归一化为 +0；我们必须照办，否则同一个数学值
    在两端会产生不同的字节，hash 漂移导致投票被 ecrecover 拒掉。"""
    pos = rust_decimal_serialize(Decimal("0"))
    neg = rust_decimal_serialize(Decimal("-0"))
    assert pos == neg, f"+0 vs -0 byte mismatch: {pos.hex()} vs {neg.hex()}"
    # 而且都应该等于全零
    assert pos == b"\x00" * 16


def test_negative_zero_at_higher_scale_also_normalizes():
    # `-0.0` (scale=1, mantissa=0, sign=1) 也要归一化
    a = rust_decimal_serialize(Decimal("-0.0"))
    b = rust_decimal_serialize(Decimal("0.0"))
    assert a == b
    # scale 字段不丢 — 这是 scale=1 的零
    assert a == bytes.fromhex("00000100" "00000000" "00000000" "00000000")


# 已用 cargo + rust_decimal 1.x 真程序生成的字节，与 Python 实现交叉验证。
# 前 5 条（0/1/0.5/0.25/0.123456789012345）是上游协议方在 dev_docs 里
# pinned 的 fixture，必须逐字节匹配 — 改动这些等于改动签名兼容性合约。
RUST_REFERENCE_BYTES = {
    # --- 上游 pinned fixture（dev_docs/GOVNET_SKILL_DEVELOPMENT.md）---
    "0":                   "00000000000000000000000000000000",
    "1":                   "00000000010000000000000000000000",
    "0.5":                 "00000100050000000000000000000000",
    "0.25":                "00000200190000000000000000000000",
    "0.123456789012345":   "00000f0079df0d864870000000000000",
    # --- 我们额外补的边界 case ---
    "-1":                  "00000080010000000000000000000000",
    "100":                 "00000000640000000000000000000000",
    "0.0001":              "00000400010000000000000000000000",
    "-0.5":                "00000180050000000000000000000000",
    "4294967296":          "00000000000000000100000000000000",
    "18446744073709551616":"00000000000000000000000001000000",
    "0.000000000000000001":"00001200010000000000000000000000",
}


@pytest.mark.parametrize("value", list(RUST_REFERENCE_BYTES.keys()))
def test_rust_cross_check(value):
    """Python 输出必须逐字节等于真 Rust 程序的 serialize() 输出。

    Rust 端 reference 字节是 `cargo run` 一个引用 `rust_decimal = "1"`
    的小程序生成的 (见 govnet-skill 提交历史 commit 注释)。如果这条
    测试失败，就意味着 Python 与 Rust 字节级合约破裂 — 投票/预测的
    voteHash / predictionHash 会 100% 与服务端不一致。
    """
    expected = RUST_REFERENCE_BYTES[value]
    got = rust_decimal_serialize(Decimal(value)).hex()
    assert got == expected, (
        f"\n  Decimal({value!r})\n  rust:   {expected}\n  python: {got}"
    )
