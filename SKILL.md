# defi-strategist SKILL.md

A skill that wraps OnChainOS DeFi primitives in a strategy +
observability layer. v0.1 is **read-only monitor mode** — opportunity
scanning, position polling, and alert evaluation against user-defined
rules. v0.2 (roadmap) adds an executor + Python `monitor()` callback.

This document is for agents and skill registries that need to invoke
the skill programmatically. Humans should start with `README.md`.

## Invocation

```sh
bin/defi-strategist <subcommand> [args...]
```

All commands print a JSON result on stdout. Errors print `FAILED: <reason>`
to stderr and exit non-zero.

## Subcommands

### `scan` — opportunity discovery

```sh
defi-strategist scan \
  --tokens USDC,USDT,DAI \
  --chains solana,ethereum,base \
  [--platforms Aave,Morpho] \
  [--product-group SINGLE_EARN|DEX_POOL|LENDING] \
  [--top 20]
```

Returns the top `--top` opportunities across the queried tokens/platforms,
sorted by APY descending, deduped by (platform, name, chain).

Result shape:
```json
{
  "ok": true,
  "result": {
    "count": 26,
    "products": [
      {
        "investment_id": "...",
        "name": "USDC",
        "platform": "Kamino / Main Pool",
        "platform_id": "...",
        "chain": "Solana",
        "apy_pct": 7.44,
        "tvl_usd": 148868451.0,
        "product_type": "...",
        "raw": { ... source payload ... }
      }
    ]
  }
}
```

### `positions` — one-shot wallet snapshot

```sh
defi-strategist positions --address <wallet> --chains solana,ethereum
```

Returns both the raw response and a flattened per-position list.

### `watch` — monitor loop

```sh
defi-strategist watch \
  --config <rules.yaml> \
  [--address <wallet>] \
  [--chains <list>] \
  [--tokens <list>] \
  [--platforms <list>] \
  --interval 60 \
  [--iterations 50]
```

Each cycle emits a JSON line on stdout:
```json
{
  "cycle_id": "...",
  "cycle_index": 0,
  "ts_utc": "...",
  "positions": [...],
  "opportunities_count": 26,
  "alerts": [
    {"severity": "info", "kind": "opportunity_above_threshold", "message": "...", "context": {...}}
  ],
  "errors": []
}
```

And appends to `state/audit.jsonl` (override path via
`DEFI_STRATEGIST_AUDIT_PATH` env var).

### `audit` — recent audit lines

```sh
defi-strategist audit --limit 20
```

## Rules config schema

YAML file with `chains`, optional `watch_tokens` / `watch_platforms`, and
a `rules` list. Three built-in rule types:

```yaml
name: my-monitor
chains: [solana, ethereum]
watch_tokens: [USDC, USDT]
watch_platforms: [Aave V3]   # optional

rules:
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
```

## Env vars

- `OKX_API_KEY` / `OKX_SECRET_KEY` / `OKX_PASSPHRASE` — passed through
  to the underlying `onchainos` CLI. defi-strategist never reads, logs,
  or persists them.
- `DEFI_STRATEGIST_AUDIT_PATH` — override audit log path (default
  `state/audit.jsonl` relative to the skill root).

## Programmatic embedding

```python
from scripts.watch import run_monitor
from scripts.onchain_defi import DefiAdapter

summary = run_monitor(
    address="0x...",
    chains=["solana"],
    watch_tokens=["USDC"],
    watch_platforms=[],
    rules_config=[
        {"id": "high-apy", "type": "opportunity_above", "threshold_pct": 5.0}
    ],
    interval_seconds=60,
    iterations=10,
    adapter=DefiAdapter(cache_ttl_sec=120),
)
# {"ok": True, "iterations": 10, "alerts_total": 30, "interrupted": False}
```

## Limits in v0.1

- **No write actions.** `deposit` / `redeem` / `claim` are NOT exposed in
  this version. The architecture supports them; v0.2 will add the
  executor + decision hook (sketched in README roadmap).
- **No IL tracking.** LP positions are surfaced but impermanent loss is
  not computed. v0.2 plan: poll `defi depth-price-chart` and compute
  vs hold-baseline.
- **No cross-chain rotation.** Opportunities are surfaced per-chain;
  cross-chain comparison is a v0.2 feature.

## License

MIT.
