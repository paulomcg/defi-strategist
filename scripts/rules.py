"""Alert + action rules engine.

Pure function: `(positions, opportunities, rules_config) -> (alerts, actions)`.

### Alert rule types (v0.1 — read-only)

  - `min_apy_floor` — alert when a held position's current APY drops below
    a threshold. Catches yields that decay below opportunity cost.
  - `max_protocol_concentration` — alert when more than N% of total DeFi
    value sits with a single platform/protocol. Catches stealth
    concentration risk.
  - `opportunity_above` — alert when an opportunity (from search/list) on
    a watched token exceeds X% APY. Surfaces yield rotation candidates.

### Action rule types (v0.1.5 — write-capable when --live)

  - `auto_compound` — when a held position has pending rewards worth
    more than `min_rewards_usd`, emit a `claim` action; if the rule has
    `reinvest: true`, ALSO emit a `reinvest` action that deposits the
    claimed rewards back into the same product. Closes the simplest
    non-trivial DeFi loop.

Each Action carries an `action` verb, the OnChainOS-relevant ids
(`investment_id`, `platform_id`, etc.), and a structured `meta` block.
The executor consumes these directly.

Each Alert carries a `severity` (info | warn | crit), a structured `kind`
(machine-readable), and a one-line `message` (human-readable).

Strategy-completeness note: this is intentionally a small set. Users
who need more can author a Python `monitor.py` with a `monitor(state) ->
(list[Alert], list[Action])` function and load it via `--monitor` (the
same hook pattern PM uses for strategies).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any


@dataclass
class Alert:
    severity: str          # info | warn | crit
    kind: str              # machine-readable category
    message: str           # human-readable
    context: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Action:
    """A v0.1.5 write action. Consumed by `executor.DefiExecutor`.

    `verb` is `claim` or `reinvest`. The remaining fields carry the
    OnChainOS identifiers the executor needs to build the calldata.
    """
    verb: str                                    # claim | reinvest
    rule_id: str
    investment_id: str | None = None
    platform_id: str | None = None
    reward_type: str = "REWARD_PLATFORM"
    token: str | None = None
    amount_minimal_units: str | None = None
    expect_output: list[dict[str, Any]] | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate(
    *,
    positions: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    rules: list[dict[str, Any]],
) -> tuple[list[Alert], list[Action]]:
    """Evaluate rules against the current state.

    Returns ``(alerts, actions)``. v0.1 callers that only care about
    alerts can take the first element.
    """
    alerts: list[Alert] = []
    actions: list[Action] = []
    total_value = sum(_f(p.get("value_usd")) for p in positions) or 1.0
    for r in rules:
        kind = r.get("type")
        if kind == "min_apy_floor":
            alerts.extend(_min_apy_floor(positions, r))
        elif kind == "max_protocol_concentration":
            alerts.extend(_max_concentration(positions, total_value, r))
        elif kind == "opportunity_above":
            alerts.extend(_opportunity_above(positions, opportunities, r))
        elif kind == "auto_compound":
            new_alerts, new_actions = _auto_compound(positions, r)
            alerts.extend(new_alerts)
            actions.extend(new_actions)
        else:
            alerts.append(Alert(
                severity="warn",
                kind="unknown_rule_type",
                message=f"unknown rule type: {kind!r}",
                context={"rule": r},
            ))
    return alerts, actions


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


def _auto_compound(
    positions: list[dict[str, Any]],
    rule: dict[str, Any],
) -> tuple[list[Alert], list[Action]]:
    """Emit `claim` (and optionally `reinvest`) actions for positions with
    pending rewards above the threshold.

    Pending rewards are read from `position["pending_rewards_usd"]`,
    which the watch loop populates by inspecting `defi position-detail`.
    Positions without that field — or with reward value below the
    threshold — are silently skipped (no alert, no action).
    """
    min_usd = _f(rule.get("min_rewards_usd"))
    do_reinvest = bool(rule.get("reinvest"))
    rule_id = rule.get("id") or "auto_compound"
    alerts: list[Alert] = []
    actions: list[Action] = []
    for p in positions:
        rewards = _f(p.get("pending_rewards_usd"))
        if rewards < min_usd:
            continue
        inv_id = p.get("investment_id")
        plat_id = p.get("platform_id")
        # claim action
        actions.append(Action(
            verb="claim",
            rule_id=rule_id,
            investment_id=inv_id,
            platform_id=plat_id,
            reward_type=p.get("reward_type") or "REWARD_PLATFORM",
            expect_output=p.get("reward_expect_output"),
            meta={
                "position": {
                    "platform": p.get("platform"),
                    "name": p.get("name"),
                    "pending_rewards_usd": rewards,
                },
            },
        ))
        if do_reinvest and inv_id and p.get("reinvest_token") and p.get("reinvest_amount_minimal_units"):
            actions.append(Action(
                verb="reinvest",
                rule_id=rule_id,
                investment_id=inv_id,
                token=p.get("reinvest_token"),
                amount_minimal_units=str(p.get("reinvest_amount_minimal_units")),
                meta={
                    "follows": "claim",
                    "position": {
                        "platform": p.get("platform"),
                        "name": p.get("name"),
                    },
                },
            ))
        alerts.append(Alert(
            severity="info",
            kind="auto_compound_triggered",
            message=(
                f"auto-compound: {p.get('platform','?')} / {p.get('name','?')} "
                f"rewards=${rewards:.2f} (threshold ${min_usd:.2f}); "
                f"will{' claim+reinvest' if do_reinvest else ' claim only'}"
            ),
            context={"rule_id": rule_id, "position": p},
        ))
    return alerts, actions


def _f(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
