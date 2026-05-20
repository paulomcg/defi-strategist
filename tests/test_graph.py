"""Tests for the dynamic graph builder + loop finder."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.graph import (
    Edge,
    Node,
    TokenGraph,
    _detail_to_edge,
    build_graph,
    find_loops,
)
from scripts.onchain_defi import DefiAdapter


# --- _detail_to_edge ---

def _detail(
    *,
    inv_id: str = "1",
    platform: str = "Jito",
    plat_id: str = "p1",
    underlying: str = "SOL",
    underlying_addr: str = "0xSOL",
    lp: str = "JitoSOL",
    lp_addr: str = "0xJitoSOL",
    rate: float = 0.05,
    tvl: float = 1_000_000,
) -> dict[str, Any]:
    return {
        "investmentId": inv_id,
        "platformName": platform,
        "platformId": plat_id,
        "underlyingToken": [{"tokenSymbol": underlying, "tokenAddress": underlying_addr}],
        "lpToken": [{"tokenSymbol": lp, "tokenAddress": lp_addr}],
        "rate": rate,
        "tvl": tvl,
    }


def test_detail_to_edge_basic_lst():
    e = _detail_to_edge(_detail(), chain="solana")
    assert e is not None
    assert e.underlying_symbol == "SOL"
    assert e.receipt_symbol == "JitoSOL"
    assert abs(e.apy_pct - 5.0) < 0.001
    assert e.tvl_usd == 1_000_000
    assert e.platform == "Jito"


def test_detail_to_edge_returns_none_without_lptoken():
    d = _detail()
    d["lpToken"] = []
    assert _detail_to_edge(d, chain="solana") is None


def test_detail_to_edge_returns_none_without_underlying():
    d = _detail()
    d["underlyingToken"] = []
    assert _detail_to_edge(d, chain="solana") is None


def test_detail_to_edge_skips_self_edges():
    """If lpToken == underlyingToken (e.g., USDC vault that mints USDC),
    the edge is a no-op for composition purposes."""
    d = _detail(underlying="USDC", lp="USDC")
    assert _detail_to_edge(d, chain="solana") is None


def test_detail_to_edge_fallback_apy():
    d = _detail(rate=0)
    d["rate"] = None
    e = _detail_to_edge(d, chain="solana", fallback_apy=0.07)
    assert abs(e.apy_pct - 7.0) < 0.001


# --- TokenGraph ---

def test_token_graph_add_edge_creates_nodes():
    g = TokenGraph()
    e = Edge(
        investment_id="1", platform="Jito", platform_id="p1", chain="solana",
        underlying_symbol="SOL", receipt_symbol="JitoSOL",
        underlying_address="A", receipt_address="B",
        apy_pct=5.0, tvl_usd=1e9,
    )
    g.add_edge(e)
    assert "solana:sol" in g.nodes
    assert "solana:jitosol" in g.nodes
    out = g.outgoing(chain="solana", symbol="SOL")
    assert len(out) == 1 and out[0] is e


def test_token_graph_outgoing_case_insensitive():
    g = TokenGraph()
    e = Edge(
        investment_id="1", platform="x", platform_id=None, chain="Solana",
        underlying_symbol="SOL", receipt_symbol="JitoSOL",
        underlying_address=None, receipt_address=None,
        apy_pct=0.0, tvl_usd=0.0,
    )
    g.add_edge(e)
    assert len(g.outgoing(chain="SOLANA", symbol="sol")) == 1
    assert len(g.outgoing(chain="solana", symbol="SoL")) == 1


def test_token_graph_stats():
    g = TokenGraph()
    g.add_edge(Edge(
        investment_id="1", platform="Jito", platform_id=None, chain="solana",
        underlying_symbol="SOL", receipt_symbol="JitoSOL",
        underlying_address=None, receipt_address=None,
        apy_pct=5.0, tvl_usd=1e9,
    ))
    g.add_edge(Edge(
        investment_id="2", platform="Solayer", platform_id=None, chain="solana",
        underlying_symbol="JitoSOL", receipt_symbol="sjitoSOL",
        underlying_address=None, receipt_address=None,
        apy_pct=2.0, tvl_usd=1e7,
    ))
    s = g.stats()
    assert s["nodes"] == 3  # SOL, JitoSOL, sjitoSOL
    assert s["edges"] == 2
    assert s["chains"] == ["solana"]


# --- find_loops ---

def _build_two_step_graph() -> TokenGraph:
    g = TokenGraph()
    g.add_edge(Edge(
        investment_id="jito", platform="Jito", platform_id="p1", chain="solana",
        underlying_symbol="SOL", receipt_symbol="JitoSOL",
        underlying_address=None, receipt_address=None,
        apy_pct=5.6, tvl_usd=1e9,
    ))
    g.add_edge(Edge(
        investment_id="solayer", platform="Solayer", platform_id="p2", chain="solana",
        underlying_symbol="JitoSOL", receipt_symbol="sjitoSOL",
        underlying_address=None, receipt_address=None,
        apy_pct=2.0, tvl_usd=1e7,
    ))
    return g


def test_find_loops_enumerates_single_and_two_step():
    g = _build_two_step_graph()
    loops = list(find_loops(g, base_asset="SOL", chain="solana", max_steps=2))
    # Single-step (just Jito) + 2-step (Jito → Solayer)
    assert len(loops) == 2
    assert len(loops[0]) == 1
    assert len(loops[1]) == 2
    assert loops[1][0].platform == "Jito"
    assert loops[1][1].platform == "Solayer"


def test_find_loops_respects_max_steps():
    g = _build_two_step_graph()
    loops = list(find_loops(g, base_asset="SOL", chain="solana", max_steps=1))
    assert len(loops) == 1
    assert len(loops[0]) == 1


def test_find_loops_filters_by_apy_floor():
    g = _build_two_step_graph()
    loops = list(find_loops(
        g, base_asset="SOL", chain="solana", max_steps=3, min_step_apy_pct=3.0,
    ))
    # Only the Jito single-step survives (5.6% >= 3%); Solayer leg (2%) filtered
    assert len(loops) == 1
    assert loops[0][0].platform == "Jito"


def test_find_loops_excludes_cycles():
    """If a graph has SOL→A→SOL, traversal must NOT loop back to SOL."""
    g = TokenGraph()
    g.add_edge(Edge(
        investment_id="1", platform="X", platform_id=None, chain="solana",
        underlying_symbol="SOL", receipt_symbol="A",
        underlying_address=None, receipt_address=None,
        apy_pct=5.0, tvl_usd=1e9,
    ))
    g.add_edge(Edge(
        investment_id="2", platform="Y", platform_id=None, chain="solana",
        underlying_symbol="A", receipt_symbol="SOL",
        underlying_address=None, receipt_address=None,
        apy_pct=5.0, tvl_usd=1e9,
    ))
    loops = list(find_loops(g, base_asset="SOL", chain="solana", max_steps=3))
    # Should find SOL→A but NOT SOL→A→SOL (would revisit SOL)
    assert all(
        # last edge's receipt is never the base
        path[-1].receipt_symbol.lower() != "sol"
        for path in loops
    )


def test_find_loops_empty_when_no_edge_from_base():
    g = TokenGraph()
    g.add_edge(Edge(
        investment_id="1", platform="X", platform_id=None, chain="solana",
        underlying_symbol="USDC", receipt_symbol="aUSDC",
        underlying_address=None, receipt_address=None,
        apy_pct=5.0, tvl_usd=1e9,
    ))
    loops = list(find_loops(g, base_asset="SOL", chain="solana", max_steps=3))
    assert loops == []


# --- build_graph (uses stub adapter) ---

class _StubAdapter(DefiAdapter):
    """Records every (key, argv) call to `_cached` and serves canned
    responses keyed by ("search", token, chain) or ("detail", id, chain)."""
    def __init__(self, search_map: dict, detail_map: dict):
        self._search_map = search_map
        self._detail_map = detail_map
        self.search_calls: list[tuple] = []
        self.cached_calls: list[tuple] = []

    def search(self, *, token=None, platform=None, chain=None, product_group=None, page=1):
        self.search_calls.append((token, chain))
        return self._search_map.get((token, chain), [])

    def _cached(self, key, argv):
        self.cached_calls.append(key)
        if key[0] == "detail":
            return self._detail_map.get(key, {})
        return {}


def test_build_graph_walks_seed_to_receipt():
    """Seed=SOL → finds Jito product → adds SOL→JitoSOL edge → queues
    JitoSOL → finds Solayer product → adds JitoSOL→sjitoSOL edge."""
    search_map = {
        ("SOL", "solana"): [{"investmentId": 1}],
        ("JitoSOL", "solana"): [{"investmentId": 2}],
        ("sjitoSOL", "solana"): [],
    }
    detail_map = {
        ("detail", "1", "solana"): _detail(inv_id="1", underlying="SOL", lp="JitoSOL", platform="Jito"),
        ("detail", "2", "solana"): _detail(inv_id="2", underlying="JitoSOL", lp="sjitoSOL", platform="Solayer"),
    }
    adapter = _StubAdapter(search_map, detail_map)
    g = build_graph(adapter=adapter, chains=["solana"], seed_tokens=["SOL"], max_products_per_chain=10)
    assert g.stats()["edges"] == 2
    assert g.stats()["nodes"] == 3  # SOL, JitoSOL, sjitoSOL


def test_build_graph_respects_product_budget():
    """Cap is `max_products_per_chain * len(chains)` total products."""
    search_map = {
        ("SOL", "solana"): [{"investmentId": str(i)} for i in range(10)],
    }
    detail_map = {
        ("detail", str(i), "solana"): _detail(inv_id=str(i), underlying="SOL", lp=f"LP{i}", platform="X")
        for i in range(10)
    }
    adapter = _StubAdapter(search_map, detail_map)
    g = build_graph(adapter=adapter, chains=["solana"], seed_tokens=["SOL"], max_products_per_chain=3)
    # 3 products explored before budget kicks in
    assert g.stats()["edges"] <= 3
