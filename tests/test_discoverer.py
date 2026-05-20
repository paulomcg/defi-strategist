"""Tests for the loop discoverer."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.discoverer import Step, _loop_id, _mk_loop, discover_loops
from scripts.onchain_defi import DefiAdapter


class _StubAdapter(DefiAdapter):
    """In-memory adapter that returns pre-canned responses for tests
    so the real OnChainOS CLI isn't invoked."""

    def __init__(self, search_responses: dict[tuple, list[dict[str, Any]]]):
        # NB: don't call super().__init__() because we don't want
        # any subprocess infrastructure to spin up.
        self._search_responses = search_responses

    def search(self, *, token=None, platform=None, chain=None, product_group=None, page=1):
        key = (token, chain)
        return self._search_responses.get(key, [])


def _product(name: str, platform: str, apy_ratio: float, tvl: float, inv_id: str = "id-x"):
    return {
        "investmentId": inv_id,
        "investmentName": name,
        "platformName": platform,
        "rate": apy_ratio,
        "tvl": tvl,
    }


def test_discover_returns_single_step_loops():
    adapter = _StubAdapter({
        ("USDC", "solana"): [
            _product("USDC", "Kamino", 0.07, 1_000_000, "inv-1"),
            _product("USDC", "Jupiter", 0.05, 500_000, "inv-2"),
        ],
    })
    loops = discover_loops(
        base_asset="USDC", chains=["solana"], adapter=adapter,
        min_tvl_usd=0, min_step_apy_pct=0,
    )
    assert len(loops) == 2
    assert all(len(lp.steps) == 1 for lp in loops)
    assert loops[0].combined_apy_pct >= loops[1].combined_apy_pct  # sorted desc


def test_discover_skips_below_tvl_floor():
    adapter = _StubAdapter({
        ("USDC", "solana"): [
            _product("USDC", "Big", 0.05, 10_000_000, "inv-a"),
            _product("USDC", "Tiny", 0.20, 1_000, "inv-b"),
        ],
    })
    loops = discover_loops(
        base_asset="USDC", chains=["solana"], adapter=adapter,
        min_tvl_usd=100_000, min_step_apy_pct=0,
    )
    assert len(loops) == 1
    assert loops[0].steps[0].platform == "Big"


def test_discover_skips_below_apy_floor():
    adapter = _StubAdapter({
        ("USDC", "solana"): [
            _product("USDC", "Decent", 0.06, 10_000_000, "inv-a"),
            _product("USDC", "Trash", 0.001, 10_000_000, "inv-b"),  # 0.1% APY
        ],
    })
    loops = discover_loops(
        base_asset="USDC", chains=["solana"], adapter=adapter,
        min_tvl_usd=0, min_step_apy_pct=1.0,  # require >= 1%
    )
    assert len(loops) == 1
    assert loops[0].steps[0].platform == "Decent"


def test_discover_2step_jito_solayer_composition():
    adapter = _StubAdapter({
        ("SOL", "solana"): [
            _product("SOL", "Jito", 0.06, 5_000_000, "inv-jito"),
        ],
        ("JitoSOL", "solana"): [
            _product("JitoSOL", "Solayer", 0.03, 1_000_000, "inv-solayer"),
        ],
    })
    loops = discover_loops(
        base_asset="SOL", chains=["solana"], adapter=adapter,
        min_tvl_usd=0, min_step_apy_pct=0,
    )
    # Expect both the single-step Jito and the 2-step Jito→Solayer
    assert len(loops) == 2
    multi = next(lp for lp in loops if len(lp.steps) == 2)
    assert multi.steps[0].platform == "Jito"
    assert multi.steps[0].output_token == "JitoSOL"
    assert multi.steps[1].platform == "Solayer"
    assert abs(multi.combined_apy_pct - 9.0) < 0.001  # 6% + 3%


def test_discover_composed_only_excludes_single_step():
    adapter = _StubAdapter({
        ("SOL", "solana"): [
            _product("SOL", "Jito", 0.06, 5_000_000, "inv-jito"),
            _product("SOL", "Kamino", 0.05, 1_000_000, "inv-kam"),  # no receipt mapping
        ],
        ("JitoSOL", "solana"): [
            _product("JitoSOL", "Solayer", 0.03, 1_000_000, "inv-sol"),
        ],
    })
    loops = discover_loops(
        base_asset="SOL", chains=["solana"], adapter=adapter,
        min_tvl_usd=0, min_step_apy_pct=0,
        include_single_step=False,
    )
    assert len(loops) == 1
    assert len(loops[0].steps) == 2


def test_discover_skips_step2_self_loop():
    """If step 2's investment_id matches step 1's, we'd be double-counting
    the same product. Discoverer must skip self-loops."""
    adapter = _StubAdapter({
        ("SOL", "solana"): [
            _product("SOL", "Jito", 0.06, 5_000_000, "same-id"),
        ],
        ("JitoSOL", "solana"): [
            _product("JitoSOL", "Solayer", 0.03, 1_000_000, "same-id"),  # same id!
        ],
    })
    loops = discover_loops(
        base_asset="SOL", chains=["solana"], adapter=adapter,
        min_tvl_usd=0, min_step_apy_pct=0,
        include_single_step=False,
    )
    assert loops == []  # the only 2-step possibility was filtered as self-loop


def test_loop_id_stable_across_calls():
    """Same composition must get same loop_id so run-loop can find it
    after a separate discover invocation."""
    step1 = Step(
        investment_id="inv-1", platform="Jito", chain="solana",
        input_token="SOL", output_token="JitoSOL", apy_pct=5.62, tvl_usd=1e9,
    )
    step2 = Step(
        investment_id="inv-2", platform="Solayer", chain="solana",
        input_token="JitoSOL", output_token="JitoSOL", apy_pct=0.0, tvl_usd=1e6,
    )
    id_a = _loop_id("SOL", "solana", [step1, step2])
    id_b = _loop_id("SOL", "solana", [step1, step2])
    assert id_a == id_b
    # Different composition → different id
    id_c = _loop_id("SOL", "solana", [step1])
    assert id_a != id_c


def test_mk_loop_attaches_note_for_multi_step():
    step1 = Step(
        investment_id="i", platform="p1", chain="solana", input_token="SOL",
        output_token="JitoSOL", apy_pct=5.0, tvl_usd=0,
    )
    step2 = Step(
        investment_id="i2", platform="p2", chain="solana", input_token="JitoSOL",
        output_token="JitoSOL", apy_pct=2.0, tvl_usd=0,
    )
    single = _mk_loop("SOL", "solana", [step1])
    multi = _mk_loop("SOL", "solana", [step1, step2])
    assert single.notes == ""
    assert "naive sum" in multi.notes  # caveat documented
