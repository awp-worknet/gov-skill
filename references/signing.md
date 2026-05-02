# EMG-SIG-V1 — Signing reference

This document is loaded on demand by the agent (or the developer) when a
signing-layer detail isn't already in `SKILL.md`. Five worked examples plus a
checklist of every byte that ends up in the EIP-712 digest.

---

## What gets signed

A single typed-data struct, `EMGRequest`:

```
EMGRequest(
  address principal,
  string  method,
  string  path,
  string  query,
  bytes32 bodyHash,
  uint256 nonce,
  uint256 timestamp
)
```

Bound to this domain (fetched from `GET /v1/auth/info`, never hardcoded):

```
EIP712Domain(
  string  name              = "EMG"
  string  version           = "1"
  uint256 chainId           = <as published>
  address verifyingContract = <as published>
)
```

The on-the-wire signature `X-EMG-Signature` is `r || s || v` (65 bytes,
0x-prefixed lowercase hex) over

```
keccak256("\x19\x01" || domainSeparator || hashStruct(EMGRequest))
```

`scripts/lib/sign.py::compute_eip712_digest` reproduces this in pure Python
for the known-answer test in `tests/test_sign.py`.

---

## Field-by-field rules

### `principal`

- 20-byte address, 0x-prefixed lowercase hex.
- Comes from `awp-wallet receive --json`.
- The Staker being acted on, **not** the signer. When `actor != principal`
  (Manager-on-behalf-of-Staker), the signer's address goes in `X-EMG-Actor`,
  the Staker's address stays in `principal`.

### `method`

- Uppercase HTTP method: `"GET"`, `"POST"`, `"DELETE"`, or the special
  `"WS_HELLO"` for WebSocket auth.

### `path`

- The path the **server-side handler** sees.
- The production stack uses `axum::Router::nest("/v1", …)`, which strips
  `/v1` before the auth middleware extracts `parts.uri.path()`. So sign
  `/orders`, **not** `/v1/orders`.
- Exception: WS `auth.hello` signs `path: "/v1/ws"` because the WS handler
  reads a hardcoded literal that includes the prefix
  (`crates/emg-api-ws/src/dispatch.rs:232`).

### `query`

- Empty string when there are no params.
- Otherwise, the **canonical** form per `crates/emg-auth/src/canonical.rs`:
  1. Split on `&`.
  2. Percent-decode key + value (RFC 3986; `+` is a literal, not a space).
  3. Sort by `(key, value)` ascending.
  4. Percent-encode key + value with **lowercase** hex; only
     `A-Z a-z 0-9 - . _ ~` pass through unescaped.
  5. Join with `&`, key/value separated by `=`.
- See `tests/test_canonical.py` for the full vector set.

### `bodyHash`

- `keccak256(body)` — note that's keccak, **not** SHA-256.
- For empty bodies: `0x00…00` (32 zero bytes).
- The idempotency cache uses SHA-256 (different hash); keep them straight.

### `nonce`

- Strictly greater than the principal's previously stored nonce.
- Persisted under `~/.govnet/nonces/<principal-lowercase>.json` with atomic
  rename (see `scripts/lib/nonce.py`).
- On `AUTH_NONCE_TOO_LOW` / `NONCE_TOO_LOW`, the skill re-fetches
  `/v1/auth/info`, calls `bump_to(server_stored)`, retries once.

### `timestamp`

- Unix seconds UTC (`int(time.time())`).
- Server tolerance: ±30 seconds.
- `AUTH_TIMESTAMP_OUT_OF_WINDOW` means the user's clock is skewed; surface a
  human-readable hint to sync NTP.

---

## Five worked examples

### 1. Public read — no signing

```
GET /v1/markets
```

No auth headers. `scripts/public/markets.py` just calls `fetch()`.

### 2. Signed GET — orders list with filter

```
GET /v1/orders?status=active&limit=50
```

- `principal`  = wallet address
- `method`     = `"GET"`
- `path`       = `"/orders"`               (POST-strip)
- `query`      = `"limit=50&status=active"` (sorted, percent-encoded)
- `bodyHash`   = `0x00…00`
- `nonce`      = next from local floor
- `timestamp`  = `int(time.time())`

### 3. Signed POST — submit order (with idempotency key)

```
POST /v1/orders
X-Idempotency-Key: 018f-aa…
Content-Type: application/json

{"worknet_id":11,"side":"buy","kind":"limit","quantity":"100",
 "limit_price":"0.2200","time_in_force":"gtc","stp_mode":"cancel_both"}
```

- `path` = `"/orders"`
- `query` = `""`
- `bodyHash` = `keccak256(<the JSON above>)`
- The `X-Idempotency-Key` header is **outside** the signed material —
  signing the same body with two different idempotency keys produces the
  same digest. Server caches `(principal, key) → response` for 24 hours.

### 4. Signed DELETE — cancel order

```
DELETE /v1/orders/018f-aa…
```

- `path` = `"/orders/018f-aa…"` (UUID is part of the path)
- `query` = `""`
- `bodyHash` = `0x00…00`
- HTTP returns 200 with a `CancelReceipt` even on no-op cases — inspect
  `status` (`cancelled`, `partially_filled_then_cancelled`, etc.).

### 5. Vote — outer EMGRequest + inner EMGVote

```
POST /v1/epochs/6/votes

{
  "vote": ["0.5","0.3","0.2","0","0","0","0"],
  "prediction": ["0.5","0.3","0.2","0","0","0","0"],
  "nonce": 1,
  "signature": "0x<inner EMGVote sig>"
}
```

- Outer `EMGRequest`:
  - `path` = `"/epochs/6/votes"`
  - `bodyHash` = `keccak256(<the JSON above>)`
- Inner `EMGVote` typed data, primaryType `EMGVote`:
  - `principal`     = wallet address
  - `epoch`         = 6
  - `voteHash`      = `keccak256(canonical_bytes(vote))`
  - `predictionHash`= `keccak256(canonical_bytes(prediction))`
  - `nonce`         = 1 (vote-level, NOT the EMG-SIG-V1 nonce)

`canonical_bytes(vec)` is implemented in `scripts/lib/canonical.py` as
`canonical_decimal_vector(vec)` — `4-byte LE u32 length || N × rust_decimal_serialize(d)`.
The 16-byte `rust_decimal_serialize` layout
`(lo: u32 | mid: u32 | hi: u32 | flags: u32, all little-endian, flags bits
16..23 = scale, bit 31 = sign)` is pinned in
`tests/test_rust_decimal.py` with 10 known-answer vectors.

### EMGVote shape — spec conflict

MAIN-SPEC §3 declares EMGVote as 5 fields with `principal address` plus
`epoch / nonce: uint256`. OpenAPI `SignedVoteRequest.signature` description
declares 4 fields without `principal`, with `epoch / nonce: uint64`. Until
the server's actual ABI is verified, the skill ships both forms behind
`GOVNET_VOTE_TYPED_DATA_VARIANT` — default `main_spec`, set to `openapi`
to switch:

```
GOVNET_VOTE_TYPED_DATA_VARIANT=openapi python3 scripts/vote/submit-vote.py …
```

`tests/test_vote_variants.py` proves the two variants produce different
EIP-712 digests (so the switch actually changes signing material).

---

## Verifying the digest locally

`tests/test_sign.py::test_reference_digest_matches_rust_pin` pins our
implementation to the Rust reference vector

```
chain_id           = 56
verifying_contract = 0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
principal          = 0x4242424242424242424242424242424242424242
method             = "POST"
path               = "/v1/orders"
query              = ""
bodyHash           = 0x1111…1111  (32 × 0x11)
nonce              = 7
timestamp          = 1745323200
```

→ digest `0x7686da836df9c9ae2a800b0d4c8987fa97e0e237d904b4ac3e708f29a8a4a092`.

Any divergence trips the test — the skill will not build until it's fixed.

---

## What the skill must NEVER do

- Hardcode the EIP-712 domain values. Always fetch from `/v1/auth/info` and
  cache locally; refuse to sign if the cached value was rotated unexpectedly.
- Read, write, or store a private key. Every signing operation MUST go
  through `awp-wallet sign-typed-data`.
- Send `http://` or `ws://` URLs. The skill rejects them in
  `scripts/lib/govnet_lib.py::_enforce_https`.
- **Follow HTTP redirects.** `urllib`'s default `HTTPRedirectHandler` would
  forward all five `X-EMG-*` signed headers to whatever target a 30x
  response points at — a misconfigured DNS or active MitM that returns
  `302 Location: https://attacker.example/v1/orders` would receive a
  valid signature for the original request. The module-level `_OPENER` in
  `govnet_lib.py` installs a `_NoRedirectHandler` that raises
  `EmgError(INSECURE_REDIRECT)` on any 30x. If the production endpoint
  truly needs to relocate, change DNS / load-balancer / `GOVNET_API_BASE`
  — never depend on a 30x.
- Bypass the confirm-before-irreversible prompt. Every signed write needs an
  explicit `y` (interactive) or `--yes` (non-interactive) consent.

## Client-side retry & rate-limit semantics

`signed_request` auto-retries exactly twice in two cases, each at most
once per logical call:

- **`NONCE_TOO_LOW` / `AUTH_NONCE_TOO_LOW` / `NONCE_CONFLICT`** (HTTP 401/409):
  refresh `/v1/auth/info`, call `nonce.bump_to(server_stored)`, retry once.
  Surfaces unchanged on second failure.
- **HTTP 429** (`RATE_LIMIT_EXCEEDED` / `RATE_LIMIT_BACKPRESSURE`): parse
  `Retry-After` header (delta-seconds OR HTTP-date format), `time.sleep`
  for at most 60 seconds (safety cap to defend against misconfigured
  servers parking the client), allocate a fresh nonce, retry once.

5xx responses with `X-EMG-Nonce-Burned: true` cause an immediate
`nonce.bump_to(used_nonce)` so a subsequent fresh call doesn't reuse the
burned nonce. Other 5xx surface to the caller without retry.

Pagination follows `pagination.has_more` as the **authoritative** stop
signal (per OpenAPI `Pagination` schema, `has_more` is required while
`next_cursor` is nullable + non-required). `paginate_all` walks pages
until `has_more === false` OR the cursor is empty. Default cap is 100
pages — when hit, the response carries `truncated_at_max_pages: true`
plus the next cursor so the agent can resume manually.
