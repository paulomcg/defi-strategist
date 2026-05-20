"""Tests for the OnChainOS adapter response normalization helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.onchain_defi import _extract_list, _f, _pct, normalize_product


def test_extract_list_from_investment_list_key():
    data = {"investmentList": [{"name": "a"}, {"name": "b"}]}
    assert _extract_list(data) == [{"name": "a"}, {"name": "b"}]


def test_extract_list_from_list_key():
    data = {"list": [{"x": 1}]}
    assert _extract_list(data) == [{"x": 1}]


def test_extract_list_from_items_key():
    data = {"items": [{"y": 2}]}
    assert _extract_list(data) == [{"y": 2}]


def test_extract_list_from_bare_list():
    data = [{"z": 3}]
    assert _extract_list(data) == [{"z": 3}]


def test_extract_list_empty_when_no_match():
    assert _extract_list({"unknown_key": [1, 2, 3]}) == []
    assert _extract_list("not a dict") == []
    assert _extract_list(None) == []


def test_pct_converts_ratio_to_percent():
    assert _pct(0.0472) == 4.72
    assert _pct("0.5") == 50.0
    assert _pct(None) == 0.0
    assert _pct("not a number") == 0.0


def test_f_safe_coerce():
    assert _f("3.14") == 3.14
    assert _f(None) == 0.0
    assert _f("bad") == 0.0
    assert _f(42) == 42.0


def test_normalize_product_canonical_shape():
    raw = {
        "investmentId": "abc",
        "investmentName": "USDC",
        "platformName": "Aave V3",
        "platformId": "p1",
        "chainName": "ethereum",
        "rate": "0.0472",
        "tvl": "1000000",
        "investmentType": "earn",
    }
    n = normalize_product(raw)
    assert n["investment_id"] == "abc"
    assert n["name"] == "USDC"
    assert n["platform"] == "Aave V3"
    assert n["platform_id"] == "p1"
    assert n["chain"] == "ethereum"
    assert n["apy_pct"] == 4.72
    assert n["tvl_usd"] == 1000000.0
    assert n["product_type"] == "earn"
    assert n["raw"] is raw


def test_normalize_product_fallback_keys():
    """Older / alternative shape uses `name` / `platform` / `apy` etc."""
    raw = {
        "id": "xyz",
        "name": "ETH",
        "platform": "Lido",
        "chain": "ethereum",
        "apy": 0.05,
    }
    n = normalize_product(raw)
    assert n["investment_id"] == "xyz"
    assert n["name"] == "ETH"
    assert n["platform"] == "Lido"
    assert n["apy_pct"] == 5.0
