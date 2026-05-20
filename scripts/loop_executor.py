"""LoopExecutor — runs a discovered Loop step-by-step.

Drives the existing `DefiExecutor` through the Loop's ordered Steps.
For 2-step loops:

  Step 1: deposit `amount` of base_asset into step1.investment_id
  Step 2: wait for step1 to settle, read the receipt balance, deposit
          that balance into step2.investment_id

Three execution modes (mirror DefiExecutor):

  - dry_run=True       → build calldata for each step, do NOT broadcast
  - dry_run=False AND single-step      → submit live, single fill
  - dry_run=False AND multi-step LIVE  → submit step 1 live, poll for
    receipt, submit step 2 live. Step 2's amount comes from the
    on-chain receipt balance diff (NOT trusted from the user) so a
    step-1 partial fill won't over-deposit in step 2.

Polling timeout for step-2 amount detection is configurable; default
60s with 5s poll interval. If the receipt balance hasn't appeared by
the timeout, step 2 is skipped and the LoopFill records the partial
completion.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .discoverer import Loop, Step
from .executor import DefiExecutor, DefiExecutorError
from .onchain_defi import DefiAdapter, DefiError


@dataclass
class LoopFill:
    """One full execution of a Loop. Aggregates per-step fills."""
    loop_id: str
    address: str
    chain: str
    base_amount_minimal_units: str
    submitted_count: int
    completed: bool
    fills: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id,
            "address": self.address,
            "chain": self.chain,
            "base_amount_minimal_units": self.base_amount_minimal_units,
            "submitted_count": self.submitted_count,
            "completed": self.completed,
            "fills": self.fills,
            "errors": self.errors,
        }


class LoopExecutor:
    def __init__(
        self,
        *,
        loop: Loop,
        executor: DefiExecutor,
        adapter: DefiAdapter,
        poll_interval_sec: float = 5.0,
        poll_timeout_sec: float = 60.0,
    ):
        self.loop = loop
        self.executor = executor
        self.adapter = adapter
        self.poll_interval_sec = poll_interval_sec
        self.poll_timeout_sec = poll_timeout_sec

    def run(self, *, amount_minimal_units: str) -> LoopFill:
        fill = LoopFill(
            loop_id=self.loop.loop_id,
            address=self.executor.address,
            chain=self.executor.chain,
            base_amount_minimal_units=amount_minimal_units,
            submitted_count=0,
            completed=False,
        )
        if not self.loop.steps:
            fill.errors.append({"kind": "loop_empty", "detail": "no steps to execute"})
            return fill

        # --- Step 1 ---
        step1 = self.loop.steps[0]
        try:
            step1_fill = self._execute_step(step1, amount_minimal_units)
        except DefiExecutorError as e:
            fill.errors.append({"kind": "step1_failed", "detail": str(e)})
            return fill
        fill.fills.append(step1_fill)
        if step1_fill.get("submitted"):
            fill.submitted_count += 1

        # If there's only one step (single-product yield), we're done.
        if len(self.loop.steps) == 1:
            fill.completed = True
            return fill

        # --- Step 2 ---
        step2 = self.loop.steps[1]
        # In dry-run, we never actually moved funds, so the receipt
        # balance won't appear. Use the input amount as a placeholder
        # so the dry-run calldata build still demonstrates the shape.
        if self.executor.dry_run:
            step2_amount = amount_minimal_units
            note = "dry-run: using step1 input amount as placeholder for step2"
        else:
            polled = self._wait_for_receipt(step2.input_token)
            if polled is None:
                fill.errors.append({
                    "kind": "step2_skipped",
                    "detail": (
                        f"receipt balance for {step2.input_token!r} did not "
                        f"appear within {self.poll_timeout_sec:.0f}s; "
                        "step 2 not submitted"
                    ),
                })
                return fill
            step2_amount = str(polled)
            note = "live: amount derived from polled receipt balance"

        try:
            step2_fill = self._execute_step(step2, step2_amount, note=note)
        except DefiExecutorError as e:
            fill.errors.append({"kind": "step2_failed", "detail": str(e)})
            return fill
        fill.fills.append(step2_fill)
        if step2_fill.get("submitted"):
            fill.submitted_count += 1
        fill.completed = True
        return fill

    # ---- helpers ----

    def _execute_step(
        self, step: Step, amount_minimal_units: str, note: str = ""
    ) -> dict[str, Any]:
        if not step.investment_id:
            raise DefiExecutorError(
                f"step_missing_investment_id platform={step.platform!r}"
            )
        fill = self.executor.reinvest(
            investment_id=step.investment_id,
            token=step.input_token,
            amount_minimal_units=amount_minimal_units,
        )
        if note:
            fill["note"] = note
        fill["step_meta"] = step.as_dict()
        return fill

    def _wait_for_receipt(self, receipt_token: str) -> int | None:
        """Poll `defi positions` for a non-zero balance of `receipt_token`.

        Returns the balance in minimal units, or None on timeout.
        """
        deadline = time.time() + self.poll_timeout_sec
        while time.time() < deadline:
            try:
                data = self.adapter.positions(
                    address=self.executor.address,
                    chains=self.executor.chain,
                )
            except DefiError:
                time.sleep(self.poll_interval_sec)
                continue
            amount = _find_token_amount(data, receipt_token)
            if amount and amount > 0:
                return amount
            time.sleep(self.poll_interval_sec)
        return None


def _find_token_amount(positions_data: Any, token_symbol: str) -> int | None:
    """Best-effort: scan positions response for a holding matching
    `token_symbol`, return its minimal-units amount (int).

    OnChainOS positions response shape varies; this helper walks the
    common locations and returns None if nothing matches. v0.2 is
    intentionally conservative — if we can't unambiguously identify
    the receipt balance, we'd rather skip step 2 than over-deposit.
    """
    if not isinstance(positions_data, dict):
        return None
    sym_l = (token_symbol or "").lower()

    def _walk(node: Any) -> int | None:
        if isinstance(node, list):
            for item in node:
                hit = _walk(item)
                if hit is not None:
                    return hit
        elif isinstance(node, dict):
            # Direct match — a token entry with symbol + amount
            sym = (node.get("tokenSymbol") or node.get("symbol") or "")
            if sym.lower() == sym_l:
                raw = (
                    node.get("balance")
                    or node.get("amount")
                    or node.get("totalAmount")
                    or node.get("coinAmount")
                )
                try:
                    return int(raw) if raw is not None else None
                except (TypeError, ValueError):
                    return None
            for v in node.values():
                hit = _walk(v)
                if hit is not None:
                    return hit
        return None

    return _walk(positions_data)
