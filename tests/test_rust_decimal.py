"""Known-answer tests for rust_decimal binary-compatible serialization.

Each KAT was hand-computed according to the 16-byte memory layout of
`rust_decimal::Decimal::serialize`. A failure here is equivalent to the
vote voteHash disagreeing with the server, which is equivalent to every
vote submission being rejected by the server's ecrecover.
"""

from decimal import Decimal

import pytest

from lib.canonical import (
    canonical_decimal_vector,
    keccak256,
    rust_decimal_serialize,
)


# Actual layout (cross-validated against a real Rust program on cargo 1.94 + rust_decimal 1.x):
# bytes[0..4]   = flags (u32 LE): bits 16..23 = scale (0..28), bit 31 = sign
# bytes[4..8]   = lo    (mantissa low 32 bits)
# bytes[8..12]  = mid   (mantissa middle 32 bits)
# bytes[12..16] = hi    (mantissa high 32 bits)


@pytest.mark.parametrize(
    "value, expected_hex",
    [
        # 0.5 → flags=scale1, lo=5
        ("0.5",     "00000100" "05000000" "00000000" "00000000"),
        # 1 → flags=0, lo=1
        ("1",       "00000000" "01000000" "00000000" "00000000"),
        # 0 → all zero
        ("0",       "00000000" "00000000" "00000000" "00000000"),
        # -1 → flags has the sign bit (0x80000000), lo=1
        ("-1",      "00000080" "01000000" "00000000" "00000000"),
        # 0.000000000000000001 → flags=scale18 (0x00120000), lo=1
        ("0.000000000000000001",
                    "00001200" "01000000" "00000000" "00000000"),
        # 100 → flags=0, lo=0x64
        ("100",     "00000000" "64000000" "00000000" "00000000"),
        # 0.0001 → flags=scale4 (0x00040000), lo=1
        ("0.0001",  "00000400" "01000000" "00000000" "00000000"),
        # -0.5 → flags=scale1+sign (0x80010000), lo=5
        ("-0.5",    "00000180" "05000000" "00000000" "00000000"),
        # Large value: mantissa=2^32 crosses the lo→mid boundary → lo=0, mid=1
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
    # rust_decimal only supports scale 0..28
    with pytest.raises(ValueError, match=r"scale 29"):
        rust_decimal_serialize(Decimal("0." + "0" * 28 + "1"))  # scale=29


def test_mantissa_above_96_bits_rejected():
    # 2^96 - 1 is legal; 2^96 is not
    just_fits = (1 << 96) - 1
    rust_decimal_serialize(Decimal(just_fits))  # does not raise
    with pytest.raises(ValueError, match=r"mantissa exceeds 96 bits"):
        rust_decimal_serialize(Decimal(1 << 96))


def test_canonical_decimal_vector_layout():
    # 4-byte LE u32 length + N × 16 bytes (flags-first layout)
    vec = [Decimal("0.5"), Decimal("0.3"), Decimal("0.2")]
    out = canonical_decimal_vector(vec)
    assert len(out) == 4 + 3 * 16
    # length prefix = 3
    assert out[:4] == b"\x03\x00\x00\x00"
    # first entry: 0.5 → flags=scale1, lo=5
    assert out[4:20] == bytes.fromhex("00000100" "05000000" "00000000" "00000000")
    # second entry: 0.3 → flags=scale1, lo=3
    assert out[20:36] == bytes.fromhex("00000100" "03000000" "00000000" "00000000")
    # third entry: 0.2 → flags=scale1, lo=2
    assert out[36:52] == bytes.fromhex("00000100" "02000000" "00000000" "00000000")


def test_empty_vector_is_just_length_prefix():
    assert canonical_decimal_vector([]) == b"\x00\x00\x00\x00"


def test_keccak_of_canonical_vector_is_deterministic():
    # Hashing the same input twice must produce the same digest — otherwise signature consistency is lost
    vec = [Decimal("0.5"), Decimal("0.3"), Decimal("0.2")]
    h1 = keccak256(canonical_decimal_vector(vec))
    h2 = keccak256(canonical_decimal_vector(vec))
    assert h1 == h2
    # Changing one element → the digest must differ
    vec2 = [Decimal("0.5"), Decimal("0.3"), Decimal("0.20000000000001")]
    assert keccak256(canonical_decimal_vector(vec2)) != h1


def test_negative_zero_normalizes_to_positive_zero():
    """rust_decimal normalizes -0 to +0; we must do the same, or the same
    mathematical value would produce different bytes on each side, and the hash
    drift would cause every vote to be rejected by ecrecover."""
    pos = rust_decimal_serialize(Decimal("0"))
    neg = rust_decimal_serialize(Decimal("-0"))
    assert pos == neg, f"+0 vs -0 byte mismatch: {pos.hex()} vs {neg.hex()}"
    # And both should equal all-zero
    assert pos == b"\x00" * 16


def test_negative_zero_at_higher_scale_also_normalizes():
    # `-0.0` (scale=1, mantissa=0, sign=1) also needs to be normalized
    a = rust_decimal_serialize(Decimal("-0.0"))
    b = rust_decimal_serialize(Decimal("0.0"))
    assert a == b
    # The scale field is preserved — this is a scale=1 zero
    assert a == bytes.fromhex("00000100" "00000000" "00000000" "00000000")


# Bytes generated by a real Rust program (cargo + rust_decimal 1.x), cross-validated against the Python implementation.
# The first 5 entries (0/1/0.5/0.25/0.123456789012345) are upstream-protocol-team
# pinned fixtures from dev_docs and must match byte for byte — changing them is
# equivalent to changing the signature-compatibility contract.
RUST_REFERENCE_BYTES = {
    # --- Upstream pinned fixture (dev_docs/GOVNET_SKILL_DEVELOPMENT.md) ---
    "0":                   "00000000000000000000000000000000",
    "1":                   "00000000010000000000000000000000",
    "0.5":                 "00000100050000000000000000000000",
    "0.25":                "00000200190000000000000000000000",
    "0.123456789012345":   "00000f0079df0d864870000000000000",
    # --- Extra boundary cases we added ---
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
    """Python output must match the real Rust program's serialize() output byte for byte.

    The Rust-side reference bytes were generated by `cargo run`-ing a small
    program that depends on `rust_decimal = "1"` (see the commit notes in
    the govnet-skill history). If this test fails, the Python ↔ Rust
    byte-level contract is broken — voteHash / predictionHash for any
    vote/prediction will be 100% inconsistent with the server.
    """
    expected = RUST_REFERENCE_BYTES[value]
    got = rust_decimal_serialize(Decimal(value)).hex()
    assert got == expected, (
        f"\n  Decimal({value!r})\n  rust:   {expected}\n  python: {got}"
    )
