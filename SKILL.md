# defi-strategist SKILL.md

Strategy + observability layer over OnChainOS DeFi primitives. Wraps
`onchainos defi *` + `wallet contract-call` in a watch loop, rules
engine, dynamic loop discoverer, risk scorer, and multi-step
executor with rollback. Reads + writes; default-safe (no on-chain
action without explicit `--live`).

This document is for agents + skill registries invoking the skill
programmatically. Humans should start with `README.md` and
`SUBMISSION.md`.

## Status

v0.3.0. Submitted to the OKX Agentic Trading Contest Skill Quality
Award (May 2026). Companion to
[portfolio-manager](https://github.com/paulomcg/portfolio-manager) +
[strategy-backtester](https://github.com/paulomcg/strategy-backtester).

## Invocation

```sh
bin/defi-strategist [--format json|table] <subcommand> [args...]
```

Global flags:

- `--format json` (default) — emits `{"ok": true, "result": {...}}`
  envelopes on stdout, suitable for agent / script consumers
- `--format table` — emits fixed-width human-readable tables; per-cycle
  watch records and final summaries both format cleanly

Errors: stderr line `FAILED: <category> <detail>`, exit non-zero.

Error vocabulary (stable, machine-parseable categories):
- `cli_not_found <bin>` — `onchainos` binary not on PATH
- `cli_timeout <argv>` — subprocess exceeded timeout_sec
- `wallet_not_logged_in` — OnChainOS auth failure (run `onchainos wallet login`)
- `cli_error <detail>` — non-zero exit from OnChainOS, detail is stderr tail
- `cli_output_invalid <reason>` — OnChainOS returned non-JSON stdout
- `api_error <detail>` — OnChainOS returned `{ok: false, error: ...}`
- `rules_not_found <path>`, `rules_config_invalid <reason>`
- `live_requires_address`, `live_requires_chains`
- `submit_calldata_missing`, `submit_to_missing` (write-path)
- `loop_not_found <id>`, `loop_empty`
- `step{N}_failed`, `step{N}_skipped` (loop execution)

## Subcommands

### `scan` — flat opportunity discovery

```sh
defi-strategist scan \
  --tokens <csv> \
  [--platforms <csv>] \
  [--chains <csv>] \
  [--product-group SINGLE_EARN|DEX_POOL|LENDING] \
  [--top 20]
```

Aggregates `defi search` results across the cartesian product of tokens
× chains × platforms. Sorts by APY desc, dedups by (platform, name,
chain). No wallet required.

JSON result: `{count: int, products: [normalized_product...]}`. Each
normalized product: `{investment_id, name, platform, platform_id,
chain, apy_pct, tvl_usd, product_type, raw}`.

### `positions` — wallet snapshot

```sh
defi-strategist positions --address <wallet> --chains <csv>
```

Calls `defi positions --address X --chains a,b,c`. Returns both the
raw response and a flattened per-position list. JSON result:
`{address, chains, positions_raw, positions_flat: [...]}`.

### `discover` — composable loop enumeration (with optional risk scoring)

```sh
defi-strategist discover \
  --token <base_asset> \
  --chains <csv> \
  [--max-steps 3] \
  [--max-products-per-chain 200] \
  [--top 10] \
  [--min-tvl 100000] \
  [--min-step-apy 0.5] \
  [--composed-only | --single-step-only] \
  [--with-risk [--min-risk-score N]] \
  [--legacy-receipt-map]
```

**Default mode** builds a dynamic token-edge graph from OnChainOS
`defi detail`'s first-class `lpToken` + `underlyingToken` fields,
then enumerates loops of length 1..max_steps starting from `--token`
on each chain. Loop edges = products; loop nodes = tokens. Cycle
detection prevents revisiting the same token.

**`--with-risk`** opt-in: for each loop, fetch `defi rate-chart` +
`defi tvl-chart` per step, compute APY-volatility + TVL-stability,
combine into per-step composite 0-100 score. Loop score = `min(per-step)`
— weakest-link aggregation. `--min-risk-score N` filters out loops
below the threshold.

**`--legacy-receipt-map`** falls back to v0.2's hardcoded 7-entry map
(diagnostic only).

JSON result:
```json
{
  "count_total": 8,
  "count_returned": 8,
  "graph": {"mode": "dynamic-graph", "nodes": 6, "edges": 7, "chains": ["solana"]},
  "loops": [
    {
      "loop_id": "4b8b976e9880",
      "base_asset": "SOL",
      "chain": "solana",
      "step_count": 2,
      "combined_apy_pct": 6.20,
      "notes": "Combined APY is the naive sum...",
      "steps": [
        {"investment_id": "22005", "platform": "Marinade Finance", "chain": "solana",
         "input_token": "SOL", "output_token": "mSOL", "apy_pct": 6.20, "tvl_usd": 273523849.10,
         "raw": {...}},
        {"investment_id": "...", "platform": "Solayer", "chain": "solana",
         "input_token": "mSOL", "output_token": "smSOL", "apy_pct": 0.0, "tvl_usd": 944624.0,
         "raw": {...}}
      ],
      "risk": {
        "loop_score": 50.0,
        "weakest_step": {"platform": "Solayer", "investment_id": "...",
                         "composite_score": 50.0, "data_quality": "none"},
        "per_step": [...]
      }
    }
  ]
}
```

`loop_id` is a stable hash of the step inventory — same composition
gets the same id across runs, so `run-loop --loop-id X` after a
separate `discover` invocation works.

### `run-loop` — execute a discovered composition

```sh
defi-strategist run-loop \
  --loop-id <id> \
  --token <base_asset> \
  --chains <single_chain> \
  --address <wallet> \
  --amount-minimal-units <int_str> \
  [--live] \
  [--poll-interval 5.0] \
  [--poll-timeout 60.0]
```

Re-runs discovery internally with permissive filters to find the
`--loop-id`, then steps through it via `DefiExecutor`. Three modes:

- (default, no `--live`) — `dry-run-actions`: builds calldata for each
  step via `defi invest`, does NOT submit. Returned fills have
  `submitted: False` and carry the prepared calldata for inspection.
- (with `--live`) — for each step: builds calldata, submits via
  `wallet contract-call`. Between steps 2..N, polls `defi positions`
  for the previous step's receipt-token balance and uses the actual
  on-chain amount for the next deposit (NOT user input — protects
  against partial-fill over-deposit). Poll timeout `--poll-timeout`
  default 60s, interval `--poll-interval` default 5s.

**Rollback on partial failure**: when step N fails after steps 1..N-1
succeeded, the executor walks back through completed legs in reverse
order, emitting best-effort `defi redeem --ratio 1` calldata for each
to exit the intermediate positions. Rollback fills land in
`fill.rollback_fills` (separate from forward `fill.fills`). Rollback
failures are logged in `errors` but DO NOT abort the rollback chain —
we try to exit every position.

JSON result:
```json
{
  "loop": {...full loop dict...},
  "fill": {
    "loop_id": "...",
    "address": "...",
    "chain": "solana",
    "base_amount_minimal_units": "1000000",
    "submitted_count": 0,
    "completed": true,
    "fills": [{step_meta, action, submitted, calldata, ...}, ...],
    "rollback_fills": [],
    "errors": []
  },
  "mode": "dry-run-actions"
}
```

### `watch` — continuous monitor loop with rules + optional actions

```sh
defi-strategist watch \
  --config <rules.yaml> \
  [--address <wallet>] \
  [--chains <csv>] \
  [--tokens <csv>] \
  [--platforms <csv>] \
  [--interval 60] \
  [--iterations N] \
  [--live | --dry-run-actions] \
  [--max-actions-per-cycle 5]
```

Polls positions + opportunities every `--interval` seconds, evaluates
the rules in `--config`, emits structured alerts AND optional actions
(when an executor is configured). Three execution modes:

- (default) — monitor mode: alerts only, actions inert even if rules
  emit them
- (`--dry-run-actions`) — build calldata for rule-emitted actions, do
  NOT broadcast
- (`--live`) — broadcast actions via the same `DefiExecutor` path as
  `run-loop`

`--max-actions-per-cycle N` caps actions executed per cycle (default
5) so a misfiring rule can't burn budget through hundreds of
round-trips.

Per-cycle output (one line of JSON per cycle in `--format json`):
```json
{
  "cycle_id": "...",
  "cycle_index": 0,
  "ts_utc": "...",
  "positions": [...flattened user positions...],
  "opportunities_count": 26,
  "alerts": [{"severity", "kind", "message", "context"}, ...],
  "actions": [{verb, rule_id, investment_id, ...}, ...],
  "fills": [...executor results when --live or --dry-run-actions...],
  "errors": []
}
```

Audit log entries (`state/audit.jsonl`): `monitor.start`, one
`monitor.cycle` per iteration, `monitor.end`.

### `audit` — replay recent audit lines

```sh
defi-strategist audit --limit 20
```

Reads back the last N events from `state/audit.jsonl` (override path
via `DEFI_STRATEGIST_AUDIT_PATH` env var).

## Rules config schema

YAML file with `chains`, optional `watch_tokens` / `watch_platforms`,
and a `rules` list. Four built-in rule types:

```yaml
name: my-monitor
chains: [solana, ethereum]
watch_tokens: [USDC, USDT]
watch_platforms: [Aave V3]   # optional

rules:
  # ALERT RULES (always emitted; never trigger actions)
  - id: yields-falling
    type: min_apy_floor
    threshold_pct: 3.0       # held APY below this triggers alert
    severity: warn           # info | warn | crit

  - id: protocol-concentration
    type: max_protocol_concentration
    threshold_pct: 50.0      # single platform > this % of DeFi value
    severity: warn

  - id: better-yield-elsewhere
    type: opportunity_above
    threshold_pct: 8.0       # opp APY above this triggers alert (if not held)
    severity: info

  # ACTION RULE (emits claim + optional reinvest when --live)
  - id: compound-pending-rewards
    type: auto_compound
    min_rewards_usd: 5.0     # don't bother claiming if rewards < $5 (gas)
    reinvest: true           # also emit reinvest action after claim
    severity: info
```

## Env vars

- `OKX_API_KEY` / `OKX_SECRET_KEY` / `OKX_PASSPHRASE` — passed through
  to the underlying `onchainos` CLI. defi-strategist never reads,
  logs, or persists them. Grep verified: no `os.environ.get("OKX_*")`
  outside this paragraph.
- `DEFI_STRATEGIST_AUDIT_PATH` — override audit log path (default
  `state/audit.jsonl` relative to the skill root).

## Programmatic embedding

```python
from scripts.discoverer import discover_loops_v3, attach_risk_scores
from scripts.executor import DefiExecutor
from scripts.loop_executor import LoopExecutor
from scripts.onchain_defi import DefiAdapter
from scripts.watch import run_monitor

# Discover + risk-score loops
adapter = DefiAdapter(cache_ttl_sec=60)
loops, graph = discover_loops_v3(
    base_asset="SOL", chains=["solana"], adapter=adapter,
    max_steps=3, min_tvl_usd=100_000, min_step_apy_pct=0.5,
)
attach_risk_scores(loops, adapter)
for lp in loops[:5]:
    print(lp.loop_id, lp.combined_apy_pct, (lp.risk or {}).get("loop_score"))

# Execute the safest loop above 5% APY
candidates = [lp for lp in loops if lp.combined_apy_pct >= 5.0]
best = max(candidates, key=lambda lp: (lp.risk or {}).get("loop_score", 0))
executor = DefiExecutor(address="...", chain="solana", dry_run=False)  # LIVE!
le = LoopExecutor(loop=best, executor=executor, adapter=adapter)
fill = le.run(amount_minimal_units="1000000")
# fill.completed, fill.submitted_count, fill.fills, fill.rollback_fills

# Or run monitor mode
summary = run_monitor(
    address="0x...", chains=["solana"], watch_tokens=["USDC"],
    watch_platforms=[],
    rules_config=[
        {"id": "high-apy", "type": "opportunity_above", "threshold_pct": 5.0}
    ],
    interval_seconds=60, iterations=10, adapter=adapter,
)
```

## Safety model

Three-stage opt-in progression for write actions:

| Stage | CLI flag | Behavior |
|---|---|---|
| Monitor (default) | (no flag) | Rules emit, actions recorded only, no OnChainOS write calls |
| Dry-run | `--dry-run-actions` | Action calldata BUILT via `defi invest/claim`, NOT submitted |
| Live | `--live` | Action calldata built AND submitted via `wallet contract-call` |

`--live` requires `--address` + non-empty `--chains` (fails fast with
`live_requires_address` / `live_requires_chains`). Per-cycle action
cap default 5. Loop executor's step 2..N amounts derived from polled
on-chain receipt balance, not user input. Rollback fires on any
step-N failure.

## Honest limits (v0.3.0)

- **Live multi-step execution untested end-to-end** against a real
  OKX agentic wallet because the development wallet had no DeFi
  position to seed from. Argv construction, calldata round-trip,
  dry-run, receipt-balance polling, and rollback logic are all
  unit-tested. The full `defi invest` → `wallet contract-call` chain
  with real funds and a real position has not fired.
- **Rollback is best-effort, not atomic.** True atomicity needs
  smart-contract-level batching with revert semantics, deferred to
  a future v0.4 once OnChainOS multicall support is understood.
- **Risk scoring covers APY volatility + TVL stability only.** No
  audit status, oracle dependence, governance risk, lockup/unbond
  delays, or sustainable-vs-inflationary emissions. Each is a
  future axis.
- **Dynamic graph discovery bounded** by `--max-products-per-chain`
  (default 200). Very large catalogs (Ethereum has 2000+ products
  across all platforms) may need cap increases to surface every
  composition.
- **No cross-chain loops.** Discovery is intra-chain. Cross-chain
  bridging adds risk (bridge hacks) deferred to v0.4.
- **Combined APY is the naive sum.** Doesn't net out gas, slippage,
  or risk premium. Every multi-step loop carries a `notes` field
  saying so. A future v0.4 will model net APY with holding-period
  amortization.
- **No Python `monitor(state) → actions` callback yet.** The roadmap
  has it (matches PM's strategy hook); v0.3 ships declarative rules
  only.

## Tests

85 tests across rules engine, adapter helpers, executor argv
construction, composability map, dynamic graph builder + cycle/budget
semantics, N-step loop executor with rollback paths, and risk scoring
math. Run `python3 -m pytest tests/ -q` after install.

## License

MIT.
