# defi-strategist

> **OKX Agentic Trading Contest, Skill Quality Award submission** — see
> [`SUBMISSION.md`](./SUBMISSION.md) for the explicit mapping of features
> to the five evaluation criteria (strategy completeness, risk control,
> execution reliability, user safety/onboarding, observability).

**The strategy + observability layer over OnChainOS DeFi primitives.**

OnChainOS gives you the wheels — `defi list`, `defi search`,
`defi positions`, `defi rate-chart`, `defi deposit/redeem/claim`.
What's missing is the *driver*: when to rebalance, when to chase a
better yield, when to halt because concentration ran away.
`defi-strategist` is the v0.1 monitor + rules layer that closes that
gap.

> Status: v0.1.0 — monitor-only. Submitted to the OKX Agentic Trading
> Contest (May 2026). Companion to
> [portfolio-manager](https://github.com/paulomcg/portfolio-manager) +
> [strategy-backtester](https://github.com/paulomcg/strategy-backtester).
> MIT licensed. Not investment advice.

---

## What it does today

`defi-strategist` is the second instance of the same architectural
pattern that `portfolio-manager` (PM) pioneered for spot trading:

```
                ┌─────────────────────────────────────────────────┐
                │  rules.yaml  (declarative thresholds + alerts)  │
                └─────────────────────────────────────────────────┘
                                      │
                                      ▼
            ┌───────────────────────────────────────────────────┐
            │  watch loop  (poll → normalize → evaluate → emit) │
            └───────────────────────────────────────────────────┘
                    │                              │
                    ▼                              ▼
        ┌──────────────────────┐         ┌─────────────────────────┐
        │  OnChainOS adapter   │         │  audit log + stdout JSONL│
        │  (subprocess + cache)│         │  (per-cycle observability)│
        └──────────────────────┘         └─────────────────────────┘
                    │
                    ▼
          ┌───────────────────────────────────────────────────┐
          │  onchainos defi list / search / positions /       │
          │  rate-chart / tvl-chart                           │
          └───────────────────────────────────────────────────┘
```

### Three things v0.1 does well

1. **Opportunity scanning across DeFi platforms.** `defi-strategist scan
   --tokens USDC,USDT --chains solana,ethereum --top 10` returns the
   top APYs across protocols, sorted, deduped, ready for review.

2. **Continuous monitoring with alerts.** `defi-strategist watch
   --config rules.yaml` runs every N seconds, polls positions +
   opportunities, evaluates rules, emits structured alerts to stdout
   AND an append-only audit log. Three built-in rule types:
   - `min_apy_floor` — your position's APY dropped below X%
   - `max_protocol_concentration` — one protocol > Y% of your DeFi value
   - `opportunity_above` — found a Z% APY product on a token you care
     about (and you don't already hold it)

3. **Honest about what it doesn't do yet.** v0.1 is monitor-only — no
   automated deposits / redemptions / rotations. The roadmap below
   sketches a v0.2 that adds an executor + decision hook (mirroring
   PM's strategy callback). The point of v0.1 is to prove the
   data-plumbing-and-rules layer works end-to-end against real
   OnChainOS data before adding write actions.

## Quickstart

```sh
# 1. Authenticate the OnChainOS CLI (one-time)
export OKX_API_KEY=... OKX_SECRET_KEY=... OKX_PASSPHRASE=...
onchainos wallet login

# 2. Scan stablecoin yields right now
./bin/defi-strategist scan --tokens USDC --chains solana,ethereum --top 10

# 3. Start the monitor loop with the bundled stablecoin example
./bin/defi-strategist watch --config examples/rules/stablecoin-yield-watch.yaml \
    --interval 300 --iterations 50

# 4. Replay recent audit lines
./bin/defi-strategist audit --limit 5
```

Output is line-delimited JSON on stdout — pipe into `jq`, append to a
file, ship to your alerting system, whatever.

## Architecture

| File | Role |
|---|---|
| `scripts/onchain_defi.py` | OnChainOS adapter: subprocess + cache + response-shape normalization |
| `scripts/watch.py` | Watch loop: poll → normalize → evaluate → emit |
| `scripts/rules.py` | Pure-function rules engine: `(positions, opportunities, rules) → alerts` |
| `scripts/audit.py` | Append-only JSONL audit log |
| `scripts/cli.py` | `watch` / `positions` / `scan` / `audit` subcommands |
| `examples/rules/` | Two sample configs covering scan-only + portfolio-health monitoring |

## Roadmap (v0.2 and beyond)

- **Executor + decision hook.** `monitor(positions, opportunities, market)
  → list[Action]` callback for user-authored Python strategies. Actions
  are `deposit` / `redeem` / `claim_compound` / `rotate`. Same shape
  PM uses today, adapted to DeFi primitives. Backed by `defi deposit`
  / `defi redeem` / `defi claim` calldata generation.
- **Auto-compound loop.** Configurable "claim and redeposit when
  rewards > $X" rule, with safety checks against silly-small claims
  (gas).
- **Yield rotation.** Atomic redeem-from-A + deposit-into-B when a
  cleaner alternative crosses a margin threshold.
- **IL monitoring.** For LP positions, track impermanent loss against
  hold-baseline via `defi depth-price-chart`, halt at user-configured
  threshold.
- **Cross-chain comparison.** Same token, multiple chains, surface
  arbitrage-class differences.
- **Dashboard.** Read-only web UI sibling of PM's (`/api/snapshot`,
  `/api/positions`, `/api/opportunities`, `/api/alerts/pending`).

## License

MIT.
