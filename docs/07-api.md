# EMG SPEC — API (REST / WebSocket / GraphQL)

## 9 API

EMG exposes two transport layers:

- **REST over HTTPS** for single-shot operations (queries, submissions, cancellations). Spec: `api/openapi.yaml`.
- **WebSocket carrying JSON-RPC 2.0** for real-time subscriptions and batch operations. Spec: `api/asyncapi.yaml`.

The two layers share the same authentication mechanism (§9.3 EMG-SIG-V1) and the same error model (§9.5).

### 9.0 Read-path consistency model (ADR-013)

The matching engine's in-memory state is authoritative during an Epoch (ADR-012); Postgres and Redis are asynchronous downstream projections. This is the architecture used by every major trading venue (Nasdaq, Binance, Hyperliquid). It means **different read paths give different freshness guarantees** — clients should choose the path that matches their needs.

**Three tiers**:

| Tier | Transport | Path | Freshness | Use case |
|---|---|---|---|---|
| **1: Real-time** | WebSocket subscribe | Matcher → push (ADR-012) | <10ms | Trading UI, market-making bots |
| **2: Near real-time** | REST | Redis → PG fallback | <100ms (hit) / seconds (miss-fallback) | Mobile, ad-hoc queries, page loads |
| **3: Analytic** | GraphQL | Postgres (read replica) | 1-5s typical, up to 30s worst case | Leaderboards, reports, history, admin |

**Per-endpoint mapping**:

| Endpoint / Subscription | Tier | Staleness source |
|---|---|---|
| WS `subscribe book.{id}` | 1 | Pushed by matcher on every state change, 50ms delta batching |
| WS `subscribe fills.me` | 1 | Pushed by matcher immediately after WAL fsync |
| WS `subscribe account` | 1 | Pushed by matcher immediately after state change |
| WS `subscribe klines.{id}.{interval}` | 1 | Matcher pushes latest bar; backfill from Redis cache |
| WS `subscribe phase` | 1 | State-machine pushes on transition |
| REST `GET /v1/markets/{market_id}/worknets/{worknet_id}/book` | 2 | Redis `book:{m}:{wn}:top20`, refreshed every 100ms by matcher |
| REST `GET /v1/markets/{market_id}/worknets/{worknet_id}/klines` | 2 | Redis read-through to PG continuous aggregate |
| REST `GET /v1/principals/{id}/state` | 2 | Redis `pstate:{e}:{p}`, debounced 50ms by matcher |
| REST `GET /v1/orders` (own) | 2 | Redis `orders:{p}:active` index |
| REST `POST /v1/orders`, `DELETE /v1/orders/{id}` | 1 | Matcher processes synchronously; returns ACK after WAL fsync |
| REST `POST /v1/positions/{split,merge}` | 1 | Same as orders — WAL-backed ACK |
| REST `POST /v1/epochs/{e}/votes` | 1 | Matcher/state-machine processes synchronously |
| REST `GET /v1/epochs/current` | 2 | Redis `epoch:current` |
| REST `GET /v1/epochs/{id}/results` | 3 | Postgres `epoch_results` (post-settlement only) |
| REST `GET /v1/epochs/{id}/votes` | 3 | Postgres `votes` (post-Phase 1 reveal) |
| REST `GET /v1/leaderboard/*` | 3 | Postgres `principal_epoch_result` or `epistemic_scores` |
| GraphQL queries | 3 | Postgres read replica |

**Response headers for transparency**:

All REST responses include headers that let clients reason about freshness:

```
X-Read-Tier:       2                        # 1, 2, or 3
X-Cache-Status:    hit | miss | miss-fallback
X-State-Timestamp: 2026-04-22T14:30:15.234Z # the as-of moment for this data
X-Consistency:     eventual | strong
```

`X-Consistency: strong` is set only for endpoints that always route to matcher memory directly (writes, and admin read endpoints that bypass cache). All other endpoints are `eventual` — which is the default for a correctly-designed exchange API.

**Clients that need strong consistency for a `GET`**:

If a client has just sent a write (e.g., `POST /v1/orders`) and needs to see its effect in a subsequent read, **use the WebSocket `account` subscription instead of polling REST**. The matcher will push the state change immediately after the WAL ack for the write, guaranteeing the client sees the updated state within milliseconds. Polling REST may see stale Redis data for up to the debounce window (50ms).

**Failure modes**:

- Redis unavailable → tier-2 REST falls back to PG with `X-Cache-Status: miss-fallback`. Latency degrades (5-50ms instead of <5ms) but correctness is preserved. Client behaviour unchanged.
- Matcher → Redis debouncer falling behind → `X-State-Timestamp` reveals the staleness. Monitoring alert (`redis_staleness_seconds > 5`) pages the operator. Clients can opt to switch to WS for stricter freshness during the incident.
- Matcher crash → WebSocket subscriptions drop; clients reconnect and re-subscribe. Matcher replays WAL, state is recovered. During the ~30s recovery window, REST tier-2 continues serving (PG data is from last flush, seconds-stale but consistent).

### 9.1 REST endpoints

REST is used for resource-oriented operations. Public endpoints are unauthenticated and cacheable; private endpoints require EMG-SIG-V1.

**Public (no authentication)**:

```
GET    /v1/markets/{market_id}/worknets/{worknet_id}/book      Order book snapshot
GET    /v1/markets/{market_id}/worknets/{worknet_id}/klines    K-line data
GET    /v1/epochs/current                          Current epoch info
GET    /v1/epochs/{id}                             Epoch info
GET    /v1/epochs/{id}/results                     Settlement results (post-settlement)
GET    /v1/epochs/{id}/votes                       Revealed votes (post-settlement)
GET    /v1/epochs/{id}/voters                      Participating principals (any phase)
GET    /v1/epochs/{id}/merkle-root                 Merkle root (post-Phase 1)
GET    /v1/epochs/{id}/votes/{principal}/proof         Merkle inclusion proof
GET    /v1/epochs/{id}/votes/{principal}/history       Submission history (post-settlement)
GET    /v1/epochs/{id}/phase                       Phase info
GET    /v1/leaderboard/epistemic                   Top Principals by E_S
GET    /v1/reports                                 List weekly reports
GET    /v1/comments                                List comments
GET    /v1/auth/info                               Signing scheme info + server time
```

**Authenticated (EMG-SIG-V1 required)**:

```
POST   /v1/epochs/{epoch}/votes                    Submit signed vote
POST   /v1/orders                                  Submit order (auto-routed)
DELETE /v1/orders/{order_id}                       Cancel order
GET    /v1/orders                                  Query principal's own orders
POST   /v1/positions/split                         Split chips → shares
POST   /v1/positions/merge                         Merge shares → chips
GET    /v1/principals/{principal}/state                 Principal epoch state (self only)
POST   /v1/reports                                 Submit weekly report (WorkNet operator)
POST   /v1/comments                                Publish comment
POST   /v1/comments/{id}/endorse                   Mark comment helpful
```

Public endpoints may be served from Redis cache or CDN. Authenticated endpoints always go through the signature middleware.

### 9.2 WebSocket + JSON-RPC 2.0

The WebSocket endpoint carries JSON-RPC 2.0 requests, responses, and notifications. It handles two classes of operations:

1. **Subscriptions** — long-running streams for real-time data (order book deltas, K-line updates, phase transitions).
2. **Batch operations** — atomic multi-action requests (e.g. submit 5 orders across different WorkNets in one message).

Connection URL: `wss://api.emg.awp.network/v1/ws`

On connect, the client **must send an authenticated handshake** within 10 seconds or the server closes the connection:

```json
{
  "jsonrpc": "2.0",
  "method": "auth.hello",
  "params": {
    "principal": "0x742d35cc6634c0532925a3b844bc9e7595f0beb7",
    "timestamp": 1745323200,
    "nonce": 42,
    "signature": "0x..."
  },
  "id": "hs-1"
}
```

The `params` are signed using the same EMG-SIG-V1 scheme as REST, with the canonical request method being `WS_HELLO` and path `/v1/ws`. After handshake, the connection is bound to that principal for the session.

**Subscriptions**:

```json
// Client → Server
{
  "jsonrpc": "2.0",
  "method": "subscribe",
  "params": { "channels": ["book.1", "book.2", "klines.1.1m", "fills.me", "phase"] },
  "id": "sub-1"
}

// Server → Client (ack)
{
  "jsonrpc": "2.0",
  "result": { "subscribed": ["book.1", "book.2", "klines.1.1m", "fills.me", "phase"] },
  "id": "sub-1"
}

// Server → Client (notifications — no id)
{
  "jsonrpc": "2.0",
  "method": "book.update",
  "params": {
    "channel": "book.1",
    "worknet_id": 1,
    "timestamp": "2026-04-22T12:00:00.123Z",
    "bids": [{"price": "0.50", "quantity": "10"}],
    "asks": [{"price": "0.51", "quantity": "5"}],
    "sequence": 12345
  }
}
```

**Channel list**:

| Channel | Authentication | Description |
|---|---|---|
| `book.{market_id}.{worknet_id}` | Public | Order book delta updates (changes-only, with absolute new_quantity per level) |
| `klines.{market_id}.{worknet_id}.{interval}` | Public | OHLCV updates for interval |
| `fills.me` | Required | Fills where the authed principal is party |
| `fills.{principal}` | Required; authed principal must be {principal} | Same as above, explicit variant |
| `phase` | Public | Epoch phase transitions |
| `reports` | Public | New weekly reports |
| `comments` | Public | New comments (with bridge score updates) |
| `orders.me` | Required | Own order status changes (created, partial fill, filled, cancelled) |

`fills.me` and `orders.me` require a completed `auth.hello` — unauthenticated connections that subscribe to them get a JSON-RPC error.

**Batch operations**:

```json
// Client → Server
[
  {
    "jsonrpc": "2.0",
    "method": "orders.submit",
    "params": { "worknet_id": 1, "side": "buy", "quantity": "10", "limit_price": "0.5" },
    "id": "b1"
  },
  {
    "jsonrpc": "2.0",
    "method": "orders.submit",
    "params": { "worknet_id": 2, "side": "sell", "quantity": "10", "limit_price": "0.3" },
    "id": "b2"
  },
  {
    "jsonrpc": "2.0",
    "method": "positions.split",
    "params": { "quantity": "100" },
    "id": "b3"
  }
]

// Server → Client (array of results, same ids)
[
  { "jsonrpc": "2.0", "result": { "order_id": "..." }, "id": "b1" },
  { "jsonrpc": "2.0", "result": { "order_id": "..." }, "id": "b2" },
  { "jsonrpc": "2.0", "result": { "agent_state": {...} }, "id": "b3" }
]
```

**Atomicity**: by default, batch items execute independently — some may succeed while others fail. For atomic semantics, set `atomic: true` in a special `batch.config` item at the front of the array; if any item fails, all are rolled back. Atomic batches are required for router synthesis operations (Split + Sell) from client-orchestrated paths; most clients use the REST `/v1/orders` endpoint and let the server's router handle atomicity internally.

**Unified method registry**: WebSocket JSON-RPC methods mirror the REST endpoints where applicable:

| JSON-RPC method | REST equivalent |
|---|---|
| `orders.submit` | `POST /v1/orders` |
| `orders.cancel` | `DELETE /v1/orders/{id}` |
| `orders.query` | `GET /v1/orders` |
| `positions.split` | `POST /v1/positions/split` |
| `positions.merge` | `POST /v1/positions/merge` |
| `votes.submit` | `POST /v1/epochs/{e}/votes` |
| `principal.state` | `GET /v1/principals/{id}/state` |
| `market.book` | `GET /v1/markets/{m}/worknets/{wn}/book` |
| `market.klines` | `GET /v1/markets/{m}/worknets/{wn}/klines` |
| `subscribe` | (WS only) |
| `unsubscribe` | (WS only) |
| `auth.hello` | (WS only) |

Clients can use either transport as convenient — the semantics are identical.

### 9.3 Authentication: EMG-SIG-V1 (EIP-712 compatible)

EMG-SIG-V1 is implemented as an **EIP-712 typed data signature**. This makes EMG-signed requests natively compatible with every Ethereum wallet (MetaMask, WalletConnect, Rabby, Safe, hardware wallets). Stakers can authenticate using their existing on-chain identity without managing separate Ed25519 keys.

The signing primitive is `secp256k1 ECDSA` over a Keccak-256 hash of a structured message — exactly what EIP-712 defines. See [EIP-712](https://eips.ethereum.org/EIPS/eip-712) for the underlying standard.

#### 9.3.1 Headers / handshake params

| Field | Header name (REST) | Format |
|---|---|---|
| Principal | `X-EMG-Principal` | `0x`-prefixed hex, 20 bytes (Staker's Ethereum address) |
| Actor | `X-EMG-Actor` | `0x`-prefixed hex, 20 bytes. Optional — defaults to `X-EMG-Principal` if omitted, meaning the Staker is operating on their own account |
| Nonce | `X-EMG-Nonce` | unsigned decimal integer, strictly monotonic per principal |
| Timestamp | `X-EMG-Timestamp` | unsigned decimal integer, Unix seconds (UTC) |
| Signature | `X-EMG-Signature` | `0x`-prefixed hex, 65 bytes (r || s || v). Signed by `actor`'s key. |

For WebSocket `auth.hello`, these fields are `params` keys.

**Identity recovery flow**:

1. Server computes EIP-712 digest from the request (see §9.3.3).
2. Server recovers `signer_address = ecrecover(digest, signature)`.
3. Server requires `signer_address == X-EMG-Actor`. If different, reject with `ACTOR_MISMATCH`.
4. If `actor == principal`, authorization is automatic (Staker operating on own account).
5. Else, server queries `AWPRegistry.delegates(principal, actor) → bool` (via emg-chain; result cached 30s). If `false`, reject with `UNAUTHORIZED_DELEGATE`.

The server does **not** need a separate public key field — `ecrecover` gives both identity verification and signature validity in one step.

**Nonce scope**: nonces are tracked **per principal, not per actor**. If Staker S has two Managers M1 and M2 both wanting to act, they share S's single nonce counter — client-side coordination is required to avoid collisions. The server enforces strict monotonicity and rejects out-of-order nonces.

#### 9.3.2 EIP-712 domain

```solidity
EIP712Domain {
  string  name             // "EMG"
  string  version          // "1"
  uint256 chainId          // e.g., 56 for BSC mainnet, 97 for BSC testnet
  address verifyingContract // On-chain EMGAuth contract address (also serves as domain separator)
}
```

`chainId` and `verifyingContract` are configured per deployment. Production uses `{chainId: 56, verifyingContract: 0x...}`; staging uses a separate contract address on testnet. This makes staging signatures **cryptographically unable** to be replayed on production — the `domainSeparator` hash differs.

#### 9.3.3 Typed data schema

There is one top-level type — `EMGRequest` — parameterized over the HTTP method, path, and **principal**:

```solidity
EMGRequest {
  address principal       // The Staker being acted upon (whether actor==principal or a Manager)
  string  method          // "GET", "POST", "DELETE", or "WS_HELLO"
  string  path            // Full URL path, e.g., "/v1/orders"
  string  query           // Canonicalized query string (sorted by key), empty if none
  bytes32 bodyHash        // Keccak-256 of request body; 0x00...00 if empty
  uint256 nonce           // Must be strictly greater than principal's previous nonce
  uint256 timestamp       // Unix seconds; ±30s of server time
}
```

Including `principal` in the signed typed data prevents a Manager from reusing one signature to act against multiple different Stakers. Each signature is cryptographically pinned to a specific principal.

```
digest = keccak256(
  ""
  || domainSeparator
  || keccak256(abi.encode(
       EMG_REQUEST_TYPEHASH,
       principal,
       keccak256(bytes(method)),
       keccak256(bytes(path)),
       keccak256(bytes(query)),
       bodyHash,
       nonce,
       timestamp
     ))
)
```

Where:
- `EMG_REQUEST_TYPEHASH = keccak256("EMGRequest(address principal,string method,string path,string query,bytes32 bodyHash,uint256 nonce,uint256 timestamp)")`
- `domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, nameHash, versionHash, chainId, verifyingContract))`

The actor signs `digest` with its secp256k1 key; the server runs `ecrecover(digest, v, r, s)` to recover the signer address, compares it to `X-EMG-Actor`, then checks the delegate relationship if needed.

**Why `principal` is in the signed struct but `actor` is not.** The typed data binds a signature to one specific Staker (`principal`) so that a Manager signing on behalf of Alice cannot have that same signature reused against Bob — different `principal` values produce different digests. The `actor` does not need to be in the typed data because the signature itself already proves actor identity: `ecrecover(digest, signature)` yields exactly one address, and the server enforces `recovered == X-EMG-Actor`. Adding `actor` to the signed struct would be redundant and would force a 65-byte signature to re-encode a fact already implicit in it. This is the standard EIP-712 pattern for delegated authentication.

#### 9.3.4 Canonicalization rules

These rules must be followed exactly — any byte difference between client construction and server reconstruction causes signature mismatch.

**`method`**: ASCII uppercase. `POST`, `GET`, `DELETE`, or `WS_HELLO` for WebSocket handshake.

**`path`**: URL path, percent-decoded then re-encoded per RFC 3986. Examples:
- `/v1/orders` → `/v1/orders`
- `/v1/epochs/5/votes` → `/v1/epochs/5/votes`
- `/v1/principals/0x123…/state` → unchanged (addresses remain as-is)

**`query`**: Empty string `""` if no query. Otherwise:
1. Parse into key-value pairs
2. URL-encode each key and value per RFC 3986 component encoding (lower-case hex)
3. Sort pairs by key, then by value for duplicate keys
4. Join with `&`

Example: `?epoch=5&principal=0xabc` → `principal=0xabc&epoch=5`

**`bodyHash`**:
- Empty body: `0x0000...0000` (32 zero bytes)
- Non-empty body: `keccak256(body_bytes)`

**`nonce`**: Unsigned 64-bit integer represented as `uint256`. Strictly greater than the highest nonce previously submitted by this `(principal, chainId, verifyingContract)` tuple. No random component — the monotonic guarantee is sufficient replay protection.

**`timestamp`**: Unix seconds as `uint256`. Server accepts values within `[now - 30, now + 30]`.

#### 9.3.5 Server verification flow

The auth layer exposes a single function contract used by every authenticated endpoint. The EIP-712 machinery is expressed precisely (since digest construction is byte-canonical) but the procedural glue is described as an algorithm.

```rust
use alloy_primitives::{Address, B256};
use alloy_sol_types::{sol, SolStruct, Eip712Domain};

sol! {
    /// EIP-712 typed data struct. Field order and names are canonical and
    /// MUST match the TYPEHASH constant in §9.3.3.
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

pub struct AuthConfig {
    pub chain_id: u64,
    pub verifying_contract: Address,
    pub max_timestamp_skew_secs: i64,  // 30
}

impl AuthConfig {
    pub fn domain(&self) -> Eip712Domain { /* standard EIP-712 domain */ }
}

/// Contract for authenticating an inbound request.
///
/// Returns a `RequestContext` on success, containing the verified
/// (principal, actor) pair plus the current epoch.
pub async fn verify_request<B: Body>(
    req: &HttpRequest<B>,
    config: &AuthConfig,
    nonce_store: &NonceStore,
    delegate_check: &dyn DelegateCheck,
) -> Result<RequestContext, AuthError>;
```

**Algorithm**:

1. **Parse headers**: extract `X-EMG-Principal` (required), `X-EMG-Actor` (optional; defaults to principal), `X-EMG-Nonce`, `X-EMG-Timestamp`, `X-EMG-Signature`.
2. **Timestamp check**: `|now - timestamp| ≤ 30` seconds. Reject `TIMESTAMP_OUT_OF_WINDOW` on failure.
3. **Nonce CAS**: atomically ensure `nonce > stored_max_nonce(principal)` in Redis; update on success. Reject `NONCE_TOO_LOW` or `NONCE_CONFLICT` on failure. **Key is principal, not actor** — the nonce counter is shared across all Actors of a principal.
4. **Body hash**: if body is empty, `bodyHash = B256::ZERO`; else `bodyHash = keccak256(body)`.
5. **Reconstruct EMGRequest** with parsed fields and canonicalized query (§9.3.4).
6. **Compute digest**: `digest = EMGRequest::eip712_signing_hash(&domain)`.
7. **Recover signer**: `recovered = ecrecover(digest, signature)`.
8. **Actor check**: `recovered == actor`? Else reject `ACTOR_MISMATCH`.
9. **Authorization check**:
   - If `actor == principal`: authorized (Staker self-operation).
   - Else: call `delegate_check.is_delegate(principal, actor)`. If false, reject `UNAUTHORIZED_DELEGATE`.
10. **Return** `RequestContext { principal, actor, epoch: current_epoch() }`.

**Invariants**:

- Step 3 must happen before step 6 so replay attempts don't consume CPU cycles on ecrecover.
- Steps 2, 7, 8 together protect against tampered request bodies — any byte mismatch produces a different digest, hence a different recovered address, hence step 8 fails.
- Step 9's delegate check uses cached state with a 30s TTL (see §10.4). A Manager revoked >30s ago cannot pass step 9.

```

#### 9.3.6 Client reference (browser / wallet)

Browser clients with MetaMask or similar wallets use `eth_signTypedData_v4`:

```javascript
// JavaScript client. `principal` = the Staker whose account is being operated
// on; `actorAddress` = the wallet actually signing (could be the Staker
// themselves, or one of their Managers). For Staker self-operation, pass
// principal === actorAddress.
async function signedFetch(method, url, body = null, principal, actorAddress) {
  const bodyBytes = body ? new TextEncoder().encode(JSON.stringify(body)) : new Uint8Array();
  const bodyHash = body ? "0x" + ethers.keccak256(bodyBytes).slice(2) : "0x" + "00".repeat(32);
  
  const urlObj = new URL(url);
  const nonce = await getNextNonce(principal);  // counter keyed by principal
  const timestamp = Math.floor(Date.now() / 1000);
  
  const typedData = {
    types: {
      EIP712Domain: [
        { name: "name", type: "string" },
        { name: "version", type: "string" },
        { name: "chainId", type: "uint256" },
        { name: "verifyingContract", type: "address" },
      ],
      EMGRequest: [
        { name: "principal", type: "address" },
        { name: "method", type: "string" },
        { name: "path", type: "string" },
        { name: "query", type: "string" },
        { name: "bodyHash", type: "bytes32" },
        { name: "nonce", type: "uint256" },
        { name: "timestamp", type: "uint256" },
      ],
    },
    domain: EMG_DOMAIN,
    primaryType: "EMGRequest",
    message: {
      principal: principal,
      method: method,
      path: urlObj.pathname,
      query: canonicalizeQuery(urlObj.search),
      bodyHash: bodyHash,
      nonce: nonce,
      timestamp: timestamp,
    },
  };
  
  const signature = await ethereum.request({
    method: "eth_signTypedData_v4",
    params: [actorAddress, JSON.stringify(typedData)],
  });
  
  const headers = {
    "Content-Type": "application/json",
    "X-EMG-Principal": principal,
    "X-EMG-Nonce": String(nonce),
    "X-EMG-Timestamp": String(timestamp),
    "X-EMG-Signature": signature,
  };
  if (actorAddress.toLowerCase() !== principal.toLowerCase()) {
    headers["X-EMG-Actor"] = actorAddress;
  }
  
  return fetch(url, {
    method: method,
    headers: headers,
    body: body ? JSON.stringify(body) : null,
  });
}
```

For bot clients without a wallet UI, a `k256`-based signer works identically:

```rust
use k256::ecdsa::{SigningKey, Signature};
use alloy_signer_local::PrivateKeySigner;

let signer = PrivateKeySigner::from_str(&secret_key_hex)?;

let request = EMGRequest { /* ... */ };
let domain = config.domain();
let digest = request.eip712_signing_hash(&domain);
let signature = signer.sign_hash(&digest).await?;

// Ship via HTTP headers...
```

#### 9.3.7 Nonce store (Redis CAS)

```rust
async fn check_and_update(
    &self,
    principal: Principal,
    new_nonce: u64,
) -> Result<()> {
    let key = format!("emg:nonce:{:?}", principal);
    
    let script = r#"
        local stored = tonumber(redis.call('GET', KEYS[1]) or '0')
        local new = tonumber(ARGV[1])
        if new > stored then
            redis.call('SET', KEYS[1], new, 'EX', 604800)  -- 7-day expiry
            return 1
        else
            return 0
        end
    "#;
    
    let ok: i32 = self.redis.eval(script)
        .keys(&[&key]).args(&[new_nonce])
        .await?;
    
    if ok != 1 {
        return Err(AuthError::NonceNotMonotonic);
    }
    Ok(())
}
```

**Memory footprint**: one u64 per principal — 8 bytes plus Redis key overhead (~60 bytes). 100K principals: ~6.5 MB. 7-day TTL cleans up inactive principals automatically (they'll start fresh on return, client initializes counter with `floor(unix_seconds * 1000)` to guarantee monotonic continuation).

#### 9.3.8 Why EIP-712 over a custom scheme

- **Wallet-native**: every major wallet supports `eth_signTypedData_v4`. No custom extensions needed.
- **Audited primitives**: ECDSA over secp256k1 + Keccak-256 is the most-audited crypto stack in Web3.
- **Chain-bound**: `chainId` in the domain separator prevents cross-chain replay.
- **Environment-bound**: different `verifyingContract` addresses separate production/staging signatures.
- **Future-proof**: the same scheme can be used for signing EMG on-chain operations (e.g., withdraw commands) without introducing a second auth system.
- **Readable by wallets**: when users sign an EMG request, their wallet shows a human-readable prompt with method, path, and timestamp fields — better UX than a raw hash.

#### 9.3.9 Bots without wallets; smart-contract wallets

For principals that need to run without an Ethereum wallet UI (e.g., backend bots), two options:

1. **Dedicated EOA**: generate an Ethereum key pair, use it as above with `k256` or `alloy-signer-local`. Recommended path — no code difference from wallet-using principals.
2. **Smart contract wallet with EIP-1271**: for organizations wanting multi-sig control over their EMG principal, a Safe (or similar) contract wallet with `isValidSignature(hash, sig) → bytes4` can be used. The server calls this view function on-chain when the recovered address is a contract. This is optional v2 support.

### 9.4 Idempotency (ADR-014 §8)

Every authenticated mutating request (`POST`, `DELETE`) may include an `X-Idempotency-Key` header for safe retry semantics. The server caches the full response (status, headers, body) keyed by `(principal, idempotency_key)` for 24 hours and returns the cached response on subsequent requests with the same key.

```http
POST /v1/orders
X-EMG-Principal: 0x742d35cc6634c0532925a3b844bc9e7595f0beb7
X-EMG-Signature: 0x...
X-EMG-Timestamp: 1745323200
X-EMG-Nonce: 42
X-Idempotency-Key: 01939c5a-8a70-7c00-abc0-1234567890ab     # UUID v7 recommended

{"worknet_id":1,"side":"buy",...}
```

**Semantics** (Stripe-style):

1. Server computes `body_hash = SHA-256(canonical_body_bytes)`.
2. Looks up `(principal, key)` in Redis (`idem:{principal}:{key}`).
3. **Miss** — executes the request, caches `{status, headers, body, body_hash, created_at}` with 24h TTL, returns response.
4. **Hit, body_hash matches** — returns cached response (same status code, same body). Request is NOT re-executed.
5. **Hit, body_hash differs** — returns `422 Unprocessable Entity` with problem type `idempotency-key-reuse` and code `IDEMPOTENCY_KEY_REUSE`:
   ```jsonc
   {
     "type":     "https://emg.awp.network/problems/idempotency-key-reuse",
     "title":    "Idempotency-Key reuse with different payload",
     "status":   422,
     "code":     "IDEMPOTENCY_KEY_REUSE",
     "detail":   "Key '01939c5a-...' was previously used with a different request body",
     "original_submitted_at": "2026-04-22T14:20:00Z"
   }
   ```
6. **Concurrent retry** (second request arrives while first is still executing) — second blocks on a Redis lock (`idem:lock:{principal}:{key}`, short TTL); when the first commits its result, the second returns it immediately.

**Rules**:

- Key is per-principal; two principals can use the same string without collision (scope is `(principal, key)`).
- Max key length: 255 bytes.
- Only mutating methods (POST, DELETE) check idempotency. `X-Idempotency-Key` on GET is silently ignored.
- `GET` requests and the entire WebSocket channel are excluded.
- Key is an opaque string — server makes no assumption about format, but UUIDv7 is strongly recommended (time-ordered, naturally unique).

**Recovery**: if the client sends a request, the server commits (matcher WAL fsync), the response is lost in transit, and the client retries with the same key — the retry returns the cached response. The order is NOT submitted twice.

Implementation: Redis `idem:{principal_hex}:{key}` → JSON envelope, 24h TTL. See `adr/ADR-014` §8 for the full spec.

### 9.5 Error model (problem+json, RFC 7807)

All `4xx` responses use `Content-Type: application/problem+json` per RFC 7807 and ADR-014 §3.

```http
HTTP/1.1 422 Unprocessable Entity
Content-Type: application/problem+json

{
  "type":      "https://emg.awp.network/problems/insufficient-balance",
  "title":     "Insufficient available chips",
  "status":    422,
  "code":      "INSUFFICIENT_BALANCE",
  "detail":    "Requested 10.0 chips but only 7.5 available (3.0 locked in orders)",
  "instance":  "/v1/orders",
  "required":  "10.0",
  "available": "7.5",
  "locked":    "3.0",
  "request_id": "req-01939c5a-8a70-7c00",
  "timestamp":  "2026-04-22T12:00:00Z"
}
```

The envelope has five stable top-level fields (per RFC 7807 + ADR-014):

| Field | Purpose |
|---|---|
| `type`     | Canonical problem-type URL. Stable across versions; safe to dispatch on. |
| `title`    | Human-readable summary (stable). |
| `status`   | HTTP status code. |
| `code`     | Machine-readable stable code from the ADR-006 codebook. |
| `detail`   | Instance-specific details (human-readable, may change). |

Endpoints are free to add problem-specific fields (like `required`, `available`, `locked` above). Clients parse via `code` or `type`, not status alone.

**HTTP status code mapping** (ADR-014 §3):

| Status | Category | Example codes |
|---|---|---|
| `400` | Malformed request | `MALFORMED_JSON`, `INVALID_VOTE_VECTOR`, `BAD_CURSOR`, `DEPTH_OUT_OF_RANGE` |
| `401` | Authentication failure | `MISSING_SIGNATURE`, `SIGNATURE_INVALID`, `TIMESTAMP_OUT_OF_WINDOW`, `NONCE_REPLAY` |
| `403` | Authorization failure | `NOT_WORKNET_OPERATOR`, `DELEGATE_NOT_AUTHORIZED`, `VOTES_NOT_REVEALED` |
| `404` | Resource not found | `ORDER_NOT_FOUND`, `EPOCH_NOT_FOUND`, `PRINCIPAL_UNKNOWN` |
| `409` | State conflict | `POST_ONLY_WOULD_CROSS`, `STP_CANCELLED`, `PHASE_MISMATCH`, `NONCE_NOT_MONOTONIC` |
| `422` | Semantic validation failure | `INSUFFICIENT_BALANCE`, `TICK_SIZE_MISMATCH`, `MIN_NOTIONAL_BELOW`, `WORKNET_RETIRED`, `IDEMPOTENCY_KEY_REUSE`, `SIMPLEX_CONSTRAINT_VIOLATED` |
| `429` | Rate limited | `RATE_LIMIT_EXCEEDED` (with `Retry-After` header + `X-RateLimit-*` headers) |
| `500` | Server error | `INTERNAL` (opaque; refer to `request_id`) |
| `503` | Service temporarily unavailable | `DATABASE_UNAVAILABLE`, `SETTLEMENT_IN_PROGRESS`, `WAL_DISK_FULL` |

Note that `409` no longer mixes "insufficient balance" with true conflicts — balance issues are `422`. See ADR-006 for the full codebook and ADR-014 §3 for rationale.

**JSON-RPC mapping**: WebSocket JSON-RPC errors use the standard `{code, message, data}` envelope. Code ranges:

- `-32700` to `-32000`: reserved by JSON-RPC spec (parse error, invalid request, method not found, etc.)
- `-40000` to `-49999`: EMG domain errors, mapping 1:1 with REST error codes. E.g., `-40001 = INSUFFICIENT_BALANCE`.

#### 9.5.1 Canonical error code dictionary

All server error emissions MUST use one of these codes. Adding a new code requires updating this table, the downstream `emg-core::errors::EmgError` enum, and at least one test that asserts the code is emitted under the documented trigger. See ADR-006 for the governance rationale.

**AUTH_\* — authentication, signature, delegate**

| Code | HTTP | Trigger | `details` shape |
|---|---:|---|---|
| `AUTH_MISSING_HEADER` | 401 | Required `X-EMG-*` header absent | `{"header": "<name>"}` |
| `AUTH_MALFORMED_SIGNATURE` | 401 | `X-EMG-Signature` is not 65-byte `0x`-prefixed hex | `{}` |
| `AUTH_SIGNATURE_INVALID` | 401 | `ecrecover` fails on the EIP-712 digest | `{}` |
| `AUTH_ACTOR_MISMATCH` | 401 | `ecrecover(signature) != X-EMG-Actor` | `{"claimed": "0x...", "recovered": "0x..."}` |
| `AUTH_UNAUTHORIZED_DELEGATE` | 401 | `actor != principal` and `AWPRegistry.delegates(principal, actor) == false` | `{"principal": "0x...", "actor": "0x..."}` |
| `AUTH_TIMESTAMP_OUT_OF_WINDOW` | 401 | `|now - timestamp| > 30s` | `{"skew_seconds": 42}` |
| `AUTH_EIP712_DOMAIN_MISMATCH` | 401 | Signed with wrong `chainId` / `verifyingContract` (e.g., staging sig on production) | `{"expected_chain_id": 56}` |
| `AUTH_SESSION_REQUIRED` | 401 | WebSocket signed-RPC method invoked on a connection that has not completed an `auth.hello` handshake. WS-only — REST uses per-request EMG-SIG-V1 headers and surfaces `AUTH_MISSING_HEADER` for the equivalent failure. | `{}` |

**VALIDATION_\* — malformed input**

| Code | HTTP | Trigger | `details` shape |
|---|---:|---|---|
| `VALIDATION_MALFORMED_JSON` | 400 | Request body is not valid JSON | `{"error": "<parser message>"}` |
| `VALIDATION_INVALID_VOTE_VECTOR` | 400 | Vote or prediction vector has wrong length or non-probability values | `{"field": "vote", "reason": "<why>"}` |
| `VALIDATION_SIMPLEX_CONSTRAINT_VIOLATED` | 422 | Vote/prediction doesn't sum to 1 within `1e-9` | `{"sum": "0.99847"}` |
| `VALIDATION_INVALID_QUANTITY` | 400 | Order `quantity <= 0` or not representable as `Decimal` | `{"value": "<as string>"}` |
| `VALIDATION_INVALID_PRICE` | 400 | Limit price not in `(0, 1)` or not a `Decimal` | `{"value": "<as string>"}` |
| `VALIDATION_UNKNOWN_WORKNET` | 400 | `worknet_id` not in current epoch's WorkNet set | `{"worknet_id": 99}` |
| `VALIDATION_UNKNOWN_ORDER_TYPE` | 400 | Unrecognized `OrderKind` variant | `{"received": "<json>"}` |
| `VALIDATION_UNKNOWN_TIME_IN_FORCE` | 400 | Unrecognized TIF | `{"received": "<string>"}` |

**NONCE_\* — replay protection**

| Code | HTTP | Trigger | `details` shape |
|---|---:|---|---|
| `NONCE_TOO_LOW` | 409 | Submitted `nonce <= max_observed_nonce_for_principal` | `{"submitted": 42, "min_acceptable": 43}` |
| `NONCE_CONFLICT` | 409 | Race-condition CAS failure in Redis nonce store (rare; retry with higher nonce) | `{}` |

**RATE_\* — throttling**

| Code | HTTP | Trigger | `details` shape |
|---|---:|---|---|
| `RATE_LIMIT_EXCEEDED` | 429 | Bucket for `(principal, endpoint_class)` drained | `{"retry_after_seconds": 1, "limit_class": "order_submit"}` |
| `RATE_LIMIT_BACKPRESSURE` | 429 | Matcher queue `try_send` returned `Full` (ADR-003) | `{"retry_after_seconds": 1, "worknet_id": 3}` |

**BUSINESS_\* — protocol-level rejections**

| Code | HTTP | Trigger | `details` shape |
|---|---:|---|---|
| `BUSINESS_PHASE_MISMATCH` | 403 | Operation not allowed in current epoch phase | `{"current_phase": "Settlement", "required_phase": "VotingAndTrading"}` |
| `BUSINESS_INSUFFICIENT_BALANCE` | 409 | Not enough `chips_available` for the requested operation | `{"required": "10.0", "available": "7.5", "locked": "3.0"}` |
| `BUSINESS_INSUFFICIENT_SHARES` | 409 | Merge or sell would require more shares than the Principal holds (net of locked) | `{"worknet_id": 3, "required": "5", "available": "3"}` |
| `BUSINESS_POSITION_LIMIT_EXCEEDED` | 409 | Would push Principal's share holding past `ω_pos` of open interest | `{"worknet_id": 3, "cap_pct": 0.20}` |
| `BUSINESS_ORDER_NOT_FOUND` | 404 | Cancel targets a non-existent or already-terminal order | `{"order_id": "..."}` |
| `BUSINESS_COMMENT_NOT_FOUND` | 404 | Endorse / fetch / delete targets a non-existent comment. Distinct from `BUSINESS_ORDER_NOT_FOUND` so clients can dispatch on `code` to tell "comment forum" misses from "matching engine" misses. | `{"comment_id": "..."}` |
| `BUSINESS_ORDER_NOT_OWNED` | 403 | Principal attempts to cancel an order belonging to another Principal | `{"order_id": "..."}` |
| `BUSINESS_NOT_WORKNET_OPERATOR` | 403 | `POST /v1/reports`: principal is not the configured operator for the target WorkNet (or the WorkNet has no `operator_principal` set — safe-default reject). | `{"worknet_id": 7, "principal": "0x..."}` |
| `BUSINESS_SELF_TRADE_REJECTED` | 409 | STP `CancelBoth` or `CancelTaker` rejected the order | `{"stp_mode": "CancelBoth", "conflicting_order_id": "..."}` |
| `BUSINESS_POST_ONLY_WOULD_CROSS` | 409 | `post_only` order would match taker-side — rejected rather than filled | `{"best_opposite_price": "0.52"}` |
| `BUSINESS_REDUCE_ONLY_WOULD_INCREASE` | 409 | `reduce_only` order would grow position rather than shrink it | `{"current_position": "10", "direction": "buy"}` |
| `BUSINESS_VOTE_ALREADY_FINAL` | 409 | Attempt to submit a vote after Phase 1 close | `{"phase_closed_at": "2026-04-23T12:00:00Z"}` |
| `BUSINESS_TRADING_ONLY_PHASE` | 403 | Attempt to submit/update vote during Phase 2 (trading only) | `{"current_phase": "TradingOnly"}` |

**STATE_\* — resource state**

| Code | HTTP | Trigger | `details` shape |
|---|---:|---|---|
| `STATE_EPOCH_NOT_FOUND` | 404 | Referenced `market_id` doesn't exist | `{"market_id": 42}` |
| `STATE_VOTES_NOT_REVEALED` | 403 | Requested vote contents during Phase 1/2 | `{"market_id": 42, "reveal_at": "2026-04-28T12:00:00Z"}` |
| `STATE_COMMIT_NOT_FOUND` | 404 | No `chain_commits` row — settlement produced results but the on-chain commit (settlement step 9) hasn't confirmed yet | `{"market_id": 42}` |
| `STATE_RESULTS_NOT_FOUND` | 404 | No `epoch_results` row — settlement pipeline (step 5) hasn't run for the epoch yet. Distinct from `STATE_COMMIT_NOT_FOUND`: this fires earlier in the pipeline. | `{"market_id": 42}` |
| `STATE_PRINCIPAL_NOT_IN_EPOCH` | 404 | Principal has zero AWP Power this epoch (no `principal_epoch_power` row) | `{"principal": "0x...", "market_id": 42}` |
| `STATE_VOTE_NOT_FOUND` | 404 | Principal didn't submit a final vote in the epoch — the `(epoch, principal)` row is absent from `votes` after the reveal gate has fired. Surfaced by `GET /epochs/{id}/votes/{principal}/proof`. Distinct from `STATE_PRINCIPAL_NOT_IN_EPOCH` (AWP Power absence) — a Staker WITH power can still skip voting. | `{"principal": "0x...", "market_id": 42}` |

> **Reserved (no current emitter)**: `BUSINESS_VOTE_ALREADY_FINAL`
> (409) was originally intended for "vote submission attempted after
> Phase 1 close." Phase 8b Turn 4a's `POST /epochs/{id}/votes` handler
> uses `BUSINESS_PHASE_MISMATCH` (403) for that case instead, because
> the openapi response table for that endpoint reserves 409
> exclusively for nonce conflicts (`NONCE_TOO_LOW`); a 409 for "phase
> closed" would collide with the nonce-conflict status space and
> mislead status-dispatching clients. `BUSINESS_VOTE_ALREADY_FINAL`
> stays in the codebook reserved for any future "vote already
> finalized for this principal in this epoch" semantics that
> genuinely fits a 409 conflict shape (e.g., a Phase 1→2 boundary
> race where the row is mid-finalization). If no such use case
> emerges by Phase 11, the variant should be deleted.
| `STATE_IDEMPOTENCY_KEY_MISMATCH` | 409 | Same `X-Idempotency-Key` reused with different payload | `{"key": "...", "previous_hash": "0x..."}` |

**CHAIN_\* — on-chain dependency failures**

| Code | HTTP | Trigger | `details` shape |
|---|---:|---|---|
| `CHAIN_API_AWP_UNAVAILABLE` | 502 | `api.awp.sh` unreachable after exponential backoff | `{"last_error": "connection refused"}` |
| `CHAIN_DELEGATE_CHECK_FAILED` | 502 | Could not verify `AWPRegistry.delegates(principal, actor)` — both api.awp.sh and RPC fallback failed | `{"principal": "0x...", "actor": "0x..."}` |
| `CHAIN_RECIPIENT_RESOLVE_FAILED` | 502 | `resolveRecipient` calls failed during settlement step 7; some Principals left in `pending_recipient` | `{"market_id": 42, "unresolved_count": 17}` |
| `CHAIN_COMMIT_FAILED` | 502 | `EMGCommitment.commitEpochResult` reverted with non-idempotent error | `{"market_id": 42, "tx_hash": "0x...", "reason": "<revert reason>"}` |
| `CHAIN_SNAPSHOT_FAILED` | 503 | Epoch-open AWP Power snapshot could not complete; epoch open held | `{"market_id": 43}` |

**INTERNAL_\* — server bugs / infrastructure**

| Code | HTTP | Trigger | `details` shape |
|---|---:|---|---|
| `INTERNAL_DATABASE_UNAVAILABLE` | 503 | Postgres connection pool exhausted or unreachable | `{"component": "postgres"}` |
| `INTERNAL_REDIS_UNAVAILABLE` | 503 | Redis (nonce store, cache) unreachable | `{"component": "redis"}` |
| `INTERNAL_SETTLEMENT_IN_PROGRESS` | 503 | Mutating operation arrives during the settlement pipeline window | `{"retry_after_seconds": 60}` |
| `INTERNAL_UNEXPECTED_STATE` | 500 | Invariant violated; probable bug | `{"request_id": "req-..."}` — opaque, see logs |
| `INTERNAL_MATCHER_UNAVAILABLE` | 503 | `POST /v1/orders` against a worknet whose matcher engine isn't running. Two distinct causes both surface here: matcher_runtime spawn failed at boot, OR the request's `worknet_id` isn't in the active set. Retryable once the operator wires the matcher; the request body is otherwise valid. Phase 13.3a-impl. | `{"worknet_id": 7}` |
| `INTERNAL_WAL_DISK_FULL` | 503 | The matcher's WAL writer reported `ENOSPC` (disk full / quota exceeded) when committing the events for this request. Phase 13 audit fix #13 — distinct from `INTERNAL_MATCHER_UNAVAILABLE` so operators page on storage saturation, not a missing-engine misconfiguration. W07 contract: the matcher halts on fail-fast and the request is genuinely failed-not-pending. Retry is pointless until storage is reclaimed. | `{"worknet_id": 7}` |

**JSON-RPC code mapping**: the numeric codes `-40000 .. -49999` are assigned block-by-prefix (`-40xxx = AUTH_*`, `-41xxx = VALIDATION_*`, etc.). The complete numeric ↔ string table is maintained in `emg-core::errors`; clients should rely on the string code, not the number, for stability.

### 9.6 Rate limiting

Enforced via sliding-window counters in Redis, keyed by `principal` (not IP).

**Per principal limits**:

| Category | Burst | Sustained | Scope |
|---|---|---|---|
| Orders (submit + cancel) | 200/sec | 50/sec | Per principal |
| Split/merge operations | 20/sec | 10/sec | Per principal |
| Vote submissions | 10/epoch | — | Per (principal, epoch) |
| Comments | 5/min | — | Per principal |
| Comment endorsements | 30/min | — | Per principal |
| Weekly reports | 2/epoch | — | Per (worknet_id, epoch) |
| Authenticated reads | 2000/sec | 500/sec | Per principal |
| Unauthenticated reads | 100/sec | — | Per IP |

**WebSocket limits**:

- Max 10 concurrent subscriptions per connection
- Max 5 connections per principal
- Max 100 RPC calls/sec per connection
- Connection kept alive with ping every 30s; drop after 60s of silence

**Response to exceeded limit**: HTTP 429 with `Retry-After: <seconds>` header and error code `RATE_LIMIT_EXCEEDED`.

**Whitelisting**: known high-volume market-maker principals can be whitelisted with higher limits. Configured in `config.toml`.

### 9.7 GraphQL for analytical queries

A third transport — GraphQL over HTTPS — is exposed at `/v1/graphql` for **complex analytical queries** that would be awkward over REST.

**GraphQL is read-only** (ADR-014 §6). Query and Subscription are supported; there is no Mutation root. Write operations (submit order, cancel, vote, split/merge, comment publish/endorse) go through REST. This avoids ambiguity in EIP-712 canonicalization over GraphQL operations and keeps writes on the matcher hot path (ADR-012).

Examples where GraphQL helps:

- "For these 10 principals, give me their position in each WorkNet, their P&L in the last 4 epochs, and their current epistemic score."
- "Show the V vs W difference by WorkNet over the last 26 epochs, along with the weekly reports."
- "Fetch all comments with bridge score > 0.5 from epoch 42 and their endorsers' epistemic scores."

Each of these is one GraphQL query. The REST equivalent would be 5-20 round trips.

#### 9.7.1 Schema location

Defined in `api/schema.graphql`. Implemented with `async-graphql 7+`.

#### 9.7.2 Schema organization

```graphql
type Query {
  # Stakers
  staker(id: Principal!): Staker
  stakers(filter: StakerFilter, limit: Int = 50, offset: Int = 0): StakerPage!
  
  # Epochs
  currentEpoch: Epoch!
  epoch(id: Int!): Epoch
  epochs(from: Int, to: Int): [Epoch!]!
  
  # Markets
  market(worknetId: Int!): Market
  markets: [Market!]!
  
  # Historical queries
  klines(worknetId: Int!, interval: KlineInterval!, from: DateTime!, to: DateTime!): [Kline!]!
  
  # Cross-cutting
  leaderboard(metric: LeaderboardMetric!, marketId: Int, limit: Int = 100): [LeaderboardEntry!]!
}

type Staker {
  id: Principal!
  displayName: String
  epistemicScore: Decimal!
  epistemicScoreHistory(lastN: Int = 26): [EpistemicScorePoint!]!
  
  # Current state (authed only if asking for yourself)
  currentState: StakerEpochState
  
  # Historical data
  positionsAt(marketId: Int!): [Position!]!
  pnlByEpoch(from: Int, to: Int): [EpochPnL!]!
  votes(marketId: Int!): [Vote!]   # null if epoch not yet revealed
  comments(limit: Int = 20): [Comment!]!
}

type StakerEpochState {
  marketId: Int!
  principal: Principal!            # scalar = Address (20 bytes)
  stake: Decimal!
  chipsAvailable: Decimal!
  chipsLockedInOrders: Decimal!
  maxCapitalAtRisk: Decimal!
  positions: [Position!]!
  pnl: Decimal
  closingChips: Decimal
  govTokensReceived: Decimal
  epistemicScorePost: Decimal
}

> **Naming convention**: The GraphQL layer uses `Staker` as the outward-facing
> type name because most clients are humans thinking in terms of "Stakers who
> hold veAWP". Internally — in Rust (`PrincipalCurrentState` / `PrincipalEpochResult`),
> SQL (`principal_current_state`, `principal_epoch_result`, `principal_positions`),
> and Protocol math (`_S` subscript) — the canonical term is **Principal**. A
> Principal IS a Staker; these are the same economic entity viewed from different
> angles. The GraphQL resolver layer maps `Staker.id: Principal!` directly to the
> Principal address — no translation required.

type Market {
  worknetId: Int!
  name: String!
  currentPrice: Decimal!
  currentBook(depth: Int = 10): OrderBookSnapshot!
  weeklyReports(lastN: Int = 10): [WeeklyReport!]!
  historicalV(lastEpochs: Int = 26): [EpochValue!]!
  historicalW(lastEpochs: Int = 26): [EpochValue!]!
}

type Subscription {
  # Same as WebSocket JSON-RPC subscriptions, but via GraphQL subscriptions protocol
  # for clients that prefer one unified endpoint
  bookUpdates(worknetId: Int!): BookDelta!
  klineUpdates(worknetId: Int!, interval: KlineInterval!): Kline!
  phaseTransitions: PhaseTransition!
}
```

Full schema in `api/schema.graphql`.

#### 9.7.3 Authentication

The GraphQL endpoint accepts the same `X-EMG-*` headers as REST. Fields that return private data (e.g., `Staker.currentState`) are resolved only when the authenticated Principal matches the queried ID. Unauthenticated queries work fine for public fields.

#### 9.7.4 DataLoader pattern

N+1 query patterns are common in GraphQL (e.g., asking for 100 principals' recent comments could trigger 100 separate comment queries). We use `async-graphql`'s `DataLoader` to batch:

```rust
pub struct CommentLoader {
    pool: PgPool,
}

#[async_trait]
impl Loader<Principal> for CommentLoader {
    type Value = Vec<Comment>;
    type Error = Arc<sqlx::Error>;
    
    async fn load(
        &self,
        keys: &[Principal],
    ) -> Result<HashMap<Principal, Self::Value>, Self::Error> {
        // Single query fetches comments for all requested principals
        let rows = sqlx::query_as!(
            CommentRow,
            "SELECT * FROM comments WHERE author_id = ANY($1) ORDER BY created_at DESC",
            &keys.iter().map(|a| a.0).collect::<Vec<_>>(),
        ).fetch_all(&self.pool).await?;
        
        // Group by Staker
        let mut by_agent: HashMap<Principal, Vec<Comment>> = HashMap::new();
        for row in rows {
            by_agent.entry(Principal(row.author_id)).or_default().push(row.into());
        }
        Ok(by_agent)
    }
}
```

Loaders are registered per-request so a single query batches all its sub-selections.

#### 9.7.5 Query depth and complexity limits

To prevent abusive queries:

- Max query depth: 10
- Max complexity score: 1000 (each field = 1 point, list fields = 1 × estimated N)
- Queries exceeding either are rejected with HTTP 400

Configured via `async-graphql`'s `depth_limit` and `complexity_limit` extensions.

#### 9.7.6 Why GraphQL in addition to REST

Over-fetching and N+1 are real problems for analytical clients (frontends, dashboards, research tools). REST would require us to either:

1. Add dozens of ad-hoc endpoints for each specialized query, or
2. Over-fetch with massive response payloads that include everything the client might want.

GraphQL lets clients describe exactly what they need, and the resolver combines it via DataLoader batching. For high-traffic mutations (order submission, vote submission), REST remains the right tool — GraphQL mutations exist in the schema but are mostly thin wrappers around the REST handlers.

