# GovNet Skill — Development Guide

> A complete development specification for `govnet-skill`, a SKILL.md-compliant
> agent skill that lets users perform every user-facing operation on the EMG
> (Epistemic Market Gauge) protocol — also known as GovNet — through natural
> language. Targets Claude Code, OpenClaw, Cursor, Codex, Gemini CLI, Windsurf,
> and any other runtime that implements [agentskills.io](https://agentskills.io/specification).

This document is the **authoritative reference for implementing the skill**.
A capable agent should be able to read this file and produce a working skill
without further input.

---

## 1. Skill Identity

| Field | Value |
|---|---|
| **Skill name** | `govnet` |
| **Repository** | `github.com/awp-core/govnet-skill` (suggested) |
| **Version** | `0.1.0` (initial) |
| **Description** | EMG protocol (a.k.a. GovNet) — list and watch prediction markets, place and cancel orders, cast private votes, manage chip positions, monitor live order books and fills, and read settlement results. |
| **Network** | Production: `https://api.gov.works/v1` (REST), `wss://api.gov.works/v1/ws` (WebSocket) |
| **Chain dependency** | Base mainnet (`chainId = 8453`) — EIP-712 domain. `verifyingContract` published at `GET /v1/auth/info`. |
| **Auth scheme** | EMG-SIG-V1 (EIP-712 typed-data) — every state-changing or private-read request carries `X-EMG-Principal` / `X-EMG-Actor` / `X-EMG-Nonce` / `X-EMG-Timestamp` / `X-EMG-Signature` headers. |
| **Trigger keywords** | EMG, GovNet, gov.works, govnet market, prediction market vote, worknet emission, chips, voting Wednesday, settlement, V_j, W_j, Σ Pⱼ |

### Relationship to existing skills

```
                     ┌──────────────────────────┐
   user prompt ─────▶│   govnet-skill           │  ◀── this doc
                     │   (markets, orders,      │
                     │    votes, fills, …)      │
                     └────────────┬─────────────┘
                                  │
        signing requests          │
        (EIP-712 typed data)      ▼
                     ┌──────────────────────────┐
                     │   awp-wallet             │  github.com/awp-core/awp-wallet
                     │   sign-typed-data        │
                     │   send / approve / etc.  │
                     └────────────┬─────────────┘
                                  │
                                  │ optional, only when the user
                                  │ needs to top up their veAWP
                                  ▼ position to gain AWP Power
                     ┌──────────────────────────┐
                     │   awp-skill              │  github.com/awp-core/awp-skill
                     │   stake / withdraw /     │
                     │   delegate (manager)     │
                     └──────────────────────────┘
```

- **`awp-wallet`** is a hard dependency. Every signed request goes through
  `awp-wallet sign-typed-data --data '{…}'`. The skill never sees a private
  key directly.
- **`awp-skill`** is a soft dependency. EMG only reads veAWP positions
  (`AWPRegistry.delegates`, `staking.getPositionsGlobal`) at epoch open;
  the user's veAWP setup is `awp-skill`'s responsibility, not this skill's.
  Cross-link in error messages: e.g. when `STATE_PRINCIPAL_NOT_IN_EPOCH`
  fires, the skill should hint *"You may need to stake veAWP via awp-skill
  before next Wednesday's epoch open."*

---

## 2. Protocol Background (essential context)

EMG is a **weekly emission allocation protocol**. Every Wednesday at
12:00 UTC, a new market opens with N "worknets" (think: candidate
governance recipients). Stakers (with veAWP-derived AWP Power):

1. **Receive chips** at epoch open, scaled linearly to AWP Power.
2. **Vote privately** for 24 hours (Wed 12:00 → Thu 12:00 UTC). Votes are
   simplex points: `Σ V_S,j = 1`. They commit to a Merkle root on-chain at
   close; reveal happens at settlement.
3. **Trade prediction-market shares** for 5 days (Wed 12:00 → Tue 12:00).
   Each worknet has its own order book; price is in `[0, 1]` and
   converges (via arbitrage) toward the post-settlement vote outcome.
4. **Settle** Tuesday 12:00 → Wednesday 12:00. Engine reveals votes,
   computes `V_j` (vote-weighted), `W_j` (final emission, simplex-projected,
   bounded), distributes Gov Tokens.
5. **Commit** on-chain.

### Key glossary

| Term | Meaning |
|---|---|
| **Principal** | The Staker (veAWP holder). 20-byte address; FK across protocol. |
| **Actor** | The signer of a request. Either equals Principal (self-auth) or is a delegated Manager. |
| **Worknet** | A prediction-market outcome / governance recipient. Identified by `worknet_id`. |
| **Market** | A weekly emission market over N worknets. Has its own `market_id`. |
| **Chips** | Per-Principal trading currency, reset weekly to `chips = AWP Power`. |
| **Shares** | Per-(Principal, worknet) holdings; settle to chips at the revealed `V_j`. |
| **AWP Power** | Time-weighted veAWP voting power; snapshot at epoch open. |
| **Phase** | One of `pending`, `voting_and_trading`, `trading_only`, `settling`, `completed`. |
| **`P₀`** | Initial price for a worknet at market open (admin-set). |
| **`V`** | Stake-weighted vote outcome vector (Σ = 1). |
| **`W`** | Final emission vector (Σ = 1, with GovNet bounds [0.08, 0.25]). |
| **EMG-SIG-V1** | The protocol's EIP-712 envelope binding (principal, method, path, query, bodyHash, nonce, timestamp). |

### Phase → operation matrix

|  | pending | voting_and_trading | trading_only | settling | completed |
|---|---|---|---|---|---|
| Read public data | ✓ | ✓ | ✓ | ✓ | ✓ |
| Read private state | ✓ | ✓ | ✓ | ✓ | ✓ |
| Submit order | ✗ | ✓ | ✓ | ✗ | ✗ |
| Cancel order | ✗ | ✓ | ✓ | ✗ | ✗ |
| Submit vote | ✗ | ✓ | ✗ | ✗ | ✗ |
| Split / merge position | ✗ | ✓ | ✓ | ✗ | ✗ |
| Read settlement results | ✗ | ✗ | ✗ | ✗ | ✓ |

---

## 3. Authentication: EMG-SIG-V1

Every state-changing request and every private-read request is signed
with EIP-712 typed data. The skill MUST canonicalize exactly what the
server canonicalizes — any drift breaks the signature.

### Signing inputs

```
EMGRequest {
  principal:  address      // 20 bytes — the Staker being acted upon
  method:     string       // "GET" | "POST" | "DELETE" — uppercase
  path:       string       // "/orders" — POST-strip path (server is
                           // mounted under /v1 via Router::nest, axum
                           // strips the prefix BEFORE the auth layer
                           // sees the path)
  query:      string       // canonical query string (keys sorted, "" if none)
  bodyHash:   bytes32      // keccak256(body); 0x00..00 if body empty
  nonce:      uint256      // strictly > principal's previously-stored nonce
  timestamp:  uint256      // Unix seconds UTC
}
```

EIP-712 domain:

```
{
  name: "EMG",
  version: "1",
  chainId: <as published by GET /v1/auth/info>,    // 8453 on mainnet
  verifyingContract: <as published by GET /v1/auth/info>
}
```

### Signing flow (per request)

1. **Resolve principal**: `awp-wallet receive` returns the user's address.
   That IS the principal.
2. **Read auth domain**: `GET /v1/auth/info` (cached by skill at startup;
   refresh on chain-id mismatch). Returns `chainId` + `verifyingContract`.
3. **Build canonical bytes**:
   - `method` is HTTP method, uppercase
   - `path` is the path the server will see — **strip the `/v1` prefix**
     (the production Caddy + axum stack uses `Router::nest("/v1", …)` which
     strips before the auth middleware extracts `parts.uri.path()`)
   - `query` is the canonicalized query string. Empty string when there
     are no params; otherwise sort keys lexicographically and join with
     `&` and `=`. Values are URL-encoded per RFC 3986.
   - `bodyHash` is `keccak256(body)`; for empty bodies use 32 zero bytes.
   - `nonce` MUST be strictly greater than the principal's previously
     stored nonce. The skill should track it locally; resolve clashes by
     re-fetching `GET /v1/auth/info` (returns the current nonce floor).
   - `timestamp` is `Math.floor(Date.now() / 1000)`. The server enforces
     `|now − timestamp| ≤ 300s`.
4. **Compute the digest** as the EIP-712 hashStruct + domain separator
   per [EIP-712 §"Specification of the encoding"](https://eips.ethereum.org/EIPS/eip-712).
   In practice: build the typed-data JSON object below and hand it to
   `awp-wallet sign-typed-data --data '<json>'`. awp-wallet handles
   keccak256 and signing with the Principal's key.
5. **Send the request** with the five `X-EMG-*` headers.

### typed-data JSON for `awp-wallet sign-typed-data`

```json
{
  "domain": {
    "name": "EMG",
    "version": "1",
    "chainId": 8453,
    "verifyingContract": "0x…"
  },
  "primaryType": "EMGRequest",
  "types": {
    "EIP712Domain": [
      { "name": "name", "type": "string" },
      { "name": "version", "type": "string" },
      { "name": "chainId", "type": "uint256" },
      { "name": "verifyingContract", "type": "address" }
    ],
    "EMGRequest": [
      { "name": "principal", "type": "address" },
      { "name": "method", "type": "string" },
      { "name": "path", "type": "string" },
      { "name": "query", "type": "string" },
      { "name": "bodyHash", "type": "bytes32" },
      { "name": "nonce", "type": "uint256" },
      { "name": "timestamp", "type": "uint256" }
    ]
  },
  "message": {
    "principal": "0xabc…",
    "method": "POST",
    "path": "/orders",
    "query": "",
    "bodyHash": "0x<keccak256-of-body-hex>",
    "nonce": "12345",
    "timestamp": "1735689600"
  }
}
```

awp-wallet returns `{ "signature": "0x<r||s||v>" }` — 65 bytes hex,
0x-prefixed.

### Vote signing (separate primary type)

Vote submissions use `EMGVote` typed data — a different `primaryType`
in the same domain. Bound:

```
EMGVote {
  principal:       address
  epoch:           uint256       // market_id
  voteHash:        bytes32       // keccak256(canonical_bytes(VoteVector))
  predictionHash:  bytes32       // keccak256(canonical_bytes(PredictionVector))
  nonce:           uint256
}
```

`canonical_bytes(VoteVector)`:
- 4 bytes little-endian `u32` length prefix
- N × 16 bytes per `Decimal` value (bincode-style serialization;
  `rust_decimal::Decimal::serialize`)

The vote's transport-layer signature (the `EMGRequest` envelope) AND the
inner `EMGVote` digest are BOTH required. Submit as one POST with both
signatures in the body.

### Idempotency

State-changing endpoints accept an optional `X-Idempotency-Key`. Server
caches the response for 24 hours keyed by `(Principal, key)`. Reuse the
SAME key with a DIFFERENT body → 503 `IDEMPOTENCY_KEY_MISMATCH`. Recommended:
the skill auto-generates a UUIDv7 per logical user action and reuses it on
retry.

---

## 4. API Surface

> Authoritative reference: `dev_docs/openapi.yaml`,
> `dev_docs/asyncapi.yaml`, `dev_docs/spec/07-api.md`. The skill should
> NOT hardcode any wire shape that disagrees with these.

### 4.1 Public REST (no auth)

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/health` | Liveness probe |
| GET | `/v1/auth/info` | Returns `{ chainId, verifyingContract }` for the EIP-712 domain |
| GET | `/v1/markets` | List all markets. Response: `{ items: [Market] }` |
| GET | `/v1/markets/{id}` | One market with `worknets[]` (sorted by position) |
| GET | `/v1/markets/{m}/worknets/{wn}/book?depth={n}` | Order-book snapshot. Response: `{ market_id, worknet_id, timestamp, bids: [{ price, total_quantity }], asks: […] }` |
| GET | `/v1/markets/{m}/worknets/{wn}/klines?interval={1m\|5m\|1h\|4h\|1d}&from={iso}&to={iso}&limit={n}` | OHLCV history. Bare array. |
| GET | `/v1/worknets` | Worknet directory: `{ items: [{ id, name, … }] }` |
| GET | `/v1/epochs/current` | Current epoch + phase |
| GET | `/v1/epochs/{id}` | Epoch metadata |
| GET | `/v1/epochs/{id}/phase` | Just the phase string |
| GET | `/v1/epochs/{id}/results` | Settlement aggregate. `{ epoch_id, v_vector, w_vector, p_open_vector, total_gov_tokens }` — vectors ordered by worknet position ASC, decimals as STRINGS at scale 18 |
| GET | `/v1/epochs/{id}/voters` | List of voters |
| GET | `/v1/epochs/{id}/merkle-root` | Vote Merkle root (post-vote-close) |
| GET | `/v1/epochs/{id}/votes/{principal}/proof` | Inclusion proof for a principal's vote |
| GET | `/v1/epochs/{id}/votes/{principal}/history` | Vote history |
| GET | `/v1/leaderboard/epistemic?epoch_id={id}&limit={n}` | Epistemic-score ranking |

### 4.2 Private REST (signed, principal-scoped)

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/principals/{id}/state` | Chips + per-worknet shares |
| GET | `/v1/principals/{id}/power` | AWP Power for current epoch |
| GET | `/v1/principals/{id}/managers` | Authorized managers |
| GET | `/v1/principals/{id}/recipient` | Resolved gov-token recipient |
| GET | `/v1/orders` | List my orders. Query: `status`, `worknet_id`, `cursor`, `limit` |
| GET | `/v1/orders/{id}` | Order detail with `fills[]` + `avg_fill_price` |
| POST | `/v1/orders` | Submit a new order |
| DELETE | `/v1/orders/{id}` | Cancel an order |
| POST | `/v1/orders/cancel-all` | Cancel all my open orders |
| POST | `/v1/orders/cancel-batch` | Cancel a list of order ids |
| POST | `/v1/positions/split` | Split chips into shares |
| POST | `/v1/positions/merge` | Merge shares back to chips |
| POST | `/v1/epochs/{id}/votes` | Submit a private vote |
| POST | `/v1/reports` | Post a weekly report |
| POST | `/v1/comments` | Post a comment |
| POST | `/v1/comments/{id}/endorse` | Endorse a comment |

### 4.3 WebSocket channels

Single endpoint: `wss://api.gov.works/v1/ws`

JSON-RPC 2.0 over WS. Subscribe RPC:

```json
{ "jsonrpc": "2.0", "id": 1, "method": "subscribe",
  "params": { "channels": ["book.6.10", "klines.6.10.1m"], "since_sequence": 0 } }
```

> **CRITICAL**: the param key is `channels`, NOT `topics`. Server-side
> `SubscribeParams::channels: Vec<String>`. Sending `topics` returns
> `INVALID_PARAMS` and silently fails.

Server-pushed notification:

```json
{ "jsonrpc": "2.0", "method": "book.update",
  "params": { "channel": "book.6.10", "market_id": 6, "worknet_id": 10,
              "timestamp": "…", "sequence": 42, "previous_sequence": 41,
              "changes": [{ "side": "bid", "price": "0.500…", "new_quantity": "12.5…" }] } }
```

| Channel | Auth | Payload |
|---|---|---|
| `book.{m}.{wn}` | Public | `book.update` — `changes[]` with `side: "bid"\|"ask"`, `price`, `new_quantity` (absolute, scale-18 string; `0` = level removed) |
| `klines.{m}.{wn}.{interval}` | Public | `klines.update` — full OHLCV bucket |
| `phase` | Public | `phase.update` — emits on every market state transition |
| `fills.me` | Authed (`auth.hello`) | `fills.update` — private fills for the authed principal |
| `orders.me` | Authed | `orders.update` — order status changes (Active → PartiallyFilled → Filled etc.) |

#### Authenticated WS handshake

```json
{ "jsonrpc": "2.0", "id": 1, "method": "auth.hello",
  "params": { "principal": "0x…", "method": "WS_HELLO", "path": "/v1/ws",
              "query": "", "bodyHash": "0x00…00", "nonce": "12345",
              "timestamp": "…", "signature": "0x…" } }
```

The `signature` is over an `EMGRequest` typed data with `method: "WS_HELLO"`,
`path: "/v1/ws"` (this one DOES use the full path with `/v1` because the
WS handler's hardcoded literal sees the full URI — see audit note in
`crates/emg-api-ws/src/dispatch.rs:232`).

Successful auth allows subscribing to `fills.me` / `orders.me`.

### 4.4 Error envelope (Problem+JSON)

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

The skill should dispatch on `code` (machine-stable) for handling logic
and surface `title` + `detail` in user-visible output.

#### Selected codes the skill MUST handle

| Code | HTTP | Meaning | Skill response |
|---|---|---|---|
| `AUTH_MISSING_HEADER` | 401 | One of `X-EMG-*` headers missing | Bug — abort, log |
| `AUTH_SIGNATURE_INVALID` | 401 | ecrecover failed | Re-fetch `/v1/auth/info`, retry once; else bail |
| `AUTH_NONCE_TOO_LOW` | 401 | Nonce ≤ stored | Bump local nonce floor + retry once |
| `AUTH_TIMESTAMP_OUT_OF_WINDOW` | 401 | Clock skew | Sync local clock; instruct user if persistent |
| `BUSINESS_PHASE_MISMATCH` | 409 | Wrong phase for op | Surface phase + when the op will be available |
| `BUSINESS_INSUFFICIENT_BALANCE` | 409 | Not enough chips | Show available chips |
| `STATE_PRINCIPAL_NOT_IN_EPOCH` | 404 | No AWP Power | Hint at `awp-skill` for staking |
| `RATE_LIMIT_EXCEEDED` | 429 | Honor `Retry-After` | Auto-retry with backoff |
| `RATE_LIMIT_BACKPRESSURE` | 429 | Matcher full | Retry after `Retry-After`; if persistent, surface congestion |
| `STATE_RESULTS_NOT_FOUND` | 404 | Settlement not done yet | Show phase; tell user to retry after settlement window |
| `INTERNAL_*` (5xx) | 500/503 | Backend issue. If response carries `X-EMG-Nonce-Burned: true`, the nonce was consumed — bump and retry. | Honor the burned-header contract |

---

## 5. Skill Layout

Recommended directory structure (mirrors `awp-skill`'s pattern):

```
govnet-skill/
├── SKILL.md                        # See §6 for the full template
├── README.md                       # User-facing quick-start
├── LICENSE                         # MIT, per AWP convention
├── package.json                    # If any Node bridges; minimal otherwise
├── scripts/
│   ├── lib/
│   │   ├── govnet_lib.py           # REST client, error mapping, fmtPrice/fmtAmount
│   │   ├── sign.py                 # build_emg_request_typed_data + invoke awp-wallet
│   │   ├── nonce.py                # local nonce tracker per principal
│   │   ├── ws.py                   # JSON-RPC over WebSocket helper
│   │   └── canonical.py            # query-string canonicalization
│   ├── public/
│   │   ├── markets.py              # list / get
│   │   ├── book.py                 # snapshot
│   │   ├── klines.py               # history
│   │   ├── worknets.py             # directory
│   │   ├── epochs.py               # current / by-id / phase / results / merkle
│   │   ├── leaderboard.py          # epistemic ranking
│   │   └── auth-info.py            # bootstrap chain id + verifying contract
│   ├── private/
│   │   ├── state.py                # principal state
│   │   ├── orders-list.py
│   │   ├── orders-get.py
│   │   └── ...
│   ├── trade/
│   │   ├── submit-order.py         # signed POST /v1/orders
│   │   ├── cancel-order.py         # signed DELETE /v1/orders/{id}
│   │   ├── cancel-all.py
│   │   └── cancel-batch.py
│   ├── vote/
│   │   ├── submit-vote.py          # signed POST /v1/epochs/{id}/votes
│   │   └── verify-proof.py
│   ├── positions/
│   │   ├── split.py
│   │   └── merge.py
│   ├── content/
│   │   ├── post-comment.py
│   │   ├── post-report.py
│   │   └── endorse.py
│   ├── stream/
│   │   ├── watch-book.py
│   │   ├── watch-klines.py
│   │   ├── watch-phase.py
│   │   └── watch-private.py        # fills.me + orders.me with auth.hello
│   └── helpers/
│       ├── what-can-i-do.py        # list ops valid in current phase
│       ├── show-receipt.py         # pretty-print a fill / settlement
│       └── countdown.py            # how long until vote close / trading close / settlement
└── references/
    ├── api-shapes.md               # request/response by endpoint
    ├── signing.md                  # EMG-SIG-V1 reference + 5 worked examples
    ├── status-state-machine.md     # phase transitions
    └── error-codes.md              # full code → user-text map
```

### Why this split

- **`scripts/public/`** — every script here works without a wallet, no
  signing. The skill should answer questions like "what markets are open"
  even if the user hasn't set up a wallet yet.
- **`scripts/private/`** — signed reads. Need the wallet bound to a
  Principal that has AWP Power in the current epoch.
- **`scripts/trade/` + `scripts/vote/` + `scripts/positions/` +
  `scripts/content/`** — signed writes. Highest-stakes paths;
  pre-flight checks (phase, balance, nonce) run before the actual
  signed POST.
- **`scripts/stream/`** — long-running WebSocket subscribers; emit
  one JSON object per event to stdout.
- **`scripts/helpers/`** — agent-side conveniences. Don't talk to the
  network for things the skill can derive locally.

---

## 6. SKILL.md Template

```markdown
---
name: govnet
version: 0.1.0
description: >
  EMG protocol (a.k.a. GovNet) — list and watch prediction markets,
  place and cancel limit/market orders, cast private votes during the
  voting window, manage chip-to-share split/merge positions, monitor
  live order books and fills, and read settlement results.

  Use this skill when the user mentions: GovNet, gov.works, EMG,
  prediction market vote, worknet emission, "chips this epoch", "split
  chips into shares", "buy aMINE / aGOV / aPRED" (worknet names),
  voting Wednesday, epoch settlement, V_j, W_j, "what is Σ Pⱼ".

  The skill composes with awp-wallet (signs every state-changing or
  private-read request via EIP-712) and optionally with awp-skill
  (when the user needs to top up a veAWP position to gain AWP Power
  for the next epoch).

  NOT for: generic Solidity/EVM operations, non-EMG DeFi protocols,
  Uniswap / Aave / Lido, raw token transfers, NFT trading. Do NOT
  trigger on bare "vote" / "trade" without GovNet/EMG context.

metadata:
  openclaw:
    requires:
      bins:
        - python3        # All bundled scripts are Python 3.10+
        - node           # awp-wallet bridge
      anyBins:
        - awp-wallet     # github.com/awp-core/awp-wallet — installed automatically on first load
      env:
        - GOVNET_NONCE_DIR  # Optional. Defaults to ~/.govnet/nonces/
    emoji: "📜"
    homepage: https://github.com/awp-core/govnet-skill
    install:
      - kind: git-bash
        repo: https://github.com/awp-core/awp-wallet
        cmd: bash install.sh
        when: missing-bin awp-wallet
    security:
      wallet_bridge:
        no_direct_key_access: true
        # Every signed request goes through `awp-wallet sign-typed-data`.
        # The skill never reads, writes, or stores a private key.
      network:
        endpoints:
          - https://api.gov.works/v1
          - wss://api.gov.works/v1/ws
        # All endpoints are HTTPS / WSS only. The skill rejects http://
        # to prevent man-in-the-middle stripping of EMG-SIG headers.
---

# GovNet (EMG Protocol) Skill

… long-form documentation continues; see §7 for content sections to include.
```

### Required SKILL.md sections

1. **Quick start** — install, first-time wallet setup, a single
   end-to-end example ("show me the open markets and tell me what's
   trading now").
2. **API mapping table** — every script ↔ endpoint, one line each.
3. **Signing semantics** — link to `references/signing.md`. Critical
   for any future agent reading the skill.
4. **Phase awareness** — the matrix in §2; embed it directly so the
   agent doesn't have to fetch a reference.
5. **Composition with awp-wallet / awp-skill** — when to delegate.
6. **Error-handling discipline** — `AUTH_NONCE_TOO_LOW` retry, etc.
7. **Confirm-before-irreversible** — every signed write should
   surface a `[TX] about to …` block and ask `proceed? (y/n)` like
   `awp-wallet send` does. Never auto-execute trades.

---

## 7. Detailed implementation notes

### 7.1 `scripts/lib/sign.py` (the load-bearing helper)

Shape the signing helper as a single function with this contract:

```python
def sign_emg_request(
    principal: str,
    method: str,        # "GET" / "POST" / "DELETE"
    path: str,          # POST-strip path, e.g. "/orders"
    query: str,         # canonical query string ("" if empty)
    body: bytes,        # raw bytes to be sent (b"" if no body)
    nonce: int,         # local nonce floor +1
    timestamp: int,     # int(time.time())
    auth_info: dict,    # {chainId, verifyingContract} from /v1/auth/info
) -> dict:
    """
    Returns a dict with the five X-EMG-* header values
    PLUS the signature bytes for downstream use.
    Internally invokes `awp-wallet sign-typed-data --data '<json>'`.
    """
```

Implementation outline:

```python
import subprocess, json, hashlib

def keccak256(data: bytes) -> bytes:
    # Python eth-hash or pysha3; awp-wallet bundles a Node bridge that
    # would also work via `awp-wallet keccak256 --hex …` if exposed.
    from eth_hash.auto import keccak
    return keccak(data)

def sign_emg_request(principal, method, path, query, body, nonce, timestamp, auth_info):
    body_hash = "0x" + keccak256(body).hex()
    typed_data = {
      "domain": {
        "name": "EMG", "version": "1",
        "chainId": int(auth_info["chainId"]),
        "verifyingContract": auth_info["verifyingContract"],
      },
      "primaryType": "EMGRequest",
      "types": {
        "EIP712Domain": [
          {"name": "name", "type": "string"},
          {"name": "version", "type": "string"},
          {"name": "chainId", "type": "uint256"},
          {"name": "verifyingContract", "type": "address"},
        ],
        "EMGRequest": [
          {"name": "principal", "type": "address"},
          {"name": "method", "type": "string"},
          {"name": "path", "type": "string"},
          {"name": "query", "type": "string"},
          {"name": "bodyHash", "type": "bytes32"},
          {"name": "nonce", "type": "uint256"},
          {"name": "timestamp", "type": "uint256"},
        ],
      },
      "message": {
        "principal": principal,
        "method": method,
        "path": path,
        "query": query,
        "bodyHash": body_hash,
        "nonce": str(nonce),
        "timestamp": str(timestamp),
      },
    }
    proc = subprocess.run(
        ["awp-wallet", "sign-typed-data", "--data", json.dumps(typed_data)],
        capture_output=True, text=True, check=True,
    )
    sig = json.loads(proc.stdout)["signature"]
    return {
      "X-EMG-Principal": principal,
      "X-EMG-Nonce": str(nonce),
      "X-EMG-Timestamp": str(timestamp),
      "X-EMG-Signature": sig,
      # Actor defaults to Principal; only set X-EMG-Actor explicitly
      # when a Manager is signing on behalf of someone else.
    }
```

### 7.2 `scripts/lib/canonical.py`

The `query` field in the EIP-712 envelope is the URL query string
**after canonicalization**. Server-side rule (verified in
`crates/emg-auth/src/canonical.rs`):

1. Split on `&`.
2. For each `k=v` pair, URL-decode both sides; URL-encode them again
   per RFC 3986 (unreserved chars unescaped; everything else `%HH`).
3. Sort pairs by key, then by value, ascending.
4. Join with `&`.
5. Empty input → empty output.

Mismatch with the server's canonicalization → `AUTH_SIGNATURE_INVALID`.

### 7.3 `scripts/lib/nonce.py`

The skill MUST track a per-Principal nonce floor. Atomic incrementing
prevents two concurrent skill invocations from racing the same nonce.

```python
# ~/.govnet/nonces/<principal-lowercased>.json
# {"nonce": 42, "updated_at": "2026-05-02T12:34:56Z"}
```

On `AUTH_NONCE_TOO_LOW` (server's stored is ahead): re-fetch
`GET /v1/auth/info` (which returns the floor) → bump local file → retry.

### 7.4 Confirmation prompts

For every signed write, the skill emits a confirmation block to stdout
and waits for `y` on stdin (matching `awp-wallet send`'s pattern):

```
[TX] about to submit order:
     market:    №6 (aMINE / aGOV / aPRED / aKYA / aARDI / aTMR / aCOM)
     worknet:   aGOV (id 11)
     side:      buy
     kind:      limit @ 0.2200
     quantity:  100
     post_only: false
     reduce_only: false
     stp_mode:  cancel_both
     idempotency-key: 018f-…
     nonce:     43 (was 42)
     proceed? (y/n)
```

If the runtime is non-interactive (no tty), accept a `--yes` flag;
without it, refuse the operation. NEVER auto-execute a signed write
without explicit consent.

### 7.5 WS subscriber pattern (`scripts/stream/*.py`)

```python
import websockets, json, sys, asyncio

async def main(channels: list[str]):
    async with websockets.connect("wss://api.gov.works/v1/ws") as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "subscribe",
            "params": {"channels": channels}
        }))
        # (Optional auth.hello here for fills.me / orders.me)
        ack = await ws.recv()
        ack = json.loads(ack)
        if ack.get("error"):
            print(json.dumps({"error": ack["error"]}))
            sys.exit(1)
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") and msg.get("params"):
                # Stream one event per line so the agent can parse
                # incrementally without buffering the whole stream.
                print(json.dumps({
                    "channel": msg["params"].get("channel"),
                    "method": msg["method"],
                    "params": msg["params"],
                }), flush=True)

asyncio.run(main(sys.argv[1:]))
```

> **Param key MUST be `channels`** — see §4.3 critical note.

### 7.6 Decimal handling

The server emits NUMERIC(28,18) and NUMERIC(20,18) values as JSON
**strings**. The skill should:
- Carry strings end-to-end on the wire.
- Coerce to a high-precision Decimal (Python `decimal.Decimal`) for
  arithmetic.
- Format with `Decimal.quantize(Decimal('0.0001'))` for 4-decimal
  user display.
- Never use Python `float` on a price or quantity. `+x` / `parseFloat`
  loses precision past 15 significant digits.

### 7.7 Phase-aware refusal

Before every signed write, fetch `/v1/markets/{id}` and check
`market.status` against the phase matrix in §2. Refuse early with a
helpful message rather than letting the server return
`BUSINESS_PHASE_MISMATCH`. Example:

```
✗ Cannot submit a vote — market №6 is in 'trading_only' phase.
  Voting closed at 2026-05-01 14:00 UTC (3h 12m ago).
  Trading closes at 2026-05-06 12:00 UTC (in 4d 21h 48m).
```

### 7.8 Composition with awp-skill

When `STATE_PRINCIPAL_NOT_IN_EPOCH` fires, surface:

```
✗ You have no AWP Power in the current epoch (id 6).
  AWP Power is snapshotted at every Wednesday 12:00 UTC. To get power
  for the NEXT epoch:
    1. Stake AWP into a veAWP position via awp-skill:
         awp <stake intent>
    2. Wait for the next epoch open (in 2d 18h 12m).
  Existing veAWP holders should check their position via
    awp positions
  to confirm it's still active.
```

The skill should NOT shell out to `awp-skill` — it should suggest the
command and let the user invoke it explicitly.

---

## 8. Example end-to-end flows

### 8.1 "What's open right now?"

1. `GET /v1/markets` → filter `status in [voting_and_trading, trading_only]`.
2. For each: format
   ```
   №6 — Voting + Trading
        7 worknets (aMINE, aGOV★, aPRED, aKYA, aARDI, aTMR, aCOM)
        Voting closes  in 3h 12m  (2026-05-01 14:00 UTC)
        Trading closes in 4d 21h  (2026-05-06 12:00 UTC)
        Σ Emission     1,000,000 govₜ
   ```
3. Hint at follow-up commands: "Show worknet details for №6", "Watch
   the order book", "Cast a vote".

### 8.2 "Buy 100 aGOV at 0.22"

1. Resolve worknet name → id via `GET /v1/worknets`. (`aGOV` → id 11.)
2. Fetch `GET /v1/markets/?worknet_id=11&status=open` → market id.
3. Phase check (`voting_and_trading` or `trading_only`).
4. `GET /v1/principals/{me}/state` → confirm chips ≥ qty × price.
5. `GET /v1/markets/{m}/worknets/{wn}/book?depth=5` → sanity-check spread.
6. Build order body:
   ```json
   {
     "market_id": 6, "worknet_id": 11, "side": "buy",
     "kind": "limit", "limit_price": "0.2200",
     "total_quantity": "100", "time_in_force": "gtc",
     "post_only": false, "reduce_only": false,
     "stp_mode": "cancel_both"
   }
   ```
7. Build EIP-712 envelope (`POST` / `/orders`); sign via awp-wallet.
8. Confirmation prompt → user types `y`.
9. POST with headers; surface response.
10. Optionally: subscribe `orders.me` + `fills.me` to stream the
    order's progression.

### 8.3 "Watch the book for aGOV"

Long-running. Run `scripts/stream/watch-book.py 6 11`:

```
[2026-05-02 12:34:56] book.6.11 seq=42  bid 0.2200 × 100  ask 0.2250 × 50
[2026-05-02 12:34:58] book.6.11 seq=43  bid 0.2200 × 100  ask 0.2250 × 30  (ask 30 cleared)
[2026-05-02 12:35:01] book.6.11 seq=44  bid 0.2210 × 25   ask 0.2250 × 30  (new bid)
```

Frontend parses the `new_quantity` per change to maintain a local book
state map.

### 8.4 "Cast my vote: 50% aGOV, 30% aMINE, 20% aPRED"

1. `GET /v1/markets/{id}` → confirm phase = `voting_and_trading`,
   read worknets[] in position order.
2. Validate: `Σ vote = 1`; every entry in `[0, 1]`.
3. Build vote vector indexed by worknet position
   (zero for omitted worknets):
   ```
   pos=0 aMINE  → 0.3
   pos=1 aGOV   → 0.5
   pos=2 aPRED  → 0.2
   pos=3 aKYA   → 0
   pos=4 aARDI  → 0
   pos=5 aTMR   → 0
   pos=6 aCOM   → 0
   ```
4. Sign the inner `EMGVote` typed data via `awp-wallet sign-typed-data`
   (different `primaryType`).
5. Sign the outer `EMGRequest` envelope.
6. Confirmation prompt:
   ```
   [VOTE] about to submit:
          market:    №6
          vote:      aMINE 0.30 · aGOV 0.50 · aPRED 0.20
                     (others: 0)
          prediction: <same / different per user input>
          principal: 0xabc…
          nonce:     7
          ⚠️  Votes are FINAL. You cannot change your vote after submission.
          proceed? (y/n)
   ```
7. POST `/v1/epochs/{id}/votes` with both signatures in body.
8. On success: surface the response's `merkle_proof_url` (if any) so
   the user can verify their vote was committed.

---

## 9. Trigger keywords — guidance for natural-language matching

Trigger when the user mentions any of:

- **Protocol names**: GovNet, gov.works, EMG, emg.gov.works
- **Operations**: "list markets", "open markets", "current epoch",
  "vote on …", "cast my vote", "split into shares", "merge back to chips",
  "submit order", "cancel my order", "watch the book", "see fills"
- **Worknet names** the user has heard of (aMINE, aGOV, aPRED, aKYA,
  aARDI, aTMR, aCOM, plus future names — fetch fresh from
  `GET /v1/worknets` to keep this list current)
- **Domain terms**: V_j, W_j, "Σ Pⱼ", "epoch settlement",
  "voting Wednesday", "Σ Emission"

Do NOT trigger on:

- Generic "vote" / "trade" without GovNet/EMG context — could refer
  to any DeFi protocol.
- "Create a market" → could refer to Polymarket, Augur, etc. Require
  explicit GovNet/EMG mention.
- veAWP staking — that's `awp-skill`'s domain. Cross-link only.

---

## 10. Testing approach

### 10.1 Unit tests

- `scripts/lib/canonical.py` query-string canonicalization: include
  vectors from `crates/emg-auth/src/canonical.rs::tests`.
- `scripts/lib/sign.py` EIP-712 typed-data construction: use
  `crates/emg-auth/src/eip712.rs::REFERENCE_DIGEST_HEX` as a known-
  answer test (reproduce the same digest from the same input).
- Phase matrix logic: golden-input → expected-allow/deny table.

### 10.2 Integration tests

The skill ships against a public dev/staging deployment if available;
otherwise developers run the workspace's `cargo run -p emg-server`
locally with `EMG_DATABASE_URL=postgres://localhost:5432/emg_test` and
test against `http://localhost:8080/v1`.

Test harness pattern: spawn a private wallet via
`awp-wallet --agent-id govnet-test`, fund the principal in the test DB,
run end-to-end flows.

### 10.3 Conformance test against the server

The `dev_docs/openapi.yaml` file is the wire contract. Run a tool like
`schemathesis` against `https://api.gov.works/v1` to confirm the
skill's request shapes match. Any 400 / 401 / 422 from the server on a
properly-formed request indicates a skill-side bug.

---

## 11. Versioning and release

- Pin SKILL.md `version` in semver. Bump major on a breaking
  user-visible change.
- Pin awp-wallet's minimum version in `metadata.openclaw.requires`.
- Track the EMG protocol version: the skill targets `EMG-SIG-V1`. If
  the server bumps to V2 (different domain `version` string), the
  skill MUST refuse to sign requests against the new domain until
  explicitly updated. This protects users from a malicious upstream
  domain swap.

---

## 12. Security checklist (every release)

- [ ] No private key ever read, written, or logged.
- [ ] Every signed write is gated by an explicit user confirmation.
- [ ] EIP-712 domain values are pulled from `GET /v1/auth/info`, not
      hardcoded — and the response is cached locally with a checksum
      so a runtime swap is observable.
- [ ] HTTPS / WSS only. No http:// or ws:// fallback.
- [ ] Nonce file uses `O_EXCL` on first create + atomic rename on
      update. Two concurrent skill invocations cannot race past a
      single nonce.
- [ ] All script outputs are valid JSON (one object or one
      JSON-Lines stream). The agent parses it directly.
- [ ] Error messages NEVER include private data (signatures, nonces,
      chips balances of OTHER principals).
- [ ] WS endpoint TLS certificate pinned to gov.works / api.gov.works
      so a compromised CA can't MitM the connection.

---

## 13. References

| Source | Relevance |
|---|---|
| `dev_docs/openapi.yaml` | REST wire contract |
| `dev_docs/asyncapi.yaml` | WebSocket wire contract |
| `dev_docs/spec/07-api.md` | Narrative API spec |
| `dev_docs/CLAUDE.md` § "Authentication: EMG-SIG-V1" | Signing scheme rationale |
| `crates/emg-auth/src/eip712.rs` | Reference EIP-712 digest implementation |
| `crates/emg-auth/src/canonical.rs` | Query-string canonicalization rules |
| `crates/emg-broadcast/src/lib.rs` | Authoritative WS event payload shapes |
| `crates/emg-api-rest/src/handlers/` | Authoritative REST handler shapes |
| `crates/emg-api-ws/src/dispatch.rs` | WS RPC method signatures (`subscribe`, `auth.hello`, `unsubscribe`) |
| `crates/emg-core/src/errors.rs` | `EmgError` variants and `code()` strings |
| `web/markets/markets.js` | Reference frontend (proven correct against the same server) |
| `awp-skill` | https://github.com/awp-core/awp-skill — composition reference |
| `awp-wallet` | https://github.com/awp-core/awp-wallet — wallet bridge |
| [EIP-712](https://eips.ethereum.org/EIPS/eip-712) | Signing scheme |
| [agentskills.io](https://agentskills.io/specification) | SKILL.md spec |

---

## 14. Implementation milestones (suggested)

| Milestone | Scope | Estimated LOC |
|---|---|---|
| **M0 — Bootstrap** | SKILL.md + README + `scripts/lib/{govnet_lib,canonical,nonce,sign,ws}.py` + `scripts/public/auth-info.py` + `scripts/public/markets.py` | ~600 |
| **M1 — Read** | Every public + private GET endpoint mapped. Phase-aware "what can I do" helper. | ~800 |
| **M2 — Trade** | Signed POST `/orders`, DELETE `/orders/{id}`, cancel-all, cancel-batch. Confirmation prompts. Pre-flight checks. | ~700 |
| **M3 — Vote** | `EMGVote` typed-data signing, `POST /epochs/{id}/votes`, proof verification. | ~400 |
| **M4 — Positions** | Split / merge. | ~250 |
| **M5 — Stream** | All 5 WS channels, including `auth.hello` for `fills.me` / `orders.me`. | ~500 |
| **M6 — Content** | Comments, weekly reports, endorsements. | ~300 |
| **M7 — Hardening** | Rate-limit handling, retries, conformance tests, security checklist. | ~400 |
| **Total** | | **~3,950 LOC** |

A careful implementation should land M0–M2 in a few sessions, with
the rest as follow-ups.

---

## 15. Worked example — a complete `submit-order.py`

```python
#!/usr/bin/env python3
"""
scripts/trade/submit-order.py

Usage:
  submit-order.py --market 6 --worknet 11 --side buy --kind limit \
                  --price 0.2200 --quantity 100 \
                  [--tif gtc] [--post-only] [--reduce-only] \
                  [--stp cancel_both] [--idem-key <uuid>] [--yes]
"""
import sys, json, time, uuid, argparse, os, urllib.request, urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from govnet_lib import API, fmt_dec, fetch, fmt_phase  # noqa
from sign import sign_emg_request                       # noqa
from nonce import next_nonce                            # noqa

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", type=int, required=True)
    ap.add_argument("--worknet", type=int, required=True)
    ap.add_argument("--side", choices=("buy", "sell"), required=True)
    ap.add_argument("--kind", choices=("limit", "market"), required=True)
    ap.add_argument("--price", type=str)            # required if kind=limit
    ap.add_argument("--quantity", type=str, required=True)
    ap.add_argument("--tif", default="gtc",
                    choices=("gtc", "ioc", "fok", "gtt"))
    ap.add_argument("--post-only", action="store_true")
    ap.add_argument("--reduce-only", action="store_true")
    ap.add_argument("--stp", default="cancel_both",
                    choices=("cancel_taker", "cancel_maker",
                             "cancel_both", "decrement_taker"))
    ap.add_argument("--idem-key", default=str(uuid.uuid4()))
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    # 1. Bootstrap chain id + verifying contract
    auth_info = fetch("GET", "/v1/auth/info")
    principal = json.loads(
        os.popen("awp-wallet receive --json").read()
    )["address"]

    # 2. Phase pre-flight
    market = fetch("GET", f"/v1/markets/{args.market}")
    if market["status"] not in ("voting_and_trading", "trading_only"):
        print(json.dumps({
            "error": f"market {args.market} is in '{market['status']}' "
                     f"phase; cannot submit orders"
        }))
        sys.exit(1)

    # 3. Build body
    body = {
        "market_id":      args.market,
        "worknet_id":     args.worknet,
        "side":           args.side,
        "kind":           args.kind,
        "total_quantity": args.quantity,
        "time_in_force":  args.tif,
        "post_only":      args.post_only,
        "reduce_only":    args.reduce_only,
        "stp_mode":       args.stp,
    }
    if args.kind == "limit":
        if not args.price:
            print(json.dumps({"error": "limit kind requires --price"}))
            sys.exit(1)
        body["limit_price"] = args.price

    # 4. Confirm
    if not args.yes and sys.stdin.isatty():
        wn = next(w for w in market["worknets"]
                  if w["worknet_id"] == args.worknet)
        sys.stderr.write(
            f"[TX] about to submit order:\n"
            f"     market:     №{args.market}\n"
            f"     worknet:    {wn.get('name', f'id {args.worknet}')}\n"
            f"     side:       {args.side}\n"
            f"     kind:       {args.kind}"
            + (f" @ {args.price}\n" if args.kind == 'limit' else "\n") +
            f"     quantity:   {args.quantity}\n"
            f"     tif:        {args.tif}\n"
            f"     stp:        {args.stp}\n"
            f"     idem-key:   {args.idem_key}\n"
            f"     proceed? (y/n) "
        )
        if input().strip().lower() != "y":
            print(json.dumps({"cancelled": True})); sys.exit(0)

    # 5. Sign
    raw_body = json.dumps(body).encode()
    nonce = next_nonce(principal)
    timestamp = int(time.time())
    headers = sign_emg_request(
        principal=principal,
        method="POST",
        path="/orders",     # POST-strip — server's nest("/v1") strips this
        query="",
        body=raw_body,
        nonce=nonce,
        timestamp=timestamp,
        auth_info=auth_info,
    )
    headers["Content-Type"] = "application/json"
    headers["X-Idempotency-Key"] = args.idem_key

    # 6. POST
    req = urllib.request.Request(
        f"{API}/orders", data=raw_body, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(resp.read().decode())
    except urllib.error.HTTPError as e:
        problem = json.loads(e.read())
        print(json.dumps({
            "error": problem.get("code", "UNKNOWN"),
            "title": problem.get("title"),
            "detail": problem.get("detail"),
            "status": e.code,
        }))
        sys.exit(2)

if __name__ == "__main__":
    main()
```

---

## 16. Where this skill plugs in

After install, a typical Claude Code session looks like:

```
user> what's trading on govnet right now?
agent> [invokes scripts/public/markets.py]
       №6 — Voting + Trading. 7 worknets.
       Voting closes in 3h 12m. Trading closes in 4d 21h.
       Want me to show the order book for any of them?

user> show aGOV book and tell me what price 100 chips would buy
agent> [invokes scripts/public/book.py 6 11 + scripts/private/state.py]
       aGOV (id 11):    bid 0.2180 × 250    ask 0.2210 × 80
                        bid 0.2150 × 500    ask 0.2240 × 100
       You have 1,000 chips this epoch.
       100 chips @ 0.2210 ≈ 452 shares.
       Want me to draft a buy order?

user> yes, post-only at 0.2200
agent> [invokes scripts/trade/submit-order.py with confirmation prompt]
       [TX] about to submit order:
            market:     №6
            worknet:    aGOV (id 11)
            …
            proceed? (y/n)
user> y
agent> [submits]
       Order submitted. id=018f-… filled_quantity=0 status=Active
       Watching for fills … (subscribe orders.me + fills.me?)
```

The skill provides every piece of glue between natural language and the
EMG protocol. No raw HTTP request / signature ever touches the user.

---

*— end of GovNet Skill Development Guide —*
