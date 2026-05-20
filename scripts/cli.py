"""defi-strategist CLI dispatcher.

Subcommands:
  watch       — monitor mode (cycles + alerts + audit)
  positions   — one-shot snapshot of user's DeFi positions
  scan        — one-shot opportunity scan (search products by token/platform)
  audit       — print recent audit lines
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from . import audit as audit_mod
from . import watch
from .discoverer import attach_risk_scores, discover_loops, discover_loops_v3
from .executor import DefiExecutor
from .format import format_result
from .loop_executor import LoopExecutor
from .onchain_defi import DefiAdapter, DefiError, normalize_product

# Set by main() before dispatch. Lets _ok() respect --format without
# threading the arg through every handler.
_OUTPUT_FORMAT = "json"
_CURRENT_COMMAND = ""

EXIT_OK = 0
EXIT_FAILED = 1


def _ok(result: Any) -> int:
    if _OUTPUT_FORMAT == "table":
        print(format_result(command=_CURRENT_COMMAND, result=result))
    else:
        print(json.dumps({"ok": True, "result": result}, default=str))
    return EXIT_OK


def _failed(line: str) -> int:
    print(f"FAILED: {line}", file=sys.stderr)
    return EXIT_FAILED


def _wrap(handler: Callable[..., int]) -> Callable[..., int]:
    @functools.wraps(handler)
    def _w(args: argparse.Namespace) -> int:
        try:
            return handler(args)
        except KeyboardInterrupt:
            return _failed("interrupted")
        except DefiError as e:
            return _failed(f"defi_error {e}")
        except Exception as e:  # noqa: BLE001
            import traceback
            return _failed(f"internal_error {type(e).__name__}: {e}\n{traceback.format_exc()}")

    return _w


def _load_rules(path: str) -> dict[str, Any]:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise DefiError(f"rules_not_found {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise DefiError("rules_config_invalid (top level must be a mapping)")
    if not isinstance(raw.get("rules"), list):
        raise DefiError("rules_config_invalid (missing 'rules' list)")
    return raw


def _split_csv(v: Any) -> list[str]:
    """Accept comma-separated string OR an already-parsed list (YAML config)."""
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [str(s).strip() for s in v if str(s).strip()]
    return [s.strip() for s in str(v).split(",") if s.strip()]


@_wrap
def cmd_watch(args: argparse.Namespace) -> int:
    cfg = _load_rules(args.config)
    chains = _split_csv(args.chains) or _split_csv(cfg.get("chains"))
    tokens = _split_csv(args.tokens) or _split_csv(cfg.get("watch_tokens"))
    platforms = _split_csv(args.platforms) or _split_csv(cfg.get("watch_platforms"))

    # Build the executor only when action execution is enabled
    # (--live OR --dry-run-actions). Pure monitor mode (default) leaves
    # the executor at None and no actions are dispatched even if rules
    # emit them.
    executor: DefiExecutor | None = None
    if args.live or args.dry_run_actions:
        if not args.address:
            return _failed("live_requires_address — set --address to enable action execution")
        if not chains:
            return _failed("live_requires_chains — set --chains or put `chains:` in the rules YAML")
        executor = DefiExecutor(
            address=args.address,
            chain=chains[0],
            dry_run=not args.live,  # --live overrides --dry-run-actions
        )

    summary = watch.run_monitor(
        address=args.address,
        chains=chains,
        watch_tokens=tokens,
        watch_platforms=platforms,
        rules_config=cfg.get("rules") or [],
        interval_seconds=args.interval,
        iterations=args.iterations,
        executor=executor,
        max_actions_per_cycle=args.max_actions_per_cycle,
    )
    return _ok(summary)


@_wrap
def cmd_positions(args: argparse.Namespace) -> int:
    chains = _split_csv(args.chains)
    if not chains:
        return _failed("positions_requires_chains (use --chains a,b,c)")
    adapter = DefiAdapter(cache_ttl_sec=0)
    data = adapter.positions(address=args.address, chains=",".join(chains))
    return _ok({
        "address": args.address,
        "chains": chains,
        "positions_raw": data,
        "positions_flat": watch._flatten_positions(data),
    })


@_wrap
def cmd_scan(args: argparse.Namespace) -> int:
    tokens = _split_csv(args.tokens)
    platforms = _split_csv(args.platforms)
    chains = _split_csv(args.chains)
    if not tokens and not platforms:
        return _failed("scan_requires_tokens_or_platforms")
    adapter = DefiAdapter(cache_ttl_sec=0)
    items: list[dict[str, Any]] = []
    chain_args: list[str | None] = chains or [None]
    for tok in tokens:
        for ch in chain_args:
            try:
                raw = adapter.search(token=tok, chain=ch, product_group=args.product_group)
            except DefiError as e:
                items.append({"error": str(e), "token": tok, "chain": ch})
                continue
            items.extend(normalize_product(p) for p in raw)
    for plat in platforms:
        for ch in chain_args:
            try:
                raw = adapter.search(platform=plat, chain=ch, product_group=args.product_group)
            except DefiError as e:
                items.append({"error": str(e), "platform": plat, "chain": ch})
                continue
            items.extend(normalize_product(p) for p in raw)
    # Sort by APY descending, dedup
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for it in sorted(items, key=lambda p: p.get("apy_pct") or 0, reverse=True):
        key = (it.get("platform"), it.get("name"), it.get("chain"))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if args.top is not None and len(out) >= args.top:
            break
    return _ok({"count": len(out), "products": out})


@_wrap
def cmd_audit(args: argparse.Namespace) -> int:
    events = audit_mod.read(limit=args.limit)
    return _ok({"count": len(events), "events": events})


@_wrap
def cmd_discover(args: argparse.Namespace) -> int:
    chains = _split_csv(args.chains)
    if not chains:
        return _failed("discover_requires_chains")
    adapter = DefiAdapter(cache_ttl_sec=0)
    if args.legacy_receipt_map:
        loops = discover_loops(
            base_asset=args.token,
            chains=chains,
            adapter=adapter,
            min_tvl_usd=args.min_tvl,
            min_step_apy_pct=args.min_step_apy,
            include_single_step=not args.composed_only,
            include_2step=not args.single_step_only,
        )
        graph_stats = {"mode": "legacy-receipt-map"}
    else:
        loops, graph = discover_loops_v3(
            base_asset=args.token,
            chains=chains,
            adapter=adapter,
            max_steps=args.max_steps,
            min_tvl_usd=args.min_tvl,
            min_step_apy_pct=args.min_step_apy,
            include_single_step=not args.composed_only,
            max_products_per_chain=args.max_products_per_chain,
        )
        graph_stats = {"mode": "dynamic-graph", **graph.stats()}
    sliced = loops[: args.top] if args.top is not None else loops
    # Risk scoring is opt-in because it adds 2 chart fetches per
    # step (1 rate, 1 tvl) and can be slow for large discover runs.
    if args.with_risk:
        attach_risk_scores(sliced, adapter)
        if args.min_risk_score is not None:
            sliced = [
                lp for lp in sliced
                if (lp.risk or {}).get("loop_score") is None
                or (lp.risk or {}).get("loop_score", 0) >= args.min_risk_score
            ]
    return _ok({
        "count_total": len(loops),
        "count_returned": len(sliced),
        "graph": graph_stats,
        "loops": [lp.as_dict() for lp in sliced],
    })


@_wrap
def cmd_run_loop(args: argparse.Namespace) -> int:
    chains = _split_csv(args.chains)
    if not chains:
        return _failed("run_loop_requires_chains")
    if len(chains) != 1:
        return _failed("run_loop_single_chain — pass exactly one chain (loops are intra-chain in v0.2)")
    adapter = DefiAdapter(cache_ttl_sec=0)
    # Permissive filters so any loop_id surfaced by a prior `discover`
    # invocation is also findable here. The user already vetted the
    # loop; we shouldn't second-guess by re-applying tight filters.
    loops = discover_loops(
        base_asset=args.token,
        chains=chains,
        adapter=adapter,
        min_tvl_usd=0.0,
        min_step_apy_pct=0.0,
    )
    target = next((lp for lp in loops if lp.loop_id == args.loop_id), None)
    if target is None:
        return _failed(
            f"loop_not_found loop_id={args.loop_id!r} — "
            f"re-run `discover --token {args.token} --chains {chains[0]}` "
            "to refresh the loop list (id is stable across runs but the "
            "loop must still exist in the current opportunity set)"
        )
    executor = DefiExecutor(
        address=args.address,
        chain=chains[0],
        dry_run=not args.live,
    )
    loop_executor = LoopExecutor(
        loop=target,
        executor=executor,
        adapter=adapter,
        poll_interval_sec=args.poll_interval,
        poll_timeout_sec=args.poll_timeout,
    )
    fill = loop_executor.run(amount_minimal_units=args.amount_minimal_units)
    return _ok({
        "loop": target.as_dict(),
        "fill": fill.as_dict(),
        "mode": "live" if args.live else "dry-run-actions",
    })


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="defi-strategist",
        description=(
            "Strategy/observability layer over OnChainOS DeFi primitives. "
            "Monitor positions, scan opportunities, fire alerts on rules, "
            "discover composable loops, execute multi-step plans with rollback."
        ),
    )
    p.add_argument("--version", action="version", version="defi-strategist 0.3.0")
    p.add_argument(
        "--format",
        choices=["json", "table"],
        default="json",
        help=(
            "Output format. `json` (default) emits {ok, result} envelopes "
            "for agent / script consumers. `table` emits human-readable "
            "fixed-width tables for direct CLI use."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    wa = sub.add_parser("watch", help="Run the monitor loop")
    wa.add_argument("--config", required=True, help="Path to rules YAML")
    wa.add_argument("--address", default=None, help="Wallet to monitor positions for (optional — scan-only if omitted)")
    wa.add_argument("--chains", default=None, help="Comma-separated chains (overrides config)")
    wa.add_argument("--tokens", default=None, help="Comma-separated tokens to scan (overrides config)")
    wa.add_argument("--platforms", default=None, help="Comma-separated platforms to scan (overrides config)")
    wa.add_argument("--interval", type=int, default=60, help="Seconds between cycles (default 60)")
    wa.add_argument("--iterations", type=int, default=None, help="Cap cycles (omit for infinite)")
    wa.add_argument(
        "--live",
        action="store_true",
        help=(
            "Execute rule-emitted actions on-chain via OnChainOS "
            "(`defi claim/invest` + `wallet contract-call`). Requires "
            "--address. WITHOUT this flag, actions are inert."
        ),
    )
    wa.add_argument(
        "--dry-run-actions",
        action="store_true",
        help=(
            "Build calldata for rule-emitted actions (real OnChainOS "
            "round-trip) but DO NOT submit. Useful for verifying that a "
            "rule emits well-formed actions before going --live."
        ),
    )
    wa.add_argument(
        "--max-actions-per-cycle",
        type=int,
        default=5,
        dest="max_actions_per_cycle",
        help=(
            "Cap actions executed per cycle (safety; default 5). A rule "
            "that misfires and emits 100 actions still spends at most "
            "N round-trips per cycle."
        ),
    )
    wa.set_defaults(_handler=cmd_watch)

    po = sub.add_parser("positions", help="One-shot snapshot of user DeFi positions")
    po.add_argument("--address", required=True)
    po.add_argument("--chains", required=True, help="Comma-separated chains")
    po.set_defaults(_handler=cmd_positions)

    sc = sub.add_parser("scan", help="One-shot opportunity scan")
    sc.add_argument("--tokens", default=None, help="Comma-separated tokens")
    sc.add_argument("--platforms", default=None, help="Comma-separated platforms")
    sc.add_argument("--chains", default=None, help="Comma-separated chains (optional)")
    sc.add_argument("--product-group", default=None, choices=["SINGLE_EARN", "DEX_POOL", "LENDING"], dest="product_group")
    sc.add_argument("--top", type=int, default=20, help="Limit results to top N by APY")
    sc.set_defaults(_handler=cmd_scan)

    au = sub.add_parser("audit", help="Print recent audit log lines")
    au.add_argument("--limit", type=int, default=20)
    au.set_defaults(_handler=cmd_audit)

    di = sub.add_parser(
        "discover",
        help="Find composable yield loops starting from a base asset",
    )
    di.add_argument("--token", required=True, help="Base asset (e.g. USDC, SOL, ETH)")
    di.add_argument("--chains", required=True, help="Comma-separated chains")
    di.add_argument("--top", type=int, default=10, help="Limit results to top N by combined APY")
    di.add_argument("--min-tvl", type=float, default=100_000.0, dest="min_tvl", help="Per-step TVL floor (default $100k)")
    di.add_argument("--min-step-apy", type=float, default=0.5, dest="min_step_apy", help="Per-step minimum APY %% (default 0.5)")
    di.add_argument("--composed-only", action="store_true", help="Show only 2+ step compositions")
    di.add_argument("--single-step-only", action="store_true", help="Show only single-step opportunities")
    di.add_argument(
        "--max-steps", type=int, default=3, dest="max_steps",
        help="Max steps per loop (graph mode only; default 3)",
    )
    di.add_argument(
        "--max-products-per-chain", type=int, default=200, dest="max_products_per_chain",
        help="Cap on products explored per chain during graph build (default 200)",
    )
    di.add_argument(
        "--legacy-receipt-map", action="store_true", dest="legacy_receipt_map",
        help=(
            "Use v0.2's hardcoded RECEIPT_MAP instead of v0.3's dynamic graph "
            "(diagnostic; falls back to 2-step LST→restake compositions only)"
        ),
    )
    di.add_argument(
        "--with-risk", action="store_true", dest="with_risk",
        help=(
            "Compute per-step risk scores (APY volatility + TVL stability) "
            "via `defi rate-chart` + `tvl-chart`. Adds 2 chart fetches per "
            "step per loop; slower but every loop gets a 0-100 risk score "
            "and its weakest-link breakdown."
        ),
    )
    di.add_argument(
        "--min-risk-score", type=float, default=None, dest="min_risk_score",
        help="With --with-risk, filter out loops scoring below this (0-100). Unscored loops are kept by default.",
    )
    di.set_defaults(_handler=cmd_discover)

    rl = sub.add_parser(
        "run-loop",
        help="Execute a discovered loop (default: --dry-run-actions; pass --live to broadcast)",
    )
    rl.add_argument("--loop-id", required=True, dest="loop_id", help="Loop id from `discover` output")
    rl.add_argument("--token", required=True, help="Same --token used in the matching discover call")
    rl.add_argument("--chains", required=True, help="Same --chains; exactly one chain (loops are intra-chain in v0.2)")
    rl.add_argument("--address", required=True)
    rl.add_argument(
        "--amount-minimal-units",
        required=True,
        dest="amount_minimal_units",
        help="Amount of the base asset in minimal units (e.g. '1000000' = 1 USDC at 6 decimals)",
    )
    rl.add_argument("--live", action="store_true", help="Broadcast on-chain (default: dry-run, calldata only)")
    rl.add_argument("--poll-interval", type=float, default=5.0, dest="poll_interval", help="Seconds between receipt-balance polls in --live multi-step")
    rl.add_argument("--poll-timeout", type=float, default=60.0, dest="poll_timeout", help="Seconds to wait for step-1 receipt balance before skipping step 2")
    rl.set_defaults(_handler=cmd_run_loop)

    return p


def main(argv: list[str] | None = None) -> int:
    global _OUTPUT_FORMAT, _CURRENT_COMMAND
    parser = build_parser()
    args = parser.parse_args(argv)
    _OUTPUT_FORMAT = getattr(args, "format", "json")
    _CURRENT_COMMAND = getattr(args, "cmd", "")
    # Propagate format to the watch loop's per-cycle emission path
    # (the streaming output bypasses _ok and needs to be informed).
    watch.set_cycle_format(_OUTPUT_FORMAT)
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.error("no handler for this command")
        return EXIT_FAILED
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
