# API shapes — request / response by endpoint

Quick reference for the wire shapes the skill talks. Authoritative source is
the OpenAPI spec; this is a developer-friendly digest.

All decimals are JSON **strings** at scale 18 (e.g. `"0.500000000000000000"`).
Carry as strings; coerce to `decimal.Decimal` only for arithmetic.

---

## Bootstrap

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

### `POST /v1/orders/cancel-batch` — body

```json
{ "order_ids": ["018f-aa", "018f-bb", "018f-cc"] }
```

Response `results[]` is a heterogeneous array — each element is either a
`CancelReceipt` (success) or a `CancelBatchError` `{order_id, code, detail}`.

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

### `POST /v1/epochs/{id}/votes`

```json
{
  "vote": ["0.5","0.3","0.2","0","0","0","0"],
  "prediction": ["0.5","0.3","0.2","0","0","0","0"],
  "nonce": 1,
  "signature": "0x<inner EMGVote sig>"
}
```

The outer transport is signed with `EMGRequest`; the inner `signature` field
is signed with `EMGVote` (different `primaryType`, same domain).

Response:

```json
{
  "epoch_id": 6,
  "vote_id": "018f-…",
  "leaf_hash": "<base64>",
  "nonce": 1,
  "received_at": "…"
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
