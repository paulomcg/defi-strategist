"""Tests for the alerts rules engine."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.rules import Alert, evaluate


def test_min_apy_floor_fires_below_threshold():
    positions = [
        {"platform": "Aave V3", "name": "USDC", "apy_pct": 2.5, "value_usd": 100},
    ]
    rules = [
        {"id": "yields-falling", "type": "min_apy_floor", "threshold_pct": 5.0},
    ]
    alerts, _ = evaluate(positions=positions, opportunities=[], rules=rules)
    assert len(alerts) == 1
    assert alerts[0].kind == "apy_below_floor"
    assert "2.50%" in alerts[0].message
    assert alerts[0].severity == "warn"


def test_min_apy_floor_silent_above_threshold():
    positions = [
        {"platform": "Aave V3", "name": "USDC", "apy_pct": 7.5, "value_usd": 100},
    ]
    rules = [
        {"id": "yields-falling", "type": "min_apy_floor", "threshold_pct": 5.0},
    ]
    alerts, _ = evaluate(positions=positions, opportunities=[], rules=rules)
    assert len(alerts) == 0


def test_max_concentration_fires_when_one_platform_dominates():
    positions = [
        {"platform": "Kamino", "name": "USDC", "apy_pct": 8.0, "value_usd": 800},
        {"platform": "Aave V3", "name": "USDT", "apy_pct": 4.0, "value_usd": 200},
    ]
    rules = [
        {"id": "concentration", "type": "max_protocol_concentration", "threshold_pct": 50.0},
    ]
    alerts, _ = evaluate(positions=positions, opportunities=[], rules=rules)
    assert len(alerts) == 1
    assert alerts[0].kind == "protocol_concentration_exceeded"
    assert alerts[0].context["platform"] == "Kamino"
    assert abs(alerts[0].context["share_pct"] - 80.0) < 0.01


def test_max_concentration_silent_when_balanced():
    positions = [
        {"platform": "Kamino", "name": "USDC", "apy_pct": 8.0, "value_usd": 500},
        {"platform": "Aave V3", "name": "USDT", "apy_pct": 4.0, "value_usd": 500},
    ]
    rules = [
        {"id": "concentration", "type": "max_protocol_concentration", "threshold_pct": 50.0},
    ]
    alerts, _ = evaluate(positions=positions, opportunities=[], rules=rules)
    assert len(alerts) == 0


def test_opportunity_above_skips_already_held():
    positions = [
        {"platform": "Kamino", "name": "USDC", "apy_pct": 6.0, "value_usd": 1000},
    ]
    opportunities = [
        {"platform": "Kamino", "name": "USDC", "apy_pct": 7.0},  # held — skip
        {"platform": "Morpho", "name": "USDC", "apy_pct": 8.0},  # not held — fire
        {"platform": "Aave V3", "name": "USDC", "apy_pct": 3.0}, # below threshold
    ]
    rules = [
        {"id": "rotate", "type": "opportunity_above", "threshold_pct": 5.0},
    ]
    alerts, _ = evaluate(positions=positions, opportunities=opportunities, rules=rules)
    assert len(alerts) == 1
    assert alerts[0].kind == "opportunity_above_threshold"
    assert alerts[0].context["opportunity"]["platform"] == "Morpho"


def test_unknown_rule_type_warns():
    rules = [{"id": "bad", "type": "nonexistent_rule"}]
    alerts, _ = evaluate(positions=[], opportunities=[], rules=rules)
    assert len(alerts) == 1
    assert alerts[0].kind == "unknown_rule_type"


def test_multiple_rules_compose():
    positions = [
        {"platform": "Aave V3", "name": "USDC", "apy_pct": 2.0, "value_usd": 800},
        {"platform": "Morpho", "name": "DAI", "apy_pct": 5.0, "value_usd": 200},
    ]
    opportunities = [
        {"platform": "Kamino", "name": "USDC", "apy_pct": 9.0},
    ]
    rules = [
        {"id": "floor", "type": "min_apy_floor", "threshold_pct": 3.0},
        {"id": "conc", "type": "max_protocol_concentration", "threshold_pct": 60.0},
        {"id": "rot", "type": "opportunity_above", "threshold_pct": 7.0},
    ]
    alerts, _ = evaluate(positions=positions, opportunities=opportunities, rules=rules)
    kinds = sorted({a.kind for a in alerts})
    assert kinds == [
        "apy_below_floor",
        "opportunity_above_threshold",
        "protocol_concentration_exceeded",
    ]
