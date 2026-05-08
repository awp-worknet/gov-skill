"""Query string canonicalization + EIP-712 byte-level primitives — mirrors crates/emg-auth/src/canonical.rs.

Before signing, the query string must be canonicalized as follows:
1. Percent-decode both sides of every key/value pair per RFC 3986.
2. Sort by (key, value) ascending.
3. Re-percent-encode using RFC 3986 component encoding (lowercase hex).
4. Join with `&`, separating key from value with `=`; an empty query returns the empty string.

The server treats the `query` field of the signing material as exactly the
canonicalized result above. Any drift causes `AUTH_SIGNATURE_INVALID`, so
this file replicates the Rust implementation byte for byte and runs the same
known-answer vectors under tests/ to verify.

Also exposed: `rust_decimal_serialize` — the inner-vote `EMGVote.voteHash`
requires packing the vector first into the 16-byte binary format used by
`rust_decimal::Decimal::serialize`, then prefixing a 4-byte LE u32 length and
running keccak256 over the result. Any byte-level drift between the two
sides means we can't produce a matching hash, and the entire vote is rejected
by the server's ecrecover.
"""

from __future__ import annotations

import hashlib
import struct
from decimal import Decimal
from typing import List, Sequence, Tuple

# eth_hash is a zero-dependency keccak256 wrapper; pyproject pins 0.7+.
from eth_hash.auto import keccak as _keccak


class CanonicalError(ValueError):
    """Canonicalization failed — truncated percent escape, non-hex character, or non-UTF-8 bytes."""


def keccak256(data: bytes) -> bytes:
    """Returns a 32-byte Keccak-256 digest. Empty input also returns a 32-byte digest."""
    return _keccak(data)


def eip712_body_hash(body: bytes) -> bytes:
    """The `bodyHash` field for the EIP-712 envelope.

    Empty body returns 32 bytes of zero (matching the server's `B256::ZERO`
    sentinel); non-empty body returns `keccak256(body)`. **Do not** use SHA-256;
    SHA-256 is reserved for idempotency caching.
    """
    if not body:
        return b"\x00" * 32
    return keccak256(body)


def idempotency_body_hash(body: bytes) -> str:
    """SHA-256 digest, strictly distinct from the EIP-712 keccak256 (spec §9.4 step 1).

    **The skill itself does not call this function** — the server uses this
    digest as part of the idempotency cache key
    (`(Principal, X-Idempotency-Key, sha256(body))`); the client only needs
    to re-send the same body verbatim to hit the cache, no client-side hashing
    required.

    This function is kept because:
      - spec §9.4 lists it as a reference implementation the client "should be able to compute"
      - tests/test_canonical.py uses it to anchor server compatibility (preventing
        a future mistake where keccak256 is used as the idempotency hash —
        keeping the two algorithms distinct is the spec's repeated anti-confusion point)
      - if we ever add client-side dedup / replay-protection tooling, it can reuse this directly
    """
    return "0x" + hashlib.sha256(body).hexdigest()


_UNRESERVED = (
    set(range(ord("0"), ord("9") + 1))
    | set(range(ord("A"), ord("Z") + 1))
    | set(range(ord("a"), ord("z") + 1))
    | {ord(c) for c in "-._~"}
)


def _hex_value(c: int) -> int:
    if 0x30 <= c <= 0x39:
        return c - 0x30
    if 0x61 <= c <= 0x66:
        return c - 0x61 + 10
    if 0x41 <= c <= 0x46:
        return c - 0x41 + 10
    raise CanonicalError("invalid hex in percent-escape")


def _percent_decode(s: str) -> str:
    # The Rust implementation operates on &str.as_bytes() — i.e. a UTF-8 byte
    # stream. Here in Python we first encode str to UTF-8 so raw unicode
    # characters (e.g. 'ä') travel as bytes; then parse percent-escape bytes;
    # then decode the whole thing back to a utf-8 string.
    raw = s.encode("utf-8")
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b == 0x25:  # %
            if i + 2 >= n:
                raise CanonicalError("truncated percent-escape sequence")
            hi = _hex_value(raw[i + 1])
            lo = _hex_value(raw[i + 2])
            out.append((hi << 4) | lo)
            i += 3
        else:
            out.append(b)
            i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError as e:
        raise CanonicalError("percent-decoded bytes are not valid UTF-8") from e


def _percent_encode(s: str) -> str:
    out: List[str] = []
    for b in s.encode("utf-8"):
        if b in _UNRESERVED:
            out.append(chr(b))
        else:
            out.append("%{:02x}".format(b))
    return "".join(out)


def canonicalize_query(query: str) -> str:
    """Canonicalize the query string. Empty input or just `?` returns ``""``.

    Important: `+` is treated as a literal plus sign (encoded as `%2b`); do
    NOT apply form-urlencoded "plus means space" semantics. Rules align
    exactly with the server's `crates/emg-auth/src/canonical.rs`.
    """
    if query.startswith("?"):
        query = query[1:]
    if not query:
        return ""
    pairs: List[Tuple[str, str]] = []
    for kv in query.split("&"):
        eq = kv.find("=")
        if eq == -1:
            raw_k, raw_v = kv, ""
        else:
            raw_k, raw_v = kv[:eq], kv[eq + 1 :]
        pairs.append((_percent_decode(raw_k), _percent_decode(raw_v)))
    pairs.sort()
    return "&".join(f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in pairs)


def rust_decimal_serialize(d: Decimal) -> bytes:
    """Replicate the 16-byte little-endian binary layout of `rust_decimal::Decimal::serialize`.

    Actual byte order (matches paupino/rust-decimal 1.x's `serialize()`,
    cross-validated against a real Rust program on cargo 1.94):
        bytes[0..4]   = flags (u32 LE): bits 16..23 = scale (0..28), bit 31 = sign
        bytes[4..8]   = lo    (mantissa low 32 bits)
        bytes[8..12]  = mid   (mantissa middle 32 bits)
        bytes[12..16] = hi    (mantissa high 32 bits)

    `Decimal("0.5").serialize()` on the Rust side produces `00000100 05000000 …`
    — flags first, **not** last. Earlier implementations had it reversed and
    every vote was rejected by the server's ecrecover.

    rust_decimal's mantissa is unsigned 96-bit; scale describes how many
    digits to shift left of the decimal point. Python `Decimal.as_tuple()`
    gives `(sign, digits, exponent)`; concatenating the digits as an integer
    yields the mantissa; scale = -exponent (must be in [0, 28]).

    Special case: negative zero `-0` is normalized to positive zero in
    rust_decimal (when mantissa==0 the sign bit is dropped); we normalize the
    same way in encoding to avoid hash drift versus the server.

    Strictly distinct from any ASCII text format — this is the byte-level
    contract for a vote's voteHash / predictionHash, and **must not** be
    replaced with `format(d, 'f').encode()`.
    """
    sign, digits, exponent = d.as_tuple()
    if exponent in ("F", "n", "N"):
        raise ValueError(f"non-finite Decimal {d!r} cannot be rust_decimal-encoded")
    scale = -int(exponent)
    if not 0 <= scale <= 28:
        raise ValueError(
            f"Decimal {d!r} has scale {scale}; rust_decimal supports 0..28"
        )
    mantissa = 0
    for digit in digits:
        mantissa = mantissa * 10 + digit
    if mantissa.bit_length() > 96:
        raise ValueError(f"Decimal {d!r} mantissa exceeds 96 bits")
    lo = mantissa & 0xFFFFFFFF
    mid = (mantissa >> 32) & 0xFFFFFFFF
    hi = (mantissa >> 64) & 0xFFFFFFFF
    flags = (scale & 0xFF) << 16
    # rust_decimal normalizes -0 to +0; when mantissa==0, the sign bit must be 0
    if sign and mantissa != 0:
        flags |= 0x80000000
    return struct.pack("<IIII", flags, lo, mid, hi)


def canonical_decimal_vector(vec: Sequence[Decimal]) -> bytes:
    """`canonical_bytes(VoteVector)` — 4-byte LE u32 length + N × 16-byte rust_decimal.

    Equivalent to the server's `crates/emg-core/src/canonical_vote.rs`.
    `keccak256(canonical_decimal_vector(vec))` is the EMGVote.voteHash field.
    """
    out = bytearray()
    out += struct.pack("<I", len(vec))
    for d in vec:
        out += rust_decimal_serialize(d)
    return bytes(out)


def build_query(params: dict) -> str:
    """Build a canonicalized query string from a dict. Keys whose value is ``None`` are skipped.

    Used for GET requests — the same query string is appended to the URL and
    fed to the signing function. Returned value does not include a leading `?`.
    """
    if not params:
        return ""
    pairs: List[Tuple[str, str]] = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        pairs.append((str(k), str(v)))
    pairs.sort()
    return "&".join(f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in pairs)
