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
    """One full execution of a Loop. Aggregates per-step fills,
    rollback fills (best-effort on failure), and any errors."""
    loop_id: str
    address: str
    chain: str
    base_amount_minimal_units: str
    submitted_count: int
    completed: bool
    fills: list[dict[str, Any]] = field(default_factory=list)
    rollback_fills: list[dict[str, Any]] = field(default_factory=list)
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
            "rollback_fills": self.rollback_fills,
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
        """Execute the loop step-by-step. On step N failure (N>=2),
        walk back through completed steps 1..N-1 in reverse and emit
        best-effort `redeem(ratio="1")` calldata to exit the
        intermediate positions. Rollback fills are recorded separately
        in `rollback_fills` so callers can distinguish forward
        progress from cleanup.

        Generalizes the v0.2 2-step path to N steps. Each subsequent
        step's deposit amount comes from polling the on-chain receipt
        balance (live mode) — protects against partial-fill over-
        deposit when intermediate steps slip."""
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

        completed_steps: list[tuple[Step, dict[str, Any]]] = []
        next_amount = amount_minimal_units

        for idx, step in enumerate(self.loop.steps):
            if idx > 0:
                # Determine this step's input amount from the previous
                # step's receipt. In dry-run, reuse the base amount as
                # a placeholder (calldata shape is what matters, not
                # the dollar value). In live, poll for the receipt
                # balance and use the actual on-chain amount.
                if self.executor.dry_run:
                    next_amount = amount_minimal_units
                    note = f"dry-run: step{idx + 1} uses base amount as placeholder"
                else:
                    polled = self._wait_for_receipt(step.input_token)
                    if polled is None:
                        fill.errors.append({
                            "kind": f"step{idx + 1}_skipped",
                            "detail": (
                                f"receipt balance for {step.input_token!r} did "
                                f"not appear within {self.poll_timeout_sec:.0f}s; "
                                f"step {idx + 1} not submitted"
                            ),
                        })
                        self._rollback(completed_steps, fill)
                        return fill
                    next_amount = str(polled)
                    note = f"live: step{idx + 1} amount derived from polled receipt balance"
            else:
                note = ""

            try:
                step_fill = self._execute_step(step, next_amount, note=note)
            except DefiExecutorError as e:
                fill.errors.append({"kind": f"step{idx + 1}_failed", "detail": str(e)})
                self._rollback(completed_steps, fill)
                return fill

            fill.fills.append(step_fill)
            if step_fill.get("submitted"):
                fill.submitted_count += 1
            completed_steps.append((step, step_fill))

        fill.completed = True
        return fill

    def _rollback(
        self,
        completed_steps: list[tuple[Step, dict[str, Any]]],
        fill: LoopFill,
    ) -> None:
        """Walk back through completed steps in reverse, emit best-
        effort redeem calldata for each. Failures during rollback are
        logged but do not abort the rollback — we try to exit every
        position even if one redeem fails.

        Best-effort by design — true atomic rollback would require
        either smart-contract-level batching with revert semantics, or
        a compensation pattern that's out of scope for v0.3. The
        residual risk (some intermediate position not cleanly exited)
        is documented in `rollback_fills` + `errors` so the operator
        can manually finish the cleanup."""
        if not completed_steps:
            return
        if self.executor.dry_run:
            # Even in dry-run, demonstrate the rollback shape — build
            # redeem calldata for inspection.
            pass
        for idx, (step, step_fill) in enumerate(reversed(completed_steps)):
            inv_id = step.investment_id
            if not inv_id:
                fill.errors.append({
                    "kind": "rollback_skipped",
                    "step_index": len(completed_steps) - 1 - idx,
                    "detail": "no investment_id on step; cannot build redeem",
                })
                continue
            try:
                rb = self.executor.redeem(investment_id=inv_id, ratio="1")
                rb["rolled_back_step_index"] = len(completed_steps) - 1 - idx
                rb["rolled_back_step_meta"] = step.as_dict()
                fill.rollback_fills.append(rb)
            except DefiExecutorError as e:
                fill.errors.append({
                    "kind": "rollback_failed",
                    "step_index": len(completed_steps) - 1 - idx,
                    "investment_id": inv_id,
                    "detail": str(e),
                })

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
