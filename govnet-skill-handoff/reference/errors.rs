//! Canonical error taxonomy for EMG (ADR-006).
//!
//! `EmgError` is the **single source of truth** for the error codes in
//! `dev_docs/spec/07-api.md` §9.5.1. All 47 codes are first-class enum
//! variants here. Downstream crates (`emg-persistence`, `emg-matching`,
//! `emg-chain`, …) define their own error enums that wrap `EmgError` via
//! `#[from]` plus crate-local variants (e.g., `sqlx::Error`).
//!
//! Three contractual methods on every variant:
//! - [`EmgError::code`] — stable SCREAMING_SNAKE_CASE identifier (the wire
//!   contract per ADR-006). **Never change** an existing code's meaning.
//! - [`EmgError::http_status`] — RFC 9110 status code per spec/07-api.md §9.5.
//! - [`EmgError::problem_type`] — RFC 7807 problem type URL (ADR-014 §3).

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;

use crate::types::{Address, OrderId};

/// Workspace-wide convenience alias. Crates that prefer their own error
/// type override the second generic parameter.
pub type Result<T, E = EmgError> = std::result::Result<T, E>;

/// The canonical error type for the EMG protocol — every wire-visible error
/// emitted by any layer has a variant here.
#[derive(Debug, thiserror::Error)]
#[non_exhaustive]
pub enum EmgError {
    // ===========================================================================
    // AUTH_*  (HTTP 401 — authentication / signature / delegate)
    // ===========================================================================
    /// Required `X-EMG-*` header absent on a protected request.
    #[error("missing required header `{header}`")]
    AuthMissingHeader {
        /// Name of the header that was expected.
        header: String,
    },

    /// `X-EMG-Signature` is not a valid 65-byte 0x-prefixed hex string.
    #[error("signature is malformed (must be 65-byte 0x-prefixed hex)")]
    AuthMalformedSignature,

    /// `ecrecover` failed on the EIP-712 digest (bad / forged signature).
    #[error("ECDSA signature recovery failed")]
    AuthSignatureInvalid,

    /// Recovered signer address differs from the claimed `X-EMG-Actor`.
    #[error("actor mismatch: claimed {claimed}, recovered {recovered}")]
    AuthActorMismatch {
        /// Address the client said signed the request.
        claimed: Address,
        /// Address `ecrecover` actually returned.
        recovered: Address,
    },

    /// `actor != principal` and `AWPRegistry.delegates(principal, actor)` is false.
    #[error("actor {actor} is not an authorized delegate of principal {principal}")]
    AuthUnauthorizedDelegate {
        /// The Staker the request claims to act for.
        principal: Address,
        /// The (rejected) Manager candidate.
        actor: Address,
    },

    /// `|server_now - timestamp| > 30s`.
    #[error("timestamp skew {skew_seconds}s exceeds ±30s window")]
    AuthTimestampOutOfWindow {
        /// Signed difference (`request_timestamp - server_now`) in seconds.
        skew_seconds: i64,
    },

    /// EIP-712 domain mismatch — typically a staging signature on production
    /// (or vice versa). The configured `chainId` / `verifyingContract` differ.
    #[error("EIP-712 domain mismatch (expected chainId {expected_chain_id})")]
    AuthEip712DomainMismatch {
        /// The chain id this server's domain separator is bound to.
        expected_chain_id: u64,
    },

    /// WebSocket request requires a prior successful `auth.hello`
    /// handshake on this connection. The asyncapi spec gates all
    /// signed-RPC write methods (`orders.submit` / `orders.cancel`
    /// / `positions.split` / `positions.merge` / `votes.submit`)
    /// AND private subscription channels (`fills.me`, `orders.me`)
    /// behind a connection-bound auth state established by
    /// `auth.hello` — distinct from REST's per-request EMG-SIG-V1
    /// pipeline. Phase 8c Turn 4 introduced this variant to
    /// replace the previous repurposing of `AuthMissingHeader`,
    /// whose display template ("missing required header `X`")
    /// misled WS clients into searching for a non-existent HTTP
    /// header.
    #[error("no authenticated session on this connection (auth.hello required)")]
    AuthSessionRequired,

    // ===========================================================================
    // VALIDATION_*  (HTTP 400, except SIMPLEX which is 422)
    // ===========================================================================
    /// Request body is not parseable as JSON.
    #[error("malformed JSON: {error}")]
    ValidationMalformedJson {
        /// Parser-provided error message.
        error: String,
    },

    /// Vote / prediction vector has wrong length, contains negatives, or other
    /// shape failures (other than the simplex sum constraint).
    #[error("invalid vote vector field `{field}`: {reason}")]
    ValidationInvalidVoteVector {
        /// Which field failed (`vote` or `prediction`).
        field: String,
        /// Human-readable reason.
        reason: String,
    },

    /// `|Σ values - 1| > 1e-9` for a vote or prediction vector.
    #[error("simplex constraint violated: Σ = {sum}")]
    ValidationSimplexConstraintViolated {
        /// The actual sum that failed the tolerance check.
        sum: Decimal,
    },

    /// Order quantity is non-positive or unrepresentable as `Decimal`.
    #[error("invalid quantity: {value}")]
    ValidationInvalidQuantity {
        /// The offending value as the client serialized it.
        value: String,
    },

    /// Limit price is outside `(0, 1)` or unrepresentable as `Decimal`.
    #[error("invalid price: {value}")]
    ValidationInvalidPrice {
        /// The offending value as the client serialized it.
        value: String,
    },

    /// `worknet_id` is not in the current Epoch's WorkNet set.
    #[error("unknown WorkNet id {worknet_id}")]
    ValidationUnknownWorknet {
        /// The unknown id.
        worknet_id: u32,
    },

    /// `OrderKind` variant unrecognized (e.g. unknown `kind` discriminator).
    #[error("unknown order type: {received}")]
    ValidationUnknownOrderType {
        /// The unknown type as serialized JSON snippet.
        received: String,
    },

    /// `TimeInForce` variant unrecognized.
    #[error("unknown time-in-force: {received}")]
    ValidationUnknownTimeInForce {
        /// The unknown TIF as serialized JSON snippet.
        received: String,
    },

    // ===========================================================================
    // NONCE_*  (HTTP 409 — replay protection)
    // ===========================================================================
    /// Submitted nonce is `≤ stored_max_nonce_for_principal`.
    #[error("nonce too low: submitted {submitted}, min acceptable {min_acceptable}")]
    NonceTooLow {
        /// The nonce the client used.
        submitted: u64,
        /// The minimum nonce that would be accepted right now.
        min_acceptable: u64,
    },

    /// Redis Lua CAS lost a race (rare; client should retry with a higher nonce).
    #[error("nonce CAS conflict (concurrent submission lost race)")]
    NonceConflict,

    // ===========================================================================
    // RATE_*  (HTTP 429 — throttling)
    // ===========================================================================
    /// Per-(principal, endpoint-class) token bucket drained.
    #[error("rate limit exceeded for class `{limit_class}`; retry after {retry_after_seconds}s")]
    RateLimitExceeded {
        /// Suggested wait before retrying.
        retry_after_seconds: u64,
        /// Bucket name (e.g. `"order_submit"`).
        limit_class: String,
    },

    /// Matcher input channel returned `Full` (`try_send` backpressure) — the
    /// API is producing faster than the matcher consumes for this WorkNet.
    #[error("matcher backpressure on WorkNet {worknet_id}; retry after {retry_after_seconds}s")]
    RateLimitBackpressure {
        /// Suggested wait before retrying.
        retry_after_seconds: u64,
        /// The saturated WorkNet's matcher.
        worknet_id: u32,
    },

    // ===========================================================================
    // BUSINESS_*  (HTTP 403 / 404 / 409 — protocol-level rejections)
    // ===========================================================================
    /// Operation not allowed in the current Epoch phase (e.g. submitting a
    /// vote during `TradingOnly`).
    #[error("phase mismatch: current `{current_phase}`, required `{required_phase}`")]
    BusinessPhaseMismatch {
        /// The actual phase the system is in.
        current_phase: String,
        /// The phase that would have allowed this operation.
        required_phase: String,
    },

    /// Not enough `chips_available` for the requested operation (after
    /// accounting for chips already locked in resting orders).
    #[error("insufficient balance: required {required}, available {available} (locked {locked})")]
    BusinessInsufficientBalance {
        /// Chips needed for this operation.
        required: Decimal,
        /// Chips currently free.
        available: Decimal,
        /// Chips currently reserved by other resting orders.
        locked: Decimal,
    },

    /// Merge or sell would require more shares than the Principal holds (net
    /// of shares already locked in resting sell orders).
    #[error(
        "insufficient shares on WorkNet {worknet_id}: required {required}, available {available}"
    )]
    BusinessInsufficientShares {
        /// WorkNet whose share balance is short.
        worknet_id: u32,
        /// Shares needed.
        required: Decimal,
        /// Shares free (= total - locked).
        available: Decimal,
    },

    /// Order would push this Principal's holding past `ω_pos` of total
    /// outstanding shares for the WorkNet.
    #[error("position limit exceeded on WorkNet {worknet_id} (cap {cap_pct})")]
    BusinessPositionLimitExceeded {
        /// WorkNet that would breach the cap.
        worknet_id: u32,
        /// The cap as a fraction (e.g. `0.20`).
        cap_pct: Decimal,
    },

    /// Cancel / query targets a non-existent or already-terminal order.
    #[error("order {order_id} not found")]
    BusinessOrderNotFound {
        /// The id that was looked up.
        order_id: OrderId,
    },

    /// Principal attempted to cancel an order belonging to another Principal.
    #[error("order {order_id} is not owned by this principal")]
    BusinessOrderNotOwned {
        /// The id whose ownership check failed.
        order_id: OrderId,
    },

    /// Principal attempted to submit a weekly report for a WorkNet
    /// they aren't the configured operator of. The check is
    /// `request.principal == worknets.operator_principal`; a
    /// `NULL` operator binding fails the check too (safe default —
    /// a config oversight produces 403 rather than silently
    /// allowing the submission).
    ///
    /// Spec: §7-api.md L925 ("Only WorkNet operators can submit
    /// reports for their own WorkNet"). Wire fixture lives in
    /// §9.5.1's BUSINESS_* table.
    #[error("principal {principal} is not the operator for WorkNet {worknet_id}")]
    BusinessNotWorknetOperator {
        /// The WorkNet whose `operator_principal` was checked.
        worknet_id: u32,
        /// The principal that attempted the submission.
        principal: Address,
    },

    /// Endorse / fetch / delete targets a non-existent comment.
    /// Distinct from [`Self::BusinessOrderNotFound`] (different
    /// codebook entry; clients can dispatch on `code` to tell
    /// "comment forum" misses from "matching engine" misses).
    #[error("comment {comment_id} not found")]
    BusinessCommentNotFound {
        /// The comment id that was looked up.
        comment_id: uuid::Uuid,
    },

    /// STP (`CancelBoth` or `CancelTaker`) rejected the new order.
    #[error("self-trade rejected by STP `{stp_mode}` (conflicting order {conflicting_order_id})")]
    BusinessSelfTradeRejected {
        /// The STP mode that fired (e.g. `"cancel_both"`).
        stp_mode: String,
        /// The Principal's own resting order that triggered the rejection.
        conflicting_order_id: OrderId,
    },

    /// `post_only` order would take liquidity (cross the book) — rejected.
    #[error("post-only order would cross the book (best opposite price {best_opposite_price})")]
    BusinessPostOnlyWouldCross {
        /// The best ask (for buys) or bid (for sells) that would have matched.
        best_opposite_price: Decimal,
    },

    /// `reduce_only` order would grow position rather than shrink it.
    #[error("reduce-only order would increase position from {current_position} ({direction})")]
    BusinessReduceOnlyWouldIncrease {
        /// Current net position size in shares.
        current_position: Decimal,
        /// Side that would have grown position (`"buy"` / `"sell"`).
        direction: String,
    },

    /// Vote submission attempted after Phase 1 close.
    #[error("vote already final for this Epoch (Phase 1 closed at {phase_closed_at})")]
    BusinessVoteAlreadyFinal {
        /// UTC timestamp at which Phase 1 closed.
        phase_closed_at: DateTime<Utc>,
    },

    /// Vote submission attempted during Phase 2 (`TradingOnly`).
    #[error("trading-only phase: vote submissions are closed (currently {current_phase})")]
    BusinessTradingOnlyPhase {
        /// The phase that's currently active.
        current_phase: String,
    },

    // ===========================================================================
    // STATE_*  (HTTP 403 / 404 / 409 — resource state)
    // ===========================================================================
    /// Referenced `epoch_id` doesn't exist.
    #[error("epoch {epoch_id} not found")]
    StateEpochNotFound {
        /// The unknown epoch id.
        epoch_id: u64,
    },

    /// Referenced `market_id` doesn't exist in the `markets` table.
    /// Phase 2C — the multi-tenant successor to [`Self::StateEpochNotFound`].
    /// Used by the per-market REST handlers (`/markets/{market_id}/...`)
    /// to reject lookups against unknown ids cleanly with 404.
    #[error("market {market_id} not found")]
    StateMarketNotFound {
        /// The unknown market id.
        market_id: i64,
    },

    /// Vote contents requested during Phase 1 / 2 (still private).
    #[error("votes for epoch {epoch_id} not yet revealed (reveal at {reveal_at})")]
    StateVotesNotRevealed {
        /// The epoch whose votes are still private.
        epoch_id: u64,
        /// UTC timestamp at which votes become public.
        reveal_at: DateTime<Utc>,
    },

    /// No `chain_commits` row for the epoch (settlement not yet run / finished).
    #[error("no on-chain commit recorded for epoch {epoch_id}")]
    StateCommitNotFound {
        /// The epoch with no commit.
        epoch_id: u64,
    },

    /// No `epoch_results` row for the epoch — the settlement pipeline
    /// hasn't written the aggregate yet (writes at settlement step 5).
    /// Distinct from `StateCommitNotFound`, which fires when settlement
    /// HAS produced results but the on-chain commit (step 9) hasn't
    /// confirmed yet. Clients see this between the start of an epoch
    /// and the completion of settlement step 5.
    #[error(
        "no settlement results recorded for epoch {epoch_id} (settlement pipeline not yet run)"
    )]
    StateResultsNotFound {
        /// The epoch with no results row.
        epoch_id: u64,
    },

    /// Principal has zero AWP Power this epoch (no `principal_epoch_power` row).
    #[error("principal {principal} has no AWP Power in epoch {epoch_id}")]
    StatePrincipalNotInEpoch {
        /// The address that's absent from the epoch snapshot.
        principal: Address,
        /// The epoch in question.
        epoch_id: u64,
    },

    /// Principal did not submit a (final) vote in the epoch — the
    /// `(epoch, principal)` row is absent from `votes` after the
    /// reveal gate has fired. Surfaced by
    /// `GET /epochs/{id}/votes/{principal}/proof` when the requested
    /// principal isn't in the epoch's leaf set; distinct from
    /// `StatePrincipalNotInEpoch` (which fires on AWP Power absence,
    /// not on vote absence — a Staker WITH power can still skip the
    /// voting phase).
    #[error("no final vote from principal {principal} in epoch {epoch_id}")]
    StateVoteNotFound {
        /// The Staker that didn't vote.
        principal: Address,
        /// The epoch they're absent from.
        epoch_id: u64,
    },

    /// Same `X-Idempotency-Key` reused with a different request body.
    #[error(
        "idempotency key `{key}` reused with different payload (previous body hash {previous_hash})"
    )]
    StateIdempotencyKeyMismatch {
        /// The reused key.
        key: String,
        /// SHA-256 hex of the previously-accepted body, for client diff.
        previous_hash: String,
    },

    // ===========================================================================
    // CHAIN_*  (HTTP 502 / 503 — on-chain dependency failures)
    // ===========================================================================
    /// `api.awp.sh` unreachable after exhausting the retry / backoff budget.
    #[error("api.awp.sh unavailable: {last_error}")]
    ChainApiAwpUnavailable {
        /// Last error returned by the HTTP client.
        last_error: String,
    },

    /// Could not verify `delegates(principal, actor)` — both api.awp.sh and
    /// direct-RPC fallback failed.
    #[error("delegate check failed for principal {principal}, actor {actor}")]
    ChainDelegateCheckFailed {
        /// The Staker.
        principal: Address,
        /// The (unverifiable) Manager candidate.
        actor: Address,
    },

    /// Settlement step 7: some Principals' recipients couldn't be resolved
    /// (left in `pending_recipient`, mint deferred).
    #[error("recipient resolution failed for {unresolved_count} principal(s) in epoch {epoch_id}")]
    ChainRecipientResolveFailed {
        /// The settling epoch.
        epoch_id: u64,
        /// Count of Principals still unresolved after retries.
        unresolved_count: u64,
    },

    /// `EMGCommitment.commitEpochResult` reverted with a non-idempotent error
    /// (i.e. not a duplicate-commit that we can confirm against on-chain state).
    #[error("chain commit failed for epoch {epoch_id} (tx {tx_hash}): {reason}")]
    ChainCommitFailed {
        /// The settling epoch.
        epoch_id: u64,
        /// The reverted transaction hash.
        tx_hash: String,
        /// On-chain revert reason or RPC error.
        reason: String,
    },

    /// AWP Power snapshot at Epoch open could not complete; epoch open held.
    #[error("AWP Power snapshot failed for epoch {epoch_id}; epoch open held")]
    ChainSnapshotFailed {
        /// The epoch whose open is blocked.
        epoch_id: u64,
    },

    // ===========================================================================
    // INTERNAL_*  (HTTP 500 / 503 — server bugs / infrastructure)
    // ===========================================================================
    /// Postgres connection pool exhausted or unreachable.
    #[error("database unavailable: {component}")]
    InternalDatabaseUnavailable {
        /// Component name for ops triage (e.g. `"primary"`, `"replica"`).
        component: String,
    },

    /// Redis (nonce store, idempotency cache, tier-2 cache) unreachable.
    #[error("redis unavailable: {component}")]
    InternalRedisUnavailable {
        /// Component name for ops triage.
        component: String,
    },

    /// Mutating operation arrived during the settlement pipeline window.
    #[error("settlement in progress; retry after {retry_after_seconds}s")]
    InternalSettlementInProgress {
        /// Suggested wait before retrying.
        retry_after_seconds: u64,
    },

    /// Invariant violated; probable bug. The `request_id` is opaque to the
    /// client and points to server logs for triage.
    #[error("internal error (request_id {request_id})")]
    InternalUnexpectedState {
        /// Tracing correlation id for log lookup.
        request_id: String,
    },

    /// Order submitted to a worknet that doesn't have a
    /// running matcher engine. Two distinct causes both
    /// surface here:
    ///   - matcher_runtime spawn failed at boot, OR
    ///   - the request's `worknet_id` isn't in the active
    ///     set (caller bug or stale client).
    /// HTTP 503 — retryable once the operator wires the
    /// matcher; the request body is otherwise valid. Phase
    /// 13.3a-impl.
    #[error("matcher unavailable for WorkNet {worknet_id}")]
    InternalMatcherUnavailable {
        /// The WorkNet whose engine couldn't be reached.
        worknet_id: u32,
    },
    /// WAL writer reported `ENOSPC` (storage full) when
    /// trying to commit the events for this request. Phase
    /// 13 audit fix #13: distinct from the generic
    /// `InternalMatcherUnavailable` so operators paged on a
    /// storage-saturation incident don't conflate it with a
    /// missing-engine misconfiguration. Surface code
    /// `INTERNAL_WAL_DISK_FULL`, HTTP 503; retry is
    /// pointless until the underlying disk is reclaimed.
    /// W07 contract: the in-memory matcher mutation is
    /// also lost (the engine halts on WAL fail-fast and
    /// will recover from the WAL on restart), so the
    /// client's request is genuinely failed-not-pending.
    #[error("WAL disk full on WorkNet {worknet_id}; request not durable")]
    InternalWalDiskFull {
        /// WorkNet whose engine reported the failure.
        worknet_id: u32,
    },
}

impl EmgError {
    /// Returns the canonical SCREAMING_SNAKE_CASE error code per ADR-006.
    /// Stable across versions — never change the meaning of an existing code.
    pub fn code(&self) -> &'static str {
        match self {
            // AUTH_*
            Self::AuthMissingHeader { .. } => "AUTH_MISSING_HEADER",
            Self::AuthMalformedSignature => "AUTH_MALFORMED_SIGNATURE",
            Self::AuthSignatureInvalid => "AUTH_SIGNATURE_INVALID",
            Self::AuthActorMismatch { .. } => "AUTH_ACTOR_MISMATCH",
            Self::AuthUnauthorizedDelegate { .. } => "AUTH_UNAUTHORIZED_DELEGATE",
            Self::AuthTimestampOutOfWindow { .. } => "AUTH_TIMESTAMP_OUT_OF_WINDOW",
            Self::AuthEip712DomainMismatch { .. } => "AUTH_EIP712_DOMAIN_MISMATCH",
            Self::AuthSessionRequired => "AUTH_SESSION_REQUIRED",
            // VALIDATION_*
            Self::ValidationMalformedJson { .. } => "VALIDATION_MALFORMED_JSON",
            Self::ValidationInvalidVoteVector { .. } => "VALIDATION_INVALID_VOTE_VECTOR",
            Self::ValidationSimplexConstraintViolated { .. } => {
                "VALIDATION_SIMPLEX_CONSTRAINT_VIOLATED"
            }
            Self::ValidationInvalidQuantity { .. } => "VALIDATION_INVALID_QUANTITY",
            Self::ValidationInvalidPrice { .. } => "VALIDATION_INVALID_PRICE",
            Self::ValidationUnknownWorknet { .. } => "VALIDATION_UNKNOWN_WORKNET",
            Self::ValidationUnknownOrderType { .. } => "VALIDATION_UNKNOWN_ORDER_TYPE",
            Self::ValidationUnknownTimeInForce { .. } => "VALIDATION_UNKNOWN_TIME_IN_FORCE",
            // NONCE_*
            Self::NonceTooLow { .. } => "NONCE_TOO_LOW",
            Self::NonceConflict => "NONCE_CONFLICT",
            // RATE_*
            Self::RateLimitExceeded { .. } => "RATE_LIMIT_EXCEEDED",
            Self::RateLimitBackpressure { .. } => "RATE_LIMIT_BACKPRESSURE",
            // BUSINESS_*
            Self::BusinessPhaseMismatch { .. } => "BUSINESS_PHASE_MISMATCH",
            Self::BusinessInsufficientBalance { .. } => "BUSINESS_INSUFFICIENT_BALANCE",
            Self::BusinessInsufficientShares { .. } => "BUSINESS_INSUFFICIENT_SHARES",
            Self::BusinessPositionLimitExceeded { .. } => "BUSINESS_POSITION_LIMIT_EXCEEDED",
            Self::BusinessOrderNotFound { .. } => "BUSINESS_ORDER_NOT_FOUND",
            Self::BusinessCommentNotFound { .. } => "BUSINESS_COMMENT_NOT_FOUND",
            Self::BusinessOrderNotOwned { .. } => "BUSINESS_ORDER_NOT_OWNED",
            Self::BusinessNotWorknetOperator { .. } => "BUSINESS_NOT_WORKNET_OPERATOR",
            Self::InternalMatcherUnavailable { .. } => "INTERNAL_MATCHER_UNAVAILABLE",
            Self::BusinessSelfTradeRejected { .. } => "BUSINESS_SELF_TRADE_REJECTED",
            Self::BusinessPostOnlyWouldCross { .. } => "BUSINESS_POST_ONLY_WOULD_CROSS",
            Self::BusinessReduceOnlyWouldIncrease { .. } => "BUSINESS_REDUCE_ONLY_WOULD_INCREASE",
            Self::BusinessVoteAlreadyFinal { .. } => "BUSINESS_VOTE_ALREADY_FINAL",
            Self::BusinessTradingOnlyPhase { .. } => "BUSINESS_TRADING_ONLY_PHASE",
            // STATE_*
            Self::StateEpochNotFound { .. } => "STATE_EPOCH_NOT_FOUND",
            Self::StateMarketNotFound { .. } => "STATE_MARKET_NOT_FOUND",
            Self::StateVotesNotRevealed { .. } => "STATE_VOTES_NOT_REVEALED",
            Self::StateCommitNotFound { .. } => "STATE_COMMIT_NOT_FOUND",
            Self::StateResultsNotFound { .. } => "STATE_RESULTS_NOT_FOUND",
            Self::StatePrincipalNotInEpoch { .. } => "STATE_PRINCIPAL_NOT_IN_EPOCH",
            Self::StateVoteNotFound { .. } => "STATE_VOTE_NOT_FOUND",
            Self::StateIdempotencyKeyMismatch { .. } => "STATE_IDEMPOTENCY_KEY_MISMATCH",
            // CHAIN_*
            Self::ChainApiAwpUnavailable { .. } => "CHAIN_API_AWP_UNAVAILABLE",
            Self::ChainDelegateCheckFailed { .. } => "CHAIN_DELEGATE_CHECK_FAILED",
            Self::ChainRecipientResolveFailed { .. } => "CHAIN_RECIPIENT_RESOLVE_FAILED",
            Self::ChainCommitFailed { .. } => "CHAIN_COMMIT_FAILED",
            Self::ChainSnapshotFailed { .. } => "CHAIN_SNAPSHOT_FAILED",
            // INTERNAL_*
            Self::InternalDatabaseUnavailable { .. } => "INTERNAL_DATABASE_UNAVAILABLE",
            Self::InternalRedisUnavailable { .. } => "INTERNAL_REDIS_UNAVAILABLE",
            Self::InternalSettlementInProgress { .. } => "INTERNAL_SETTLEMENT_IN_PROGRESS",
            Self::InternalUnexpectedState { .. } => "INTERNAL_UNEXPECTED_STATE",
            Self::InternalWalDiskFull { .. } => "INTERNAL_WAL_DISK_FULL",
        }
    }

    /// HTTP status code per spec/07-api.md §9.5.
    pub fn http_status(&self) -> u16 {
        match self {
            // AUTH_* — all 401
            Self::AuthMissingHeader { .. }
            | Self::AuthMalformedSignature
            | Self::AuthSignatureInvalid
            | Self::AuthActorMismatch { .. }
            | Self::AuthUnauthorizedDelegate { .. }
            | Self::AuthTimestampOutOfWindow { .. }
            | Self::AuthEip712DomainMismatch { .. }
            | Self::AuthSessionRequired => 401,

            // VALIDATION_* — 400, except SIMPLEX which is 422 (semantic, not parse)
            Self::ValidationSimplexConstraintViolated { .. } => 422,
            Self::ValidationMalformedJson { .. }
            | Self::ValidationInvalidVoteVector { .. }
            | Self::ValidationInvalidQuantity { .. }
            | Self::ValidationInvalidPrice { .. }
            | Self::ValidationUnknownWorknet { .. }
            | Self::ValidationUnknownOrderType { .. }
            | Self::ValidationUnknownTimeInForce { .. } => 400,

            // NONCE_* — both 409
            Self::NonceTooLow { .. } | Self::NonceConflict => 409,

            // RATE_* — both 429
            Self::RateLimitExceeded { .. } | Self::RateLimitBackpressure { .. } => 429,

            // BUSINESS_* — mixed
            Self::BusinessPhaseMismatch { .. } | Self::BusinessTradingOnlyPhase { .. } => 403,
            Self::BusinessOrderNotOwned { .. } => 403,
            Self::BusinessNotWorknetOperator { .. } => 403,
            Self::InternalMatcherUnavailable { .. } => 503,
            Self::BusinessOrderNotFound { .. } | Self::BusinessCommentNotFound { .. } => 404,
            Self::BusinessInsufficientBalance { .. }
            | Self::BusinessInsufficientShares { .. }
            | Self::BusinessPositionLimitExceeded { .. }
            | Self::BusinessSelfTradeRejected { .. }
            | Self::BusinessPostOnlyWouldCross { .. }
            | Self::BusinessReduceOnlyWouldIncrease { .. }
            | Self::BusinessVoteAlreadyFinal { .. } => 409,

            // STATE_* — mostly 404
            Self::StateVotesNotRevealed { .. } => 403,
            Self::StateIdempotencyKeyMismatch { .. } => 409,
            Self::StateEpochNotFound { .. }
            | Self::StateMarketNotFound { .. }
            | Self::StateCommitNotFound { .. }
            | Self::StateResultsNotFound { .. }
            | Self::StatePrincipalNotInEpoch { .. }
            | Self::StateVoteNotFound { .. } => 404,

            // CHAIN_* — 502 except snapshot which is 503
            Self::ChainSnapshotFailed { .. } => 503,
            Self::ChainApiAwpUnavailable { .. }
            | Self::ChainDelegateCheckFailed { .. }
            | Self::ChainRecipientResolveFailed { .. }
            | Self::ChainCommitFailed { .. } => 502,

            // INTERNAL_* — 503 except UNEXPECTED_STATE which is 500
            Self::InternalUnexpectedState { .. } => 500,
            Self::InternalDatabaseUnavailable { .. }
            | Self::InternalRedisUnavailable { .. }
            | Self::InternalSettlementInProgress { .. }
            | Self::InternalWalDiskFull { .. } => 503,
        }
    }

    /// Canonical RFC 7807 problem-type URL (ADR-014 §3). Derived from
    /// [`code`](Self::code) by lowercasing and replacing `_` with `-`.
    pub fn problem_type(&self) -> String {
        let kebab: String = self
            .code()
            .chars()
            .map(|c| match c {
                '_' => '-',
                _ => c.to_ascii_lowercase(),
            })
            .collect();
        format!("https://emg.awp.network/problems/{kebab}")
    }
}

// =============================================================================
// Tests — verify the wire contract for every code.
// =============================================================================

#[cfg(test)]
mod tests {
    use chrono::TimeZone;
    use rust_decimal_macros::dec;
    use uuid::Uuid;

    use super::*;

    /// Helper to construct one variant of every category for status / code
    /// table-driven verification.
    fn one_of_each() -> Vec<EmgError> {
        let addr = Address([0xab; 20]);
        let oid = OrderId(Uuid::nil());
        let now = Utc.with_ymd_and_hms(2026, 4, 23, 12, 0, 0).unwrap();
        vec![
            // AUTH_*
            EmgError::AuthMissingHeader { header: "X-EMG-Principal".into() },
            EmgError::AuthMalformedSignature,
            EmgError::AuthSignatureInvalid,
            EmgError::AuthActorMismatch { claimed: addr, recovered: addr },
            EmgError::AuthUnauthorizedDelegate { principal: addr, actor: addr },
            EmgError::AuthTimestampOutOfWindow { skew_seconds: 42 },
            EmgError::AuthEip712DomainMismatch { expected_chain_id: 8453 },
            EmgError::AuthSessionRequired,
            // VALIDATION_*
            EmgError::ValidationMalformedJson { error: "expected `,`".into() },
            EmgError::ValidationInvalidVoteVector {
                field: "vote".into(),
                reason: "negative entry".into(),
            },
            EmgError::ValidationSimplexConstraintViolated { sum: dec!(0.99) },
            EmgError::ValidationInvalidQuantity { value: "-1".into() },
            EmgError::ValidationInvalidPrice { value: "1.5".into() },
            EmgError::ValidationUnknownWorknet { worknet_id: 99 },
            EmgError::ValidationUnknownOrderType { received: "stop".into() },
            EmgError::ValidationUnknownTimeInForce { received: "good_til_2".into() },
            // NONCE_*
            EmgError::NonceTooLow { submitted: 5, min_acceptable: 10 },
            EmgError::NonceConflict,
            // RATE_*
            EmgError::RateLimitExceeded {
                retry_after_seconds: 1,
                limit_class: "order_submit".into(),
            },
            EmgError::RateLimitBackpressure { retry_after_seconds: 1, worknet_id: 3 },
            // BUSINESS_*
            EmgError::BusinessPhaseMismatch {
                current_phase: "Settlement".into(),
                required_phase: "VotingAndTrading".into(),
            },
            EmgError::BusinessInsufficientBalance {
                required: dec!(10),
                available: dec!(7),
                locked: dec!(3),
            },
            EmgError::BusinessInsufficientShares {
                worknet_id: 1,
                required: dec!(5),
                available: dec!(3),
            },
            EmgError::BusinessPositionLimitExceeded { worknet_id: 1, cap_pct: dec!(0.20) },
            EmgError::BusinessOrderNotFound { order_id: oid },
            EmgError::BusinessCommentNotFound { comment_id: Uuid::nil() },
            EmgError::BusinessOrderNotOwned { order_id: oid },
            EmgError::BusinessNotWorknetOperator {
                worknet_id: 1,
                principal: Address([0u8; 20]),
            },
            EmgError::InternalMatcherUnavailable { worknet_id: 1 },
            EmgError::BusinessSelfTradeRejected {
                stp_mode: "cancel_both".into(),
                conflicting_order_id: oid,
            },
            EmgError::BusinessPostOnlyWouldCross { best_opposite_price: dec!(0.52) },
            EmgError::BusinessReduceOnlyWouldIncrease {
                current_position: dec!(10),
                direction: "buy".into(),
            },
            EmgError::BusinessVoteAlreadyFinal { phase_closed_at: now },
            EmgError::BusinessTradingOnlyPhase { current_phase: "TradingOnly".into() },
            // STATE_*
            EmgError::StateEpochNotFound { epoch_id: 42 },
            EmgError::StateMarketNotFound { market_id: 42 },
            EmgError::StateVotesNotRevealed { epoch_id: 42, reveal_at: now },
            EmgError::StateCommitNotFound { epoch_id: 42 },
            EmgError::StateResultsNotFound { epoch_id: 42 },
            EmgError::StatePrincipalNotInEpoch { principal: addr, epoch_id: 42 },
            EmgError::StateVoteNotFound { principal: addr, epoch_id: 42 },
            EmgError::StateIdempotencyKeyMismatch {
                key: "abc".into(),
                previous_hash: "0xdead".into(),
            },
            // CHAIN_*
            EmgError::ChainApiAwpUnavailable { last_error: "timeout".into() },
            EmgError::ChainDelegateCheckFailed { principal: addr, actor: addr },
            EmgError::ChainRecipientResolveFailed { epoch_id: 42, unresolved_count: 17 },
            EmgError::ChainCommitFailed {
                epoch_id: 42,
                tx_hash: "0xfeed".into(),
                reason: "OOG".into(),
            },
            EmgError::ChainSnapshotFailed { epoch_id: 43 },
            // INTERNAL_*
            EmgError::InternalDatabaseUnavailable { component: "primary".into() },
            EmgError::InternalRedisUnavailable { component: "nonce".into() },
            EmgError::InternalSettlementInProgress { retry_after_seconds: 60 },
            EmgError::InternalUnexpectedState { request_id: "req-x".into() },
            EmgError::InternalWalDiskFull { worknet_id: 7 },
        ]
    }

    /// Compile-time guard against `EmgError` variant drift.
    ///
    /// `#[non_exhaustive]` on `EmgError` only affects **downstream** crates —
    /// inside `emg-core` exhaustive matching is still allowed and required.
    /// Adding a new variant without extending this match yields a
    /// `non_exhaustive_patterns` compile error, which forces the implementer
    /// to also revisit [`one_of_each`] (and thus the
    /// `http_status` / `problem_type` / category-prefix tests).
    ///
    /// Returns the variant's expected category prefix for cross-validation
    /// against [`EmgError::code`]'s prefix.
    fn variant_category(e: &EmgError) -> &'static str {
        match e {
            EmgError::AuthMissingHeader { .. }
            | EmgError::AuthMalformedSignature
            | EmgError::AuthSignatureInvalid
            | EmgError::AuthActorMismatch { .. }
            | EmgError::AuthUnauthorizedDelegate { .. }
            | EmgError::AuthTimestampOutOfWindow { .. }
            | EmgError::AuthEip712DomainMismatch { .. }
            | EmgError::AuthSessionRequired => "AUTH",

            EmgError::ValidationMalformedJson { .. }
            | EmgError::ValidationInvalidVoteVector { .. }
            | EmgError::ValidationSimplexConstraintViolated { .. }
            | EmgError::ValidationInvalidQuantity { .. }
            | EmgError::ValidationInvalidPrice { .. }
            | EmgError::ValidationUnknownWorknet { .. }
            | EmgError::ValidationUnknownOrderType { .. }
            | EmgError::ValidationUnknownTimeInForce { .. } => "VALIDATION",

            EmgError::NonceTooLow { .. } | EmgError::NonceConflict => "NONCE",

            EmgError::RateLimitExceeded { .. } | EmgError::RateLimitBackpressure { .. } => "RATE",

            EmgError::BusinessPhaseMismatch { .. }
            | EmgError::BusinessInsufficientBalance { .. }
            | EmgError::BusinessInsufficientShares { .. }
            | EmgError::BusinessPositionLimitExceeded { .. }
            | EmgError::BusinessOrderNotFound { .. }
            | EmgError::BusinessCommentNotFound { .. }
            | EmgError::BusinessOrderNotOwned { .. }
            | EmgError::BusinessNotWorknetOperator { .. }
            | EmgError::BusinessSelfTradeRejected { .. }
            | EmgError::BusinessPostOnlyWouldCross { .. }
            | EmgError::BusinessReduceOnlyWouldIncrease { .. }
            | EmgError::BusinessVoteAlreadyFinal { .. }
            | EmgError::BusinessTradingOnlyPhase { .. } => "BUSINESS",

            EmgError::StateEpochNotFound { .. }
            | EmgError::StateMarketNotFound { .. }
            | EmgError::StateVotesNotRevealed { .. }
            | EmgError::StateCommitNotFound { .. }
            | EmgError::StateResultsNotFound { .. }
            | EmgError::StatePrincipalNotInEpoch { .. }
            | EmgError::StateVoteNotFound { .. }
            | EmgError::StateIdempotencyKeyMismatch { .. } => "STATE",

            EmgError::ChainApiAwpUnavailable { .. }
            | EmgError::ChainDelegateCheckFailed { .. }
            | EmgError::ChainRecipientResolveFailed { .. }
            | EmgError::ChainCommitFailed { .. }
            | EmgError::ChainSnapshotFailed { .. } => "CHAIN",

            EmgError::InternalMatcherUnavailable { .. }
            | EmgError::InternalDatabaseUnavailable { .. }
            | EmgError::InternalRedisUnavailable { .. }
            | EmgError::InternalSettlementInProgress { .. }
            | EmgError::InternalUnexpectedState { .. }
            | EmgError::InternalWalDiskFull { .. } => "INTERNAL",
        }
    }

    #[test]
    fn every_code_present_and_unique() {
        let codes: Vec<&'static str> = one_of_each().iter().map(EmgError::code).collect();
        // 52 variants per spec/07-api.md §9.5.1. Recent additions:
        // STATE_RESULTS_NOT_FOUND (Phase 8b Turn 3b1 review),
        // STATE_VOTE_NOT_FOUND (Turn 3b4), BUSINESS_COMMENT_NOT_FOUND
        // (Turn 6), AUTH_SESSION_REQUIRED (Phase 8c Turn 4 review),
        // BUSINESS_NOT_WORKNET_OPERATOR (Phase 8c follow-up,
        // migration 0016), BUSINESS_MATCHER_UNAVAILABLE
        // (Phase 13.3a-impl — orders.submit when the worknet's
        // matcher engine isn't running, HTTP 503),
        // INTERNAL_WAL_DISK_FULL (Phase 13 audit fix #13 — the
        // matcher's WAL writer reported `ENOSPC`; distinct from
        // the generic INTERNAL_MATCHER_UNAVAILABLE so operators
        // page on storage saturation, not missing-engine
        // misconfiguration), and STATE_MARKET_NOT_FOUND (Phase 2C
        // — multi-tenant successor to STATE_EPOCH_NOT_FOUND used by
        // the per-market REST handlers).
        assert_eq!(codes.len(), 52, "EmgError variant count drifted");
        let mut sorted = codes.clone();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(sorted.len(), codes.len(), "duplicate code(s) detected");
    }

    /// Cross-validates that [`variant_category`] (exhaustive match, drift
    /// guard) agrees with [`EmgError::code`]'s prefix. If the two ever
    /// disagree, either the categorization in `variant_category` is stale or
    /// `code()` returned a string with the wrong prefix — both are bugs.
    #[test]
    fn variant_category_matches_code_prefix() {
        for err in one_of_each() {
            let category = variant_category(&err);
            let code_prefix = err.code().split('_').next().unwrap();
            assert_eq!(
                category,
                code_prefix,
                "variant_category={} but code={} starts with {}",
                category,
                err.code(),
                code_prefix,
            );
        }
    }

    #[test]
    fn http_status_in_documented_range() {
        for err in one_of_each() {
            let status = err.http_status();
            assert!(
                (400..=599).contains(&status),
                "code {} returned bogus status {}",
                err.code(),
                status,
            );
        }
    }

    #[test]
    fn category_prefix_maps_to_expected_status_class() {
        // Spec/07-api.md §9.5 status-class mapping: AUTH=401, VALIDATION=400/422,
        // NONCE=409, RATE=429, BUSINESS=403/404/409, STATE=403/404/409,
        // CHAIN=502/503, INTERNAL=500/503.
        for err in one_of_each() {
            let code = err.code();
            let status = err.http_status();
            let prefix = code.split('_').next().unwrap();
            let allowed: &[u16] = match prefix {
                "AUTH" => &[401],
                "VALIDATION" => &[400, 422],
                "NONCE" => &[409],
                "RATE" => &[429],
                "BUSINESS" => &[403, 404, 409],
                "STATE" => &[403, 404, 409],
                "CHAIN" => &[502, 503],
                "INTERNAL" => &[500, 503],
                _ => panic!("unknown category for code {code}"),
            };
            assert!(
                allowed.contains(&status),
                "code {code} has status {status} but {prefix} only permits {allowed:?}",
            );
        }
    }

    #[test]
    fn problem_type_url_is_kebab_lowercase() {
        let e = EmgError::BusinessInsufficientBalance {
            required: dec!(1),
            available: dec!(0),
            locked: dec!(0),
        };
        assert_eq!(
            e.problem_type(),
            "https://emg.awp.network/problems/business-insufficient-balance",
        );
    }

    #[test]
    fn problem_type_unique_per_code() {
        let urls: Vec<String> = one_of_each().iter().map(EmgError::problem_type).collect();
        let mut sorted = urls.clone();
        sorted.sort();
        sorted.dedup();
        assert_eq!(sorted.len(), urls.len(), "duplicate problem-type URL");
    }

    #[test]
    fn display_message_includes_useful_context() {
        let e = EmgError::AuthTimestampOutOfWindow { skew_seconds: 99 };
        assert!(e.to_string().contains("99"), "got: {}", e);
    }

    #[test]
    fn result_alias_works_in_practice() {
        fn ok_path() -> Result<u32> {
            Ok(7)
        }
        fn err_path() -> Result<u32> {
            Err(EmgError::NonceConflict)
        }
        assert_eq!(ok_path().unwrap(), 7);
        assert!(matches!(err_path(), Err(EmgError::NonceConflict)));
    }
}
