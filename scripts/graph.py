"""Dynamic DeFi token-edge graph builder.

The v0.2 discoverer relied on a hardcoded `RECEIPT_MAP` of curated
(chain, platform, deposit_token) → receipt_token edges. That works for
a known set of LSTs but doesn't scale — new protocols need code
changes, exotic compositions (Pendle PT/YT, Convex/Aura layers) need
bespoke entries.

v0.3 replaces that with **dynamic graph discovery** sourced directly
from OnChainOS `defi detail`, which exposes `lpToken` (the receipt
minted by the product) and `underlyingToken` (the deposit asset) as
first-class fields. For every product we can enumerate via
`defi list` / `defi search`, we have a canonical edge:

    underlying_token --[product apy_pct, tvl_usd]--> receipt_token

The graph is built once per discovery run, cached, and reused for
cycle detection up to N steps. The hardcoded receipt map remains as
an override / fallback (some products' `lpToken` field is empty when
the receipt is non-tokenized, e.g. lending positions tracked
internally).

## What changes vs v0.2

- **v0.2**: enumeration cap = number of entries in `RECEIPT_MAP`
  (7 hand-curated mappings)
- **v0.3**: enumeration cap = number of products OnChainOS has data
  for (thousands across all chains) — limited only by polling budget

## What stays brittle

- Some products have empty `lpToken` (lending positions, vault
  tokens that aren't ERC20). We skip those rather than guess.
- The graph is intra-chain — cross-chain edges (e.g., stETH → wstETH
  → bridge → wstETH-on-arbitrum) aren't modeled.
- We don't model receipt-token redemption locks (e.g., Lido stETH
  has instant redeem; some LSTs have multi-day unbond). v0.4 will
  surface this as a risk/liquidity dimension.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from .onchain_defi import DefiAdapter, DefiError


@dataclass
class Node:
    """One token in the graph. `key` is `f"{chain}:{symbol}"` for
    intra-chain matching."""
    chain: str
    symbol: str
    address: str | None = None

    @property
    def key(self) -> str:
        return f"{self.chain.lower()}:{self.symbol.lower()}"


@dataclass
class Edge:
    """One product = one directed edge from underlying to receipt.

    `apy_pct` is the product's headline APY (in percent, not ratio).
    `tvl_usd` is the product's TVL. `investment_id` is what the
    executor needs for `defi invest` calldata generation. `platform`
    is the protocol display name. `platform_id` is the OnChainOS
    platform id, used to disambiguate same-token cross-platform
    products."""
    investment_id: str
    platform: str
    platform_id: str | None
    chain: str
    underlying_symbol: str
    receipt_symbol: str
    underlying_address: str | None
    receipt_address: str | None
    apy_pct: float
    tvl_usd: float
    rate_details: list[dict[str, Any]] = field(default_factory=list)
    has_rate_chart: bool = False
    has_tvl_chart: bool = False
    raw_detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenGraph:
    """A built graph: nodes keyed by `{chain}:{symbol}`, edges grouped
    by source node key for fast traversal."""
    nodes: dict[str, Node] = field(default_factory=dict)
    edges_by_source: dict[str, list[Edge]] = field(default_factory=dict)

    def add_edge(self, e: Edge) -> None:
        src = Node(chain=e.chain, symbol=e.underlying_symbol, address=e.underlying_address)
        dst = Node(chain=e.chain, symbol=e.receipt_symbol, address=e.receipt_address)
        self.nodes.setdefault(src.key, src)
        self.nodes.setdefault(dst.key, dst)
        self.edges_by_source.setdefault(src.key, []).append(e)

    def outgoing(self, *, chain: str, symbol: str) -> list[Edge]:
        return self.edges_by_source.get(f"{chain.lower()}:{symbol.lower()}", [])

    def stats(self) -> dict[str, Any]:
        return {
            "nodes": len(self.nodes),
            "edges": sum(len(v) for v in self.edges_by_source.values()),
            "chains": sorted({n.chain for n in self.nodes.values()}),
        }


def build_graph(
    *,
    adapter: DefiAdapter,
    chains: list[str],
    seed_tokens: list[str] | None = None,
    max_products_per_chain: int = 200,
    detail_concurrency: int = 1,
    on_progress=None,
) -> TokenGraph:
    """Build a TokenGraph by walking OnChainOS products on each chain.

    Strategy:
      1. For each (chain, seed_token), call `defi search` to discover
         products that accept the seed token as input.
      2. For each product, call `defi detail` to get the lpToken
         (receipt) — that's the edge target.
      3. Add the edge to the graph. Use the receipt symbol as a
         second-pass seed if not already explored.
      4. Repeat until no new tokens to explore, or until the
         `max_products_per_chain` budget is exhausted.

    `seed_tokens` controls the starting set. Default: the canonical
    base assets on each chain (USDC, USDT, ETH, SOL, WBTC, DAI).

    `detail_concurrency` is reserved for future parallelism; current
    implementation is serial because the OnChainOS CLI rate-limits
    concurrent calls.

    `on_progress(stage: str, payload: dict)` is called at major
    transitions for live feedback during long graph builds.
    """
    seeds = seed_tokens or _default_seeds()
    graph = TokenGraph()
    queue: list[tuple[str, str]] = []  # (chain, symbol) to explore
    explored: set[str] = set()  # `{chain}:{symbol}` already searched

    for chain in chains:
        for s in seeds:
            queue.append((chain, s))

    products_seen = 0
    while queue:
        if products_seen >= max_products_per_chain * len(chains):
            if on_progress:
                on_progress("budget_exhausted", {"products_seen": products_seen})
            break
        chain, symbol = queue.pop(0)
        node_key = f"{chain.lower()}:{symbol.lower()}"
        if node_key in explored:
            continue
        explored.add(node_key)

        try:
            search_results = adapter.search(token=symbol, chain=chain)
        except DefiError as e:
            if on_progress:
                on_progress("search_failed", {"chain": chain, "symbol": symbol, "error": str(e)})
            continue

        for product in search_results:
            if products_seen >= max_products_per_chain * len(chains):
                break
            inv_id = product.get("investmentId") or product.get("investmentID")
            if not inv_id:
                continue
            products_seen += 1
            try:
                detail = adapter._cached(
                    ("detail", str(inv_id), chain),
                    ["defi", "detail", "--investment-id", str(inv_id), "--chain", chain],
                )
            except DefiError as e:
                if on_progress:
                    on_progress("detail_failed", {"investment_id": inv_id, "error": str(e)})
                continue

            edge = _detail_to_edge(detail, chain=chain, fallback_apy=product.get("rate"))
            if edge is None:
                continue
            graph.add_edge(edge)
            if on_progress:
                on_progress("edge_added", {
                    "platform": edge.platform,
                    "underlying": edge.underlying_symbol,
                    "receipt": edge.receipt_symbol,
                    "apy_pct": edge.apy_pct,
                })

            # Explore the receipt symbol next — that's how multi-step
            # compositions surface.
            receipt_key = f"{chain.lower()}:{edge.receipt_symbol.lower()}"
            if receipt_key not in explored:
                queue.append((chain, edge.receipt_symbol))

    if on_progress:
        on_progress("complete", graph.stats())
    return graph


def find_loops(
    graph: TokenGraph,
    *,
    base_asset: str,
    chain: str,
    max_steps: int = 3,
    min_step_apy_pct: float = 0.0,
    min_step_tvl_usd: float = 0.0,
) -> Iterator[list[Edge]]:
    """Enumerate all paths of length 1..max_steps starting from
    (chain, base_asset). Each yielded path is a list of edges in
    traversal order. Paths that would create cycles (visit same node
    twice) are excluded — a "loop" here means a multi-step composition,
    not a graph cycle."""
    base_key = f"{chain.lower()}:{base_asset.lower()}"

    def _walk(current_key: str, path: list[Edge], visited: set[str]):
        if path:  # don't yield empty paths
            yield list(path)
        if len(path) >= max_steps:
            return
        for edge in graph.edges_by_source.get(current_key, []):
            if edge.apy_pct < min_step_apy_pct:
                continue
            if edge.tvl_usd < min_step_tvl_usd:
                continue
            receipt_key = f"{edge.chain.lower()}:{edge.receipt_symbol.lower()}"
            if receipt_key in visited:
                continue  # would create a graph cycle
            path.append(edge)
            visited.add(receipt_key)
            yield from _walk(receipt_key, path, visited)
            path.pop()
            visited.remove(receipt_key)

    yield from _walk(base_key, [], {base_key})


def _detail_to_edge(
    detail: dict[str, Any], *, chain: str, fallback_apy: Any = None
) -> Edge | None:
    """Extract an Edge from a `defi detail` response, or None if the
    product has no usable lpToken (lending positions, vault internals,
    etc)."""
    lp_tokens = detail.get("lpToken") or []
    underlying = detail.get("underlyingToken") or []
    if not lp_tokens or not underlying:
        return None
    lp = lp_tokens[0] if isinstance(lp_tokens, list) else lp_tokens
    und = underlying[0] if isinstance(underlying, list) else underlying
    lp_sym = lp.get("tokenSymbol")
    und_sym = und.get("tokenSymbol")
    if not lp_sym or not und_sym:
        return None
    # Skip self-edges (some products list the same token for both)
    if lp_sym.lower() == und_sym.lower():
        return None
    apy = detail.get("rate") or fallback_apy or 0
    try:
        apy_pct = float(apy) * 100.0 if apy else 0.0
    except (TypeError, ValueError):
        apy_pct = 0.0
    try:
        tvl = float(detail.get("tvl") or 0)
    except (TypeError, ValueError):
        tvl = 0.0
    return Edge(
        investment_id=str(detail.get("investmentId") or ""),
        platform=detail.get("platformName") or "?",
        platform_id=str(detail.get("platformId") or "") or None,
        chain=chain,
        underlying_symbol=und_sym,
        receipt_symbol=lp_sym,
        underlying_address=und.get("tokenAddress"),
        receipt_address=lp.get("tokenAddress"),
        apy_pct=apy_pct,
        tvl_usd=tvl,
        rate_details=detail.get("rateDetails") or [],
        has_rate_chart=bool(detail.get("hasRateChart")),
        has_tvl_chart=bool(detail.get("hasTvlChart")),
        raw_detail=detail,
    )


def _default_seeds() -> list[str]:
    """Canonical base assets that anchor the graph. Discovery walks
    outward from these via receipt-token edges, so we don't need to
    seed exotic tokens — they'll surface organically as receipts of
    products on these seeds."""
    return ["USDC", "USDT", "DAI", "ETH", "WETH", "SOL", "WBTC", "BTC"]
