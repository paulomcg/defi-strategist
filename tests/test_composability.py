"""Tests for the composability map / find_receipt lookups."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.composability import (
    RECEIPT_MAP,
    ReceiptMapping,
    find_receipt,
    list_mappings_for_chain,
)


def test_find_receipt_jito_sol_solana():
    assert find_receipt(chain="solana", platform="Jito", deposit_token="SOL") == "JitoSOL"


def test_find_receipt_marinade_sol_solana():
    assert find_receipt(chain="solana", platform="Marinade Finance", deposit_token="SOL") == "mSOL"


def test_find_receipt_lido_eth_ethereum_returns_some_st_token():
    """Lido mapping returns the first matching receipt (stETH or wstETH).
    Both are valid Lido receipts; the first declared wins."""
    result = find_receipt(chain="ethereum", platform="Lido", deposit_token="ETH")
    assert result in {"stETH", "wstETH"}


def test_find_receipt_substring_match_on_platform():
    """Platform_pattern matches by substring (case-insensitive) so e.g.
    'Kamino / Jito Pool' should match the 'jito' pattern."""
    # NB: this assertion documents the substring-match behavior — if
    # Kamino's "Jito Pool" actually mints JitoSOL (verified live), this
    # is the right surface; if not, the mapping needs platform_pattern
    # tightened to 'jito sol' or similar.
    assert find_receipt(
        chain="solana", platform="Kamino / Jito Pool", deposit_token="SOL"
    ) == "JitoSOL"


def test_find_receipt_unknown_returns_none():
    assert find_receipt(chain="solana", platform="UnknownProtocol", deposit_token="SOL") is None
    assert find_receipt(chain="bsc", platform="Jito", deposit_token="SOL") is None  # wrong chain
    assert find_receipt(chain="solana", platform="Jito", deposit_token="USDC") is None  # wrong token


def test_find_receipt_case_insensitive():
    assert find_receipt(chain="SOLANA", platform="JITO", deposit_token="sol") == "JitoSOL"


def test_list_mappings_for_chain():
    solana = list_mappings_for_chain("solana")
    assert len(solana) >= 3  # Jito, Marinade, BlazeStake
    assert all(isinstance(m, ReceiptMapping) for m in solana)
    assert all(m.chain.lower() == "solana" for m in solana)


def test_receipt_map_has_no_empty_entries():
    for m in RECEIPT_MAP:
        assert m.chain
        assert m.platform_pattern
        assert m.deposit_token
        assert m.receipt_token
        assert m.deposit_token != m.receipt_token  # would be a no-op
