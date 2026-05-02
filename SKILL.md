---
name: govnet
version: 0.1.0
description: >
  EMG protocol (a.k.a. GovNet) — list/watch prediction markets,
  place/cancel limit/market orders, cast private votes during the
  Wed-Thu voting window, split chips into worknet shares (or merge),
  watch live order books + fills.me/orders.me, read settlement
  results.

  Use this skill whenever the user mentions: GovNet, gov.works, EMG,
  emission market, worknet (aMINE/aGOV/aPRED/aKYA/aARDI/aTMR/aCOM),
  "chips this epoch", "split into shares", voting Wednesday,
  settlement Tuesday, V_j / W_j / Σ Pⱼ, AWP Power, "this week's
  market", "trading closes", "market phase". Trigger even when the
  user does not type "govnet" — any of these phrases (chips, worknet,
  weekly emission, per-Principal voting) means this skill is the
  right tool.

  Composes with awp-wallet (every signed request goes through it)
  and awp-skill (veAWP / AWP Power).

  NOT for: Polymarket, Augur, Hyperliquid, Binance, Uniswap, Aave,
  Lido, generic DAO proposals (Compound, Snapshot), veAWP staking
  (awp-skill), raw token transfers, NFT trading.

metadata:
  openclaw:
    requires:
      bins:
        - python3
        - node
      anyBins:
        - awp-wallet
      env:
        - GOVNET_NONCE_DIR
        - GOVNET_API_BASE
        - GOVNET_WS_URL
        - GOVNET_VOTE_TYPED_DATA_VARIANT
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
      network:
        endpoints:
          - https://api.gov.works/v1
          - wss://api.gov.works/v1/ws
---

# GovNet (EMG Protocol) Skill

A natural-language interface for the EMG (Epistemic Market Gauge) prediction-market
protocol — also known as GovNet. The skill exposes every user-facing operation:
list and watch markets, place and cancel orders, cast private votes during the
voting window, split chips into shares (or merge them back), monitor live order
books and fills, and read settlement results.

The skill is signing-aware. State-changing requests are gated by an EIP-712
signature produced via `awp-wallet sign-typed-data` — the skill itself never
reads, writes, or stores a private key.

---

## Quick start

1. **Install awp-wallet** (one-time): `awp-wallet --version`. If missing, the
   harness will install it from <https://github.com/awp-core/awp-wallet>.
2. **First-run handshake**: the first invocation of any signed script runs
   `GET /v1/auth/info` to learn the EIP-712 chain id and verifying contract,
   then caches both under `~/.govnet/auth-info.json`.
3. **Public reads** work without a wallet:
   ```
   python3 scripts/public/markets.py
   python3 scripts/public/book.py --market 6 --worknet 11
   ```
4. **Signed reads/writes** auto-resolve principal via `awp-wallet receive`:
   ```
   python3 scripts/private/state.py
   python3 scripts/trade/submit-order.py --market 6 --worknet 11 \
       --side buy --kind limit --price 0.22 --quantity 100
   ```

End-to-end demo:

```
$ python3 scripts/helpers/what-can-i-do.py
{
  "phase": "voting_and_trading",
  "epoch_id": 6,
  "next_transition_at": "2026-05-03T12:00:00Z",
  "available": ["list-markets", "vote", "submit-order", "split", "merge", …]
}
```

---

## API mapping

| Script                                  | Method | Path                                                            | Auth |
|-----------------------------------------|--------|-----------------------------------------------------------------|------|
| `public/auth-info.py`                   | GET    | `/v1/auth/info`                                                 | —    |
| `public/markets.py`                     | GET    | `/v1/markets`, `/v1/markets/{id}`                               | —    |
| `public/book.py`                        | GET    | `/v1/markets/{m}/worknets/{wn}/book`                            | —    |
| `public/klines.py`                      | GET    | `/v1/markets/{m}/worknets/{wn}/klines`                          | —    |
| `public/worknets.py`                    | GET    | `/v1/worknets`                                                  | —    |
| `public/epochs.py`                      | GET    | `/v1/epochs/current`, `/v1/epochs/{id}`, `…/phase`, `…/results`, `…/voters`†, `…/votes/{principal}/history`† | —    |
| `public/leaderboard.py`†                | GET    | `/v1/leaderboard/epistemic`                                     | —    |
| `public/merkle.py`                      | GET    | `/v1/epochs/{id}/merkle-root`, `…/votes/{principal}/proof`       | —    |
| `private/state.py`                      | GET    | `/v1/principals/{me}/state`                                     | sig  |
| `private/power.py`                      | GET    | `/v1/principals/{me}/power`                                     | sig  |
| `private/managers.py`                   | GET    | `/v1/principals/{me}/managers`                                  | sig  |
| `private/recipient.py`                  | GET    | `/v1/principals/{me}/recipient`                                 | sig  |
| `private/orders-list.py`†               | GET    | `/v1/orders`                                                    | sig  |
| `private/orders-get.py`                 | GET    | `/v1/orders/{id}`                                               | sig  |
| `trade/submit-order.py`                 | POST   | `/v1/orders`                                                    | sig  |
| `trade/cancel-order.py`                 | DELETE | `/v1/orders/{id}`                                               | sig  |
| `trade/cancel-all.py`                   | POST   | `/v1/orders/cancel-all`                                         | sig  |
| `trade/cancel-batch.py`                 | POST   | `/v1/orders/cancel-batch`                                       | sig  |
| `vote/submit-vote.py`                   | POST   | `/v1/epochs/{id}/votes`                                         | sig  |
| `vote/verify-proof.py`                  | local  | (Merkle proof reconstruction)                                    | —    |
| `positions/split.py`                    | POST   | `/v1/positions/split`                                           | sig  |
| `positions/merge.py`                    | POST   | `/v1/positions/merge`                                           | sig  |
| `content/post-comment.py`               | POST   | `/v1/comments`                                                  | sig  |
| `content/post-report.py`                | POST   | `/v1/reports`                                                   | sig  |
| `content/endorse.py`                    | POST   | `/v1/comments/{id}/endorse`                                     | sig  |
| `stream/watch-book.py`                  | WS     | `book.{m}.{wn}`                                                 | —    |
| `stream/watch-klines.py`                | WS     | `klines.{m}.{wn}.{interval}`                                    | —    |
| `stream/watch-phase.py`                 | WS     | `phase`                                                         | —    |
| `stream/watch-private.py`               | WS     | `fills.me`, `orders.me` (with `auth.hello`)                     | sig  |
| `helpers/what-can-i-do.py`              | local  | (phase-aware operation listing)                                 | —    |
| `helpers/countdown.py`                  | local  | (time until next phase boundary)                                 | —    |
| `helpers/show-receipt.py`               | local  | (pretty-print a fill / settlement result)                        | —    |

Every script emits a single JSON object (or one JSON-Lines stream for `stream/*`)
to stdout so the calling agent can parse it directly.

Scripts marked **†** support a `--all-pages` flag that walks
`pagination.next_cursor` until exhausted and concatenates all `data[]` arrays
into one response. `has_more === false` is the authoritative stop signal
(see `references/api-shapes.md`). Default cap is 100 pages — when hit, the
output carries `truncated_at_max_pages: true` plus `next_cursor` for resume.
Private listings still cost one nonce per page (each page is a separately-signed
request) — don't blindly enable `--all-pages` on huge listings.

---

## Signing (EMG-SIG-V1) — the load-bearing summary

Every authenticated request carries five headers:

| Header              | Value                                                       |
|---------------------|-------------------------------------------------------------|
| `X-EMG-Principal`   | 0x-hex 20-byte Staker address                                |
| `X-EMG-Actor`       | 0x-hex 20-byte signer (omit when actor == principal)         |
| `X-EMG-Nonce`       | strictly-greater unsigned integer                            |
| `X-EMG-Timestamp`   | Unix seconds UTC (server enforces ±30s window)               |
| `X-EMG-Signature`   | 0x-hex 65 bytes (r‖s‖v); over the EIP-712 `EMGRequest` digest|

Critical contract gotchas — these have all bitten implementers before:

1. **Strip the `/v1` prefix** when populating the `path` field of the `EMGRequest`
   typed data. The production server is mounted under `/v1` via axum's
   `Router::nest`, which strips the prefix BEFORE the auth middleware sees the
   URI. Sign `/orders`, not `/v1/orders`.
   - Exception: WebSocket `auth.hello` signs `path: "/v1/ws"` because the WS
     handler reads the full URI via a hardcoded literal.
2. **WebSocket subscribe param is `channels`, NOT `topics`.** Server returns
   `INVALID_PARAMS` and silently drops every push if you send `topics`.
3. **WS notification field is `params.channel`, NOT `params.topic`.**
4. **BookDelta `new_quantity` is ABSOLUTE**, not a diff. `0` removes the level.
5. **Side enum**: book channel uses `bid`/`ask`; orders use `buy`/`sell`.
6. **Decimals are strings at scale 18.** Carry as strings on the wire; coerce
   to `decimal.Decimal` for arithmetic; never use `float`.
7. **Nonce is strictly greater than the server's stored value**, tracked
   per-principal under `~/.govnet/nonces/<principal>.json` with atomic rename.
8. **`AUTH_NONCE_TOO_LOW` / `NONCE_TOO_LOW` retry**: re-fetch `/v1/auth/info`
   to read the server's stored value, bump local +1, retry once.
9. **5xx with `X-EMG-Nonce-Burned: true`** means the server consumed the nonce
   even though the request failed. Bump local nonce floor BEFORE retrying.
10. **Idempotency keys** (`X-Idempotency-Key`): same key + same body returns
    cached response within 24 h. Same key + different body → 503 / 409
    `STATE_IDEMPOTENCY_KEY_MISMATCH` (client bug). Generate a fresh key per
    logical action; reuse on retry.

Full implementation reference: `references/signing.md`.

---

## Phase awareness

Refuse signed writes early when the current phase doesn't allow them. Fetch
`/v1/epochs/current` (or `/v1/epochs/{id}/phase`) and check this matrix:

|                              | pending | voting_and_trading | trading_only | settling | completed |
|------------------------------|:-------:|:------------------:|:------------:|:--------:|:---------:|
| Read public data             |    ✓    |         ✓          |      ✓       |    ✓     |     ✓     |
| Read private state           |    ✓    |         ✓          |      ✓       |    ✓     |     ✓     |
| Submit order                 |    ✗    |         ✓          |      ✓       |    ✗     |     ✗     |
| Cancel order                 |    ✗    |         ✓          |      ✓       |    ✗     |     ✗     |
| Submit vote                  |    ✗    |         ✓          |      ✗       |    ✗     |     ✗     |
| Split / merge position       |    ✗    |         ✓          |      ✓       |    ✗     |     ✗     |
| Read settlement results      |    ✗    |         ✗          |      ✗       |    ✗     |     ✓     |

Server-side phase enums use both snake_case (`voting_and_trading`) and
CamelCase (`VotingAndTrading`); helpers in `scripts/lib/govnet_lib.py`
normalize either form. `references/status-state-machine.md` has the full map.

---

## Composition with awp-wallet / awp-skill

- **`awp-wallet`** is a hard dependency. Every signed request goes through
  `awp-wallet sign-typed-data --data '<json>'`. The skill never sees a private
  key directly. See <https://github.com/awp-core/awp-wallet>.
- **`awp-skill`** is a soft dependency. EMG snapshots veAWP-derived AWP Power
  at every Wednesday 12:00 UTC. When `STATE_PRINCIPAL_NOT_IN_EPOCH` fires, the
  skill should hint at `awp-skill` rather than try to handle staking itself.

---

## Error handling discipline

Map server `code` → user-facing action. Every signed-write script has the same
retry policy:

| Code                                              | Action                                                                                              |
|---------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| `AUTH_MISSING_HEADER`                             | Log and abort — skill bug.                                                                          |
| `AUTH_SIGNATURE_INVALID`                          | Refresh `/v1/auth/info`, retry once. Else surface as a domain-mismatch.                             |
| `AUTH_NONCE_TOO_LOW` / `NONCE_TOO_LOW`            | `signed_request` auto: refresh auth-info, `bump_to(server_stored)`, retry once.                     |
| `AUTH_TIMESTAMP_OUT_OF_WINDOW`                    | Print "your clock is X seconds off; sync NTP and retry."                                            |
| `BUSINESS_PHASE_MISMATCH`                         | Surface phase + countdown to when the op opens again.                                               |
| `BUSINESS_INSUFFICIENT_BALANCE`                   | Surface `chips_available`.                                                                          |
| `STATE_PRINCIPAL_NOT_IN_EPOCH`                    | Suggest `awp-skill` to stake veAWP for next epoch.                                                  |
| `RATE_LIMIT_EXCEEDED` / `RATE_LIMIT_BACKPRESSURE` | `signed_request` auto: parse `Retry-After` (delta-seconds OR HTTP-date), sleep ≤ 60s, retry once.   |
| `STATE_RESULTS_NOT_FOUND`                         | Tell user to retry after settlement window.                                                         |
| `INTERNAL_*` (5xx) with `X-EMG-Nonce-Burned`      | Bump nonce, surface error to caller (no auto-retry; idempotency unclear).                           |
| `INSECURE_TRANSPORT` *(client)*                   | Set `GOVNET_API_BASE` / `GOVNET_WS_URL` to `https://` / `wss://`. Skill refuses plaintext.          |
| `INSECURE_REDIRECT` *(client)*                    | Server returned 30x. Skill refuses to follow (signed headers would leak). Fix DNS / config upstream.|

Full code → message map: `references/error-codes.md` (incl. client-emitted codes section).

---

## Confirm-before-irreversible

Every signed-write script writes a confirmation block to stderr and waits for
`y` on stdin (matching `awp-wallet send`'s pattern):

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
     idem-key:  018f-…
     nonce:     43 (was 42)
     proceed? (y/n)
```

If stdin is not a tty, the script aborts unless `--yes` was supplied. NEVER
auto-execute a signed write without explicit consent.

---

## Bundled references

Load on demand:

- `references/api-shapes.md` — request/response shape per endpoint.
- `references/signing.md` — EMG-SIG-V1 walkthrough with worked examples.
- `references/status-state-machine.md` — phase transitions + countdown logic.
- `references/error-codes.md` — full code → user-text map.

---

## Layout

```
gov-skill/                          # repo root === skill root
├── SKILL.md
├── README.md
├── LICENSE
├── scripts/
│   ├── lib/        # canonical, sign, nonce, ws, govnet_lib
│   ├── public/     # 8 unauthenticated readers
│   ├── private/    # signed reads
│   ├── trade/      # signed writes — orders
│   ├── vote/       # signed writes — votes
│   ├── positions/  # signed writes — split/merge
│   ├── content/    # signed writes — comments / reports
│   ├── stream/     # JSON-Lines WebSocket subscribers
│   └── helpers/    # phase-aware local helpers
├── references/     # markdown loaded on demand by the agent
└── tests/          # pytest — known-answer canonical + EIP-712 digest
```
