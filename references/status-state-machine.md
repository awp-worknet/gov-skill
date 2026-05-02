# Phase / status state machine

EMG epochs cycle through a fixed weekly schedule. Every operation has a
window in which it's valid; outside that window the server returns
`BUSINESS_PHASE_MISMATCH` and the skill SHOULD pre-empt the call with a
phase check.

---

## Weekly cycle (UTC)

```
Wed 12:00 ─────► Thu 12:00 ─────► Tue 12:00 ─────► Wed 11:30 ─────► Wed 12:00
   │                │                  │                 │                 │
   │ voting_and_    │   trading_only   │     settling    │    completed    │
   │ trading        │                  │  (server-side)  │   (read-only)   │
   ▼                ▼                  ▼                 ▼                 ▼
   epoch opens   votes close       trading closes   settlement done   next epoch
   chips issued                                                       opens
```

| Phase                | Wall-clock window           | Length |
|----------------------|-----------------------------|--------|
| `voting_and_trading` | Wed 12:00 → Thu 12:00 UTC   | 24 h   |
| `trading_only`       | Thu 12:00 → Tue 12:00 UTC   | 5 d    |
| `settling`           | Tue 12:00 → Wed 11:30 UTC   | ~24 h  |
| `completed`          | Wed 11:30 → Wed 12:00 UTC   | 30 min |

The protocol uses snake_case enums in some endpoints
(`voting_and_trading`, `trading_only`, `settlement`, `weekly_report`,
`completed`) and CamelCase in others (`VotingAndTrading`, `TradingOnly`,
`Settlement`, `Reporting`). `lib.govnet_lib.normalize_phase()` collapses
both forms — the skill's internal state machine uses the snake_case form.

---

## Operation × phase matrix

|                              | pending | voting_and_trading | trading_only | settling | completed |
|------------------------------|:-------:|:------------------:|:------------:|:--------:|:---------:|
| Read public data             |    ✓    |         ✓          |      ✓       |    ✓     |     ✓     |
| Read private state           |    ✓    |         ✓          |      ✓       |    ✓     |     ✓     |
| Submit order                 |    ✗    |         ✓          |      ✓       |    ✗     |     ✗     |
| Cancel order                 |    ✗    |         ✓          |      ✓       |    ✗     |     ✗     |
| Submit vote                  |    ✗    |         ✓          |      ✗       |    ✗     |     ✗     |
| Split / merge position       |    ✗    |         ✓          |      ✓       |    ✗     |     ✗     |
| Read settlement results      |    ✗    |         ✗          |      ✗       |    ✗     |     ✓     |

`scripts/helpers/what-can-i-do.py` projects this matrix against the current
phase and returns the available script list — the agent should call it
before initiating any uncertain action.

---

## Countdown UX

`scripts/helpers/countdown.py` reports the time until the next phase
boundary in both seconds and `Xd Yh Zm` form. Pair it with
`scripts/stream/watch-phase.py` for live transitions — the
`phase.changed` notification carries `next_transition_at` so a UI can
self-rearm.

---

## Pre-flight refusal

Every signed-write script SHOULD fetch the current epoch state and bail with
a localized error before signing:

```
✗ Cannot submit a vote — market №6 is in 'trading_only' phase.
  Voting closed at 2026-04-30 12:00 UTC (3h 12m ago).
  Trading closes at 2026-05-04 12:00 UTC (in 4d 21h 48m).
```

This is purely a UX optimization — the server enforces the matrix
authoritatively via `BUSINESS_PHASE_MISMATCH` (HTTP 409 / 403). But surfacing
the issue locally avoids burning a nonce and lets us point at the next
window without an extra round-trip.
