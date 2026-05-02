# Error codes — full reference

All 4xx responses use RFC 7807 `application/problem+json`. The skill
dispatches on `code` (machine-stable, ADR-006 codebook) and surfaces
`title` + `detail` to the user.

```json
{
  "type": "https://gov.works/errors/AUTH_NONCE_TOO_LOW",
  "title": "Nonce not strictly greater than stored",
  "status": 401,
  "code": "AUTH_NONCE_TOO_LOW",
  "detail": "stored=42 supplied=42; bump nonce and retry",
  "instance": "/v1/orders"
}
```

---

## Code families

| Family       | HTTP statuses |
|--------------|---------------|
| `AUTH_*`     | 401           |
| `VALIDATION_*` | 400, 422    |
| `NONCE_*`    | 401, 409      |
| `RATE_*`     | 429           |
| `BUSINESS_*` | 403, 404, 409 |
| `STATE_*`    | 403, 404, 409 |
| `CHAIN_*`    | 502, 503      |
| `INTERNAL_*` | 500, 503      |

---

## Per-code map

### Authentication (`AUTH_*`) — HTTP 401

| Code                              | Action                                                                   |
|-----------------------------------|--------------------------------------------------------------------------|
| `AUTH_MISSING_HEADER`             | Skill bug — abort, log, file an issue. Do NOT retry.                     |
| `AUTH_MALFORMED_SIGNATURE`        | Bug. Refresh `auth-info`, then abort if it persists.                     |
| `AUTH_SIGNATURE_INVALID`          | Refresh `auth-info` (`force_refresh=True`); retry once. Else bail.       |
| `AUTH_ACTOR_MISMATCH`             | Wrong key in wallet OR Manager not delegated. Surface to user.           |
| `AUTH_UNAUTHORIZED_DELEGATE`      | Manager's delegation was revoked on-chain. User must re-grant.           |
| `AUTH_TIMESTAMP_OUT_OF_WINDOW`    | Local clock drift > 30s. Tell user to sync NTP.                          |
| `AUTH_SESSION_REQUIRED`           | Trying to subscribe to a private WS channel without `auth.hello` first.  |
| `AUTH_NONCE_TOO_LOW` (alias)      | See `NONCE_TOO_LOW`.                                                      |

### Nonce (`NONCE_*`) — HTTP 401 / 409

| Code            | Action                                                                            |
|-----------------|-----------------------------------------------------------------------------------|
| `NONCE_TOO_LOW` | `signed_request` auto-handles: refresh auth-info, `bump_to`, retry once.         |
| `NONCE_CONFLICT`| Concurrent skill invocation raced. Same handling as `NONCE_TOO_LOW`.              |

### Validation (`VALIDATION_*`) — HTTP 400 / 422

| Code                                  | Meaning                                                              |
|---------------------------------------|----------------------------------------------------------------------|
| `VALIDATION_MALFORMED_JSON`           | Body unparseable, missing required field, or wrong type.             |
| `VALIDATION_INVALID_VOTE_VECTOR`      | Vote vector length / element bounds wrong.                           |
| `VALIDATION_SIMPLEX_CONSTRAINT_VIOLATED` | Σ ≠ 1 or any element ∉ [0, 1].                                    |
| `VALIDATION_INVALID_QUANTITY`         | Quantity ≤ 0 or below tick size.                                     |
| `VALIDATION_INVALID_PRICE`            | Price ∉ [0, 1] or not a multiple of 0.0001.                          |
| `VALIDATION_UNKNOWN_WORKNET`          | `worknet_id` not part of this market.                                |
| `VALIDATION_UNKNOWN_ORDER_TYPE`       | Bad `kind` enum.                                                      |
| `VALIDATION_UNKNOWN_TIME_IN_FORCE`    | Bad `time_in_force` enum.                                             |

### Rate limits (`RATE_*`) — HTTP 429

| Code                       | Action                                                                  |
|----------------------------|-------------------------------------------------------------------------|
| `RATE_LIMIT_EXCEEDED`      | Honor `Retry-After`. Auto-retry with jitter.                            |
| `RATE_LIMIT_BACKPRESSURE`  | Matcher full. Same handling, but if persistent, surface congestion.    |

### Business (`BUSINESS_*`) — HTTP 403 / 404 / 409

| Code                                  | Meaning / action                                                        |
|---------------------------------------|-------------------------------------------------------------------------|
| `BUSINESS_PHASE_MISMATCH`             | Phase doesn't allow this op. Surface phase + countdown.                 |
| `BUSINESS_INSUFFICIENT_BALANCE`       | Not enough chips. Surface available chips.                              |
| `BUSINESS_INSUFFICIENT_SHARES`        | Not enough shares for `merge`. Surface per-worknet share counts.        |
| `BUSINESS_POSITION_LIMIT_EXCEEDED`    | Order would exceed position cap.                                        |
| `BUSINESS_ORDER_NOT_FOUND`            | Order id unknown to server.                                             |
| `BUSINESS_ORDER_NOT_OWNED`            | Order belongs to a different principal.                                 |
| `BUSINESS_COMMENT_NOT_FOUND`          | Comment id unknown.                                                     |
| `BUSINESS_NOT_WORKNET_OPERATOR`       | Only the worknet operator can post weekly reports.                      |
| `BUSINESS_SELF_TRADE_REJECTED`        | STP triggered.                                                          |
| `BUSINESS_POST_ONLY_WOULD_CROSS`      | Post-only order would take liquidity. Resubmit at a non-crossing price. |
| `BUSINESS_REDUCE_ONLY_WOULD_INCREASE` | Reduce-only order would grow position.                                  |
| `BUSINESS_VOTE_ALREADY_FINAL`         | Voting window closed.                                                    |
| `BUSINESS_TRADING_ONLY_PHASE`         | Op valid only outside trading_only.                                     |

### State (`STATE_*`) — HTTP 403 / 404 / 409

| Code                                  | Meaning / action                                                        |
|---------------------------------------|-------------------------------------------------------------------------|
| `STATE_EPOCH_NOT_FOUND`               | Unknown `epoch_id` (not opened or beyond retention).                    |
| `STATE_MARKET_NOT_FOUND`              | Same family for `market_id`.                                            |
| `STATE_VOTES_NOT_REVEALED`            | Trying to read votes before settlement reveal gate fired.               |
| `STATE_COMMIT_NOT_FOUND`              | Merkle root not yet committed.                                          |
| `STATE_RESULTS_NOT_FOUND`             | Settlement not yet complete. Tell user to retry after settlement.       |
| `STATE_PRINCIPAL_NOT_IN_EPOCH`        | No AWP Power → no chips. Hint at `awp-skill` for staking.               |
| `STATE_VOTE_NOT_FOUND`                | Principal didn't submit a vote in this epoch.                           |
| `STATE_IDEMPOTENCY_KEY_MISMATCH`      | Same idempotency key reused with different body. Generate fresh key.    |

### Chain (`CHAIN_*`) — HTTP 502 / 503

| Code                          | Meaning                                                          |
|-------------------------------|------------------------------------------------------------------|
| `CHAIN_API_AWP_UNAVAILABLE`   | Upstream `api.awp.sh` down.                                       |
| `CHAIN_DELEGATE_CHECK_FAILED` | Delegate check via emg-chain failed.                              |
| `CHAIN_RECIPIENT_RESOLVE_FAILED` | AWPRegistry recipient resolution failed.                       |
| `CHAIN_COMMIT_FAILED`         | On-chain root commit failed.                                      |
| `CHAIN_SNAPSHOT_FAILED`       | Epoch-open AWP Power snapshot failed.                             |

Treat all as transient — backoff and retry; if persistent, tell the user to
check status page.

### Internal (`INTERNAL_*`) — HTTP 500 / 503

| Code                                  | Meaning                                                       |
|---------------------------------------|---------------------------------------------------------------|
| `INTERNAL_MATCHER_UNAVAILABLE`        | Matcher engine for `(market, worknet)` not running.          |
| `INTERNAL_DATABASE_UNAVAILABLE`       | Postgres/Redis dropped.                                      |
| `INTERNAL_REDIS_UNAVAILABLE`          | Redis dropped.                                               |
| `INTERNAL_SETTLEMENT_IN_PROGRESS`     | Trading endpoints temporarily refused during settlement.    |
| `INTERNAL_UNEXPECTED_STATE`           | Server-side invariant violation. Bail.                       |
| `INTERNAL_WAL_DISK_FULL`              | Persistence layer at capacity.                               |

---

## Special header — `X-EMG-Nonce-Burned`

Some 5xx responses carry `X-EMG-Nonce-Burned: true`, meaning the server
consumed the nonce even though the request failed. The skill MUST bump its
local nonce floor before retrying — `lib.govnet_lib.signed_request` does
this automatically on the way out of `_attempt`.

---

## Cancel-batch per-id errors

`POST /v1/orders/cancel-batch` returns 200 with a heterogeneous
`results[]`. Each element is either a `CancelReceipt` or a
`CancelBatchError {order_id, code, detail}`. Common per-id codes:

- `BUSINESS_ORDER_NOT_FOUND`
- `BUSINESS_ORDER_NOT_OWNED`

The `code` field's presence is the structural distinguisher between the two
shapes — `CancelReceipt` never carries it.

---

## WS subscribe per-channel errors

`SubscribeResponse.failed[]` carries WS-specific codes (different codebook
from REST):

- `CHANNEL_NOT_FOUND` — bad channel string
- `CHANNEL_INVALID_FIELD` — pattern matched but field malformed
- `SEQUENCE_TOO_OLD` — `since_sequence` outside WAL retention; cold-restart
  via REST + re-subscribe without `since_sequence`
- `AUTH_SESSION_REQUIRED` — private channel on unauthenticated connection
- `RATE_LIMIT_EXCEEDED` — per-connection subscribe-rate cap

---

## Client-emitted codes (NOT from the server)

These are raised inside `scripts/lib/govnet_lib.py` before any wire
contact, or in response to wire conditions the skill refuses to honor.
The `EmgError.code` is set to one of these so callers' `except EmgError`
branches can dispatch on them the same way as server codes.

| Code                  | When                                                                          |
|-----------------------|-------------------------------------------------------------------------------|
| `INSECURE_TRANSPORT`  | URL scheme is `http://` or `ws://`. Skill refuses plaintext to keep `X-EMG-*` headers from leaking. Set `GOVNET_API_BASE` / `GOVNET_WS_URL` to an `https://` / `wss://` URL. |
| `INSECURE_REDIRECT`   | Server returned 30x. Skill refuses to follow because `urllib` would forward the signed `X-EMG-*` headers to the redirect target — a replay vector. Production endpoints should change via DNS / load-balancer / config, never via 30x. The `EmgError.detail` includes the `Location:` header value for diagnostics. |
| `NETWORK_ERROR`       | DNS resolution failed, connection refused, TLS handshake failure, etc. Wraps `urllib.error.URLError`. Retry semantics are the caller's call. |
| `MALFORMED_JSON`      | Server returned non-JSON body on a 2xx response. Either misconfigured upstream or a partial read. Surface to the user — there's no safe automatic retry. |
