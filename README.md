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

> Status: v0.3.0 — dynamic graph discovery + risk scoring + N-step
> execution + rollback. Submitted to the OKX Agentic Trading Contest
> (May 2026). Companion to
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

### What v0.3 does

**NEW in v0.3 — four pillars of "real product" composable yield.**

1. **Dynamic graph discovery.** `discover` no longer relies on a
   hardcoded receipt-token map. It walks OnChainOS `defi detail`'s
   first-class `lpToken` + `underlyingToken` fields to build a token-
   edge graph dynamically, then enumerates loops up to `--max-steps N`
   (default 3) starting from any base asset. New protocols surface
   automatically — no code changes needed.
2. **Risk scoring.** With `--with-risk`, each step gets a 0-100
   composite score from APY volatility (stdev over historical
   `rate-chart`) + TVL stability (1 - stdev/mean over `tvl-chart`).
   Loop score = `min(per-step scores)` — weakest-link aggregation.
   `--min-risk-score 60` filters out shaky compositions.
3. **N-step execution with receipt-balance polling.** `run-loop`
   handles arbitrary-length loops; each subsequent step's amount is
   derived from polling the on-chain receipt-token balance after the
   previous step settles (live mode). Protects against partial-fill
   over-deposit in the next leg.
4. **Rollback on partial failure.** If step N fails after steps
   1..N-1 succeeded, the executor walks back through completed legs
   in reverse and emits best-effort `defi redeem` calldata for each
   to exit the intermediate positions. Rollback fills are recorded
   separately in `rollback_fills` so the operator can distinguish
   forward progress from cleanup.

**Carry-over from v0.2 — composable loop discovery + on-demand execution.**
`defi-strategist discover --token SOL --chains solana` enumerates not
just single-product opportunities, but **N-step compositions where
each step's receipt token is accepted by the next product**. On
Solana this surfaces SOL → Jito (mints JitoSOL) → Solayer (restakes
JitoSOL → sjitoSOL). On Ethereum it surfaces ETH → Lido (mints
stETH/wstETH) → Aave V3 / Morpho (uses wstETH as collateral). Loops
are sorted by combined APY, each gets a stable `loop_id`, and
`run-loop --loop-id <id> --amount-minimal-units N` executes the full
sequence step-by-step.

Five things v0.2 does:

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

3. **Auto-compound write actions (v0.1.5).** When `--live` is set,
   rule-emitted actions execute via OnChainOS: `defi claim` /
   `defi invest` build calldata → `wallet contract-call` signs +
   broadcasts via the Agentic Wallet's TEE-backed signer.
   `--dry-run-actions` round-trips with OnChainOS to verify calldata is
   well-formed without broadcasting.

4. **Composable loop discovery + execution (v0.2).** `discover` walks
   the known receipt-token map and enumerates 2-step compositions
   directly from OnChainOS `defi search` data. `run-loop` executes a
   chosen loop step-by-step — for the 2nd step in `--live` mode, it
   polls `defi positions` for the receipt-token balance and uses the
   actual on-chain amount (not a user-supplied estimate) for step 2's
   deposit, protecting against partial-fill over-deposit.

5. **Safety by default.** Without `--live`, actions are inert even when
   rules emit them or a loop is invoked. `--max-actions-per-cycle` caps
   actions per cycle (default 5). `run-loop`'s second-step submission
   is gated on a successfully-detected receipt-token balance.

6. **Honest about what's still deferred.** No yield rotation that bridges
   to a DIFFERENT input token. No IL tracking. No cross-chain
   comparison. No multi-step (3+ leg) loops. No dashboard. The
   architecture supports all; the roadmap below tracks which is next.

## Install

```sh
git clone https://github.com/paulomcg/defi-strategist.git ~/Projects/defi-strategist
cd ~/Projects/defi-strategist && ./install.sh
echo 'export PATH="$HOME/Projects/defi-strategist/bin:$PATH"' >> ~/.bashrc  # or .zshrc
defi-strategist --version
```

### Using this skill from an agent (Claude / Codex / etc.)

| Method | Path |
|---|---|
| Drop into Claude Code's skills dir | `cp -r . ~/.claude/skills/defi-strategist/` then restart Claude |
| Point a custom agent at the SKILL.md | parse YAML frontmatter (name / description / trigger phrases); shell out to `bin/defi-strategist` per command |
| Register with the OKX Plugin Store | `plugin.yaml` carries the manifest (schema_version: 1) |

Every CLI command emits `{"ok": bool, "result": {...}}` JSON envelopes
on stdout (with `--format json`, the default). For direct human use,
pass `--format table` for fixed-width readable output. Errors print
`FAILED: <category> <detail>` to stderr — categories are stable +
machine-parseable. See `SKILL.md` for the full schema, error
vocabulary, and programmatic embedding examples.

## Quickstart

```sh
# 1. Authenticate the OnChainOS CLI (one-time)
export OKX_API_KEY=... OKX_SECRET_KEY=... OKX_PASSPHRASE=...
onchainos wallet login

# 2. Scan stablecoin yields right now (no wallet required)
./bin/defi-strategist scan --tokens USDC --chains solana,ethereum --top 10

# 3. Discover composable loops starting from a base asset
./bin/defi-strategist discover --token SOL --chains solana --top 8 --min-step-apy 0
# returns: 1-step yields (Kamino Sanctum 9.4%, Jito 5.6%, ...) AND
# 2-step loops (Marinade→Solayer, Jito→Solayer, etc.) ranked by
# combined APY, each with a stable loop_id

# 4. Dry-run a discovered loop (builds calldata via OnChainOS, does NOT broadcast)
./bin/defi-strategist run-loop \
    --loop-id <id-from-discover> --token SOL --chains solana \
    --address <your-wallet> --amount-minimal-units 1000000   # 0.001 SOL

# 5. Run the loop live (broadcasts step 1, polls for receipt, broadcasts step 2)
./bin/defi-strategist run-loop \
    --loop-id <id> --token SOL --chains solana \
    --address <your-wallet> --amount-minimal-units 1000000 --live

# 6. Monitor mode — alerts only, no on-chain actions
./bin/defi-strategist watch --config examples/rules/stablecoin-yield-watch.yaml \
    --interval 300 --iterations 50

# 7. Run auto-compound on a held position
./bin/defi-strategist watch --config examples/rules/auto-compound.yaml \
    --address <your-wallet> --live --interval 3600

# 8. Replay recent audit lines
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
| `scripts/composability.py` | Curated map of (chain, platform, deposit) → receipt token, e.g. (solana, Jito, SOL) → JitoSOL |
| `scripts/discoverer.py` | Enumerates 1- and 2-step yield loops from OnChainOS `defi search` data, ranked by combined APY |
| `scripts/loop_executor.py` | Drives DefiExecutor through a discovered loop's steps; polls for receipt-token balance between steps in live mode |
| `scripts/watch.py` | Watch loop: poll → normalize → evaluate → emit + execute |
| `scripts/rules.py` | Pure-function rules engine: `(positions, opportunities, rules) → (alerts, actions)` |
| `scripts/audit.py` | Append-only JSONL audit log |
| `scripts/cli.py` | `watch` / `positions` / `scan` / `audit` / `discover` / `run-loop` subcommands |
| `examples/rules/` | Three sample configs: scan-only, portfolio-health, auto-compound |
| `tests/` | 42 tests covering rules engine + adapter helpers + executor + composability map + discoverer |

## Roadmap (v0.3 and beyond)

- ✅ **Auto-compound loop (v0.1.5).** `auto_compound` rule with
  `min_rewards_usd` threshold + optional `reinvest` flag. Implemented.
- ✅ **Composable loop discovery + execution (v0.2).** `discover`
  enumerates 2-step LST-restake compositions; `run-loop` executes a
  chosen loop with receipt-balance polling between steps. Implemented.
- **First-class receipt-token field in OnChainOS.** Once `defi detail`
  returns the receipt token explicitly, the hardcoded composability
  map can be retired in favor of dynamic discovery across ALL
  protocols, not just curated LSTs.
- **3+ step loops.** Pendle PT/YT, Convex/Aura layers, leveraged loops.
- **Risk-adjusted combined APY.** Net out gas + slippage + protocol
  concentration + smart-contract age. Combined-APY-as-upper-bound
  caveat goes away.
- **Yield rotation across DIFFERENT base assets.** Atomic redeem-A +
  swap to B + deposit-into-B-product when a non-trivial APY edge
  exists net of swap costs.
- **Python `monitor(state) → (alerts, actions)` callback.** Same shape
  PM uses for live strategies, adapted to DeFi primitives.
- **IL monitoring.** For LP positions, track impermanent loss against
  hold-baseline via `defi depth-price-chart`, halt at user-configured
  threshold.
- **Cross-chain comparison.** Same token, multiple chains, surface
  arbitrage-class differences.
- **Dashboard.** Read-only web UI sibling of PM's (`/api/snapshot`,
  `/api/positions`, `/api/opportunities`, `/api/alerts/pending`).

## License

MIT.
