# GovNet Skill — Developer Handoff Package

> Everything you need to implement `govnet-skill`, an SKILL.md-compliant
> agent skill that lets users perform every user-facing operation on
> the EMG (Epistemic Market Gauge / GovNet) protocol via natural
> language. Targets Claude Code, OpenClaw, Cursor, Codex, Gemini CLI,
> Windsurf, and any other runtime implementing
> [agentskills.io](https://agentskills.io/specification).

---

## What this package contains

```
delivery/govnet-skill-handoff/
├── README.md                   ← this file
├── 01-MAIN-SPEC.md             ← read first; the development guide
├── 02-openapi.yaml             ← REST wire contract (authoritative)
├── 03-asyncapi.yaml            ← WebSocket wire contract (authoritative)
├── 04-api-narrative.md         ← API narrative spec — freshness tiers, auth headers
└── reference/
    ├── eip712.rs               ← EIP-712 signing reference + REFERENCE_DIGEST_HEX
    ├── canonical.rs            ← Query-string canonicalization rules + tests
    ├── errors.rs               ← EmgError variants + code() strings
    ├── frontend-api.js         ← Reference WS + REST client (proven against live server)
    └── frontend-markets.js     ← Reference frontend logic (book delta apply, phase gating)
```

---

## Read order (suggested)

### Day 1 — orientation

1. **`01-MAIN-SPEC.md`** § 1–4 (Skill identity, protocol background,
   EMG-SIG-V1 signing, API surface). About 400 lines; ~30 min.
2. **`04-api-narrative.md`** § 9 (auth headers, freshness tiers, error
   codes). 50–80 lines you'll re-read often.

You should now be able to answer:
- What's the authentication scheme? (EIP-712 typed data, EMG-SIG-V1)
- What does the wire envelope look like? (5 `X-EMG-*` headers + body)
- What endpoints are public vs private? (table in § 4 of MAIN-SPEC)
- What's the phase × operation matrix? (table in § 2 of MAIN-SPEC)

### Day 2 — signing in earnest

3. **`reference/eip712.rs`** — the Rust reference implementation. The
   constant `REFERENCE_DIGEST_HEX` near the bottom is your
   known-answer test: feed the same input fields into your Python
   EIP-712 implementation and verify the digest matches byte-for-byte
   before going further. **A single bit of drift here breaks every
   signed request.**
4. **`reference/canonical.rs`** — query-string canonicalization. Read
   the `#[cfg(test)] mod tests` block at the bottom; reproduce those
   test vectors in your skill's `scripts/lib/canonical.py` unit tests.
5. **`01-MAIN-SPEC.md`** § 7 (implementation notes). Concrete
   `sign.py` / `nonce.py` / `canonical.py` shapes.

### Day 3 — wire shapes & the actual scripts

6. **`02-openapi.yaml`** — every REST request/response. Use a tool
   like `openapi-python-client` to generate type-checked stubs if
   you want, or read directly. Pay attention to:
   - `Decimal` fields are serialized as **strings** at scale 18.
     Don't coerce to `float`/JS-Number on the wire path.
   - `nullable: true` means JSON `null`, not omitted.
   - The auth-required endpoints carry `security: [emg_sig_v1]`.
7. **`03-asyncapi.yaml`** — every WS channel + payload.
8. **`reference/frontend-api.js`** — minimal vanilla-JS client,
   ~250 lines. Notice:
   - WS subscribe uses `params: { channels: [...] }` — NOT `topics`.
   - Push notifications dispatch on `params.channel` — NOT `topic`.
   - Reconnect with ±20 % jitter.
   - The `getWS()` lazy singleton pattern.
9. **`reference/frontend-markets.js`** — production frontend (~1380
   lines, post 5 audit rounds). Cherry-pick:
   - **`applyBookDelta`** at line ~830: the canonical "absolute
     `new_quantity` per level" delta-apply pattern.
   - **`pushBucket`** at line ~1040: 1-minute K-line bucket dedup +
     in-place mutation pattern for streaming OHLCV.
   - **`buildPhaseRibbon`** + **`startCountdownTick`**: phase-aware UI
     with self-rearming setTimeout cadence.
   - **`subscribeInspector`** + request-token bail pattern: how to
     avoid stale-render races when the user clicks rapidly.

### Day 4+ — implementation

Follow `01-MAIN-SPEC.md` § 14 milestones. M0–M2 should land in a few
focused sessions; M3–M7 are follow-up turns.

---

## What's NOT in this package (and why)

- **The full Rust workspace** (~109 k LOC across 18 crates): the
  matcher engine, settlement math, persistence layer, etc. You don't
  need any of that to build a client skill. Wire shapes are what
  matters; those are in 02/03/04.
- **The web frontend's HTML/CSS**: the UI is illustrative for end
  users; the skill is a CLI tool. Only `frontend-markets.js` and
  `frontend-api.js` made the cut because they encode the wire-shape
  logic.
- **Database migrations / settlement runner / chain integration**:
  these are server-side and irrelevant to a skill that talks to the
  public REST/WS API.

---

## Critical contract gotchas (read these before writing any code)

These have all bitten implementers before. Each is documented in
context in `01-MAIN-SPEC.md` but called out here so you don't miss them.

### 1. Path canonicalization for signing — strip `/v1`

Production server is mounted under `/v1` via axum's `Router::nest`,
which strips the prefix BEFORE the auth middleware sees the URI. So
when signing a request whose URL is `https://api.gov.works/v1/orders`,
the `path` field in the `EMGRequest` typed data is **`/orders`**, NOT
`/v1/orders`. Sign the wrong one → `AUTH_SIGNATURE_INVALID` with a
mystery error.

The unit test in `crates/emg-api-rest/src/auth_layer.rs` (under
`mod nest_strip_tests`) pins this contract; replicate it in your
skill's tests.

### 2. WebSocket subscribe params — `channels`, NOT `topics`

```json
// Right
{ "method": "subscribe", "params": { "channels": ["book.6.10"] } }

// WRONG — server returns INVALID_PARAMS, every push is silently dropped
{ "method": "subscribe", "params": { "topics": ["book.6.10"] } }
```

Server contract at
`crates/emg-api-ws/src/dispatch.rs::SubscribeParams` declares
`channels: Vec<String>`. The frontend originally got this wrong and
nothing worked for two months — fixed in commit `d4ee3f8`. Your skill
MUST start with `channels`.

### 3. WS notification field — `params.channel`, NOT `params.topic`

```json
// Server emits this
{ "method": "book.update", "params": { "channel": "book.6.10", … } }
```

Same historical bug, opposite direction. Read `params.channel`. The
`topic` field never existed on the wire.

### 4. BookDelta `new_quantity` is ABSOLUTE, not a diff

```json
{ "changes": [{ "side": "bid", "price": "0.220", "new_quantity": "12.5" }] }
```

`new_quantity` is the resting quantity at that price level **after**
the change. `0` means the level was removed. Do NOT add/subtract
deltas; just upsert `(side, price) → new_quantity`. See
`reference/frontend-markets.js::applyBookDelta` for the canonical
implementation.

### 5. Side enum — `bid` / `ask` for book; `buy` / `sell` for orders

The book channel uses `"bid"` / `"ask"` (asyncapi convention).
Order submission and order responses use `"buy"` / `"sell"`. Don't
mix them.

### 6. Decimal precision

Server emits `NUMERIC(28,18)` (and `NUMERIC(20,18)` for simplex
coords) as JSON strings at scale 18, e.g. `"0.500000000000000000"`.

- **Carry as strings on the wire.** Hash them as strings. Compare as
  strings.
- **Coerce only at the display path.** Use `decimal.Decimal` in
  Python for any arithmetic. Never `float()`.
- The first 4 decimal places are what users see; quantize for
  display only.

### 7. EIP-712 domain values — fetch from the wire

```
GET /v1/auth/info → { chainId, verifyingContract }
```

Don't hardcode `chainId: 8453` in the skill. Fetch + cache + checksum
on first run; refuse to sign if the response changes unexpectedly
between sessions. This protects against a malicious upstream domain
swap.

### 8. Nonce is per-Principal, monotonic, strictly greater

- Stored on server in Redis CAS-style.
- Skill MUST track a local floor file under `~/.govnet/nonces/`.
- On `AUTH_NONCE_TOO_LOW`: re-fetch `/v1/auth/info` to read the
  server's stored value, bump local + 1, retry once.
- Atomic file rename when updating to prevent race between two
  concurrent skill invocations.

### 9. 5xx response with `X-EMG-Nonce-Burned: true` header

If a 5xx crosses the auth gate, the nonce was already consumed.
The header signals "bump nonce on retry, do NOT reuse." If you
see this header, increment your local nonce floor BEFORE retrying.

### 10. Idempotency keys

Every state-changing endpoint accepts `X-Idempotency-Key`. Server
caches the response for 24 hours keyed by `(Principal, key)`. **Reuse
the same key on retry; generate a new key for a new logical action.**
Server returns 503 `IDEMPOTENCY_KEY_MISMATCH` if you reuse a key with
a DIFFERENT body — that's a client bug, not a transient error.

---

## Composition with awp-wallet & awp-skill

- **`awp-wallet`** is a **hard dependency**. Every signed request
  goes through `awp-wallet sign-typed-data --data '<json>'`. The
  skill never reads, writes, or stores a private key. See
  https://github.com/awp-core/awp-wallet for the wallet's CLI.
- **`awp-skill`** is a **soft dependency**. Users get AWP Power from
  veAWP positions managed by awp-skill. When the user has no power
  for the current epoch, the skill should hint at the awp-skill
  command instead of trying to do anything itself. See
  https://github.com/awp-core/awp-skill.

Concretely, the skill's `metadata.openclaw.requires` block should
declare `awp-wallet` under `anyBins` so it's auto-installed on first
load. `awp-skill` is NOT a hard dep — only mentioned in error hints.

---

## Live deployment endpoints

```
REST:        https://api.gov.works/v1
WebSocket:   wss://api.gov.works/v1/ws
Frontend:    https://gov.works  (illustrative; not part of skill scope)
```

All endpoints are HTTPS / WSS only. The skill must reject `http://`
or `ws://` URLs to prevent man-in-the-middle stripping of `X-EMG-*`
auth headers. (HTTPS validation should also pin to gov.works as a
defense against compromised CAs.)

---

## Operations the skill MUST support

Drawn from `01-MAIN-SPEC.md` § 4. If any of these is missing in your
implementation, the skill is incomplete.

### Public reads (no auth)

- List markets / get market detail
- Order book snapshot for `(market, worknet)`
- K-line history for `(market, worknet, interval)`
- Worknet directory
- Current epoch + phase
- Epoch metadata + settlement results
- Vote Merkle root + per-principal proof
- Epistemic-score leaderboard

### Private reads (signed)

- My principal state (chips + per-worknet shares)
- My AWP Power for the current epoch
- My authorized managers + recipient
- My orders (list, single)
- My order's fills + computed VWAP

### Private writes (signed)

- Submit order (limit/market, all TIF flavors, post_only, reduce_only,
  STP modes, iceberg)
- Cancel order (single, batch, all)
- Submit private vote (during voting window)
- Split chips → shares
- Merge shares → chips
- Post weekly report
- Post comment / endorse comment

### WebSocket subscriptions

- `book.{m}.{wn}` — order-book deltas (public)
- `klines.{m}.{wn}.{interval}` — OHLCV updates (public)
- `phase` — market phase transitions (public)
- `fills.me` — private fill stream (auth.hello required)
- `orders.me` — private order-status stream (auth.hello required)

### Helpers (skill-side, not REST calls)

- "What can I do right now?" — phase-aware operation listing
- "How long until X?" — countdown to next phase boundary
- Pretty-print a fill / settlement receipt

---

## Estimated scope

Per `01-MAIN-SPEC.md` § 14:

| Milestone | LOC | Status |
|---|---|---|
| M0 — Bootstrap | ~600 | needed first |
| M1 — Reads | ~800 | unblocks "show me what's open" UX |
| M2 — Trade | ~700 | unblocks "submit order" UX |
| M3 — Vote | ~400 | required for voting Wednesdays |
| M4 — Positions | ~250 | split/merge |
| M5 — Stream | ~500 | fills.me + book deltas |
| M6 — Content | ~300 | comments / reports |
| M7 — Hardening | ~400 | retries, conformance tests |
| **Total** | **~3,950** | |

A focused implementer with the spec + reference code should deliver
M0–M2 in 2–3 working sessions and the rest as follow-ups.

---

## Questions / clarifications

This package is a one-shot handoff. If you hit ambiguity:

1. **Wire shape ambiguity** → cross-reference `02-openapi.yaml` /
   `03-asyncapi.yaml` against `reference/frontend-api.js`. The
   frontend has been audited five times against the live server and
   is the most reliable real-world reference.

2. **Signing ambiguity** → run `reference/eip712.rs`'s test vectors
   through your implementation. If the digest matches
   `REFERENCE_DIGEST_HEX`, your signing is correct; if not, your
   canonical encoding has drifted.

3. **Phase / state-machine ambiguity** → see the matrix in
   `01-MAIN-SPEC.md` § 2.

4. **Error code ambiguity** → `reference/errors.rs` is the
   authoritative `code() → string` map.

If after exhausting all four references something still isn't clear,
the protocol's REST and WS endpoints are publicly observable — fire
a request and read the response.

---

## License

The skill itself should be MIT-licensed (matches awp-wallet /
awp-skill / awp-core conventions).

---

*— end of handoff package README —*
