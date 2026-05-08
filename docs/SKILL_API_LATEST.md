# GovNet API — Latest snapshot for skill development

> **Audience**: an LLM agent (Claude / Codex / Gemini / etc.) building a
> Skill against the live EMG protocol on `gov.works`. Read this file
> alongside `dev_docs/openapi.yaml` (REST wire shapes), `dev_docs/asyncapi.yaml`
> (WS RPC + event shapes), and `dev_docs/spec/07-api.md` (semantics).
>
> **Audit basis**: state of `origin/main` after the 2026-05-08 deployment
> (`e91e726`). Reflects every fix through M-WS-FANOUT.

## TL;DR

- Production REST: `https://api.gov.works/v1`
- Production WS:   `wss://api.gov.works/v1/ws`
- Auth:            EMG-SIG-V1 — EIP-712 typed-data on `chainId = 8453` (Base mainnet). Verifying contract published at `GET /v1/auth/info`.
- All decimals on the wire are **strings** at scale 18 (e.g. `"0.500000000000000000"`).
- All timestamps are ISO 8601 UTC.
- Settlement results stay **off-chain** (DB-only); `chain.enabled = false` on production by design — do NOT try to read on-chain commits.

---

## 1. Endpoint surface

### 1.1 Public REST (no auth)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/v1/health` | Returns `OK`. Liveness probe. |
| `GET` | `/v1/auth/info` | `{ chainId, verifyingContract }` — EIP-712 domain. Cache locally; refresh on `AUTH_EIP712_DOMAIN_MISMATCH`. |
| `GET` | `/v1/auth/time` | Server's `now` as epoch seconds + ISO. Use to detect client clock skew before signing. |
| `GET` | `/v1/markets` | `{ items: [Market] }`. Each `Market` carries `id, name, status, voting_open_at, voting_close_at, trading_close_at, settled_at, total_gov_emission, worknets[]`. |
| `GET` | `/v1/markets/{id}` | One market with full `worknets[]` (sorted by `position` ASC). |
| `GET` | `/v1/markets/{m}/worknets/{wn}/book?depth={n}` | `OrderBookSnapshot` — `{ market_id, worknet_id, timestamp, bids[], asks[] }`. Each level: `{ price, total_quantity }` (scale-18 strings). |
| `GET` | `/v1/markets/{m}/worknets/{wn}/klines?interval={1m\|5m\|1h\|4h\|1d}&from={iso}&to={iso}&limit={n}` | OHLCV bare array. |
| `GET` | `/v1/worknets` | `{ items: [WorkNet] }` — global registry (id, name, description, is_active). Markets carry their own worknet sets via `market_worknets`; this is the directory. |
| `GET` | `/v1/epochs/{id}/results` | Settlement aggregate when `markets.status = 'completed'`. `{ market_id, v_vector, w_vector, p_open_vector, total_gov_tokens, started_at, finished_at, duration_ms }`. Vectors ordered by worknet `position` ASC. |
| `GET` | `/v1/epochs/{id}/voters` | List of voter principals (paginated: `?limit&cursor`). |
| `GET` | `/v1/epochs/{id}/merkle-root` | Vote Merkle root after vote close. |
| `GET` | `/v1/epochs/{id}/votes/{principal}/proof` | Inclusion proof for a principal's vote. |
| `GET` | `/v1/leaderboard/epistemic?market_id={id}&limit={n}&cursor={hex}` | Top-N principals by epistemic score. **Cursor is the principal address (`0x…` hex)** — pass back the last row's `principal` as `?cursor=…` for the next page. |
| `GET` | `/v1/comments?sort={recent\|bridge_score}&worknet_id={n}&author_id={hex}&market_id={n}&limit={n}&cursor={uuid}` | Forum listing. `cursor` is the last row's `id` UUID; only `sort=recent` paths support keyset pagination — `sort=bridge_score` falls back to limit-truncation envelope. |
| `GET` | `/v1/reports?market_id={n}&worknet_id={n}&limit={n}&cursor={uuid}` | Weekly-report listing. At least one of `market_id`/`worknet_id` required. Cursor is the last row's `id`. |

### 1.2 Authenticated REST

Every signed request carries `X-EMG-Principal` / `X-EMG-Actor` / `X-EMG-Nonce` / `X-EMG-Timestamp` / `X-EMG-Signature` headers. See §2.

| Method | Path | Notes |
|---|---|---|
| `GET`    | `/v1/principals/{addr}/state` | Per-market chip state + positions + last-settled epoch. Three reads parallelized server-side (M-H2). |
| `GET`    | `/v1/principals/{addr}/power` | AWP Power for current/specified epoch. |
| `GET`    | `/v1/principals/{addr}/managers` | Authorized managers per spec/08. |
| `GET`    | `/v1/principals/{addr}/recipient` | Resolved gov-token recipient address. |
| `GET`    | `/v1/orders?worknet_id&status&limit&cursor` | List principal's orders. Filters pushed into SQL (M-ORDERS); `next_cursor` is the last row's `id`. |
| `GET`    | `/v1/orders/{order_id}` | Order detail with embedded `fills[]` and `avg_fill_price`. |
| `POST`   | `/v1/orders` | Submit a new order. Idempotent via `X-Idempotency-Key`. |
| `DELETE` | `/v1/orders/{order_id}` | Cancel one order. |
| `POST`   | `/v1/orders/cancel-batch` | Cancel a list of order ids in one round-trip. |
| `POST`   | `/v1/orders/cancel-all` | Cancel every open order for the principal in the current market. |
| `POST`   | `/v1/orders/synthesize` | Smart-Order-Router: split chips on the SOR-chosen target then sell on others to synthesize a target-only buy. Server-side BidLadderCache (M-SYN-CACHE) reuses recent BookSnapshots. |
| `POST`   | `/v1/positions/split` | Atomic chips → shares per market WorkNet (per-market set, not global). |
| `POST`   | `/v1/positions/merge` | Atomic shares → chips. |
| `POST`   | `/v1/epochs/{market_id}/votes` | Submit a private vote. Reveal happens at settlement. |
| `POST`   | `/v1/reports` | One report per `(market_id, worknet_id)`. Duplicates return `409 BUSINESS_REPORT_ALREADY_SUBMITTED`. |
| `POST`   | `/v1/comments` | Post a comment. |
| `POST`   | `/v1/comments/{comment_id}/endorse` | Endorse a comment (idempotent at the DB layer; no `X-Idempotency-Key` needed). |

### 1.3 WebSocket

Single endpoint: `wss://api.gov.works/v1/ws`. JSON-RPC 2.0.

| Method | Auth | Purpose |
|---|---|---|
| `auth.hello` | Public | Establish authenticated session. Sign `EMGRequest` with `method:"WS_HELLO" path:"/v1/ws" query:"" bodyHash:"0x00…00"` using a fresh nonce + timestamp. Server returns `{ principal, actor, current_sequences[]}` so the client can compute reconnect `since_sequence`. |
| `subscribe` | Mixed (per channel) | Params `{ channels: [..], since_sequence?: { ch: u64 } }`. **Field is `channels`, not `topics`.** |
| `unsubscribe` | — | Params `{ channels: [..] }`. |
| `orders.submit` | Authed | Same wire shape as REST `POST /v1/orders` body, returned via JSON-RPC. |
| `orders.cancel` | Authed | `params: { order_id }`. |
| `positions.split` / `positions.merge` | Authed | Per-market split/merge (uses `ctx.epoch.market_id`). |
| `votes.submit` | Authed | Submit a vote inside the WS session — same EIP-712 typed data as REST. |
| `batch.config` | — | `params: { atomic: bool }`. Future: bundles multiple RPC calls in atomic mode. |
| `ping` | Public | `params: {}` — returns server time. |

#### Channel id → wire shape

| Channel | Wire `method` | Per-event params payload |
|---|---|---|
| `book.{m}.{wn}` | `book.update` | `{ channel, market_id, worknet_id, timestamp, sequence, previous_sequence, changes:[{ side:"bid"\|"ask", price, new_quantity }] }`. `new_quantity = "0"` removes the level. |
| `klines.{m}.{wn}.{interval}` | `klines.update` | Full OHLCV bucket. |
| `phase` | `phase.changed` | Global phase transitions. |
| `phase.{m}` | `phase.changed` | Same payload, scoped to one market. M-WS-FANOUT delivers per-channel without waking subscribers of other markets. |
| `fills.me` | `fills.update` | Authed. Per-principal — server gates on `auth.hello` principal. |
| `orders.me` | `orders.update` | Authed. |
| `reports`, `comments` | `reports.update`, `comments.update` | Public content streams. |
| `account` | `account.update` | Authed; per-principal balance + position deltas. |

Subscriptions wake **only** on their channel post-fix (M-WS-FANOUT, 2026-05-08): each subscribe spawns a per-channel pump task; an event on `book.6.10` does NOT wake `book.6.11` subscribers.

`since_sequence` reconnect contract:
- After `auth.hello` the server reply includes `current_sequences` per active channel. Persist client-side.
- On reconnect, send `subscribe` with `since_sequence: { "book.6.10": 42 }` to replay events with `seq > 42`.
- Replay-vs-live races dedup via the per-(connection, channel) `record_emitted_seq` ratchet.
- If the client's anchor is older than the broker's ring (1024 events / channel), server returns `SEQUENCE_TOO_OLD`; client must drop state and resync via `GET /v1/markets/{m}/worknets/{wn}/book` then start fresh.

---

## 2. EMG-SIG-V1 (EIP-712)

### 2.1 Domain

```
{ name: "EMG", version: "1", chainId: 8453, verifyingContract: <from /v1/auth/info> }
```

### 2.2 Typed data — `EMGRequest` (transport auth)

```solidity
EMGRequest {
  address principal   // Staker being acted upon (binds against cross-principal replay)
  string  method      // HTTP method UPPERCASE: "GET" "POST" "DELETE" "WS_HELLO"
  string  path        // URL path. REST: "/orders" (no /v1 prefix). WS auth.hello: "/v1/ws".
  string  query       // canonical query string (keys sorted) or "" if none
  bytes32 bodyHash    // keccak256(body bytes); 0x00..00 if empty body
  uint256 nonce       // strictly > principal's previous nonce
  uint256 timestamp   // unix seconds UTC
}
```

**Quirk**: REST `path` uses `/orders` (no `/v1` prefix); WS `auth.hello` `path` uses `/v1/ws` (with `/v1`). This asymmetry is real — see `dev_docs/spec/07-api.md` §9.3.4.

### 2.3 Typed data — `EMGVote` (vote integrity)

```solidity
EMGVote {
  address principal
  uint64  market_id
  uint64  vote_revision   // 1, 2, … per market — strictly increasing
  bytes32 vote_hash       // keccak256(vote_array_bytes)
  bytes32 prediction_hash // keccak256(prediction_array_bytes) (or 0x00 if empty)
  uint256 timestamp
}
```

Vote bodies serialize the simplex vector with NUMERIC(20,18); the hash is over the raw bytes of `Vec<Decimal>`. Both `vote` and `prediction` arrays must satisfy `|sum − 1| ≤ 1e-9` (D2 / mig 0047).

### 2.4 Headers

```
X-EMG-Principal:  0x… (20 bytes)
X-EMG-Actor:      0x… (20 bytes; defaults to Principal)
X-EMG-Nonce:      monotonic decimal integer per (principal, domain)
X-EMG-Timestamp:  unix seconds UTC
X-EMG-Signature:  0x… (65 bytes; r || s || v)
```

Server runs `ecrecover` against the EIP-712 digest → recovered address must equal `X-EMG-Actor`. If `Actor != Principal`, server consults `AWPRegistry.delegates(principal, actor)` across `[8453, 56, 1]` chains in **parallel** (H-7) and authorizes if any chain says yes. Cross-chain pollution is fixed (H-6): only positive grants are cached.

### 2.5 Idempotency

Optional `X-Idempotency-Key: <up to 255 bytes>` on every mutating REST endpoint. Server caches response status+body for 24h keyed by `(Principal, key, body_hash)`.

- **Hit (same key + same body)**: replays the cached response with header `X-Idempotency-Replay: true`.
- **Conflict (same key + different body)**: 422 `IDEMPOTENCY_KEY_REUSE` (post-H3 — was 409 STATE_IDEMPOTENCY_KEY_MISMATCH pre-2026-05). Wire envelope:
  ```json
  {"type":"…/idempotency-key-reuse","status":422,"code":"IDEMPOTENCY_KEY_REUSE",
   "detail":"…","details":{"previous_hash":"<hex sha256 of original body>"}}
  ```
  `details.previous_hash` is a 64-char SHA-256 hex digest of the original body — clients can dispatch on it to detect replays.

`POST /v1/comments/{id}/endorse` does NOT accept `X-Idempotency-Key` (handler is naturally idempotent at the DB layer via `ON CONFLICT DO NOTHING`).

---

## 3. Error envelope

RFC 7807 Problem+JSON. Every non-2xx response:

```json
{
  "type": "https://emg.awp.network/problems/<kebab-code>",
  "title": "<short human title>",
  "status": <number>,
  "code": "<MACHINE_CODE>",
  "detail": "<long human explanation>",
  "instance": "<request path>",
  "details": { ... } // optional, code-specific
}
```

Dispatch on `code`. Categories (prefix):
- `AUTH_*`        → 401
- `VALIDATION_*`  → 400 (or 422 for `VALIDATION_SIMPLEX_CONSTRAINT_VIOLATED`)
- `NONCE_*`       → 409
- `RATE_*`        → 429 (honor `Retry-After`)
- `BUSINESS_*`    → 403 / 404 / 409 (request-scope conflicts)
- `STATE_*`       → 403 / 404 (resource-state mismatches)
- `IDEMPOTENCY_*` → 422 (post-H3 split out from STATE_*)
- `CHAIN_*`       → 502 / 503 (api.awp.sh unreachable / delegate check failed)
- `INTERNAL_*`    → 500 / 503

### Codes the skill MUST handle correctly

| Code | HTTP | Action |
|---|---|---|
| `AUTH_NONCE_TOO_LOW` | 401 | Bump local nonce floor + retry once. |
| `AUTH_TIMESTAMP_OUT_OF_WINDOW` | 401 | Re-fetch `/v1/auth/time`, re-sign with corrected timestamp, retry once. |
| `AUTH_EIP712_DOMAIN_MISMATCH` | 401 | Re-fetch `/v1/auth/info` for `chainId` + `verifyingContract`. Retry once. |
| `AUTH_UNAUTHORIZED_DELEGATE` | 401 | Surface "this manager is not authorized for this principal" (likely a stale `awp-skill` config). |
| `BUSINESS_PHASE_MISMATCH` | 403 | Show current phase + when the op will be available. |
| `BUSINESS_TRADING_ONLY_PHASE` | 403 | "Voting closed; trading still open until <ts>." |
| `BUSINESS_INSUFFICIENT_BALANCE` | 409 | Show available chips. Do NOT auto-retry. |
| `BUSINESS_INSUFFICIENT_SHARES` | 409 | Show available shares per worknet. |
| `BUSINESS_REDUCE_ONLY_WOULD_INCREASE` | 409 | Buy with `reduce_only=true` is always rejected. |
| `BUSINESS_REPORT_ALREADY_SUBMITTED` | 409 | One report per (market, worknet); do NOT auto-retry. |
| `BUSINESS_VOTE_ALREADY_FINAL` | 409 | Phase 1 has closed; show `phase_closed_at`. |
| `BUSINESS_NOT_WORKNET_OPERATOR` | 403 | Only the WorkNet's configured `operator_principal` may submit reports. |
| `BUSINESS_ORDER_NOT_FOUND` | 404 | `/v1/orders/{id}` for an unknown id. |
| `BUSINESS_ORDER_NOT_OWNED` | 403 | Returned in place of 404 when the row exists but principal differs (so the caller can tell "doesn't exist" from "exists but yours"). |
| `STATE_PRINCIPAL_NOT_IN_EPOCH` | 404 | Principal has zero AWP Power for the epoch — hint at staking via `awp-skill`. |
| `STATE_RESULTS_NOT_FOUND` | 404 | Settlement hasn't completed; retry after `phase = completed`. |
| `IDEMPOTENCY_KEY_REUSE` | 422 | Same `X-Idempotency-Key` for a different body — pick a fresh key. |
| `RATE_LIMIT_EXCEEDED` | 429 | Honor `Retry-After`. |
| `VALIDATION_SIMPLEX_CONSTRAINT_VIOLATED` | 422 | Vote vector doesn't sum to 1 within `1e-9`. |
| `INTERNAL_MATCHER_UNAVAILABLE` | 503 | Per-worknet matcher down; retry with backoff. If persists > 1 min, surface congestion. |
| `INTERNAL_WAL_DISK_FULL` | 503 | Operator-paging condition; never auto-retry — surface and bail. |
| Any 5xx with `X-EMG-Nonce-Burned: true` | 5xx | Nonce was consumed; bump + retry once. |

---

## 4. Pagination

All list endpoints emit:

```json
{ "data": [...], "pagination": { "next_cursor": "<opaque>" | null, "has_more": <bool>, "limit": <n> } }
```

- `next_cursor = null` AND `has_more = false` → end of result set.
- `next_cursor != null` → pass back as `?cursor=…`. Cursor is opaque per endpoint:
  - `/v1/comments` (sort=recent), `/v1/reports`: UUID of the last row.
  - `/v1/leaderboard/epistemic`: principal address (`0x…` hex) of the last row.
  - `/v1/orders`, `/v1/fills`: UUID of the last row.

Malformed cursors return 400 `VALIDATION_MALFORMED_JSON`.

---

## 5. End-to-end flows

### 5.1 Submit a limit-buy order

```
1. Skill collects user intent: { worknet_id, side: "buy", price, qty, time_in_force: "gtc" }.
2. Skill builds JSON body. body_hash = keccak256(body).
3. Skill builds EMGRequest: { principal, method: "POST", path: "/orders",
   query: "", bodyHash: body_hash, nonce: nonce_floor + 1,
   timestamp: now_unix }.
4. Skill calls `awp-wallet sign-typed-data` with the EMGRequest typed data.
5. POST https://api.gov.works/v1/orders with X-EMG-* headers + body.
6. On 202: parse body for `{ id, status, fills[], avg_fill_price }`. Store nonce.
7. On 401 AUTH_NONCE_TOO_LOW: bump nonce floor (read from server `details.min_acceptable`), retry once.
8. On 422 IDEMPOTENCY_KEY_REUSE: pick a fresh key, do NOT auto-retry the same body.
9. On 503 INTERNAL_MATCHER_UNAVAILABLE: backoff (250ms × 2^attempt, max 5s), max 3 retries.
```

### 5.2 Cast a vote

```
1. Build vote vector (Σ = 1, |vote| = market.worknets.len()).
2. Compute vote_hash = keccak256(vote bytes).
3. Build EMGVote { principal, market_id, vote_revision: 1, vote_hash, prediction_hash: 0x00, timestamp }.
4. Sign via awp-wallet; ALSO sign the EMGRequest envelope.
5. POST /v1/epochs/{market_id}/votes with body { vote, prediction?, signature: <EMGVote sig> }.
6. Server validates simplex sum at app layer (1e-9 tolerance) AND DB layer (mig 0047 CHECK at 1e-9).
7. On 422 VALIDATION_SIMPLEX_CONSTRAINT_VIOLATED: re-normalize the vector and retry.
```

### 5.3 Subscribe to live book + private fills

```
1. Open WebSocket to wss://api.gov.works/v1/ws.
2. Send auth.hello with EMG-SIG-V1 signature (path: "/v1/ws", method: "WS_HELLO").
3. Server replies with { principal, actor, current_sequences: { ... } }. Persist sequences.
4. Send subscribe with channels: ["book.1.6", "fills.me"], since_sequence: { ... }.
5. Receive notifications:
   - book.update on book.1.6 — apply changes to local order book.
   - fills.update on fills.me — record fills.
6. On connection drop: reconnect, re-auth.hello, re-subscribe with persisted current_sequences.
   The broker's per-channel ring buffer (1024 events) replays events with seq > anchor.
7. SEQUENCE_TOO_OLD: anchor too old; re-fetch /v1/markets/.../book then resubscribe with seq=current.
```

---

## 6. Skill Layout (recommended)

```
govnet-skill/
├── SKILL.md                 # invokes scripts based on user intent
├── scripts/
│   ├── markets.py           # GET /v1/markets, /v1/markets/{id}
│   ├── book.py              # GET book + WS subscribe book.{m}.{wn}
│   ├── orders/
│   │   ├── submit.py        # POST /v1/orders (signed)
│   │   ├── cancel.py        # DELETE /v1/orders/{id} (signed)
│   │   ├── list.py          # GET /v1/orders?... (signed) with cursor pagination
│   │   └── synthesize.py    # POST /v1/orders/synthesize (signed)
│   ├── positions/
│   │   ├── split.py         # POST /v1/positions/split (signed)
│   │   └── merge.py         # POST /v1/positions/merge (signed)
│   ├── votes/
│   │   ├── cast.py          # POST /v1/epochs/{id}/votes (signed; EMGVote typed-data)
│   │   └── reveal.py        # GET vote receipt + merkle proof
│   ├── content/
│   │   ├── post_report.py   # POST /v1/reports (signed)
│   │   └── post_comment.py  # POST /v1/comments (signed)
│   ├── leaderboard.py       # GET /v1/leaderboard/epistemic
│   └── ws/
│       ├── auth.py          # auth.hello flow
│       └── subscribe.py     # subscribe + reconnect with since_sequence
├── lib/
│   ├── eip712.py            # EMGRequest + EMGVote typed-data builders
│   ├── auth.py              # invokes awp-wallet sign-typed-data
│   ├── http.py              # signed HTTP client with retry / nonce bump
│   ├── ws_client.py         # JSON-RPC 2.0 over websockets with reconnect
│   ├── decimals.py          # NUMERIC(20,18) string ↔ Decimal
│   ├── pagination.py        # cursor walker for list endpoints
│   └── errors.py            # error code → user message dispatch table
└── tests/
    └── golden/              # known-good signatures for regression
```

---

## 7. Testing the skill against production

The deployed system has 0 fills today. To exercise the skill end-to-end:

```bash
# 1. Confirm prod is reachable
curl -fsS https://api.gov.works/v1/health
curl -fsS https://api.gov.works/v1/auth/info

# 2. List markets
curl -fsS https://api.gov.works/v1/markets | jq '.items[0]'

# 3. Get the live order book for the first market's first worknet
curl -fsS 'https://api.gov.works/v1/markets/1/worknets/0/book?depth=20'

# 4. Validate cursor pagination
curl -fsS 'https://api.gov.works/v1/comments?limit=10' | jq '.pagination'
```

Signed flows require the skill's `awp-wallet` integration; the server validates EIP-712 against `chainId=8453` + `verifyingContract` from `/v1/auth/info`.

---

## 8. Cross-references

- Wire shapes (authoritative): `dev_docs/openapi.yaml`, `dev_docs/asyncapi.yaml`
- Protocol semantics: `dev_docs/spec/01..10-*.md`
- Existing fuller skill spec: `dev_docs/GOVNET_SKILL_DEVELOPMENT.md` (1186 lines; this file is the focused subset)
- AWP integration: `dev_docs/AWP_STAKING_REFERENCE.md`, `dev_docs/staking-ws-api.md`
- Deployment runbook: `dev_docs/PRODUCTION_RUNBOOK.md`
- Recent change ledger: `dev_docs/BUILD_PLAN.md` ("Known deferred gaps" + R8/M-* ids)

## Recent breaking changes (since the last skill build, if any)

| Item | Effect on the skill |
|---|---|
| **H3 (2026-05-08)**: idempotency conflict 409 → 422; code `STATE_IDEMPOTENCY_KEY_MISMATCH` → `IDEMPOTENCY_KEY_REUSE` | Skill must dispatch on the new code AND on 422 status. |
| **`BUSINESS_REPORT_ALREADY_SUBMITTED` (2026-05-08)** | New code, replaces an overload of the old idempotency code. Skill should treat as "do not retry." |
| **Cursor pagination wired (2026-05-08)** | `/v1/comments` (sort=recent), `/v1/leaderboard/epistemic`, `/v1/reports` previously rejected non-empty `cursor` with 400; now they paginate properly. Skills can drop any "ignore cursor" workaround. |
| **`/v1/orders` filter pushed into SQL (2026-05-08)** | `?worknet_id=` + `?status=` are now SQL-filtered. The pagination heuristic is correct; large status-filtered result sets are reachable. |
| **WAL EMGWAL02 → EMGWAL03 (2026-05-08)** | Not visible to clients. Operator-only. |
| **Per-channel WS pump (2026-05-08)** | Not visible to clients. Each subscription is independent; subscriptions to other channels don't affect yours. |
| **Settlement results stay off-chain (decision 2026-05-05)** | `chain.enabled = false`. Skill should NOT try to read `chain_commits` rows or wait for on-chain confirmation. The DB row at `/v1/epochs/{id}/results` is the durable source of truth. |
