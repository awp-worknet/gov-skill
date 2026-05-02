# govnet-skill

A SKILL.md-compliant agent skill for the EMG (Epistemic Market Gauge) prediction-market
protocol — also known as GovNet. Targets Claude Code, OpenClaw, Cursor, Codex,
Gemini CLI, Windsurf, and any runtime implementing
[agentskills.io](https://agentskills.io/specification).

The skill turns natural-language requests like *"buy 100 aGOV at 0.22"* or
*"watch the order book for aMINE"* into signed REST calls + JSON-RPC WebSocket
subscriptions against `https://api.gov.works/v1`.

---

## Install

```bash
git clone https://github.com/awp-core/govnet-skill ~/.claude/skills/govnet
```

Then make sure `awp-wallet` is on `PATH` — the skill shells out to it for every
EIP-712 signature and never reads, writes, or stores a private key directly.
See <https://github.com/awp-core/awp-wallet> for the wallet bridge.

Optional environment variables:

| Variable           | Default                              | Purpose                              |
|--------------------|--------------------------------------|--------------------------------------|
| `GOVNET_API_BASE`  | `https://api.gov.works/v1`           | REST base URL                        |
| `GOVNET_WS_URL`    | `wss://api.gov.works/v1/ws`          | WebSocket URL                        |
| `GOVNET_NONCE_DIR` | `~/.govnet/nonces/`                  | Per-principal nonce floor cache      |
| `GOVNET_AUTH_DIR`  | `~/.govnet/`                         | Cached `auth-info.json`              |

---

## Quick start

```bash
# Public reads (no wallet needed)
python3 scripts/public/markets.py
python3 scripts/public/book.py --market 6 --worknet 11 --depth 5
python3 scripts/public/epochs.py current

# Signed reads
python3 scripts/private/state.py
python3 scripts/private/orders-list.py --status active

# Signed write — confirmation prompt before sending
python3 scripts/trade/submit-order.py \
    --market 6 --worknet 11 --side buy --kind limit \
    --price 0.22 --quantity 100

# Streams (one JSON object per line)
python3 scripts/stream/watch-book.py --market 6 --worknet 11
python3 scripts/stream/watch-private.py
```

---

## Tests

```bash
pip install eth_account websockets pytest
pytest -q
```

The test suite includes:

- `tests/test_canonical.py` — query canonicalization vectors ported from
  `crates/emg-auth/src/canonical.rs::tests`.
- `tests/test_sign.py` — known-answer EIP-712 digest test against
  `REFERENCE_DIGEST_HEX` (port of `crates/emg-auth/src/eip712.rs`).
- `tests/test_nonce.py` — atomic nonce floor (concurrency).

---

## Operations covered

Public reads, signed reads, signed writes (orders / votes / positions /
content), and 5 WebSocket channels (book, klines, phase, fills.me, orders.me).
Full mapping is in [SKILL.md](SKILL.md#api-mapping).

---

## License

MIT.
