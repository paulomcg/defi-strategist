"""Composable yield-loop discoverer.

Given a base asset (USDC, SOL, ETH...) and target chains, enumerate
both:

  1. **Direct opportunities**: all products that accept the base asset
     as a deposit, ranked by APY. This is cross-protocol yield
     comparison — the simplest "composition" being a single deposit.

  2. **2-step compositions**: where a step-1 product mints a known
     receipt token (see `composability.RECEIPT_MAP`) AND that receipt
     token is itself accepted by other products. Combined APY is the
     naive sum of the two legs (v0.2 simplification — does NOT
     account for gas, slippage, lockup, or risk premium).

The output is a list of `Loop` objects, ranked by combined APY,
ready for execution via the `LoopExecutor`.

## Limits and caveats

- **Combined APY is naive** — it sums the per-leg APYs and does not
  net out gas, slippage, the cost of receiving the receipt token, or
  any lockup penalty on redeem. Treat it as an UPPER BOUND.
- **Discovery is bounded by the receipt-token map** — only loops whose
  step-1 receipt is in `composability.RECEIPT_MAP` are surfaced as
  2-step loops. Future versions will widen this via either
  first-class OnChainOS support or graph-search over `defi detail`
  response fields.
- **No cross-chain composition** — both legs of a 2-step loop must be
  on the same chain. Cross-chain bridging adds risk (bridge hacks)
  that's out of scope here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict, field
from typing import Any

from .composability import find_receipt
from .onchain_defi import DefiAdapter, DefiError, normalize_product


@dataclass
class Step:
    """One leg of a loop — a single deposit into a single product."""
    investment_id: str | None
    platform: str
    chain: str
    input_token: str
    output_token: str           # receipt token (or same as input for single-leg)
    apy_pct: float
    tvl_usd: float
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


@dataclass
class Loop:
    """A composed sequence of steps starting from a base asset.

    `loop_id` is a stable hash of the step inventory — same loop on
    different runs gets the same id, so it can be referenced from
    `run-loop --loop-id <id>`.
    """
    loop_id: str
    base_asset: str
    chain: str
    steps: list[Step]
    combined_apy_pct: float
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id,
            "base_asset": self.base_asset,
            "chain": self.chain,
            "step_count": len(self.steps),
            "combined_apy_pct": round(self.combined_apy_pct, 4),
            "notes": self.notes,
            "steps": [s.as_dict() for s in self.steps],
        }


def discover_loops(
    *,
    base_asset: str,
    chains: list[str],
    adapter: DefiAdapter,
    min_tvl_usd: float = 100_000.0,
    min_step_apy_pct: float = 0.5,
    include_single_step: bool = True,
    include_2step: bool = True,
) -> list[Loop]:
    """Discover loops starting from `base_asset` on each of `chains`.

    Returns Loops sorted by combined_apy_pct descending. Caller can slice
    to top N before display.
    """
    loops: list[Loop] = []
    for chain in chains:
        try:
            step1_products = adapter.search(token=base_asset, chain=chain)
        except DefiError:
            continue
        step1 = [normalize_product(p) for p in step1_products]
        step1 = [
            s for s in step1
            if (s.get("tvl_usd") or 0) >= min_tvl_usd
            and (s.get("apy_pct") or 0) >= min_step_apy_pct
        ]

        for s1 in step1:
            step1_obj = Step(
                investment_id=s1.get("investment_id"),
                platform=s1.get("platform") or "?",
                chain=chain,
                input_token=base_asset,
                output_token=base_asset,   # default: no receipt mapping known
                apy_pct=s1.get("apy_pct") or 0.0,
                tvl_usd=s1.get("tvl_usd") or 0.0,
                raw=s1.get("raw") or {},
            )

            if include_single_step:
                loops.append(_mk_loop(base_asset, chain, [step1_obj]))

            if not include_2step:
                continue

            receipt = find_receipt(
                chain=chain,
                platform=step1_obj.platform,
                deposit_token=base_asset,
            )
            if not receipt:
                continue
            step1_obj.output_token = receipt

            try:
                step2_products = adapter.search(token=receipt, chain=chain)
            except DefiError:
                continue
            step2 = [normalize_product(p) for p in step2_products]
            step2 = [
                s for s in step2
                if (s.get("tvl_usd") or 0) >= min_tvl_usd
                and (s.get("apy_pct") or 0) >= min_step_apy_pct
                # Skip self-loops (same product as step 1)
                and s.get("investment_id") != step1_obj.investment_id
            ]
            for s2 in step2:
                step2_obj = Step(
                    investment_id=s2.get("investment_id"),
                    platform=s2.get("platform") or "?",
                    chain=chain,
                    input_token=receipt,
                    output_token=receipt,  # we don't track further receipts in v0.2
                    apy_pct=s2.get("apy_pct") or 0.0,
                    tvl_usd=s2.get("tvl_usd") or 0.0,
                    raw=s2.get("raw") or {},
                )
                loops.append(_mk_loop(base_asset, chain, [step1_obj, step2_obj]))

    loops.sort(key=lambda lp: lp.combined_apy_pct, reverse=True)
    return loops


def _mk_loop(base: str, chain: str, steps: list[Step]) -> Loop:
    combined = sum(s.apy_pct for s in steps)
    notes_parts: list[str] = []
    if len(steps) > 1:
        notes_parts.append(
            "Combined APY is the naive sum of per-leg APYs; does NOT net "
            "out gas, slippage, or risk premium. Treat as upper bound."
        )
    return Loop(
        loop_id=_loop_id(base, chain, steps),
        base_asset=base,
        chain=chain,
        steps=steps,
        combined_apy_pct=combined,
        notes=" ".join(notes_parts),
    )


def _loop_id(base: str, chain: str, steps: list[Step]) -> str:
    """Stable hash so the same composition gets the same loop_id across
    runs (so users can `run-loop --loop-id <id>` after a separate
    `discover` invocation)."""
    h = hashlib.sha1()
    h.update(str(base).encode())
    h.update(str(chain).encode())
    for s in steps:
        h.update(str(s.investment_id or "").encode())
        h.update(str(s.platform or "").encode())
    return h.hexdigest()[:12]
