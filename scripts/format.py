"""Human-readable formatters for CLI output.

The skill's primary output is JSON (`{"ok": true, "result": {...}}`)
so agent callers + scripts can parse it directly. But humans need
readable tables. This module knows how to render each command's
result shape as a fixed-width text table.

Add a new formatter when you add a new command — keep the JSON shape
stable, the formatter handles presentation.
"""

from __future__ import annotations

from typing import Any


def format_result(*, command: str, result: dict[str, Any]) -> str:
    """Dispatch to the per-command formatter. Returns a multi-line
    string suitable for stdout."""
    fn = _FORMATTERS.get(command)
    if fn is None:
        return f"(no human formatter for command={command!r})"
    return fn(result)


def _fmt_scan(r: dict[str, Any]) -> str:
    lines = [f"opportunity scan — {r.get('count', 0)} products:"]
    lines.append("")
    lines.append(f"  {'platform':22s} {'name':20s} {'apy':>7s}  {'tvl':>14s}")
    lines.append(f"  {'-'*22} {'-'*20} {'-'*7}  {'-'*14}")
    for p in r.get("products", []):
        plat = (p.get("platform") or "?")[:22]
        name = (p.get("name") or "?")[:20]
        apy = p.get("apy_pct", 0) or 0
        tvl = p.get("tvl_usd", 0) or 0
        lines.append(f"  {plat:22s} {name:20s} {apy:>6.2f}%  ${tvl:>12,.0f}")
    return "\n".join(lines)


def _fmt_discover(r: dict[str, Any]) -> str:
    g = r.get("graph", {})
    lines = [
        f"discover — mode={g.get('mode','?')} "
        f"graph: {g.get('nodes',0)} tokens / {g.get('edges',0)} edges  "
        f"loops: {r.get('count_returned', 0)} (of {r.get('count_total', 0)} found)"
    ]
    lines.append("")
    has_risk = any("risk" in lp for lp in r.get("loops", []))
    if has_risk:
        lines.append(f"  {'loop_id':10s} {'risk':>5s} {'apy':>7s} {'steps':>5s}  composition")
    else:
        lines.append(f"  {'loop_id':10s} {'apy':>7s} {'steps':>5s}  composition")
    lines.append("  " + "-" * 78)
    for lp in r.get("loops", []):
        steps = " → ".join(
            f"{s.get('platform','?')}[{s.get('input_token','?')}→{s.get('output_token','?')}]"
            f" {s.get('apy_pct',0):.2f}%"
            for s in lp.get("steps", [])
        )
        loop_id = (lp.get("loop_id") or "")[:10]
        apy = lp.get("combined_apy_pct", 0) or 0
        nsteps = lp.get("step_count", 0)
        if has_risk:
            risk = (lp.get("risk") or {}).get("loop_score")
            risk_s = f"{risk:>4.1f}" if risk is not None else "  ?  "
            lines.append(f"  {loop_id:10s} {risk_s:>5s} {apy:>6.2f}% {nsteps:>5d}  {steps}")
        else:
            lines.append(f"  {loop_id:10s} {apy:>6.2f}% {nsteps:>5d}  {steps}")
    if has_risk:
        lines.append("")
        lines.append("  risk: 0-100, higher = safer (weakest-link of per-step APY volatility + TVL stability)")
    return "\n".join(lines)


def _fmt_positions(r: dict[str, Any]) -> str:
    flat = r.get("positions_flat") or []
    if not flat:
        return f"positions — address {r.get('address','?')[:16]}... — no positions held on {r.get('chains')}"
    lines = [f"positions for {r.get('address','?')[:16]}... on {r.get('chains')}:"]
    lines.append("")
    lines.append(f"  {'platform':20s} {'name':18s} {'apy':>7s}  {'value_usd':>10s}")
    lines.append(f"  {'-'*20} {'-'*18} {'-'*7}  {'-'*10}")
    for p in flat:
        lines.append(
            f"  {(p.get('platform') or '?')[:20]:20s} "
            f"{(p.get('name') or '?')[:18]:18s} "
            f"{(p.get('apy_pct') or 0):>6.2f}%  "
            f"${(p.get('value_usd') or 0):>8,.2f}"
        )
    return "\n".join(lines)


def _fmt_run_loop(r: dict[str, Any]) -> str:
    lp = r.get("loop", {})
    fill = r.get("fill", {})
    lines = [
        f"run-loop — mode={r.get('mode','?')}  "
        f"{lp.get('step_count', 0)}-step loop, combined APY {lp.get('combined_apy_pct', 0)}%",
    ]
    lines.append(f"  result: completed={fill.get('completed')} fills={len(fill.get('fills',[]))} submitted={fill.get('submitted_count', 0)}")
    if fill.get("rollback_fills"):
        lines.append(f"  ROLLBACK fired: {len(fill['rollback_fills'])} legs walked back")
    if fill.get("errors"):
        lines.append(f"  errors: {len(fill['errors'])}")
        for e in fill["errors"][:3]:
            lines.append(f"    - [{e.get('kind','?')}] {e.get('detail','')[:80]}")
    lines.append("")
    lines.append("  steps:")
    for i, f in enumerate(fill.get("fills", [])):
        sm = f.get("step_meta", {})
        submitted = "yes" if f.get("submitted") else "no"
        lines.append(
            f"    [{i + 1}] {(sm.get('platform') or '?')[:18]:18s} "
            f"{(sm.get('input_token') or '?'):8s} → "
            f"{(sm.get('output_token') or '?'):8s} "
            f"APY {sm.get('apy_pct', 0):5.2f}%  submitted={submitted}"
        )
        if f.get("note"):
            lines.append(f"        note: {f['note']}")
    if fill.get("rollback_fills"):
        lines.append("")
        lines.append("  rollback (reverse order):")
        for i, rb in enumerate(fill["rollback_fills"]):
            meta = rb.get("rolled_back_step_meta", {})
            lines.append(
                f"    -{i + 1} redeem {(meta.get('platform') or '?')[:18]:18s} "
                f"submitted={'yes' if rb.get('submitted') else 'no'}"
            )
    return "\n".join(lines)


def _fmt_watch(r: dict[str, Any]) -> str:
    lines = [
        f"watch summary — iterations={r.get('iterations', 0)} "
        f"alerts_total={r.get('alerts_total', 0)} "
        f"actions_total={r.get('actions_total', 0)} "
        f"submitted_total={r.get('submitted_total', 0)}",
    ]
    if r.get("halted"):
        lines.append(f"  HALTED: {r.get('halt_reason','')}")
    if r.get("interrupted"):
        lines.append("  interrupted by signal")
    return "\n".join(lines)


def _fmt_audit(r: dict[str, Any]) -> str:
    events = r.get("events", [])
    lines = [f"audit — {len(events)} recent events:"]
    lines.append("")
    for e in events[-20:]:
        ts = (e.get("ts_utc") or "")[:19]
        ev = e.get("event") or "?"
        extra = ""
        if ev == "monitor.cycle":
            extra = f" cycle={e.get('cycle_index')} alerts={len(e.get('alerts') or [])} fills={len(e.get('fills') or [])}"
        elif ev == "monitor.start":
            extra = f" address={e.get('address')} chains={e.get('chains')}"
        elif ev == "monitor.end":
            extra = f" iters={e.get('iterations')} alerts={e.get('alerts_total')}"
        lines.append(f"  {ts}  {ev:20s}{extra}")
    return "\n".join(lines)


_FORMATTERS = {
    "scan": _fmt_scan,
    "discover": _fmt_discover,
    "positions": _fmt_positions,
    "run-loop": _fmt_run_loop,
    "watch": _fmt_watch,
    "audit": _fmt_audit,
}
