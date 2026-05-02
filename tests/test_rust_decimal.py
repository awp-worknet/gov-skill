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


# layout: lo(u32 LE) | mid(u32 LE) | hi(u32 LE) | flags(u32 LE)
# flags: bits 16..23 = scale, bit 31 = sign


@pytest.mark.parametrize(
    "value, expected_hex",
    [
        # 0.5 → mantissa=5, scale=1
        ("0.5",     "05000000" "00000000" "00000000" "00000100"),
        # 1 → mantissa=1, scale=0
        ("1",       "01000000" "00000000" "00000000" "00000000"),
        # 0 → mantissa=0, scale=0
        ("0",       "00000000" "00000000" "00000000" "00000000"),
        # -1 → mantissa=1, scale=0, sign bit
        ("-1",      "01000000" "00000000" "00000000" "00000080"),
        # 0.000000000000000001 → mantissa=1, scale=18 (0x12)
        ("0.000000000000000001",
                    "01000000" "00000000" "00000000" "00001200"),
        # 100 → mantissa=100, scale=0
        ("100",     "64000000" "00000000" "00000000" "00000000"),
        # 0.0001 → mantissa=1, scale=4
        ("0.0001",  "01000000" "00000000" "00000000" "00000400"),
        # -0.5 → mantissa=5, scale=1, sign bit
        ("-0.5",    "05000000" "00000000" "00000000" "00000180"),
        # 大数：mantissa=2^32 = 4294967296，跨越 lo→mid 边界
        # mantissa=2^32 → lo=0, mid=1, hi=0, scale=0
        ("4294967296",
                    "00000000" "01000000" "00000000" "00000000"),
        # mantissa=2^64 → lo=0, mid=0, hi=1, scale=0
        ("18446744073709551616",
                    "00000000" "00000000" "01000000" "00000000"),
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
    # 4-byte LE u32 length + N × 16 bytes
    vec = [Decimal("0.5"), Decimal("0.3"), Decimal("0.2")]
    out = canonical_decimal_vector(vec)
    assert len(out) == 4 + 3 * 16
    # 长度前缀 = 3
    assert out[:4] == b"\x03\x00\x00\x00"
    # 第一个 entry 是 0.5 的 16 字节
    assert out[4:20] == bytes.fromhex("05000000" "00000000" "00000000" "00000100")
    # 第二个 entry 是 0.3 → mantissa=3, scale=1
    assert out[20:36] == bytes.fromhex("03000000" "00000000" "00000000" "00000100")
    # 第三个 entry 是 0.2 → mantissa=2, scale=1
    assert out[36:52] == bytes.fromhex("02000000" "00000000" "00000000" "00000100")


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
