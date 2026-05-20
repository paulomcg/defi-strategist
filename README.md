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

> Status: v0.1.5 — monitor + auto-compound write actions. Submitted to
> the OKX Agentic Trading Contest (May 2026). Companion to
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

### What v0.1.5 does

1. **Opportunity scanning across DeFi platforms.** `defi-strategist scan
   --tokens USDC,USDT --chains solana,ethereum --top 10` returns the
   top APYs across protocols, sorted, deduped, ready for review.

2. **Continuous monitoring with alerts.** `defi-strategist watch
   --config rules.yaml` runs every N seconds, polls positions +
   opportunities, evaluates rules, emits structured alerts to stdout
   AND an append-only audit log. Four built-in rule types:
   - `min_apy_floor` — your position's APY dropped below X%
   - `max_protocol_concentration` — one protocol > Y% of your DeFi value
   - `opportunity_above` — found a Z% APY product on a token you care
     about (and you don't already hold it)
   - `auto_compound` — pending rewards > $X, fire a `claim` action and
     (optionally) `reinvest` it. The simplest non-trivial DeFi loop.

3. **Auto-compound write actions (new in v0.1.5).** When `--live` is
   set, rule-emitted actions execute via OnChainOS:
   `defi claim` / `defi invest` build calldata → `wallet contract-call`
   signs + broadcasts via the Agentic Wallet's TEE-backed signer.
   `--dry-run-actions` round-trips with OnChainOS to verify calldata is
   well-formed without broadcasting — the recommended smoke test before
   the first `--live` run on a new position.

4. **Safety by default.** Without `--live`, actions are inert even when
   rules emit them. `--max-actions-per-cycle` caps how many actions
   may execute per cycle (default 5) so a misfiring rule can't drain
   gas through hundreds of round-trips.

5. **Honest about what's still deferred.** No yield rotation (atomic
   redeem+deposit across protocols). No IL tracking. No cross-chain
   comparison. No dashboard. The architecture supports all of them; the
   roadmap below sketches a v0.2 that adds those.

## Quickstart

```sh
# 1. Authenticate the OnChainOS CLI (one-time)
export OKX_API_KEY=... OKX_SECRET_KEY=... OKX_PASSPHRASE=...
onchainos wallet login

# 2. Scan stablecoin yields right now (no wallet required)
./bin/defi-strategist scan --tokens USDC --chains solana,ethereum --top 10

# 3. Monitor mode — alerts only, no on-chain actions
./bin/defi-strategist watch --config examples/rules/stablecoin-yield-watch.yaml \
    --interval 300 --iterations 50

# 4. Verify auto-compound calldata without broadcasting
./bin/defi-strategist watch --config examples/rules/auto-compound.yaml \
    --address <your-wallet> --dry-run-actions --iterations 1

# 5. Run auto-compound live (claims + reinvests when rewards > threshold)
./bin/defi-strategist watch --config examples/rules/auto-compound.yaml \
    --address <your-wallet> --live --interval 3600

# 6. Replay recent audit lines
./bin/defi-strategist audit --limit 10
```

**Three-stage safety progression**: monitor (no actions) → dry-run-actions
(calldata only) → live (broadcast). Each stage is opt-in via an
explicit CLI flag. There is no implicit upgrade.

Output is line-delimited JSON on stdout — pipe into `jq`, append to a
file, ship to your alerting system, whatever.

## Architecture

| File | Role |
|---|---|
| `scripts/onchain_defi.py` | OnChainOS DATA adapter: `defi list/search/positions/rate-chart` wrappers with cache + response-shape normalization |
| `scripts/executor.py` | OnChainOS WRITE adapter: `defi claim` / `defi invest` → `wallet contract-call` two-step pipeline with dry-run / live modes |
| `scripts/watch.py` | Watch loop: poll → normalize → evaluate → emit + execute |
| `scripts/rules.py` | Pure-function rules engine: `(positions, opportunities, rules) → (alerts, actions)` |
| `scripts/audit.py` | Append-only JSONL audit log |
| `scripts/cli.py` | `watch` / `positions` / `scan` / `audit` subcommands |
| `examples/rules/` | Three sample configs: scan-only, portfolio-health, auto-compound |
| `tests/` | 26 tests covering rules engine + adapter helpers + executor argv construction |

## Roadmap (v0.2 and beyond)

- ✅ **Auto-compound loop (v0.1.5).** `auto_compound` rule with
  `min_rewards_usd` threshold + optional `reinvest` flag. Implemented.
- **Python `monitor(state) → (alerts, actions)` callback.** Same shape
  PM uses for live strategies, adapted to DeFi primitives. Lets users
  author arbitrary strategies in Python beyond the declarative rules.
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
