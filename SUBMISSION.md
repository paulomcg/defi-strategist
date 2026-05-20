# Submission — OKX Agentic Trading Contest, Skill Quality Award Track

**Skill name:** `defi-strategist`
**Submitted by:** Paulo Goncalves
**OnChainOS as primary data + trading source:** ✅ (every API call is a
subprocess of `onchainos defi *`)
**Status:** v0.3.0 — dynamic graph discovery + risk scoring + N-step execution + rollback. Companion to
[`portfolio-manager`](https://github.com/paulomcg/portfolio-manager)
+ [`strategy-backtester`](https://github.com/paulomcg/strategy-backtester)

This skill exists because of a gap I noticed mid-contest: OnChainOS
ships best-in-class DeFi primitives (search, positions, calldata,
historical APY/TVL) but no skill that does the strategy / observability
layer on top — when to rotate, when to compound, when to halt because
concentration ran away. `defi-strategist` is the v0.1 of that layer.

---

## 1. Strategy completeness

The strategy surface is a **declarative rules engine** with four built-
in rule types covering the most common DeFi monitoring + automation
needs. The pure-function `evaluate()` returns `(alerts, actions)` —
v0.1 callers can use just the alerts, v0.1.5 callers consume the
actions to drive write operations.

| What | Where |
|---|---|
| Pure-function rules engine: `(positions, opportunities, rules) → (alerts, actions)` | `scripts/rules.py:evaluate` |
| `min_apy_floor` rule — alert when held APY drops below threshold | `scripts/rules.py:_min_apy_floor` |
| `max_protocol_concentration` rule — alert when one platform exceeds X% of DeFi value | `scripts/rules.py:_max_concentration` |
| `opportunity_above` rule — surface yield-rotation candidates not already held | `scripts/rules.py:_opportunity_above` |
| `auto_compound` rule — emit `claim` (and optional `reinvest`) actions when pending rewards exceed threshold | `scripts/rules.py:_auto_compound` |
| Composable loop discoverer — enumerate 1-step through N-step yield compositions across protocols (LST → restake, lending → collateral, etc.) | `scripts/discoverer.py:discover_loops_v3` |
| **Dynamic graph builder** — derives token-edge graph from OnChainOS `defi detail`'s lpToken/underlyingToken fields. No hardcoded receipt map needed; new protocols surface automatically | `scripts/graph.py:build_graph` |
| **Risk scoring engine** — APY volatility (`rate-chart`) + TVL stability (`tvl-chart`) → 0-100 composite. Loop score = weakest-link min | `scripts/risk.py` |
| Receipt-token map — kept as v0.2 fallback (override path via `--legacy-receipt-map`) | `scripts/composability.py` |
| N-step loop executor with receipt-balance polling between steps in live mode | `scripts/loop_executor.py:LoopExecutor.run` |
| **Rollback on partial failure** — best-effort `defi redeem` walk-back of completed legs in reverse order; failures during rollback are logged but don't abort the cleanup chain | `scripts/loop_executor.py:LoopExecutor._rollback` |
| Three example rule configs: scan-only, portfolio-health, auto-compound | `examples/rules/` |
| Six CLI verbs: `watch`, `positions`, `scan`, `audit`, `discover`, `run-loop` | `scripts/cli.py` |

**Evidence — live opportunity scan:**

```sh
$ defi-strategist scan --tokens USDC --chains solana --top 5
# returns Kamino Main Pool 7.44% / Kamino 6.47% / Jupiter 4.72% on USDC,
# sorted by APY, deduped across platforms.
```

**Evidence — alert evaluation:**

```sh
$ defi-strategist watch --config examples/rules/stablecoin-yield-watch.yaml --iterations 1
# fires: opportunity_above_threshold for Kamino Main / USDC @ 7.44%,
# Kamino / USDC @ 6.47%, Morpho / Gauntlet DAI Core @ 5.66%
```

**Evidence — write-action pipeline (v0.1.5):**

```sh
# Three-stage progression, each gated by an explicit flag:
$ defi-strategist watch --config rules.yaml --iterations 1
# (default: monitor — actions inert)

$ defi-strategist watch --config rules.yaml --address $W --dry-run-actions
# (build calldata, do NOT broadcast — safety smoke test)

$ defi-strategist watch --config rules.yaml --address $W --live
# (broadcast via wallet contract-call)
```

**Evidence — v0.3 dynamic discovery + risk scoring on real OnChainOS data:**

```sh
$ defi-strategist discover --token SOL --chains solana --max-products-per-chain 8 \
    --max-steps 3 --top 5 --min-step-apy 0 --with-risk
# graph mode=dynamic-graph nodes=6 edges=7 loops_total=7
#   combined=7.26%  risk=57.4/100  weakest=Kamino dq=partial
#   combined=6.20%  risk=98.3/100  weakest=Marinade Finance dq=partial
#   combined=6.18%  risk=85.3/100  weakest=Kamino dq=partial
#   combined=5.61%  risk=92.2/100  weakest=Jito dq=partial
#   combined=5.27%  risk=61.9/100  weakest=Kamino dq=partial
# — Marinade scored 98.3/100, Jito 92.2 — based on real APY history
#   from `defi rate-chart`, NOT made up.
# — Kamino pools score 57-85 because their APY/TVL is more volatile.
# — Discovery itself: no hardcoded receipt map — the SOL→mSOL,
#   SOL→JitoSOL, etc. edges were derived live from `defi detail`'s
#   first-class lpToken/underlyingToken fields.

$ defi-strategist discover --token SOL --chains solana --max-products-per-chain 20 \
    --max-steps 3 --composed-only --min-step-apy 0
# also surfaces 3-step compositions when product cap is large enough,
# e.g. SOL → Marinade → mSOL → Solayer → smSOL (the previously-hidden
# Solayer-staked-mSOL receipt symbol the v0.2 hardcoded map didn't know)

$ defi-strategist run-loop --loop-id <id> --token SOL --chains solana \
    --address $W --amount-minimal-units 1000000
# dry-run by default: builds calldata for each step via OnChainOS,
# completed=True, fills=[...per-step calldata...]
# On step N failure: rollback_fills=[...best-effort defi-redeem
# calldata for each completed leg in reverse order...]
```

---

## 2. Risk control framework

Risk in DeFi has two shapes:
- **Yield risk** — yields decay (APY falls), concentration drifts (one
  protocol silently eats the portfolio), rotation latency (a better
  product exists and you didn't notice).
- **Action risk** — once write actions are wired in, an misfiring rule
  could drain gas or move funds incorrectly. Three explicit safety
  gates address this.

| Control | Where | Why it matters |
|---|---|---|
| `min_apy_floor` rule | `scripts/rules.py` | Catches yields that decay below the user's reservation rate |
| `max_protocol_concentration` rule | `scripts/rules.py` | Single-protocol blow-up risk (e.g. a lending platform hack) |
| `opportunity_above` rule | `scripts/rules.py` | Catches the *cost* of inaction — money sitting at 3% when 7% is available |
| **Default-inert actions** — without `--live`, actions are recorded only, never sent to OnChainOS | `scripts/cli.py:cmd_watch` | Most powerful safety guard: a misconfigured rules file can't move funds |
| **`--dry-run-actions`** — build calldata via OnChainOS but DO NOT broadcast | `scripts/cli.py`, `scripts/executor.py:dry_run` | Smoke-test that calldata is well-formed before the first live run |
| **`--max-actions-per-cycle`** — cap actions per cycle (default 5) | `scripts/watch.py` | A rule that misfires emitting 100 actions still spends at most N gas units per cycle |
| `--live` REQUIRES `--address` — fails fast | `scripts/cli.py:cmd_watch` | Refuses to construct an executor without explicit wallet context |
| `min_rewards_usd` floor on `auto_compound` — won't claim below threshold | `scripts/rules.py:_auto_compound` | Protects against silly-small claims where gas exceeds reward |
| Per-cycle error capture; per-action error short-circuits the batch | `scripts/watch.py` | One failing action doesn't cascade through the rest of a cycle |
| Adapter timeouts + auth error normalization (`wallet_not_logged_in`) | `scripts/onchain_defi.py`, `scripts/executor.py` | Operators get a clear error instead of a stack trace |

---

## 3. Execution reliability

v0.1.5 has both a read and a write path. Both share the same
subprocess-wrapper safety story.

### Read path (always active)

| What | Where |
|---|---|
| OnChainOS subprocess wrapper with timeout, return-code check, stderr-aware error parsing | `scripts/onchain_defi.py:_run` |
| Per-call caching with configurable TTL — a watch loop polling every 60s doesn't burn 60 API calls/min | `scripts/onchain_defi.py:_cached` |
| Response-shape normalization across endpoints (`investmentList` / `list` / `items` variants) | `scripts/onchain_defi.py:_extract_list` |
| Position flattening that handles empty (`assetStatus: 1`), nested, and platform-list response shapes | `scripts/watch.py:_flatten_positions` |
| APY ratio → percent conversion at adapter boundary, so rules use stable units | `scripts/onchain_defi.py:_pct` |

### Write path (--live / --dry-run-actions)

| What | Where |
|---|---|
| Two-step pipeline: `defi {claim,invest}` → `wallet contract-call` | `scripts/executor.py:_build_and_submit` |
| Solana / EVM dispatch — uses `--unsigned-tx` for SOL, `--input-data` for EVM, automatically | `scripts/executor.py:_submit_argv` |
| `_extract_submit_fields` handles flat, nested-under-txData/transaction, and list-of-txs response shapes | `scripts/executor.py` |
| `_extract_tx_hash` covers `txHash`, `transactionHash`, `orderId`, `hash` variants | `scripts/executor.py` |
| Auth error mapped to `wallet_not_logged_in` (same vocabulary as read path) | `scripts/executor.py:_run` |
| Refuses to submit if `to` is missing for EVM input_data, or if neither `input_data` nor `unsigned_tx` was extracted | `scripts/executor.py` |
| Loop executor polls `defi positions` between steps to get the actual receipt-token balance, protects against partial-fill over-deposit in subsequent legs | `scripts/loop_executor.py:_wait_for_receipt` |
| **Rollback on any-step failure** — walks completed legs in reverse, emits `defi redeem` calldata for each. Rollback failures don't abort the chain — we try to exit every position. | `scripts/loop_executor.py:_rollback` |
| Self-loop / cycle filter — N-step traversal can't revisit the same token (avoid graph cycles that would mint duplicates) | `scripts/graph.py:find_loops` |
| Combined APY is the naive sum and labeled as such — every multi-step loop carries a `notes` field stating it's an upper bound | `scripts/discoverer.py:_mk_loop` |
| Per-product risk score cache so the same product across multiple loops only gets chart-fetched once | `scripts/discoverer.py:attach_risk_scores` |
| **85 tests** covering rules + adapter + executor + composability + discoverer + dynamic graph builder + N-step loop executor with rollback + risk scoring — all green | `tests/` |

**Evidence:** the live scan returns 26 opportunities across 3 stablecoins
on Solana+Ethereum in a single cycle, normalized into a stable shape and
sorted by APY. See README.md quickstart for the reproducible command.

---

## 4. User safety + onboarding experience

| What | Where |
|---|---|
| Quickstart in README: 4 commands from auth → scan → watch → audit | `README.md` |
| **Three-stage opt-in progression** — monitor (default) → `--dry-run-actions` (build but don't broadcast) → `--live` (broadcast). Each stage is opt-in via an explicit flag; no implicit upgrades. | `scripts/cli.py:cmd_watch` |
| `OKX_API_KEY` / `OKX_SECRET_KEY` / `OKX_PASSPHRASE` read by underlying `onchainos` CLI only — `defi-strategist` never reads, logs, or persists secrets (grep verified) | `scripts/onchain_defi.py` (no env var reads) |
| Plugin manifest for skill registry | `plugin.yaml` |
| Clear error vocabulary: `cli_not_found`, `cli_timeout`, `wallet_not_logged_in`, `cli_error`, `cli_output_invalid`, `api_error`, `rules_not_found` | `scripts/onchain_defi.py`, `scripts/cli.py` |
| Two example rule configs covering both "I have no DeFi positions yet, just scan opportunities" and "I'm actively yield-farming, watch my health" | `examples/rules/` |
| `--iterations N` cap for safe smoke tests | `scripts/cli.py` |

---

## 5. Observability

Two surfaces, both writing the same source-of-truth records:

| What | Where |
|---|---|
| Per-cycle JSON record on stdout — cycle index, positions snapshot, opportunities count, alerts list, errors | `scripts/watch.py` |
| Append-only audit log (`state/audit.jsonl`) with `monitor.start`, `monitor.cycle`, `monitor.end` event types | `scripts/audit.py` |
| `defi-strategist audit --limit N` to replay recent audit lines | `scripts/cli.py:cmd_audit` |
| Structured alerts: `severity` (info/warn/crit), machine-readable `kind`, human-readable `message`, full `context` dict | `scripts/rules.py:Alert` |
| JSON result envelope (`{"ok": true, "result": ...}`) on every one-shot command for programmatic consumers | `scripts/cli.py:_ok` |

**Evidence — a real audit cycle (`monitor.cycle` event):**

```json
{
  "event": "monitor.cycle",
  "cycle_index": 0,
  "ts_utc": "2026-05-20T13:XX:XX...",
  "positions": [],
  "opportunities_count": 26,
  "alerts": [
    {
      "severity": "info",
      "kind": "opportunity_above_threshold",
      "message": "opportunity: Kamino / Main Pool / USDC @ 7.44% (threshold 5.00%)",
      "context": {...}
    }
  ],
  "errors": []
}
```

---

## Bundled with two companion skills

| Skill | URL | Role |
|---|---|---|
| `portfolio-manager` | https://github.com/paulomcg/portfolio-manager | Live + monitor spot trading, strategy hook, rule engine, executor, kill-switches |
| `strategy-backtester` | https://github.com/paulomcg/strategy-backtester | Historical OHLCV replay for PM strategies, deterministic, interactive HTML report |
| `defi-strategist` (this) | https://github.com/paulomcg/defi-strategist | DeFi opportunity scanning + position monitoring + alerts |

Same architectural shape across all three: watch loop → rules engine
→ audit + stdout → CLI. One mental model, three problem domains.

## License

MIT.
