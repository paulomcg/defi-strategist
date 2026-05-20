"""Composability map — known receipt-token transformations.

When you deposit asset A into product P, you typically receive a
"receipt token" R that represents your stake (e.g., deposit SOL into
Jito → receive JitoSOL). Some receipt tokens are themselves usable as
inputs to OTHER products — that's the composition that creates yield
loops (e.g., JitoSOL → Solayer = SOL staking yield + restaking yield
stacked).

OnChainOS does not currently expose this relationship as first-class
metadata. Until it does, we maintain a hardcoded map of the most
common compositions. This is INTENTIONALLY narrow — we only include
mappings where:
  1. The receipt token is independently searchable via `defi search`
     (so the composition can actually be enumerated)
  2. The protocol relationship is stable and well-known (not a fly-
     by-night that might shuffle next week)

Verified at build time via:
  $ onchainos defi search --token <receipt> --chain <chain>

The map is read by `discoverer.py` to surface 2-step compositions
that exist on the user's chain.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReceiptMapping:
    """One known (chain, platform, deposit_token) → receipt_token edge.

    Used by the discoverer: after enumerating step-1 products, look up
    each (chain, platform, deposit_token) in this map; if a mapping
    exists, the receipt_token is the step-2 input candidate.
    """
    chain: str
    platform_pattern: str   # case-insensitive substring match on `platformName`
    deposit_token: str
    receipt_token: str
    notes: str = ""


# Curated mappings — Solana and Ethereum LSTs / wrapped derivatives.
# Each entry has been spot-checked: searching for `receipt_token`
# returns at least one valid follow-up product on the same chain.
RECEIPT_MAP: list[ReceiptMapping] = [
    # --- Solana liquid staking → restaking ---
    ReceiptMapping(
        chain="solana",
        platform_pattern="jito",
        deposit_token="SOL",
        receipt_token="JitoSOL",
        notes="Jito liquid staking; JitoSOL accepted by Solayer for restake",
    ),
    ReceiptMapping(
        chain="solana",
        platform_pattern="marinade",
        deposit_token="SOL",
        receipt_token="mSOL",
        notes="Marinade liquid staking; mSOL accepted by Solayer for restake",
    ),
    ReceiptMapping(
        chain="solana",
        platform_pattern="blazestake",
        deposit_token="SOL",
        receipt_token="bSOL",
        notes="BlazeStake liquid staking",
    ),

    # --- Ethereum liquid staking → restaking / lending collateral ---
    ReceiptMapping(
        chain="ethereum",
        platform_pattern="lido",
        deposit_token="ETH",
        receipt_token="stETH",
        notes="Lido staking; stETH accepted by Aave / Puffer / Pendle",
    ),
    ReceiptMapping(
        chain="ethereum",
        platform_pattern="lido",
        deposit_token="ETH",
        receipt_token="wstETH",
        notes="Wrapped Lido stake; wstETH accepted by Aave V3 / Morpho",
    ),
    ReceiptMapping(
        chain="ethereum",
        platform_pattern="rocket pool",
        deposit_token="ETH",
        receipt_token="rETH",
        notes="Rocket Pool decentralized staking",
    ),
    ReceiptMapping(
        chain="ethereum",
        platform_pattern="coinbase",
        deposit_token="ETH",
        receipt_token="cbETH",
        notes="Coinbase liquid staking",
    ),

    # --- Base ---
    ReceiptMapping(
        chain="base",
        platform_pattern="coinbase",
        deposit_token="ETH",
        receipt_token="cbETH",
        notes="Coinbase liquid staking on Base",
    ),
]


def find_receipt(
    *, chain: str, platform: str, deposit_token: str
) -> str | None:
    """Return the known receipt token for a (chain, platform, deposit)
    triple, or None if no mapping exists.

    Matches are case-insensitive on both chain and platform. The
    platform comparison is substring-based to handle the various names
    OnChainOS returns (e.g., "Lido", "Lido Finance", "Lido V2").
    """
    chain_l = (chain or "").lower()
    platform_l = (platform or "").lower()
    token_l = (deposit_token or "").lower()
    for m in RECEIPT_MAP:
        if m.chain.lower() != chain_l:
            continue
        if m.deposit_token.lower() != token_l:
            continue
        if m.platform_pattern.lower() in platform_l:
            return m.receipt_token
    return None


def list_mappings_for_chain(chain: str) -> list[ReceiptMapping]:
    chain_l = (chain or "").lower()
    return [m for m in RECEIPT_MAP if m.chain.lower() == chain_l]
