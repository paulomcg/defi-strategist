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
from .onchain_defi import DefiAdapter, DefiError, normalize_product

EXIT_OK = 0
EXIT_FAILED = 1


def _ok(result: Any) -> int:
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
            return _failed(f"internal_error {type(e).__name__}: {e}")

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
    summary = watch.run_monitor(
        address=args.address,
        chains=chains,
        watch_tokens=tokens,
        watch_platforms=platforms,
        rules_config=cfg.get("rules") or [],
        interval_seconds=args.interval,
        iterations=args.iterations,
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="defi-strategist",
        description=(
            "Strategy/observability layer over OnChainOS DeFi primitives. "
            "Monitor positions, scan opportunities, fire alerts on rules."
        ),
    )
    p.add_argument("--version", action="version", version="defi-strategist 0.1.0")
    sub = p.add_subparsers(dest="cmd", required=True)

    wa = sub.add_parser("watch", help="Run the monitor loop")
    wa.add_argument("--config", required=True, help="Path to rules YAML")
    wa.add_argument("--address", default=None, help="Wallet to monitor positions for (optional — scan-only if omitted)")
    wa.add_argument("--chains", default=None, help="Comma-separated chains (overrides config)")
    wa.add_argument("--tokens", default=None, help="Comma-separated tokens to scan (overrides config)")
    wa.add_argument("--platforms", default=None, help="Comma-separated platforms to scan (overrides config)")
    wa.add_argument("--interval", type=int, default=60, help="Seconds between cycles (default 60)")
    wa.add_argument("--iterations", type=int, default=None, help="Cap cycles (omit for infinite)")
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

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.error("no handler for this command")
        return EXIT_FAILED
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
