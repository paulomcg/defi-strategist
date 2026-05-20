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

## What you can ask the agent to do

Natural-language prompts the user says, and what the agent does
with them.

---

> **"Where should I park my idle USDC right now?"**

The agent scans every USDC-accepting product across Solana,
Ethereum, and Base via OnChainOS, ranks by APY, dedups across
protocols, and shows the user the top opportunities side-by-side.
Kamino Main Pool at 7.41%, Syrup at 4.88%, Aave V3 at 3.83% — the
agent surfaces all of them and lets the user pick.

---

> **"Find me a composable yield loop on SOL and tell me which is safest."**

The agent dynamically builds a token-edge graph from OnChainOS
DeFi data, walks it up to 3 steps deep, surfaces every loop
starting from SOL — both single-product yields AND multi-step
compositions (Marinade → Solayer restake, Jito → Solayer, etc.).
With risk scoring on, each loop gets a 0-100 score derived from
real APY-volatility + TVL-stability history.

> Marinade at 6.20% scores **98.3/100** (rock-steady APY).
> Kamino's top APY at 9.36% scores **29.2/100** (volatile).
> The agent shows the trade-off so the user chooses between APY
> and trustworthiness, not just chases the biggest number.

---

> **"Run that Marinade → Solayer loop with 0.1 SOL."**

The agent dry-runs first by default — builds the calldata for both
legs via OnChainOS, shows the user what's about to be broadcast,
no funds move. Once the user confirms, the agent re-runs in live
mode. The executor broadcasts step 1, polls the on-chain mSOL
balance until it confirms, then uses the ACTUAL received amount
(not the user's estimate) for step 2's Solayer deposit. If step 2
fails, the executor walks back through completed legs and emits
best-effort redeem calldata to exit the intermediate position.

---

> **"Auto-compound my rewards weekly when they exceed $10."**

User-authored rule:

```yaml
rules:
  - id: compound
    type: auto_compound
    min_rewards_usd: 10
    reinvest: true
```

The agent points `defi-strategist watch` at the wallet + rule.
Every cycle the rule fires if pending rewards > $10, the executor
builds the `claim` calldata (and a `reinvest` follow-up if rewards
came back as the underlying token), and `wallet contract-call`
signs + broadcasts via the Agentic Wallet's TEE-backed signer. The
simplest non-trivial DeFi loop, fully unattended.

---

> **"Watch my DeFi positions and alert me if any yield drops below 3%."**

```yaml
rules:
  - id: yields-falling
    type: min_apy_floor
    threshold_pct: 3.0
    severity: warn
  - id: protocol-concentration
    type: max_protocol_concentration
    threshold_pct: 50.0
    severity: warn
```

The agent starts a continuous monitor — no on-chain actions, just
polls positions + opportunities and fires structured alerts to the
audit log when any rule trips. The agent can come back later and
ask *"any new alerts?"* or *"what changed overnight?"* by tailing
the audit.

---

> **"Is there a better yield for my existing position anywhere?"**

The `opportunity_above` rule watches a list of tokens and surfaces
products above an APY threshold that the user doesn't already hold.
The agent gets a structured alert:

> *"Morpho Steakhouse Prime USD at 8.2% beats your current Kamino
> USDC at 6.45% — want me to rotate?"*

---

> **"Stop everything immediately — I need to think."**

The agent kills the watch process. Without `--live` no on-chain
action fires anyway; with `--live`, the loop stops at the next
interval boundary. There is no persisted "live mode on" state.

---

### Three-stage opt-in safety

| Stage | Flag | Behavior |
|---|---|---|
| **Monitor** (default) | (no flag) | Rules emit, actions recorded only, no OnChainOS write calls |
| **Dry-run** | `--dry-run-actions` | Action calldata built via OnChainOS, **NOT** broadcast |
| **Live** | `--live` | Calldata built AND broadcast via `wallet contract-call` |

`--max-actions-per-cycle N` caps actions per cycle (default 5) so a
misfiring rule can't burn budget through hundreds of round-trips.

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

### Wiring into an agent (Claude Code, Codex, custom harness)

| Method | Path |
|---|---|
| Drop into Claude Code's skills dir | `cp -r . ~/.claude/skills/defi-strategist/` then restart Claude |
| Point a custom agent at SKILL.md | parse YAML frontmatter; shell out to `bin/defi-strategist` per command |
| Register with the OKX Plugin Store | `plugin.yaml` schema_version: 1 |

Every command emits `{"ok": bool, "result": {...}}` JSON on stdout
(default, agent-friendly). Pass `--format table` for human-readable
output during direct CLI use. Errors print `FAILED: <category>
<detail>` to stderr with stable machine-parseable categories.

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
