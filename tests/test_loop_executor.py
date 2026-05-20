"""Tests for the N-step LoopExecutor with rollback."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.discoverer import Loop, Step
from scripts.executor import DefiExecutor, DefiExecutorError
from scripts.loop_executor import LoopExecutor
from scripts.onchain_defi import DefiAdapter


class _StubExecutor(DefiExecutor):
    """Records calls + lets tests inject behavior (success / failure /
    canned responses) per verb without subprocess invocation."""

    def __init__(self, *, address="0xA", chain="solana", dry_run=True):
        self.address = address
        self.chain = chain
        self.dry_run = dry_run
        self.reinvest_calls: list[dict] = []
        self.redeem_calls: list[dict] = []
        self.reinvest_should_fail_on_index: int | None = None  # 0-indexed
        self.redeem_should_fail = False

    def reinvest(self, *, investment_id, token, amount_minimal_units, slippage="0.01", biz_type="defi"):
        call = {
            "investment_id": investment_id,
            "token": token,
            "amount_minimal_units": amount_minimal_units,
        }
        self.reinvest_calls.append(call)
        if (
            self.reinvest_should_fail_on_index is not None
            and len(self.reinvest_calls) - 1 == self.reinvest_should_fail_on_index
        ):
            raise DefiExecutorError(f"forced_failure on reinvest index {self.reinvest_should_fail_on_index}")
        return {
            "ok": True,
            "action": "reinvest",
            "investment_id": investment_id,
            "token": token,
            "submitted": not self.dry_run,
            "tx_hash": "0xfake" if not self.dry_run else None,
        }

    def redeem(self, *, investment_id, ratio="1", **kwargs):
        self.redeem_calls.append({"investment_id": investment_id, "ratio": ratio})
        if self.redeem_should_fail:
            raise DefiExecutorError("forced_redeem_failure")
        return {
            "ok": True,
            "action": "redeem",
            "investment_id": investment_id,
            "submitted": not self.dry_run,
        }


def _step(inv_id: str, platform: str, in_tok: str, out_tok: str, apy: float = 5.0) -> Step:
    return Step(
        investment_id=inv_id, platform=platform, chain="solana",
        input_token=in_tok, output_token=out_tok,
        apy_pct=apy, tvl_usd=1e6,
    )


def _loop(steps: list[Step]) -> Loop:
    return Loop(
        loop_id="test-loop",
        base_asset=steps[0].input_token,
        chain="solana",
        steps=steps,
        combined_apy_pct=sum(s.apy_pct for s in steps),
    )


def test_run_single_step_dry_run_emits_one_fill():
    loop = _loop([_step("a", "Aave", "USDC", "aUSDC")])
    ex = _StubExecutor(dry_run=True)
    le = LoopExecutor(loop=loop, executor=ex, adapter=DefiAdapter())
    fill = le.run(amount_minimal_units="1000000")
    assert fill.completed is True
    assert len(fill.fills) == 1
    assert fill.submitted_count == 0  # dry-run
    assert ex.reinvest_calls[0]["investment_id"] == "a"


def test_run_two_step_dry_run_uses_base_amount_for_step2():
    """In dry-run, step 2's amount is the same as step 1's (placeholder)."""
    loop = _loop([
        _step("jito", "Jito", "SOL", "JitoSOL"),
        _step("solayer", "Solayer", "JitoSOL", "sjitoSOL"),
    ])
    ex = _StubExecutor(dry_run=True)
    le = LoopExecutor(loop=loop, executor=ex, adapter=DefiAdapter())
    fill = le.run(amount_minimal_units="1000000")
    assert fill.completed is True
    assert len(fill.fills) == 2
    assert ex.reinvest_calls[0]["amount_minimal_units"] == "1000000"
    assert ex.reinvest_calls[1]["amount_minimal_units"] == "1000000"
    assert "placeholder" in fill.fills[1].get("note", "")


def test_run_three_step_loop_calls_each_in_order():
    loop = _loop([
        _step("s1", "P1", "SOL", "A"),
        _step("s2", "P2", "A", "B"),
        _step("s3", "P3", "B", "C"),
    ])
    ex = _StubExecutor(dry_run=True)
    le = LoopExecutor(loop=loop, executor=ex, adapter=DefiAdapter())
    fill = le.run(amount_minimal_units="500")
    assert fill.completed is True
    assert [c["investment_id"] for c in ex.reinvest_calls] == ["s1", "s2", "s3"]
    assert [c["token"] for c in ex.reinvest_calls] == ["SOL", "A", "B"]


def test_run_step2_failure_triggers_rollback_of_step1():
    loop = _loop([
        _step("s1", "P1", "SOL", "A"),
        _step("s2", "P2", "A", "B"),
    ])
    ex = _StubExecutor(dry_run=True)
    ex.reinvest_should_fail_on_index = 1  # fail step 2
    le = LoopExecutor(loop=loop, executor=ex, adapter=DefiAdapter())
    fill = le.run(amount_minimal_units="1000")
    assert fill.completed is False
    assert len(fill.fills) == 1  # only step 1 succeeded
    assert len(fill.rollback_fills) == 1  # step 1 was rolled back
    assert ex.redeem_calls[0]["investment_id"] == "s1"
    # Error vocabulary: "step2_failed"
    assert any("step2_failed" in e.get("kind", "") for e in fill.errors)


def test_run_step3_failure_rolls_back_steps_1_and_2_in_reverse():
    loop = _loop([
        _step("s1", "P1", "SOL", "A"),
        _step("s2", "P2", "A", "B"),
        _step("s3", "P3", "B", "C"),
    ])
    ex = _StubExecutor(dry_run=True)
    ex.reinvest_should_fail_on_index = 2
    le = LoopExecutor(loop=loop, executor=ex, adapter=DefiAdapter())
    fill = le.run(amount_minimal_units="1000")
    assert fill.completed is False
    assert len(fill.fills) == 2  # s1 + s2 succeeded
    assert len(fill.rollback_fills) == 2
    # Reverse order: s2 redeemed first, then s1
    assert [c["investment_id"] for c in ex.redeem_calls] == ["s2", "s1"]


def test_rollback_continues_when_one_redeem_fails():
    """A failing redeem mid-rollback shouldn't abort the rest of the
    rollback chain. We still try to exit every position."""
    loop = _loop([
        _step("s1", "P1", "SOL", "A"),
        _step("s2", "P2", "A", "B"),
        _step("s3", "P3", "B", "C"),
    ])
    ex = _StubExecutor(dry_run=True)
    ex.reinvest_should_fail_on_index = 2
    ex.redeem_should_fail = True  # every redeem fails
    le = LoopExecutor(loop=loop, executor=ex, adapter=DefiAdapter())
    fill = le.run(amount_minimal_units="1000")
    # Both rollbacks were attempted even though both failed
    assert len(ex.redeem_calls) == 2
    # Both failures recorded in errors
    rollback_failures = [e for e in fill.errors if e.get("kind") == "rollback_failed"]
    assert len(rollback_failures) == 2


def test_step1_failure_no_rollback_needed():
    """If step 1 fails, nothing was committed, so no rollback fires."""
    loop = _loop([
        _step("s1", "P1", "SOL", "A"),
        _step("s2", "P2", "A", "B"),
    ])
    ex = _StubExecutor(dry_run=True)
    ex.reinvest_should_fail_on_index = 0
    le = LoopExecutor(loop=loop, executor=ex, adapter=DefiAdapter())
    fill = le.run(amount_minimal_units="1000")
    assert fill.completed is False
    assert len(fill.fills) == 0
    assert len(fill.rollback_fills) == 0
    assert ex.redeem_calls == []


def test_empty_loop_returns_error():
    # Build a loop with one step then strip it (bypasses _loop's
    # dependency on a first step for base_asset/chain inference).
    loop = _loop([_step("placeholder", "X", "SOL", "A")])
    loop.steps = []
    ex = _StubExecutor(dry_run=True)
    le = LoopExecutor(loop=loop, executor=ex, adapter=DefiAdapter())
    fill = le.run(amount_minimal_units="1000")
    assert fill.completed is False
    assert any(e.get("kind") == "loop_empty" for e in fill.errors)
