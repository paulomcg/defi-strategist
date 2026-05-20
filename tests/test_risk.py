"""Tests for risk scoring."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.risk import (
    _apy_volatility,
    _extract_numeric_series,
    _series_age_days,
    _tvl_stability,
    score_loop_breakdowns,
    score_step,
    RiskBreakdown,
)
from scripts.onchain_defi import DefiAdapter


# ---- _extract_numeric_series ----

def test_extract_series_from_list_of_rate_points():
    data = {"list": [{"ts": 1, "rate": "0.05"}, {"ts": 2, "rate": "0.06"}]}
    assert _extract_numeric_series(data, ("rate",)) == [0.05, 0.06]


def test_extract_series_from_bare_list():
    data = [{"ts": 1, "value": 100}, {"ts": 2, "value": 110}]
    assert _extract_numeric_series(data, ("value",)) == [100.0, 110.0]


def test_extract_series_tries_multiple_keys():
    data = {"items": [{"ts": 1, "apy": "0.05"}]}
    assert _extract_numeric_series(data, ("rate", "value", "apy")) == [0.05]


def test_extract_series_skips_non_numeric():
    data = {"list": [{"ts": 1, "rate": "not a number"}, {"ts": 2, "rate": "0.05"}]}
    assert _extract_numeric_series(data, ("rate",)) == [0.05]


def test_extract_series_empty_when_none():
    assert _extract_numeric_series(None) == []
    assert _extract_numeric_series({}) == []


# ---- _apy_volatility / _tvl_stability ----

def test_apy_volatility_steady_yields_high_score():
    """An APY that doesn't move = volatility 0 = score 100."""
    vol, score = _apy_volatility([0.05] * 30)
    assert vol == 0.0
    assert score == 100.0


def test_apy_volatility_swinging_yields_low_score():
    """50/50 swings around mean → volatility ≈ 0.33 (Bessel-corrected) → mid score."""
    vol, score = _apy_volatility([0.02, 0.10] * 15)  # mean 0.06, stdev ≈ 0.04
    assert vol > 0.5
    assert score < 50


def test_apy_volatility_too_few_points_returns_neutral():
    assert _apy_volatility([]) == (0.0, 50.0)
    assert _apy_volatility([0.05]) == (0.0, 50.0)


def test_tvl_stability_steady_yields_high_score():
    stab, score = _tvl_stability([1_000_000] * 30)
    assert stab > 0.99
    assert score >= 99


def test_tvl_stability_collapsing_yields_low_score():
    """TVL halving day-over-day → high stdev/mean → low stability."""
    points = [1_000_000 / (1 + i * 0.5) for i in range(30)]
    stab, score = _tvl_stability(points)
    assert stab < 0.5
    assert score < 50


def test_tvl_stability_too_few_points_returns_neutral():
    assert _tvl_stability([]) == (0.0, 50.0)
    assert _tvl_stability([1e6]) == (0.0, 50.0)


# ---- _series_age_days ----

def test_series_age_days_ms_timestamps():
    import time
    one_year_ago_ms = int((time.time() - 365 * 86400) * 1000)
    data = [{"ts": one_year_ago_ms, "rate": "0.05"}, {"ts": int(time.time() * 1000), "rate": "0.06"}]
    age = _series_age_days(data)
    assert age is not None
    assert 360 <= age <= 370


def test_series_age_days_seconds_timestamps():
    import time
    one_month_ago_s = int(time.time() - 30 * 86400)
    data = [{"ts": one_month_ago_s, "rate": "0.05"}, {"ts": int(time.time()), "rate": "0.06"}]
    age = _series_age_days(data)
    assert age is not None
    assert 29 <= age <= 31


def test_series_age_days_returns_none_when_no_timestamps():
    assert _series_age_days(None) is None
    assert _series_age_days([{"rate": "0.05"}]) is None


# ---- score_loop_breakdowns ----

def _bd(score: float, platform: str = "P", inv_id: str = "i") -> RiskBreakdown:
    return RiskBreakdown(
        investment_id=inv_id, platform=platform,
        apy_volatility=0.0, apy_volatility_score=score,
        tvl_stability=1.0, tvl_stability_score=score,
        age_days=30, composite_score=score, data_quality="full",
    )


def test_score_loop_breakdowns_weakest_link():
    """Loop score = min of per-step composite scores."""
    out = score_loop_breakdowns([_bd(90, "A"), _bd(50, "B"), _bd(80, "C")])
    assert out["loop_score"] == 50.0
    assert out["weakest_step"]["platform"] == "B"


def test_score_loop_breakdowns_empty_returns_none():
    out = score_loop_breakdowns([])
    assert out["loop_score"] is None


def test_score_loop_breakdowns_single_step():
    out = score_loop_breakdowns([_bd(75, "X", "i1")])
    assert out["loop_score"] == 75.0
    assert out["weakest_step"]["platform"] == "X"


# ---- score_step (uses stub adapter) ----

class _StubAdapter(DefiAdapter):
    def __init__(self, rate_chart_data=None, tvl_chart_data=None):
        self._rate = rate_chart_data
        self._tvl = tvl_chart_data
    def rate_chart(self, *, investment_id, chain, platform_id=None):
        return self._rate or []
    def _cached(self, key, argv):
        if key[0] == "tvl-chart":
            return self._tvl or []
        return {}


def test_score_step_full_data_yields_composite():
    adapter = _StubAdapter(
        rate_chart_data={"list": [{"ts": i, "rate": 0.05} for i in range(30)]},
        tvl_chart_data={"list": [{"ts": i, "tvl": 1_000_000} for i in range(30)]},
    )
    bd = score_step(
        investment_id="i1", platform="P1", chain="solana", adapter=adapter,
    )
    assert bd.data_quality == "full"
    assert bd.composite_score >= 90  # both signals are perfectly stable


def test_score_step_no_data_yields_neutral():
    adapter = _StubAdapter(rate_chart_data=[], tvl_chart_data=[])
    bd = score_step(
        investment_id="i1", platform="P1", chain="solana", adapter=adapter,
        has_rate_chart=False, has_tvl_chart=False,
    )
    assert bd.data_quality == "none"
    assert bd.composite_score == 50.0
    assert "neutral" in bd.notes


def test_score_step_partial_data():
    """If only one of rate/tvl chart is available, composite is just
    that one's score (no double-counting)."""
    adapter = _StubAdapter(
        rate_chart_data={"list": [{"ts": i, "rate": 0.05} for i in range(30)]},
        tvl_chart_data=[],
    )
    bd = score_step(
        investment_id="i1", platform="P1", chain="solana", adapter=adapter,
        has_tvl_chart=False,
    )
    assert bd.data_quality == "partial"
    assert bd.composite_score >= 90  # APY-only score
