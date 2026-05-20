# Submission — OKX Agentic Trading Contest, Skill Quality Award Track

**Skill name:** `defi-strategist`
**Submitted by:** Paulo Goncalves
**OnChainOS as primary data + trading source:** ✅ (every API call is a
subprocess of `onchainos defi *`)
**Status:** v0.1.0, monitor-only — companion to
[`portfolio-manager`](https://github.com/paulomcg/portfolio-manager)
+ [`strategy-backtester`](https://github.com/paulomcg/strategy-backtester)

This skill exists because of a gap I noticed mid-contest: OnChainOS
ships best-in-class DeFi primitives (search, positions, calldata,
historical APY/TVL) but no skill that does the strategy / observability
layer on top — when to rotate, when to compound, when to halt because
concentration ran away. `defi-strategist` is the v0.1 of that layer.

---

## 1. Strategy completeness

For v0.1 (monitor-only), the strategy surface is a **declarative rules
engine** with three built-in rule types covering the most common DeFi
monitoring needs. A v0.2 Python `monitor(state) → list[Action]` callback
is in the roadmap (same pattern PM uses today for live strategies).

| What | Where |
|---|---|
| Pure-function rules engine: `(positions, opportunities, rules) → alerts` | `scripts/rules.py` |
| `min_apy_floor` rule — alert when held APY drops below threshold | `scripts/rules.py:_min_apy_floor` |
| `max_protocol_concentration` rule — alert when one platform exceeds X% of DeFi value | `scripts/rules.py:_max_concentration` |
| `opportunity_above` rule — surface yield-rotation candidates not already held | `scripts/rules.py:_opportunity_above` |
| Two example rule configs covering scan-only and portfolio-health patterns | `examples/rules/` |
| Three CLI verbs: `watch`, `positions`, `scan` | `scripts/cli.py` |

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

---

## 2. Risk control framework

Risk in DeFi looks different from risk in spot trading. Spot risk =
sudden price drops. DeFi risk = yield decay (APY falls below opportunity
cost), concentration drift (one protocol silently eats your portfolio),
and rotation latency (a better product appeared, you didn't notice).
v0.1 directly addresses all three.

| Control | Where | Why it matters |
|---|---|---|
| `min_apy_floor` rule | `scripts/rules.py` | Catches yields that decay below the user's reservation rate |
| `max_protocol_concentration` rule | `scripts/rules.py` | Single-protocol blow-up risk (e.g. a lending platform hack) |
| `opportunity_above` rule | `scripts/rules.py` | Catches the *cost* of inaction — money sitting at 3% when 7% is available |
| Per-cycle error capture (positions fetch failed, search failed) | `scripts/watch.py` | Failures don't kill the loop; subsequent cycles retry |
| Adapter timeouts + auth error normalization (`wallet_not_logged_in`) | `scripts/onchain_defi.py` | Operators get a clear error instead of a stack trace |
| Read-only by design in v0.1 | core architecture | Can't accidentally drain a position; safe to run against any wallet |

---

## 3. Execution reliability

v0.1 is read-only — no on-chain writes — so "execution" here means the
*data plumbing* doesn't lose its mind under real-world conditions.

| What | Where |
|---|---|
| OnChainOS subprocess wrapper with timeout, return-code check, stderr-aware error parsing | `scripts/onchain_defi.py:_run` |
| Per-call caching with configurable TTL — a watch loop polling every 60s doesn't burn 60 API calls/min | `scripts/onchain_defi.py:_cached` |
| Response-shape normalization across endpoints (`investmentList` / `list` / `items` variants) | `scripts/onchain_defi.py:_extract_list` |
| Position flattening that handles empty (`assetStatus: 1`), nested, and platform-list response shapes | `scripts/watch.py:_flatten_positions` |
| APY ratio → percent conversion at adapter boundary, so rules use stable units | `scripts/onchain_defi.py:_pct` |
| Auth error mapped to `wallet_not_logged_in` for operator clarity | `scripts/onchain_defi.py:_run` |

**Evidence:** the live scan returns 26 opportunities across 3 stablecoins
on Solana+Ethereum in a single cycle, normalized into a stable shape and
sorted by APY. See README.md quickstart for the reproducible command.

---

## 4. User safety + onboarding experience

| What | Where |
|---|---|
| Quickstart in README: 4 commands from auth → scan → watch → audit | `README.md` |
| **Read-only v0.1** — there is no `--live` flag; you cannot accidentally move funds. Write actions are explicitly deferred to v0.2 with the design sketched in README roadmap | core architecture |
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
