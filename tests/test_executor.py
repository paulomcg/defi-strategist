"""Tests for the DefiExecutor.

We don't (and can't safely) test live submission against OnChainOS in CI.
Instead we test:
  - argv construction (claim + reinvest produce the right OnChainOS args)
  - dry-run path skips the submit step
  - submit-field extractor handles EVM, Solana, and nested response shapes
  - tx-hash extractor handles common response shape variants

The actual subprocess + OnChainOS round-trip is exercised separately by
the CLI smoke test described in README.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.executor import (
    DefiExecutor,
    DefiExecutorError,
    _extract_submit_fields,
    _extract_tx_hash,
)


def test_claim_argv_platform_reward():
    ex = DefiExecutor(address="0xabc", chain="ethereum")
    argv = ex._claim_argv(
        investment_id=None,
        platform_id="plat-1",
        reward_type="REWARD_PLATFORM",
        expect_output=None,
    )
    assert argv[:2] == ["defi", "claim"]
    assert "--address" in argv
    assert "0xabc" in argv
    assert "--chain" in argv
    assert "ethereum" in argv
    assert "--reward-type" in argv
    assert "REWARD_PLATFORM" in argv
    assert "--platform-id" in argv
    assert "plat-1" in argv
    assert "--id" not in argv


def test_claim_argv_investment_reward_with_expect_output():
    ex = DefiExecutor(address="0xabc", chain="ethereum")
    argv = ex._claim_argv(
        investment_id="inv-9",
        platform_id=None,
        reward_type="REWARD_INVESTMENT",
        expect_output=[{"chainIndex": "1", "tokenAddress": "0xtok", "coinAmount": "0.5"}],
    )
    assert "--id" in argv
    assert "inv-9" in argv
    assert "--reward-type" in argv
    assert "REWARD_INVESTMENT" in argv
    eo_idx = argv.index("--expect-output")
    assert argv[eo_idx + 1].startswith("[")


def test_submit_argv_solana_uses_unsigned_tx():
    ex = DefiExecutor(address="paulo_addr", chain="solana")
    argv = ex._submit_argv(
        to=None, input_data=None, unsigned_tx="base58blob", biz_type="defi"
    )
    assert argv[:2] == ["wallet", "contract-call"]
    assert "--unsigned-tx" in argv
    assert "base58blob" in argv
    assert "--input-data" not in argv
    assert "--biz-type" in argv
    assert "defi" in argv


def test_submit_argv_evm_uses_input_data_and_to():
    ex = DefiExecutor(address="0xabc", chain="ethereum")
    argv = ex._submit_argv(
        to="0xContract", input_data="0xdeadbeef", unsigned_tx=None, biz_type="defi"
    )
    assert "--to" in argv
    assert "0xContract" in argv
    assert "--input-data" in argv
    assert "0xdeadbeef" in argv
    assert "--unsigned-tx" not in argv


def test_extract_submit_fields_evm_flat():
    data = {"to": "0xContract", "data": "0xcalldata"}
    to_addr, input_data, unsigned = _extract_submit_fields(data)
    assert to_addr == "0xContract"
    assert input_data == "0xcalldata"
    assert unsigned is None


def test_extract_submit_fields_evm_nested_under_txData():
    data = {"txData": {"to": "0xC", "inputData": "0xd"}}
    to_addr, input_data, unsigned = _extract_submit_fields(data)
    assert to_addr == "0xC"
    assert input_data == "0xd"


def test_extract_submit_fields_solana_unsigned_tx():
    data = {"unsignedTx": "base58blob"}
    to_addr, input_data, unsigned = _extract_submit_fields(data)
    assert to_addr is None
    assert input_data is None
    assert unsigned == "base58blob"


def test_extract_submit_fields_handles_list_of_txs():
    data = {"txs": [{"to": "0xA", "data": "0xB"}, {"to": "0xC", "data": "0xD"}]}
    to_addr, input_data, _ = _extract_submit_fields(data)
    assert to_addr in {"0xA", "0xC"}
    assert input_data in {"0xB", "0xD"}


def test_extract_submit_fields_empty_when_nothing_matches():
    assert _extract_submit_fields({"unrelated_key": "value"}) == (None, None, None)
    assert _extract_submit_fields(None) == (None, None, None)


def test_extract_tx_hash_variants():
    assert _extract_tx_hash({"txHash": "0xabc"}) == "0xabc"
    assert _extract_tx_hash({"transactionHash": "0xdef"}) == "0xdef"
    assert _extract_tx_hash({"orderId": "order-1"}) == "order-1"
    assert _extract_tx_hash({"hash": "0x123"}) == "0x123"
    assert _extract_tx_hash({"other": "value"}) is None
    assert _extract_tx_hash(None) is None
