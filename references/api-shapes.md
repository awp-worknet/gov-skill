# API shapes — request / response by endpoint

Quick reference for the wire shapes the skill talks. Authoritative source is
the OpenAPI spec; this is a developer-friendly digest.

All decimals are JSON **strings** at scale 18 (e.g. `"0.500000000000000000"`).
Carry as strings; coerce to `decimal.Decimal` only for arithmetic.

---

## Bootstrap

### `GET /v1/auth/time`

Lightweight clock probe (no auth, no caching):

```json
{ "server_time_unix": 1778000000 }
```

Use to detect drift before signing — a `>30s` skew will be rejected
as `AUTH_TIMESTAMP_OUT_OF_WINDOW`. The skill's `signed_request` auto-retries
that error once with a server-corrected timestamp; manual probe via
`scripts/public/auth-time.py --check`.

### `GET /v1/auth/info`

```json
{
  "protocol_version": "v4.0",
  "server_time_unix": 1745323200,
  "signature_scheme": "EMG-SIG-V1-EIP712",
  "eip712_domain": {
    "name": "EMG", "version": "1",
    "chainId": 8453,
    "verifyingContract": "0x…"
  },
  "eip712_primary_type": "EMGRequest",
  "max_timestamp_skew_seconds": 30
}
```

---

## Markets / epochs (public)

### `GET /v1/epochs/current`

```json
{
  "id": 6,
  "phase": "voting_and_trading",
  "opened_at": "2026-04-29T12:00:00Z",
  "voting_closes_at": "2026-04-30T12:00:00Z",
  "trading_closes_at": "2026-05-04T12:00:00Z",
  "settled_at": null,
  "n_worknets": 7,
  "protocol_version": "v4.0"
}
```

### `GET /v1/epochs/{id}/phase`

```json
{
  "epoch_id": 6,
  "phase": "VotingAndTrading",
  "phase_started_at": "2026-04-29T12:00:00Z",
  "next_transition_at": "2026-04-30T12:00:00Z",
  "next_phase": "TradingOnly"
}
```

> Phase strings come in two flavors: snake_case (`voting_and_trading`)
> from `/v1/epochs/{id}` and CamelCase (`VotingAndTrading`) from
> `/v1/epochs/{id}/phase`. `lib.govnet_lib.normalize_phase()` collapses both.

### `GET /v1/epochs/{id}/results`

```json
{
  "epoch_id": 5,
  "v_vector": ["0.300…", "0.200…", "0.500…"],
  "p_open_vector": ["0.250…", "0.250…", "0.500…"],
  "w_vector": ["0.270…", "0.180…", "0.550…"],
  "total_gov_tokens": "1000000.000…"
}
```

Vectors are ordered by worknet position ASC (same as `/v1/markets/{id}`).

---

## Order book / klines (public)

### `GET /v1/markets/{m}/worknets/{wn}/book?depth=20`

```json
{
  "worknet_id": 11,
  "timestamp": "2026-04-29T13:00:00Z",
  "bids": [{"price":"0.220…","total_quantity":"100.0","order_count":3}, ...],
  "asks": [{"price":"0.225…","total_quantity":"50.0","order_count":1}, ...]
}
```

### `GET /v1/markets/{m}/worknets/{wn}/klines?interval=1m`

Bare array:

```json
[
  {"timestamp":"…","open":"0.22","high":"0.23","low":"0.21","close":"0.225",
   "volume":"500","trade_count":17},
  ...
]
```

---

## Orders

### `POST /v1/orders` (signed; 202 Accepted)

Request:

```json
{
  "worknet_id": 11,
  "side": "buy",
  "kind": "limit",
  "quantity": "100",
  "limit_price": "0.2200",
  "time_in_force": "gtc",
  "post_only": false,
  "reduce_only": false,
  "stp_mode": "cancel_both",
  "allow_synthesis": true
}
```

Response:

```json
{
  "order_id": "018f-…",
  "status": "accepted",
  "initial_fills": [],
  "remaining_quantity": "100",
  "accepted_at": "2026-04-29T13:00:00Z"
}
```

> **Async write semantics.** Subsequent fills don't appear in the REST
> response — subscribe to `fills.me` / `orders.me` over WebSocket.

### `POST /v1/orders/synthesize` (signed; 202 Accepted) — Smart Order Router

Acquires `quantity` shares of one target worknet by routing through the
Smart Order Router (R8-D). The planner picks the cheaper of:

- **Direct**: match the target worknet's ask side
- **Synthesis**: split 1 chip → N shares, sell each non-target share at
  best bid, net cost = `1 - Σ best_bid(non-target)`

Request body:

```json
{
  "market_id": 6,
  "worknet_id": 11,
  "quantity": "100",
  "max_price": "0.25"
}
```

`max_price` is per-share, strictly in `(0, 1)`. The handler multiplies
by `quantity` to get the planner's total-cost cap; over-budget plans
return `409 BUSINESS_INSUFFICIENT_BALANCE`.

Response (202 `SynthesizeAcceptedResponse`):

```json
{
  "order_id": "018f-…",
  "status": "accepted",
  "actual_quantity": "100",
  "slippage_quantity": "0",
  "accepted_at": "…"
}
```

### `DELETE /v1/orders/{id}` (signed; 200 with receipt)

```json
{
  "order_id": "018f-…",
  "status": "cancelled",
  "cancelled_quantity": "100",
  "final_filled_quantity": "0",
  "last_fill_at": null,
  "cancel_processed_at": "…"
}
```

`status` values: `cancelled`, `partially_filled_then_cancelled`,
`already_fully_filled`, `already_cancelled`.

### `GET /v1/fills` (signed; cursor-paginated, self-only)

Lists the authenticated principal's fills, newest-first. **No `principal`
query parameter** — visibility is locked to the caller. Optional `?worknet_id`
+ `?since=<iso>` filters.

Cursor is opaque base64url (encoding `(filled_at, fill_id)` composite key —
treat as a black box). Real-time fills land on the WS `fills.me` channel;
this endpoint is for cold catch-up + backfill between two `fills.me` sessions.

Response uses standard `Pagination`:

```json
{
  "data": [
    {
      "id": "018f-fill-1",
      "order_id": "018f-order-7",
      "market_id": 6,
      "worknet_id": 11,
      "side": "buy",
      "role": "taker",
      "price": "0.221…",
      "quantity": "10.0",
      "filled_at": "2026-05-08T13:00:00Z"
    }
  ],
  "pagination": {"next_cursor": "<opaque>", "has_more": true, "limit": 100}
}
```

### `POST /v1/orders/cancel-batch` — body

```json
{ "order_ids": ["018f-aa", "018f-bb", "018f-cc"] }
```

Response `results[]` is a heterogeneous array — each element is either a
`CancelReceipt` (success) or a `CancelBatchError` `{order_id, code, detail}`.

---

## Principal-scoped reads (sig despite OpenAPI saying public)

OpenAPI marks the `/v1/principals/{addr}/*` GET endpoints with
`security: []` (public read). **Production actually requires EMG-SIG-V1**;
the skill signs every call. This is a known doc/server drift to keep in
mind for any reimplementation.

The query parameter for selecting an epoch is canonically `market_id`;
the server also accepts `epoch_id` as an alias (post-9387e78).

### `GET /v1/principals/{addr}/state[?market_id=N]`

Per-market chips + worknet share holdings + last-settled epoch. Three
reads parallelized server-side (M-H2). Used by `private/state.py`.

### `GET /v1/principals/{addr}/power[?market_id=N]`

AWP Power snapshot for the given market. `total_voting_power` is in
scale-18 string-decimal across all chains; `per_chain[]` breaks down
per-chain veAWP positions for audit.

A 404 `STATE_PRINCIPAL_NOT_IN_EPOCH` has three possible causes — see
`references/error-codes.md` for the disambiguation. Don't auto-prompt
"go stake" without verifying on-chain.

### `GET /v1/principals/{addr}/managers`

Currently authorized Managers (read from AWPRegistry on-chain via
api.awp.sh). The response includes `checked_at`; a value of
`1970-01-01T00:00:00Z` means the indexer has never resolved this row
— escalate as a data-freshness incident.

### `GET /v1/principals/{addr}/recipient[?market_id=N]`

Resolved gov-token recipient at settlement. 404 until the epoch has
settled.

---

## Positions

### `POST /v1/positions/split` / `merge`

```json
{ "quantity": "10.0" }
```

Both return the updated `StakerEpochState`:

```json
{
  "principal": "0x…",
  "epoch_id": 6,
  "stake": "1000",
  "chips_available": "990",
  "chips_locked_in_orders": "0",
  "max_capital_at_risk": "10",
  "positions": [{"worknet_id": 11, "shares": "10"}, …]
}
```

---

## Vote

### `POST /v1/epochs/{market_id}/votes`

Body (the skill emits both `nonce` AND `vote_revision` for forward+back compat
across the H3 / 2026-05-08 transition):

```json
{
  "vote": ["0.5","0.3","0.2","0","0","0","0"],
  "prediction": ["0.5","0.3","0.2","0","0","0","0"],
  "nonce": 1,
  "vote_revision": 1,
  "signature": "0x<inner EMGVote sig>"
}
```

Vote / prediction tolerance: `|Σ − 1| ≤ 1e-9` (D2 / mig 0047 — DB CHECK
+ application layer both validate). The skill pre-checks client-side
before spending a nonce.

The outer transport is signed with `EMGRequest`; the inner `signature` is
signed with `EMGVote` typed-data — see `references/signing.md` §5 for the
6-field shape (post-2026-05-08) and the variant switch.

Response (`VoteReceipt`):

```json
{
  "market_id": 6,
  "vote_id": "018f-…",
  "leaf_hash": "<base64>",
  "vote_revision": 1,
  "received_at": "…"
}
```

### `GET /v1/principals/{me}/votes/{market_id}` (signed; reveal-gated)

Self-only — path principal must equal authenticated principal. Returns the
caller's `RevealedVote` after the Phase 2→3 boundary has fired (settlement
started). Pre-reveal returns 403 `STATE_VOTES_NOT_REVEALED`.

```json
{
  "principal": "0x…",
  "market_id": 6,
  "vote": ["0.5", "0.3", "0.2"],
  "prediction": ["0.5", "0.3", "0.2"],
  "vote_revision": 3,
  "signature": "<base64>"
}
```

---

## WebSocket

Wire format: JSON-RPC 2.0. Single endpoint
`wss://api.gov.works/v1/ws`.

### `subscribe` request

```json
{ "jsonrpc": "2.0", "id": 1,
  "method": "subscribe",
  "params": { "channels": ["book.6.11", "klines.6.11.1m"], "since_sequence": 0 } }
```

> **Param key is `channels`, NOT `topics`** — sending `topics` returns
> `INVALID_PARAMS` and silently drops every push.

### `auth.hello` request (private channels only)

```json
{ "jsonrpc": "2.0", "id": 1,
  "method": "auth.hello",
  "params": { "principal": "0x…",
              "nonce": 42,
              "timestamp": 1745323200,
              "signature": "0x…" } }
```

The `signature` is over `EMGRequest` typed data with
`method="WS_HELLO"`, `path="/v1/ws"` (full path, **not** stripped).

### `book.update` notification

```json
{ "jsonrpc": "2.0",
  "method": "book.update",
  "params": {
    "channel": "book.6.11",
    "worknet_id": 11,
    "timestamp": "…", "sequence": 42, "previous_sequence": 41,
    "changes": [{"side": "bid", "price": "0.220…", "new_quantity": "12.5…"}]
  } }
```

> `new_quantity` is **absolute**, not a delta. `0` removes the level.

### `phase.changed` notification

```json
{ "jsonrpc": "2.0",
  "method": "phase.changed",
  "params": { "epoch_id": 6, "previous_phase": "voting_and_trading",
              "current_phase": "trading_only",
              "transitioned_at": "…", "next_transition_at": "…" } }
```

Notification field is `params.channel` (not `params.topic`).

---

## Pagination (cursor-based)

Every list endpoint returns a `Pagination` envelope alongside `data[]`:

```json
{
  "data": [ … ],
  "pagination": {
    "next_cursor": "opaque-string-or-null",
    "has_more": true,
    "limit": 100
  }
}
```

OpenAPI (`02-openapi.yaml` lines 2313-2327) marks `has_more` and `limit`
as **required**, and `next_cursor` as `nullable + non-required`. **`has_more`
is the authoritative stop signal** — `next_cursor` may legally be omitted
or null even when more pages exist (servers vary). Clients must dispatch on
`has_more` first.

The skill's `lib.govnet_lib.paginate_all(fetch_page, …)` helper implements
this:

1. `has_more === false` → stop unconditionally
2. cursor is null/empty → stop (cannot advance regardless of `has_more`)
3. otherwise → set `params["cursor"] = next_cursor`, fetch next page

Default page cap is 100. When hit, output gains `truncated_at_max_pages: true`
plus the surviving `next_cursor` so the agent can resume manually. `--all-pages`
is opt-in on the listing scripts (`leaderboard`, `orders-list`, `epochs voters`,
`epochs history`); without it scripts return one page + raw `pagination` field
so the agent can parse `next_cursor` itself.

For signed listings (`orders-list --all-pages`) each page consumes one
nonce — pagination is N independent signed GETs, not a single request. Don't
enable on million-row listings without `--max-pages`.

---

## Error envelope (RFC 7807 problem+json)

```json
{
  "type": "https://gov.works/errors/AUTH_NONCE_TOO_LOW",
  "title": "Nonce not strictly greater than stored",
  "status": 401,
  "code": "AUTH_NONCE_TOO_LOW",
  "detail": "stored=42 supplied=42; bump nonce and retry",
  "instance": "/v1/orders",
  "request_id": "…"
}
```

Dispatch on `code` (machine-stable). See `references/error-codes.md` for the
full code → message map.
