//! Canonical request encoding for EIP-712 digest reconstruction
//! (`spec/07-api.md` §9.3.4).
//!
//! Two helpers — both pure and total over their well-formed input domains:
//! - [`body_hash`] — `keccak256` of the request body, with the spec's
//!   all-zero sentinel for the empty body.
//! - [`canonicalize_query`] — RFC 3986 percent-decode → re-encode with
//!   lowercase hex → sort by (key, value) → join with `&`. Designed so that
//!   semantically equivalent encodings (e.g. `%41` vs `A`, `?b=2&a=1` vs
//!   `?a=1&b=2`) yield the same canonical form, hence the same EIP-712
//!   digest, hence the same signature.
//!
//! ## Spec sort-order note
//!
//! Spec §9.3.4 says "Sort pairs by key, then by value for duplicate keys"
//! and gives this example:
//!
//! > `?epoch=5&principal=0xabc` → `principal=0xabc&epoch=5`
//!
//! The example is internally inconsistent: ascending lexicographic sort puts
//! `epoch` before `principal` (`'e' < 'p'`), so the canonical form is
//! `epoch=5&principal=0xabc` — the opposite of what the example shows. This
//! implementation follows the *rule* (ascending lex sort), not the example
//! (treated as a doc typo). The pinned regression test
//! `canonicalize_spec_example_input` fixes the canonical output we will hold
//! every client to. Flag for spec correction in a follow-up review.

use alloy_primitives::{B256, keccak256};
use sha2::{Digest, Sha256};

/// Keccak-256 of the request body for the EIP-712 `bodyHash` field.
/// Empty body returns [`B256::ZERO`] per §9.3.4 (`bodyHash = 0x00..00`).
///
/// **Do NOT use for the idempotency cache key** — use
/// [`idempotency_body_hash`] (SHA-256) for that purpose. The two hashes
/// use different digest functions; confusing them causes every legitimate
/// idempotency retry to produce `Conflict` instead of `Hit`.
pub fn eip712_body_hash(body: &[u8]) -> B256 {
    if body.is_empty() { B256::ZERO } else { keccak256(body) }
}

/// Deprecated alias for [`eip712_body_hash`]. Use the explicit name so
/// the EIP-712 vs idempotency hash distinction is clear at call sites.
#[deprecated(since = "0.1.0", note = "use `eip712_body_hash` instead")]
pub fn body_hash(body: &[u8]) -> B256 {
    eip712_body_hash(body)
}

/// SHA-256 hex digest of the request body for the idempotency cache key
/// (spec §9.4 step 1 / ADR-014 §8 step 1). Returns a `0x`-prefixed
/// lowercase hex string.
///
/// **Do NOT use for the EIP-712 `bodyHash`** — use [`eip712_body_hash`]
/// (keccak-256) for that purpose.
pub fn idempotency_body_hash(body: &[u8]) -> String {
    let hash = Sha256::digest(body);
    format!("0x{}", hex::encode(hash))
}

/// Canonicalize a query string per §9.3.4.
///
/// Returns `""` for empty input. Returns `Err(CanonicalError)` if the input
/// contains a malformed percent-escape (`%` not followed by two hex digits)
/// or a non-UTF-8 percent-decoded byte sequence.
///
/// The leading `?` is optional — both `"epoch=5"` and `"?epoch=5"` are
/// accepted. The output never carries a leading `?`.
pub fn canonicalize_query(query: &str) -> Result<String, CanonicalError> {
    let stripped = query.strip_prefix('?').unwrap_or(query);
    if stripped.is_empty() {
        return Ok(String::new());
    }
    let mut pairs: Vec<(String, String)> = Vec::new();
    for kv in stripped.split('&') {
        let (raw_key, raw_val) = match kv.find('=') {
            Some(i) => (&kv[..i], &kv[i + 1..]),
            None => (kv, ""),
        };
        pairs.push((percent_decode(raw_key)?, percent_decode(raw_val)?));
    }
    // Tuple sort: by key ascending, then value ascending — handles the
    // duplicate-key case (`?a=2&a=1` → `a=1&a=2`).
    pairs.sort();
    Ok(pairs
        .into_iter()
        .map(|(k, v)| format!("{}={}", percent_encode(&k), percent_encode(&v)))
        .collect::<Vec<_>>()
        .join("&"))
}

/// Errors from query canonicalization. Internal to the auth crate; callers
/// at the verify boundary wrap these into the public `AuthError` variants
/// (Turn 2).
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum CanonicalError {
    /// `%` appeared without two following hex digits.
    #[error("truncated percent-escape sequence")]
    TruncatedPercentEscape,
    /// `%` followed by a non-hex character.
    #[error("invalid hex in percent-escape")]
    InvalidPercentEscape,
    /// Percent-decoded byte sequence is not valid UTF-8.
    #[error("percent-decoded bytes are not valid UTF-8")]
    InvalidUtf8,
}

/// Percent-decode a URL component. Rejects truncated and non-hex escapes.
/// Accepts both upper- and lower-case hex digits on input.
fn percent_decode(s: &str) -> Result<String, CanonicalError> {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            if i + 2 >= bytes.len() {
                return Err(CanonicalError::TruncatedPercentEscape);
            }
            let hi = hex_value(bytes[i + 1])?;
            let lo = hex_value(bytes[i + 2])?;
            out.push((hi << 4) | lo);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8(out).map_err(|_| CanonicalError::InvalidUtf8)
}

fn hex_value(c: u8) -> Result<u8, CanonicalError> {
    match c {
        b'0'..=b'9' => Ok(c - b'0'),
        b'a'..=b'f' => Ok(c - b'a' + 10),
        b'A'..=b'F' => Ok(c - b'A' + 10),
        _ => Err(CanonicalError::InvalidPercentEscape),
    }
}

/// Percent-encode a string per RFC 3986 component encoding with **lowercase**
/// hex digits (spec §9.3.4).
///
/// Unreserved characters (`A-Z a-z 0-9 - . _ ~`) pass through; everything
/// else is encoded as `%xx`. Non-ASCII input is encoded byte-by-byte over
/// its UTF-8 representation, so the output is pure ASCII.
fn percent_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for &b in s.as_bytes() {
        if is_unreserved(b) {
            out.push(b as char);
        } else {
            out.push('%');
            out.push(hex_lower(b >> 4));
            out.push(hex_lower(b & 0x0F));
        }
    }
    out
}

fn is_unreserved(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~')
}

fn hex_lower(nibble: u8) -> char {
    match nibble {
        0..=9 => (b'0' + nibble) as char,
        10..=15 => (b'a' + nibble - 10) as char,
        _ => unreachable!("nibble is always the result of a 4-bit mask"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloy_primitives::keccak256;

    // ----- eip712_body_hash -------------------------------------------------

    #[test]
    fn eip712_body_hash_empty_is_zero() {
        assert_eq!(eip712_body_hash(&[]), B256::ZERO);
    }

    #[test]
    fn eip712_body_hash_nonempty_matches_keccak256() {
        let body = b"{\"side\":\"buy\"}";
        assert_eq!(eip712_body_hash(body), keccak256(body));
    }

    #[test]
    fn eip712_body_hash_distinguishes_one_byte_change() {
        assert_ne!(eip712_body_hash(b"abc"), eip712_body_hash(b"abd"));
    }

    // ----- idempotency_body_hash --------------------------------------------

    #[test]
    fn idempotency_body_hash_is_sha256_not_keccak() {
        let body = b"hello";
        let got = idempotency_body_hash(body);
        // SHA-256("hello") = 2cf24dba... (well-known test vector).
        assert!(got.starts_with("0x2cf24dba"), "expected SHA-256 hash, got {got}");
        // Must differ from keccak256.
        let keccak_hex = format!("0x{}", hex::encode(keccak256(body)));
        assert_ne!(got, keccak_hex, "idempotency hash must not equal keccak256 hash");
    }

    #[test]
    fn idempotency_body_hash_is_prefixed_lowercase_hex() {
        let hash = idempotency_body_hash(b"test body");
        assert!(hash.starts_with("0x"), "must be 0x-prefixed");
        // 0x + 64 hex chars = 66 chars total for SHA-256.
        assert_eq!(hash.len(), 66, "SHA-256 produces 32 bytes = 64 hex chars");
        assert!(
            hash[2..].chars().all(|c| c.is_ascii_hexdigit() && !c.is_uppercase()),
            "hex must be lowercase"
        );
    }

    #[test]
    fn idempotency_body_hash_empty_body() {
        // SHA-256("") = e3b0c44298fc1c14... (well-known).
        let got = idempotency_body_hash(b"");
        assert!(
            got.starts_with("0xe3b0c44298fc1c14"),
            "expected SHA-256 of empty string, got {got}"
        );
    }

    #[test]
    fn idempotency_body_hash_distinguishes_one_byte_change() {
        assert_ne!(idempotency_body_hash(b"abc"), idempotency_body_hash(b"abd"));
    }

    // ----- canonicalize_query: empty + leading-? handling -------------------

    #[test]
    fn empty_query_canonicalizes_to_empty() {
        assert_eq!(canonicalize_query("").unwrap(), "");
        assert_eq!(canonicalize_query("?").unwrap(), "");
    }

    #[test]
    fn leading_question_mark_is_stripped() {
        assert_eq!(canonicalize_query("?a=1").unwrap(), "a=1");
        assert_eq!(canonicalize_query("a=1").unwrap(), "a=1");
    }

    // ----- canonicalize_query: sort behaviour -------------------------------

    #[test]
    fn pairs_are_sorted_ascending_by_key() {
        assert_eq!(canonicalize_query("?b=2&a=1").unwrap(), "a=1&b=2");
    }

    #[test]
    fn duplicate_keys_sorted_by_value() {
        assert_eq!(canonicalize_query("?a=2&a=1").unwrap(), "a=1&a=2");
    }

    #[test]
    fn canonicalize_spec_example_input() {
        // Spec §9.3.4 example input, with the *correct* ascending-sort
        // output. The spec's printed example output has the order reversed
        // (likely a doc typo); we hold every client to ascending lex sort
        // because that matches the rule "Sort pairs by key".
        assert_eq!(
            canonicalize_query("?epoch=5&principal=0xabc").unwrap(),
            "epoch=5&principal=0xabc"
        );
    }

    // ----- canonicalize_query: percent decoding/encoding equivalence --------

    #[test]
    fn uppercase_percent_escape_decodes_to_unreserved() {
        // %41 == 'A' (unreserved); decode-then-re-encode collapses to bare A.
        assert_eq!(canonicalize_query("?a=%41").unwrap(), "a=A");
    }

    #[test]
    fn lowercase_percent_escape_round_trip() {
        // %2a == '*' (reserved); decode-then-re-encode emits lowercase %2a.
        assert_eq!(canonicalize_query("?a=%2A").unwrap(), "a=%2a");
        assert_eq!(canonicalize_query("?a=%2a").unwrap(), "a=%2a");
    }

    #[test]
    fn space_encodes_as_percent_20() {
        // %20 (decoded to ' ') should re-encode as lowercase %20, NOT '+'.
        assert_eq!(canonicalize_query("?a=hi%20there").unwrap(), "a=hi%20there");
    }

    #[test]
    fn plus_is_treated_as_literal_plus_not_space() {
        // RFC 3986 component encoding; form-urlencoded `+` semantics do NOT
        // apply. Decoded `+` is the literal '+' byte; re-encoded as %2b.
        assert_eq!(canonicalize_query("?a=%2b").unwrap(), "a=%2b");
        assert_eq!(canonicalize_query("?a=+").unwrap(), "a=%2b");
    }

    #[test]
    fn unreserved_unicode_via_percent_escape_lowercases_hex() {
        // 'ä' is U+00E4, UTF-8 = c3 a4. Either input form should produce
        // the same lowercase canonical hex.
        assert_eq!(canonicalize_query("?a=%C3%A4").unwrap(), "a=%c3%a4");
        assert_eq!(canonicalize_query("?a=%c3%a4").unwrap(), "a=%c3%a4");
    }

    #[test]
    fn raw_unicode_in_input_encodes_byte_by_byte_lowercase() {
        // Raw 'ä' in input bytes encodes its two UTF-8 bytes as %c3%a4.
        assert_eq!(canonicalize_query("?name=ä").unwrap(), "name=%c3%a4");
    }

    #[test]
    fn unreserved_chars_are_preserved_verbatim() {
        // A-Z a-z 0-9 - . _ ~ pass through.
        assert_eq!(canonicalize_query("?k=A-z.0_9~").unwrap(), "k=A-z.0_9~");
    }

    // ----- canonicalize_query: missing-value + empty-value cases ------------

    #[test]
    fn missing_equals_treated_as_empty_value() {
        assert_eq!(canonicalize_query("?flag").unwrap(), "flag=");
    }

    #[test]
    fn explicit_empty_value() {
        assert_eq!(canonicalize_query("?flag=").unwrap(), "flag=");
    }

    // ----- canonicalize_query: malformed input rejected ---------------------

    #[test]
    fn truncated_percent_escape_rejected() {
        assert_eq!(canonicalize_query("?a=%4"), Err(CanonicalError::TruncatedPercentEscape));
        assert_eq!(canonicalize_query("?a=%"), Err(CanonicalError::TruncatedPercentEscape));
    }

    #[test]
    fn non_hex_in_percent_escape_rejected() {
        assert_eq!(canonicalize_query("?a=%G0"), Err(CanonicalError::InvalidPercentEscape));
    }

    #[test]
    fn invalid_utf8_after_decode_rejected() {
        // %ff alone is not a valid UTF-8 sequence (continuation byte
        // without lead byte); decoded bytes fail UTF-8 validation.
        assert_eq!(canonicalize_query("?a=%ff"), Err(CanonicalError::InvalidUtf8));
    }

    // ----- canonicalize_query: idempotence ---------------------------------

    #[test]
    fn canonical_output_is_idempotent() {
        // Running canonicalize_query on its own output returns the same
        // string — a stronger invariant than just sort+encode correctness.
        let inputs =
            ["?b=2&a=1", "?a=%41&b=%2a", "?a=2&a=1", "?name=café", "?epoch=5&principal=0xabc"];
        for s in inputs {
            let once = canonicalize_query(s).unwrap();
            let twice = canonicalize_query(&once).unwrap();
            assert_eq!(once, twice, "non-idempotent for input {s:?}");
        }
    }

    // ----- canonicalize_query: equivalent inputs produce equal outputs ------

    #[test]
    fn semantically_equivalent_inputs_collapse_to_same_canonical() {
        let a = canonicalize_query("?a=A&b=B").unwrap();
        let b = canonicalize_query("?b=B&a=A").unwrap();
        let c = canonicalize_query("?a=%41&b=%42").unwrap();
        assert_eq!(a, b);
        assert_eq!(a, c);
    }

    proptest::proptest! {
        #![proptest_config(proptest::test_runner::Config { cases: 128, ..Default::default() })]

        #[test]
        fn canonicalize_idempotence_property(
            input in proptest::collection::vec(
                ("[a-zA-Z0-9]{1,6}", "[a-zA-Z0-9 _\\-.~%]{0,12}"),
                0..6,
            )
        ) {
            // Build a query string from random (key, value) pairs. Using the
            // unreserved + a few reserved chars keeps inputs well-formed
            // (no truncated %-escapes).
            let s = if input.is_empty() {
                String::new()
            } else {
                let parts: Vec<String> = input.iter().map(|(k, v)| {
                    // Collapse stray '%' characters so we don't accidentally
                    // generate truncated escape sequences.
                    let safe_v = v.replace('%', "x");
                    format!("{}={}", k, safe_v)
                }).collect();
                format!("?{}", parts.join("&"))
            };
            let once = canonicalize_query(&s).unwrap();
            let twice = canonicalize_query(&once).unwrap();
            proptest::prop_assert_eq!(once, twice);
        }
    }
}
