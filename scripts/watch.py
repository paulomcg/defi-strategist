"""Watch loop — monitor mode.

Cycle body (every interval seconds):
    1. fetch user DeFi positions across configured chains
    2. fetch opportunities (search/list) on watched tokens
    3. normalize both into stable shapes
    4. evaluate rules → list of Alerts
    5. append cycle record to audit log (and stdout JSONL)
    6. sleep

Pure read-only. No on-chain actions. Future versions will add an
executor + decision hook for rotation / claim-compound / rebalance.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, TextIO
from uuid import uuid4

from . import audit, rules as rules_engine
from .executor import DefiExecutor, DefiExecutorError
from .onchain_defi import DefiAdapter, DefiError, normalize_product

# Optional formatter for per-cycle output. None = JSONL (default,
# backwards compatible). When set to "table", the loop emits a
# concise human-readable line per cycle instead.
_CYCLE_FORMAT = "json"


def set_cycle_format(fmt: str) -> None:
    """Module-level switch consumed by run_monitor's emission path."""
    global _CYCLE_FORMAT
    _CYCLE_FORMAT = fmt

_DEFAULT_INTERVAL = 60


class MonitorHalt(Exception):
    """Raised to terminate the loop cleanly mid-cycle (kill-switch trip)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_monitor(
    *,
    address: str | None,
    chains: list[str],
    watch_tokens: list[str],
    watch_platforms: list[str],
    rules_config: list[dict[str, Any]],
    interval_seconds: int = _DEFAULT_INTERVAL,
    iterations: int | None = None,
    sink: TextIO | None = None,
    sleep_fn=time.sleep,
    adapter: DefiAdapter | None = None,
    executor: DefiExecutor | None = None,
    max_actions_per_cycle: int = 5,
) -> dict[str, Any]:
    """Run the monitor loop. Returns a summary dict on exit.

    `address` may be None — in that case only opportunity scanning runs
    (no position fetch). Useful for "scan-only" mode where the user
    wants to surface yield opportunities without exposing a wallet.

    `executor` controls action handling:
      - None (default): actions emitted by rules are recorded only, never
        sent to OnChainOS. Pure monitor mode.
      - DefiExecutor instance (dry_run=True): calldata is BUILT for each
        action (a real OnChainOS round-trip), recorded for inspection,
        but never broadcast. Useful for verifying that a rule emits
        actions OnChainOS understands.
      - DefiExecutor instance (dry_run=False): calldata is built AND
        submitted via `wallet contract-call`. Real on-chain transactions
        fire. Requires `--live` on the CLI side.

    `max_actions_per_cycle` caps how many actions may execute per cycle
    (safety against a rule misfire emitting hundreds of actions).
    """
    sink = sink or sys.stdout
    adapter = adapter or DefiAdapter()
    cycles = 0
    alerts_total = 0
    actions_total = 0
    submitted_total = 0
    halt_reason: str | None = None
    interrupted = False

    def _on_sigint(signum, frame):  # noqa: ARG001
        nonlocal interrupted
        interrupted = True

    prev_sigint = signal.signal(signal.SIGINT, _on_sigint)
    audit.append({
        "event": "monitor.start",
        "address": address,
        "chains": chains,
        "watch_tokens": watch_tokens,
        "watch_platforms": watch_platforms,
        "rules_count": len(rules_config),
        "interval_seconds": interval_seconds,
        "iterations_cap": iterations,
        "executor": (
            {
                "address": executor.address,
                "chain": executor.chain,
                "dry_run": executor.dry_run,
            }
            if executor is not None
            else None
        ),
        "max_actions_per_cycle": max_actions_per_cycle,
    })
    try:
        while True:
            if interrupted:
                break
            cycle_id = uuid4().hex
            cycle = {
                "cycle_id": cycle_id,
                "cycle_index": cycles,
                "ts_utc": _now(),
                "errors": [],
            }
            positions = _fetch_positions(adapter, address, chains, cycle)
            opportunities = _fetch_opportunities(
                adapter, watch_tokens, watch_platforms, chains, cycle
            )
            cycle["positions"] = positions
            cycle["opportunities_count"] = len(opportunities)
            alerts, actions = rules_engine.evaluate(
                positions=positions,
                opportunities=opportunities,
                rules=rules_config,
            )
            cycle["alerts"] = [a.as_dict() for a in alerts]
            cycle["actions"] = [a.as_dict() for a in actions]
            alerts_total += len(alerts)
            actions_total += len(actions)

            # Execute actions if an executor was provided. Capped by
            # max_actions_per_cycle and short-circuited on any error
            # so a single failure doesn't cascade into the rest of the
            # batch.
            cycle["fills"] = []
            if executor is not None and actions:
                for i, act in enumerate(actions):
                    if i >= max_actions_per_cycle:
                        cycle["errors"].append({
                            "kind": "max_actions_per_cycle",
                            "detail": (
                                f"capped at {max_actions_per_cycle}; "
                                f"{len(actions) - i} actions skipped"
                            ),
                        })
                        break
                    try:
                        fill = _execute_action(executor, act)
                    except DefiExecutorError as e:
                        cycle["errors"].append({
                            "kind": "executor_error",
                            "verb": act.verb,
                            "rule_id": act.rule_id,
                            "detail": str(e),
                        })
                        break
                    cycle["fills"].append(fill)
                    if fill.get("submitted"):
                        submitted_total += 1

            if _CYCLE_FORMAT == "table":
                sink.write(_format_cycle_line(cycle) + "\n")
            else:
                sink.write(json.dumps(cycle, default=str) + "\n")
            sink.flush()
            audit.append({"event": "monitor.cycle", **cycle})
            cycles += 1

            if iterations is not None and cycles >= iterations:
                break
            if interrupted:
                break
            sleep_fn(interval_seconds)
    finally:
        signal.signal(signal.SIGINT, prev_sigint)

    summary = {
        "ok": True,
        "iterations": cycles,
        "alerts_total": alerts_total,
        "actions_total": actions_total,
        "submitted_total": submitted_total,
        "halted": halt_reason is not None,
        "halt_reason": halt_reason,
        "interrupted": interrupted,
    }
    audit.append({"event": "monitor.end", **summary})
    return summary


def _format_cycle_line(cycle: dict[str, Any]) -> str:
    """One-line per-cycle summary for --format table mode. Surfaces
    only what an operator scanning the output needs: cycle index,
    counts, top alerts. Full detail still lands in the audit log."""
    n_pos = len(cycle.get("positions") or [])
    n_opps = cycle.get("opportunities_count", 0)
    n_alerts = len(cycle.get("alerts") or [])
    n_actions = len(cycle.get("actions") or [])
    n_fills = len(cycle.get("fills") or [])
    n_errors = len(cycle.get("errors") or [])
    head = (
        f"cycle {cycle.get('cycle_index', '?')}: "
        f"positions={n_pos} opps={n_opps} alerts={n_alerts} "
        f"actions={n_actions} fills={n_fills} errors={n_errors}"
    )
    parts = [head]
    for a in (cycle.get("alerts") or [])[:5]:
        parts.append(
            f"  [{a.get('severity','?'):4s}] {a.get('kind','?')}: {a.get('message','')[:90]}"
        )
    for e in (cycle.get("errors") or [])[:3]:
        parts.append(f"  ERR [{e.get('kind','?')}] {str(e.get('detail',''))[:90]}")
    return "\n".join(parts)


def _execute_action(executor: DefiExecutor, action) -> dict[str, Any]:
    """Dispatch an Action to the right executor verb."""
    if action.verb == "claim":
        return executor.claim(
            investment_id=action.investment_id,
            platform_id=action.platform_id,
            reward_type=action.reward_type,
            expect_output=action.expect_output,
        )
    if action.verb == "reinvest":
        if not (action.investment_id and action.token and action.amount_minimal_units):
            raise DefiExecutorError(
                f"reinvest_missing_fields rule_id={action.rule_id}"
            )
        return executor.reinvest(
            investment_id=action.investment_id,
            token=action.token,
            amount_minimal_units=action.amount_minimal_units,
        )
    raise DefiExecutorError(f"unknown_action_verb {action.verb!r}")


# ---- helpers ----

def _fetch_positions(
    adapter: DefiAdapter,
    address: str | None,
    chains: list[str],
    cycle: dict[str, Any],
) -> list[dict[str, Any]]:
    if not address or not chains:
        return []
    try:
        data = adapter.positions(address=address, chains=",".join(chains))
    except DefiError as e:
        cycle["errors"].append({"kind": "positions_fetch", "detail": str(e)})
        return []
    return _flatten_positions(data)


def _fetch_opportunities(
    adapter: DefiAdapter,
    tokens: list[str],
    platforms: list[str],
    chains: list[str],
    cycle: dict[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    chain_args: list[str | None] = chains or [None]
    if tokens:
        for tok in tokens:
            for ch in chain_args:
                try:
                    items = adapter.search(token=tok, chain=ch)
                except DefiError as e:
                    cycle["errors"].append(
                        {"kind": "search_failed", "token": tok, "chain": ch, "detail": str(e)}
                    )
                    continue
                out.extend(normalize_product(p) for p in items)
    if platforms:
        for plat in platforms:
            for ch in chain_args:
                try:
                    items = adapter.search(platform=plat, chain=ch)
                except DefiError as e:
                    cycle["errors"].append(
                        {"kind": "search_failed", "platform": plat, "chain": ch, "detail": str(e)}
                    )
                    continue
                out.extend(normalize_product(p) for p in items)
    # Dedup by (platform, name, chain)
    seen: set[tuple] = set()
    deduped: list[dict[str, Any]] = []
    for opp in out:
        key = (opp.get("platform"), opp.get("name"), opp.get("chain"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(opp)
    return deduped


def _flatten_positions(positions_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten OnChainOS positions response into a list of per-protocol holdings.

    The actual response shape varies — when no positions, returns
    {"assetStatus": 1}. When holdings exist, returns nested
    walletIdPlatformList / platformList structures. This helper
    is permissive and best-effort; future versions will tighten as we
    see more real-world shapes.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(positions_data, dict):
        return out
    # Possible shapes:
    #   {"assetStatus": 1, "updateAt": ...}            (no positions)
    #   {"walletIdPlatformList": [{ ... }, ...]}
    #   {"platformList": [{ ... }, ...]}
    candidates: list[dict[str, Any]] = []
    for key in ("walletIdPlatformList", "platformList"):
        v = positions_data.get(key)
        if isinstance(v, list):
            candidates.extend(v)
    for platform in candidates:
        plat_name = platform.get("platformName") or platform.get("platform") or "?"
        for inv in platform.get("investmentList") or platform.get("list") or []:
            out.append({
                "platform": plat_name,
                "platform_id": platform.get("platformId"),
                "chain": inv.get("chainName") or inv.get("chain") or platform.get("chainName"),
                "name": inv.get("investmentName") or inv.get("name") or "?",
                "investment_id": inv.get("investmentId") or inv.get("id"),
                "apy_pct": _pct(inv.get("rate") or inv.get("apy")),
                "value_usd": _f(inv.get("totalValue") or inv.get("usdValue")),
                "raw": inv,
            })
    return out


def _f(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _pct(v: Any) -> float:
    return _f(v) * 100.0
