//! EIP-712 typed-data primitives for EMG-SIG-V1
//! (`spec/07-api.md` §9.3.2 — §9.3.3).
//!
//! - [`EMGRequest`] — the canonical typed-data struct. Field order, names,
//!   and spelling MUST match the TYPEHASH literal in §9.3.3 byte-for-byte;
//!   the `sol!` macro generates exactly that string and emits the matching
//!   `abi.encode` / hash routines.
//! - [`AuthConfig`] — chain-bound domain identifiers (`chainId`,
//!   `verifyingContract`). Different `chainId` / `verifyingContract` values
//!   yield different `domainSeparator` hashes, which is what makes
//!   prod-vs-staging signatures cryptographically incompatible
//!   (§9.3.8 "environment-bound").
//!
//! Callers compute the 32-byte EIP-712 digest
//! `keccak256("\x19\x01" || domainSeparator || hashStruct)` via the
//! [`SolStruct`](alloy_sol_types::SolStruct) trait method
//! `request.eip712_signing_hash(&domain)` — no wrapper helper is provided.

use alloy_primitives::Address;
use alloy_sol_types::{Eip712Domain, eip712_domain, sol};

sol! {
    /// EIP-712 typed-data struct for EMG request authentication.
    ///
    /// Field order, names, and types MUST match the TYPEHASH literal in
    /// `spec/07-api.md` §9.3.3:
    ///
    /// ```text
    /// EMGRequest(address principal,string method,string path,string query,
    ///            bytes32 bodyHash,uint256 nonce,uint256 timestamp)
    /// ```
    ///
    /// The `sol!` macro emits exactly this string at compile time, so any
    /// reorder / rename / type change here would diverge from every signed
    /// fixture and every interoperating client.
    #[allow(missing_docs)]
    struct EMGRequest {
        address principal;
        string  method;
        string  path;
        string  query;
        bytes32 bodyHash;
        uint256 nonce;
        uint256 timestamp;
    }
}

/// Static EIP-712 domain configuration that binds every signature to one
/// deployment. Spec §9.3.2.
#[derive(Debug, Clone)]
pub struct AuthConfig {
    /// EVM chain id (e.g. 56 for BSC mainnet, 97 for testnet).
    pub chain_id: u64,
    /// Address of the on-chain `EMGAuth` contract for this deployment. Acts
    /// as a domain separator — different addresses partition staging from
    /// production cryptographically (a staging signature cannot validate
    /// against a production verifyingContract).
    pub verifying_contract: Address,
    /// Maximum allowed `|now - timestamp|` skew, in seconds. Spec default 30.
    pub max_timestamp_skew_secs: i64,
}

impl AuthConfig {
    /// Spec default: ±30s timestamp window.
    pub const DEFAULT_TIMESTAMP_SKEW_SECS: i64 = 30;

    /// Construct with the spec-default timestamp window.
    pub fn new(chain_id: u64, verifying_contract: Address) -> Self {
        Self {
            chain_id,
            verifying_contract,
            max_timestamp_skew_secs: Self::DEFAULT_TIMESTAMP_SKEW_SECS,
        }
    }

    /// Build the EIP-712 domain (`name=EMG, version=1`) for this deployment.
    pub fn domain(&self) -> Eip712Domain {
        eip712_domain! {
            name: "EMG",
            version: "1",
            chain_id: self.chain_id,
            verifying_contract: self.verifying_contract,
        }
    }
}

/// Convert an [`emg_core::Address`] to its [`alloy_primitives::Address`]
/// view. Both are 20 raw bytes; this is a no-cost reinterpretation.
pub fn alloy_address(addr: emg_core::Address) -> Address {
    Address::from(addr.0)
}

/// Convert an [`alloy_primitives::Address`] back to an [`emg_core::Address`].
pub fn emg_address(addr: Address) -> emg_core::Address {
    emg_core::Address(addr.into_array())
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloy_primitives::{B256, U256, keccak256};
    use alloy_signer::SignerSync;
    use alloy_signer_local::PrivateKeySigner;
    use alloy_sol_types::SolStruct;

    /// The canonical type string from `spec/07-api.md` §9.3.3 — pinned here
    /// in source so any drift in the `sol!` output trips this test instead of
    /// quietly producing signatures incompatible with the spec.
    const SPEC_TYPE_STRING: &str = "EMGRequest(address principal,string method,string path,string query,bytes32 bodyHash,uint256 nonce,uint256 timestamp)";

    fn sample_request() -> EMGRequest {
        EMGRequest {
            principal: Address::from([0x42u8; 20]),
            method: "POST".into(),
            path: "/v1/orders".into(),
            query: String::new(),
            bodyHash: B256::from([0x11u8; 32]),
            nonce: U256::from(7u64),
            timestamp: U256::from(1_745_323_200u64),
        }
    }

    #[test]
    fn typehash_matches_spec_literal() {
        let req = sample_request();
        let expected = keccak256(SPEC_TYPE_STRING.as_bytes());
        assert_eq!(
            req.eip712_type_hash(),
            expected,
            "EMGRequest typehash diverged from spec §9.3.3 — did the sol! \
             struct's fields, names, or types change?"
        );
    }

    #[test]
    fn different_chain_id_produces_different_digest() {
        // Cross-environment replay protection: the same request signed under
        // chainId=56 (prod) and chainId=97 (testnet) must produce distinct
        // digests, hence distinct signatures (§9.3.8).
        let req = sample_request();
        let prod = AuthConfig::new(56, Address::from([0xaa; 20])).domain();
        let staging = AuthConfig::new(97, Address::from([0xaa; 20])).domain();
        assert_ne!(req.eip712_signing_hash(&prod), req.eip712_signing_hash(&staging));
    }

    #[test]
    fn different_verifying_contract_produces_different_digest() {
        let req = sample_request();
        let domain_a = AuthConfig::new(56, Address::from([0xaa; 20])).domain();
        let domain_b = AuthConfig::new(56, Address::from([0xbb; 20])).domain();
        assert_ne!(req.eip712_signing_hash(&domain_a), req.eip712_signing_hash(&domain_b));
    }

    #[test]
    fn mutating_any_field_changes_digest() {
        let domain = AuthConfig::new(56, Address::from([0xaa; 20])).domain();
        let base = sample_request();
        let baseline = base.eip712_signing_hash(&domain);

        let mut p = base.clone();
        p.principal = Address::from([0x43u8; 20]);
        assert_ne!(p.eip712_signing_hash(&domain), baseline, "principal");

        let mut m = base.clone();
        m.method = "GET".into();
        assert_ne!(m.eip712_signing_hash(&domain), baseline, "method");

        let mut path = base.clone();
        path.path = "/v1/votes".into();
        assert_ne!(path.eip712_signing_hash(&domain), baseline, "path");

        let mut q = base.clone();
        q.query = "epoch=5".into();
        assert_ne!(q.eip712_signing_hash(&domain), baseline, "query");

        let mut b = base.clone();
        b.bodyHash = B256::from([0x22u8; 32]);
        assert_ne!(b.eip712_signing_hash(&domain), baseline, "bodyHash");

        let mut n = base.clone();
        n.nonce = U256::from(8u64);
        assert_ne!(n.eip712_signing_hash(&domain), baseline, "nonce");

        let mut t = base;
        t.timestamp = U256::from(1_745_323_201u64);
        assert_ne!(t.eip712_signing_hash(&domain), baseline, "timestamp");
    }

    #[test]
    fn digest_is_deterministic() {
        let req = sample_request();
        let domain = AuthConfig::new(56, Address::from([0xaa; 20])).domain();
        let d1 = req.eip712_signing_hash(&domain);
        let d2 = req.eip712_signing_hash(&domain);
        assert_eq!(d1, d2);
    }

    #[test]
    fn pinned_reference_digest_for_sample_request() {
        // End-to-end EIP-712 digest pinned to a known hex literal so any
        // silent change in alloy_sol_types' encoding (domain ABI layout,
        // typehash construction, "\x19\x01" framing) trips this test
        // instead of silently producing signatures incompatible with every
        // existing client. Companion to `typehash_matches_spec_literal`,
        // which only pins the type hash.
        //
        // Inputs:
        //   chain_id           = 56
        //   verifying_contract = 0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        //   principal          = 0x4242424242424242424242424242424242424242
        //   method             = "POST"
        //   path               = "/v1/orders"
        //   query              = ""
        //   bodyHash           = 0x1111...1111 (32x 0x11)
        //   nonce              = 7
        //   timestamp          = 1745323200
        //
        // Cross-implementation interop verification (signing the same
        // typed data with ethers.js / cast) lands in Phase 8a Turn 5.
        const REFERENCE_DIGEST_HEX: &str =
            "7686da836df9c9ae2a800b0d4c8987fa97e0e237d904b4ac3e708f29a8a4a092";

        let req = sample_request();
        let domain = AuthConfig::new(56, Address::from([0xaa; 20])).domain();
        let digest = req.eip712_signing_hash(&domain);
        assert_eq!(
            hex::encode(digest.as_slice()),
            REFERENCE_DIGEST_HEX,
            "EIP-712 digest drifted — alloy_sol_types changed its encoding, \
             the sol! struct changed, or AuthConfig::domain changed."
        );
    }

    #[test]
    fn sign_and_recover_roundtrip_self_operation() {
        let signer = PrivateKeySigner::random();
        let signer_addr = signer.address();

        let cfg = AuthConfig::new(56, Address::from([0xaa; 20]));
        let domain = cfg.domain();
        let mut req = sample_request();
        req.principal = signer_addr; // actor == principal (self-operation).

        let digest = req.eip712_signing_hash(&domain);
        let sig = signer.sign_hash_sync(&digest).expect("sign");
        let recovered = sig.recover_address_from_prehash(&digest).expect("recover");
        assert_eq!(recovered, signer_addr);
    }

    #[test]
    fn sign_and_recover_roundtrip_delegated_actor() {
        // `actor != principal`: a Manager signs on behalf of a Staker. The
        // recovered address must equal the Manager's address; the principal
        // is bound into the digest so signing for a different Staker would
        // produce a different digest (and signature mismatch downstream).
        let manager = PrivateKeySigner::random();
        let staker = Address::from([0x99u8; 20]);

        let cfg = AuthConfig::new(56, Address::from([0xaa; 20]));
        let domain = cfg.domain();
        let mut req = sample_request();
        req.principal = staker;

        let digest = req.eip712_signing_hash(&domain);
        let sig = manager.sign_hash_sync(&digest).expect("sign");
        let recovered = sig.recover_address_from_prehash(&digest).expect("recover");
        assert_eq!(recovered, manager.address());
        assert_ne!(recovered, staker);
    }

    #[test]
    fn address_conversion_is_lossless() {
        let a = emg_core::Address([
            0u8, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19,
        ]);
        let alloy = alloy_address(a);
        assert_eq!(alloy.into_array(), a.0);
        let back = emg_address(alloy);
        assert_eq!(back, a);
    }

    proptest::proptest! {
        #![proptest_config(proptest::test_runner::Config { cases: 64, ..Default::default() })]

        #[test]
        fn sign_and_recover_holds_for_arbitrary_request(
            principal_bytes in proptest::array::uniform20(0u8..=255u8),
            method_idx in 0usize..4usize,
            path_tail in "[a-z]{1,12}",
            nonce in 0u64..=u64::MAX,
            timestamp in 0u64..=4_000_000_000u64,
            body in proptest::collection::vec(0u8..=255u8, 0..256),
        ) {
            let methods = ["GET", "POST", "DELETE", "WS_HELLO"];
            let signer = PrivateKeySigner::random();
            let cfg = AuthConfig::new(56, Address::from([0xaa; 20]));
            let domain = cfg.domain();

            let body_hash = if body.is_empty() {
                B256::ZERO
            } else {
                keccak256(&body)
            };

            let req = EMGRequest {
                principal: Address::from(principal_bytes),
                method: methods[method_idx].to_string(),
                path: format!("/v1/{}", path_tail),
                query: String::new(),
                bodyHash: body_hash,
                nonce: U256::from(nonce),
                timestamp: U256::from(timestamp),
            };

            let digest = req.eip712_signing_hash(&domain);
            let sig = signer.sign_hash_sync(&digest).unwrap();
            let recovered = sig.recover_address_from_prehash(&digest).unwrap();
            proptest::prop_assert_eq!(recovered, signer.address());
        }
    }
}
