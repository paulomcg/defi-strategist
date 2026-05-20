"""Risk scoring for discovered loops.

OnChainOS exposes `defi rate-chart` and `defi tvl-chart` per product.
We use those to compute two stability signals:

  - **APY volatility** — stdev(APY history) / max(mean, 0.01). Lower
    is better. A product whose APY swings 50%/wk is less trustworthy
    than one that holds steady.
  - **TVL stability** — 1 - stdev/mean over the same window. Higher
    is better. A product hemorrhaging TVL is signaling stress (rate
    cut imminent, smart-money exit, possible exploit).

These combine into a 0-100 risk score per product. A loop's score is
`min(per-step scores)` — the weakest-link principle, because the loop
inherits the risk of its sketchiest leg.

What this DOESN'T capture (honest limits):
  - Smart-contract audit status (no OnChainOS data source)
  - Oracle dependence (could be inferred from rateType / lpToken
    contract type, but we don't model it)
  - Governance risk (upgradeable contracts, multisig signers)
  - Lockup / redemption delay (some LSTs unbond over days)
  - Reward emissions vs sustainable yield (a 30% APY paid in
    inflationary tokens is not the same as 30% in base asset)

For products without historical data (rate-chart/tvl-chart returns
empty), we surface a neutral 50 score with `data_quality: "none"`
flag, so the operator knows the score is an estimate.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from .onchain_defi import DefiAdapter, DefiError


@dataclass
class RiskBreakdown:
    """Per-product risk scoring detail. Aggregated by `score_loop()`
    into a loop-level score."""
    investment_id: str
    platform: str
    apy_volatility: float        # 0.0+ (lower better; 1.0 = stdev == mean)
    apy_volatility_score: float  # 0-100 (higher better)
    tvl_stability: float         # 0.0-1.0 (higher better)
    tvl_stability_score: float   # 0-100 (higher better)
    age_days: int | None         # how long we have history for
    composite_score: float       # 0-100
    data_quality: str            # "full" | "partial" | "none"
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "investment_id": self.investment_id,
            "platform": self.platform,
            "apy_volatility": round(self.apy_volatility, 4),
            "apy_volatility_score": round(self.apy_volatility_score, 1),
            "tvl_stability": round(self.tvl_stability, 4),
            "tvl_stability_score": round(self.tvl_stability_score, 1),
            "age_days": self.age_days,
            "composite_score": round(self.composite_score, 1),
            "data_quality": self.data_quality,
            "notes": self.notes,
        }


def score_step(
    *,
    investment_id: str,
    platform: str,
    chain: str,
    adapter: DefiAdapter,
    has_rate_chart: bool = True,
    has_tvl_chart: bool = True,
) -> RiskBreakdown:
    """Compute a per-product risk score using `rate-chart` and
    `tvl-chart`. Falls back to a neutral 50 with `data_quality: "none"`
    if no history is available for this product."""
    apy_points: list[float] = []
    tvl_points: list[float] = []
    age_days: int | None = None
    notes_parts: list[str] = []

    if has_rate_chart:
        try:
            rate_chart = adapter.rate_chart(investment_id=investment_id, chain=chain)
            apy_points = _extract_numeric_series(rate_chart, value_key=("rate", "value", "apy"))
            age_days = _series_age_days(rate_chart)
        except DefiError as e:
            notes_parts.append(f"rate_chart unavailable ({e})")

    if has_tvl_chart:
        try:
            tvl_chart = adapter._cached(
                ("tvl-chart", investment_id, chain),
                ["defi", "tvl-chart", "--investment-id", str(investment_id), "--chain", chain],
            )
            tvl_points = _extract_numeric_series(tvl_chart, value_key=("tvl", "value"))
        except DefiError as e:
            notes_parts.append(f"tvl_chart unavailable ({e})")

    apy_vol, apy_vol_score = _apy_volatility(apy_points)
    tvl_stab, tvl_stab_score = _tvl_stability(tvl_points)

    have_apy = len(apy_points) >= 5
    have_tvl = len(tvl_points) >= 5
    if have_apy and have_tvl:
        data_quality = "full"
        composite = (apy_vol_score * 0.5) + (tvl_stab_score * 0.5)
    elif have_apy or have_tvl:
        data_quality = "partial"
        composite = apy_vol_score if have_apy else tvl_stab_score
    else:
        data_quality = "none"
        composite = 50.0
        notes_parts.append("no historical data — neutral score")

    return RiskBreakdown(
        investment_id=investment_id,
        platform=platform,
        apy_volatility=apy_vol,
        apy_volatility_score=apy_vol_score,
        tvl_stability=tvl_stab,
        tvl_stability_score=tvl_stab_score,
        age_days=age_days,
        composite_score=composite,
        data_quality=data_quality,
        notes="; ".join(notes_parts),
    )


def score_loop_breakdowns(per_step: list[RiskBreakdown]) -> dict[str, Any]:
    """Aggregate per-step breakdowns into a loop-level summary.

    Loop score = min(per-step composite) — weakest-link principle. The
    loop inherits the worst step's risk because failure of any leg
    breaks the composition."""
    if not per_step:
        return {"loop_score": None, "weakest_step": None, "per_step": []}
    weakest = min(per_step, key=lambda b: b.composite_score)
    return {
        "loop_score": round(weakest.composite_score, 1),
        "weakest_step": {
            "platform": weakest.platform,
            "investment_id": weakest.investment_id,
            "composite_score": round(weakest.composite_score, 1),
            "data_quality": weakest.data_quality,
        },
        "per_step": [b.as_dict() for b in per_step],
    }


# ---- helpers ----

def _extract_numeric_series(
    chart_data: Any, value_key: tuple[str, ...] = ("rate", "value")
) -> list[float]:
    """OnChainOS chart responses come back as list-of-points where each
    point is `{ts, rate}` or `{ts, value}` or similar. This is permissive
    — tries every key in value_key until one yields numbers."""
    if not chart_data:
        return []
    points = chart_data if isinstance(chart_data, list) else (
        chart_data.get("list") or chart_data.get("items") or []
    )
    out: list[float] = []
    for p in points or []:
        if not isinstance(p, dict):
            continue
        for k in value_key:
            v = p.get(k)
            if v is not None:
                try:
                    out.append(float(v))
                    break
                except (TypeError, ValueError):
                    continue
    return out


def _series_age_days(chart_data: Any) -> int | None:
    """Best-effort: read the oldest timestamp from the series and
    return its age in days. OnChainOS timestamps are usually in ms."""
    if not chart_data:
        return None
    points = chart_data if isinstance(chart_data, list) else (
        chart_data.get("list") or chart_data.get("items") or []
    )
    timestamps: list[int] = []
    for p in points or []:
        if not isinstance(p, dict):
            continue
        ts = p.get("ts") or p.get("time") or p.get("timestamp")
        if ts is None:
            continue
        try:
            timestamps.append(int(ts))
        except (TypeError, ValueError):
            continue
    if not timestamps:
        return None
    import time
    now_ms = int(time.time() * 1000)
    oldest = min(timestamps)
    # Heuristic: if "oldest" is < 1e10, assume seconds and convert
    if oldest < 10**10:
        oldest *= 1000
    age_ms = now_ms - oldest
    return max(0, age_ms // (1000 * 60 * 60 * 24))


def _apy_volatility(points: list[float]) -> tuple[float, float]:
    """Returns (raw_volatility, score_0_to_100). Score is
    100 * (1 - min(volatility, 1))."""
    if len(points) < 2:
        return 0.0, 50.0
    mean = max(statistics.fmean(points), 0.0001)  # avoid div0 on zero-APY products
    stdev = statistics.pstdev(points)
    vol = stdev / mean
    score = max(0.0, min(100.0, 100.0 * (1.0 - min(vol, 1.0))))
    return vol, score


def _tvl_stability(points: list[float]) -> tuple[float, float]:
    """Returns (raw_stability, score_0_to_100). Stability is
    1 - stdev/mean; we floor it at 0."""
    if len(points) < 2:
        return 0.0, 50.0
    mean = max(statistics.fmean(points), 1.0)
    stdev = statistics.pstdev(points)
    stab = max(0.0, 1.0 - (stdev / mean))
    score = max(0.0, min(100.0, 100.0 * stab))
    return stab, score
