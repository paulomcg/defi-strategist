"""Alert rules engine.

Pure function: `(positions, opportunities, rules_config) -> list[Alert]`.

Three built-in rule types covering the most common DeFi monitoring needs:

  - `min_apy_floor` — alert when a held position's current APY drops below
    a threshold. Catches yields that decay below opportunity cost.
  - `max_protocol_concentration` — alert when more than N% of total DeFi
    value sits with a single platform/protocol. Catches stealth
    concentration risk.
  - `opportunity_above` — alert when an opportunity (from search/list) on
    a watched token exceeds X% APY. Surfaces yield rotation candidates.

Each alert carries a `severity` (info | warn | crit), a structured `kind`
(machine-readable), and a one-line `message` (human-readable).

Strategy-completeness note: this is intentionally a small set. Users
who need more can author a Python `monitor.py` with a `monitor(state) ->
list[Alert]` function and load it via `--monitor` (the same hook pattern
PM uses for strategies).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class Alert:
    severity: str          # info | warn | crit
    kind: str              # machine-readable category
    message: str           # human-readable
    context: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate(
    *,
    positions: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    rules: list[dict[str, Any]],
) -> list[Alert]:
    out: list[Alert] = []
    total_value = sum(_f(p.get("value_usd")) for p in positions) or 1.0
    for r in rules:
        kind = r.get("type")
        if kind == "min_apy_floor":
            out.extend(_min_apy_floor(positions, r))
        elif kind == "max_protocol_concentration":
            out.extend(_max_concentration(positions, total_value, r))
        elif kind == "opportunity_above":
            out.extend(_opportunity_above(positions, opportunities, r))
        else:
            out.append(Alert(
                severity="warn",
                kind="unknown_rule_type",
                message=f"unknown rule type: {kind!r}",
                context={"rule": r},
            ))
    return out


# ---- rule implementations ----

def _min_apy_floor(
    positions: list[dict[str, Any]], rule: dict[str, Any]
) -> list[Alert]:
    floor = _f(rule.get("threshold_pct"))
    out: list[Alert] = []
    for p in positions:
        apy = _f(p.get("apy_pct"))
        if apy < floor:
            out.append(Alert(
                severity=rule.get("severity", "warn"),
                kind="apy_below_floor",
                message=(
                    f"{p.get('platform','?')} / {p.get('name','?')} APY "
                    f"{apy:.2f}% < floor {floor:.2f}%"
                ),
                context={"position": p, "rule_id": rule.get("id"), "floor": floor},
            ))
    return out


def _max_concentration(
    positions: list[dict[str, Any]], total: float, rule: dict[str, Any]
) -> list[Alert]:
    cap_pct = _f(rule.get("threshold_pct"))
    if cap_pct <= 0:
        return []
    by_platform: dict[str, float] = {}
    by_platform_items: dict[str, list[dict[str, Any]]] = {}
    for p in positions:
        plat = p.get("platform") or "unknown"
        by_platform[plat] = by_platform.get(plat, 0.0) + _f(p.get("value_usd"))
        by_platform_items.setdefault(plat, []).append(p)
    out: list[Alert] = []
    for plat, val in by_platform.items():
        share = (val / total) * 100.0 if total > 0 else 0.0
        if share > cap_pct:
            out.append(Alert(
                severity=rule.get("severity", "warn"),
                kind="protocol_concentration_exceeded",
                message=(
                    f"{plat} is {share:.1f}% of DeFi portfolio "
                    f"(cap {cap_pct:.1f}%); positions: "
                    f"{[p.get('name','?') for p in by_platform_items[plat]]}"
                ),
                context={
                    "platform": plat,
                    "value_usd": val,
                    "share_pct": share,
                    "cap_pct": cap_pct,
                    "rule_id": rule.get("id"),
                },
            ))
    return out


def _opportunity_above(
    positions: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    rule: dict[str, Any],
) -> list[Alert]:
    threshold = _f(rule.get("threshold_pct"))
    held_keys = {(p.get("platform"), p.get("name")) for p in positions}
    out: list[Alert] = []
    for opp in opportunities:
        apy = _f(opp.get("apy_pct"))
        if apy < threshold:
            continue
        key = (opp.get("platform"), opp.get("name"))
        if key in held_keys:
            continue  # already holding this product
        out.append(Alert(
            severity=rule.get("severity", "info"),
            kind="opportunity_above_threshold",
            message=(
                f"opportunity: {opp.get('platform','?')} / "
                f"{opp.get('name','?')} @ {apy:.2f}% "
                f"(threshold {threshold:.2f}%)"
            ),
            context={"opportunity": opp, "rule_id": rule.get("id"), "threshold": threshold},
        ))
    return out


def _f(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
