# defi-strategist

**The strategy + observability layer over OnChainOS DeFi primitives.**

OnChainOS gives you the wheels — `defi list`, `defi search`,
`defi positions`, `defi rate-chart`, `defi deposit/redeem/claim`,
`wallet contract-call`. What's missing is the *driver*: discovering
composable yield loops across protocols, scoring their risk on real
APY-volatility + TVL-stability history, executing multi-step plans
with receipt-balance polling between legs, and walking back on
partial failure. `defi-strategist` is that driver.

---

## What you can do with it

### Find the best yield for a stablecoin across chains

```sh
defi-strategist --format table scan \
  --tokens USDC,USDT --chains solana,ethereum,base --top 10
```

```
opportunity scan — 10 products:

  platform               name                  apy             tvl
  ---------------------- -------------------- ------  --------------
  Kamino / Main Pool     USDC                  7.41%  $ 149,236,776
  Kamino                 USDC                  6.45%  $  32,117,289
  Syrup                  USDC                  4.88%  $1,395,711,039
  Jupiter                USDC                  4.72%  $ 449,002,741
  Fluid                  USDC                  4.65%  $ 201,515,883
  Spark / Main Pool      USDC                  4.55%  $  36,189,449
  Compound V3            USDC                  4.44%  $  10,375,118
  Morpho                 Steakhouse Prime USD  4.41%  $ 465,979,664
  Aave V3                USDC                  3.83%  $2,032,163,029
  Syrup                  USDT                  4.43%  $ 388,160,999
```

### Discover composable yield loops + score them by risk

```sh
defi-strategist --format table discover \
  --token SOL --chains solana \
  --max-steps 3 --top 8 --with-risk
```

```
discover — mode=dynamic-graph  graph: 8 tokens / 11 edges  loops: 8 (of 11 found)

  loop_id     risk     apy steps  composition
  ------------------------------------------------------------------------------
  6e9135f22c  29.2   9.36%     1  Kamino[SOL→LP] 9.36%
  4a818a8f1a  57.4   7.26%     1  Kamino[SOL→LP] 7.26%
  0a3c007064  98.3   6.20%     1  Marinade Finance[SOL→mSOL] 6.20%
  4b8b976e98  50.0   6.20%     2  Marinade Finance[SOL→mSOL] 6.20% → Solayer[mSOL→smSOL] 0.00%
  2ba6dd8315  85.3   6.18%     1  Kamino[SOL→LP] 6.18%
  6b1562f24a  92.2   5.61%     1  Jito[SOL→JitoSOL] 5.61%
  890b64a1df  50.0   5.61%     2  Jito[SOL→JitoSOL] 5.61% → Solayer[JitoSOL→sjitoSOL] 0.00%
  212a781032  61.9   5.27%     1  Kamino[SOL→LP] 5.27%

  risk: 0-100, higher = safer (weakest-link of per-step APY volatility + TVL stability)
```

The agent has surfaced both single-product yields AND the
"LST-then-restake" compositions Solana is famous for. **Marinade
scores 98.3/100** (very stable APY history) — that's a strong
risk-adjusted pick at 6.20%. **Kamino's top APY at 9.36% scores
29.2/100** — much more volatile, much less trustworthy. The user
chooses; the agent shows the trade-off honestly.

The receipt-token names (`smSOL`, `sjitoSOL`) were derived live from
OnChainOS `defi detail`'s `lpToken` field — nothing is hardcoded.

### Execute a discovered loop in dry-run, then live

Pick a `loop_id` from the discover output, then:

```sh
# DRY-RUN: build calldata via OnChainOS, do NOT broadcast
defi-strategist run-loop \
  --loop-id 4b8b976e98 --token SOL --chains solana \
  --address <your-solana-address> \
  --amount-minimal-units 1000000     # 0.001 SOL
```

```
run-loop — mode=dry-run-actions  2-step loop, combined APY 6.2%
  result: completed=True fills=2 submitted=0

  steps:
    [1] Marinade Finance   SOL      → mSOL     APY  6.20%  submitted=no
    [2] Solayer            mSOL     → mSOL     APY  0.00%  submitted=no
        note: dry-run: step2 uses base amount as placeholder
```

Once the calldata looks right, broadcast:

```sh
defi-strategist run-loop \
  --loop-id 4b8b976e98 --token SOL --chains solana \
  --address <your-solana-address> \
  --amount-minimal-units 1000000 \
  --live
```

In `--live` mode the executor:
1. Broadcasts step 1 via `wallet contract-call`
2. Polls `defi positions` for the receipt-token balance (e.g., mSOL)
3. Uses the **actual on-chain balance** for step 2's deposit amount
   (not the user-supplied estimate — protects against partial-fill
   over-deposit)
4. Broadcasts step 2
5. On any step failure, walks back through completed legs in reverse
   and emits best-effort `defi redeem` calldata for each to exit the
   intermediate positions. Rollback fills are recorded separately so
   the operator can distinguish forward progress from cleanup.

### Continuously monitor a wallet for yield rotation opportunities

```yaml
# portfolio-health.yaml
name: portfolio-health
chains: [solana, ethereum, base]
watch_tokens: [USDC, USDT, SOL, ETH]
rules:
  - id: yields-falling
    type: min_apy_floor
    threshold_pct: 3.0
    severity: warn
  - id: protocol-concentration
    type: max_protocol_concentration
    threshold_pct: 50.0
    severity: warn
  - id: better-yield-elsewhere
    type: opportunity_above
    threshold_pct: 8.0
    severity: info
```

```sh
defi-strategist watch --config portfolio-health.yaml \
  --address <your-wallet> --interval 3600
```

Polls positions + opportunities every hour, fires structured alerts
to stdout AND an append-only audit log when any rule trips. The agent
can come back later and pull the audit:

```sh
defi-strategist --format table audit --limit 10
```

### Auto-compound rewards across positions

```yaml
# auto-compound.yaml
name: auto-compound
chains: [solana]
rules:
  - id: compound-pending-rewards
    type: auto_compound
    min_rewards_usd: 5.0     # don't bother claiming if rewards < $5 (gas)
    reinvest: true           # claim + redeposit
```

```sh
# Dry-run first (build the calldata, don't broadcast)
defi-strategist watch --config auto-compound.yaml \
  --address <your-wallet> --dry-run-actions --interval 3600

# Then go live
defi-strategist watch --config auto-compound.yaml \
  --address <your-wallet> --live --interval 3600
```

When pending rewards exceed `min_rewards_usd`, the rule emits a
`claim` action (and a `reinvest` follow-up if configured). The
executor builds the calldata via `defi claim` + `defi invest` and
submits via `wallet contract-call`.

### Three-stage opt-in safety

| Stage | Flag | Behavior |
|---|---|---|
| Monitor (default) | (no flag) | Rules emit, actions recorded only, no OnChainOS write calls |
| Dry-run | `--dry-run-actions` | Action calldata built via OnChainOS, NOT broadcast |
| Live | `--live` | Calldata built AND broadcast via `wallet contract-call` |

`--max-actions-per-cycle N` caps actions per cycle (default 5) so a
misfiring rule can't burn budget through hundreds of round-trips.

### Use it from Claude Code / Codex / a custom agent

| Method | Path |
|---|---|
| Drop into Claude Code's skills dir | `cp -r . ~/.claude/skills/defi-strategist/` then restart Claude |
| Point a custom agent at SKILL.md | parse YAML frontmatter; shell out to `bin/defi-strategist` per command |
| Register with the OKX Plugin Store | `plugin.yaml` schema_version: 1 |

Every command emits `{"ok": bool, "result": {...}}` JSON on stdout
(default, agent-friendly). Pass `--format table` for human-readable
output. Errors print `FAILED: <category> <detail>` to stderr with
stable machine-parseable categories.

---

## Install

```sh
git clone https://github.com/paulomcg/defi-strategist.git ~/Projects/defi-strategist
cd ~/Projects/defi-strategist && ./install.sh
echo 'export PATH="$HOME/Projects/defi-strategist/bin:$PATH"' >> ~/.bashrc
defi-strategist --version
```

For real-wallet operations:

```sh
onchainos wallet login <your-email>
export OKX_API_KEY=... OKX_SECRET_KEY=... OKX_PASSPHRASE=...
```

`defi-strategist` itself never reads those env vars — only the
underlying `onchainos` CLI does.

---

## Architecture

```
                ┌─────────────────────────────────────────────────┐
                │  rules.yaml  (declarative thresholds + alerts)  │
                └─────────────────────────────────────────────────┘
                                      │
                                      ▼
            ┌───────────────────────────────────────────────────┐
            │  watch / discover / run-loop                      │
            │  (poll → normalize → evaluate → emit → execute)   │
            └───────────────────────────────────────────────────┘
              │              │              │              │
              ▼              ▼              ▼              ▼
       OnChainOS DATA    Token graph    Risk scorer    OnChainOS WRITE
       (defi list/        (build from   (rate-chart +   (defi claim/
        search/           defi detail    tvl-chart      invest →
        positions/        lpToken +      → 0-100        wallet
        rate-chart/       underlying-     composite      contract-call)
        tvl-chart)        Token)         score)
                                                          │
                                                          ▼
                                              ┌── audit log + stdout JSONL
                                              │   (per-cycle observability)
                                              │
                                       (live) ▼── Rollback executor
                                              │   (defi redeem on any
                                              │    partial-failure)
                                              ▼
                                     OnChainOS DeFi protocols
                                     (Marinade, Jito, Lido,
                                      Aave, Solayer, Morpho, ...)
```

### Core invariants

- **Dynamic graph discovery, not hardcoded receipt maps.** Edges come
  from OnChainOS `defi detail`'s first-class `lpToken` +
  `underlyingToken` fields. New protocols surface automatically.
  Cycle detection up to `--max-steps N` (default 3) starting from
  any base asset.
- **Risk scoring uses real history.** APY volatility = `stdev/mean`
  over `defi rate-chart`. TVL stability = `1 - stdev/mean` over
  `defi tvl-chart`. Loop score = `min(per-step)` — weakest-link
  aggregation. Products without historical data get a neutral 50
  with `data_quality: "none"`.
- **N-step execution with receipt-balance polling between legs.**
  Step 2..N's deposit amount comes from polling the on-chain
  receipt-token balance after the previous step settles. NOT a user
  estimate — protects against partial-fill over-deposit when
  intermediate steps slip.
- **Best-effort rollback on partial failure.** On step N failure
  with steps 1..N-1 already submitted, the executor walks back
  through completed legs in reverse and emits best-effort
  `defi redeem --ratio 1` calldata. Rollback failures DO NOT abort
  the chain — we try to exit every position.
- **Three-stage opt-in safety.** Default = monitor mode (no
  on-chain action ever). `--dry-run-actions` round-trips calldata
  without broadcasting. `--live` broadcasts. Each stage requires an
  explicit CLI flag.

### Six CLI verbs

| Verb | What it does |
|---|---|
| `scan` | Cross-chain yield discovery across N tokens/platforms; sorted by APY, deduped |
| `positions` | One-shot user DeFi snapshot across chains |
| `discover` | Enumerate composable yield loops (dynamic graph + optional `--with-risk` scoring) |
| `run-loop` | Execute a discovered loop step-by-step (default dry-run; `--live` to broadcast) |
| `watch` | Continuous monitor loop with declarative rules + optional auto-actions |
| `audit` | Replay recent events from the append-only audit log |

### Four built-in rule types

| Type | Triggers when | Action |
|---|---|---|
| `min_apy_floor` | held position's APY < `threshold_pct` | alert |
| `max_protocol_concentration` | one platform > `threshold_pct` of DeFi value | alert |
| `opportunity_above` | non-held product's APY > `threshold_pct` | alert |
| `auto_compound` | pending rewards > `min_rewards_usd` | claim (+ optional reinvest) |

### Files

| File | Role |
|---|---|
| `scripts/onchain_defi.py` | DATA adapter: `defi list/search/positions/rate-chart` wrappers, cache, response normalization |
| `scripts/executor.py` | WRITE adapter: `defi claim/invest` → `wallet contract-call` two-step pipeline, dry-run / live |
| `scripts/composability.py` | Curated `(chain, platform, deposit) → receipt` mappings (fallback for diagnostic mode) |
| `scripts/graph.py` | Dynamic token-edge graph builder from `defi detail`; N-step cycle detection |
| `scripts/discoverer.py` | Enumerates yield loops, ranks by combined APY, optionally attaches risk scores |
| `scripts/risk.py` | APY volatility + TVL stability scoring engine |
| `scripts/loop_executor.py` | Drives DefiExecutor through a loop's steps with receipt-balance polling + rollback |
| `scripts/rules.py` | Pure-function rules engine: `(positions, opportunities, rules) → (alerts, actions)` |
| `scripts/watch.py` | Monitor loop: poll → normalize → evaluate → emit + execute |
| `scripts/format.py` | Human-readable table formatters for `--format table` mode |
| `scripts/audit.py` | Append-only JSONL audit log |
| `scripts/cli.py` | CLI dispatcher |
| `examples/rules/` | Sample configs (scan-only, portfolio-health, auto-compound) |

### Tests

```sh
.venv/bin/pytest tests/ -q
```

Covers the rules engine, OnChainOS adapter helpers, executor argv
construction, dynamic graph builder, N-step loop executor with
rollback paths, and risk-scoring math (volatility + stability +
weakest-link aggregation).

---

## License

MIT — see [`LICENSE`](LICENSE).

## Disclaimer

DeFi yields are not guaranteed; LST-restaking adds smart-contract +
slashing risk on top of native staking risk. Risk scores are
heuristic and don't capture audit status, oracle dependence, or
governance risk. Test with `--dry-run-actions` before going `--live`
on any new wallet. The authors disclaim all liability for losses.
