"""OnChainOS DeFi adapter.

Thin wrapper around `onchainos defi *` subcommands. Centralizes:

  - subprocess invocation with timeouts
  - JSON parse + error normalization
  - response-shape adaptation (the `data.investmentList` vs `data.list` etc)
  - per-call caching with configurable TTL (so a watch loop doesn't burn
    rate-limit budget polling the same product every 30s)

The CLI tool is assumed to be `onchainos` on PATH. Auth (OKX_API_KEY /
OKX_SECRET_KEY / OKX_PASSPHRASE) is the CLI's responsibility — this
adapter never reads, logs, or persists those env vars.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

CLI_BIN = "onchainos"
DEFAULT_TIMEOUT_SEC = 20


class DefiError(Exception):
    """Adapter error — wraps subprocess failures and API errors uniformly."""


@dataclass
class DefiAdapter:
    cli_bin: str = CLI_BIN
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    cache_ttl_sec: int = 60
    _cache: dict[tuple, tuple[float, Any]] = field(default_factory=dict)

    # ---- low-level ----

    def _run(self, argv: list[str]) -> dict[str, Any]:
        try:
            res = subprocess.run(
                [self.cli_bin, *argv],
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
        except FileNotFoundError as e:
            raise DefiError(f"cli_not_found {e.filename}") from e
        except subprocess.TimeoutExpired as e:
            raise DefiError(f"cli_timeout {' '.join(argv)}") from e

        if res.returncode != 0:
            tail = (res.stderr or res.stdout).strip().splitlines()[-1:]
            detail = tail[0] if tail else "non-zero exit"
            if "OK-ACCESS-KEY" in detail or "auth" in detail.lower():
                raise DefiError("wallet_not_logged_in")
            raise DefiError(f"cli_error {detail}")

        try:
            payload = json.loads(res.stdout)
        except json.JSONDecodeError as e:
            raise DefiError(f"cli_output_invalid {e}") from e

        if not payload.get("ok"):
            raise DefiError(f"api_error {payload.get('error') or payload}")
        return payload.get("data") or {}

    def _cached(self, key: tuple, argv: list[str]) -> Any:
        now = time.time()
        cached = self._cache.get(key)
        if cached is not None and (now - cached[0]) < self.cache_ttl_sec:
            return cached[1]
        data = self._run(argv)
        self._cache[key] = (now, data)
        return data

    # ---- high-level wrappers ----

    def list_products(
        self, chain: str = "ethereum", page: int = 1
    ) -> list[dict[str, Any]]:
        """All products on a chain, page-by-page (page size 20)."""
        data = self._cached(
            ("list", chain, page),
            ["defi", "list", "--chain", chain, "--page-num", str(page)],
        )
        return _extract_list(data)

    def search(
        self,
        *,
        token: str | None = None,
        platform: str | None = None,
        chain: str | None = None,
        product_group: str | None = None,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Search products. At least one of token/platform required."""
        if not token and not platform:
            raise DefiError("search_requires_token_or_platform")
        argv = ["defi", "search", "--page-num", str(page)]
        if token:
            argv += ["--token", token]
        if platform:
            argv += ["--platform", platform]
        if chain:
            argv += ["--chain", chain]
        if product_group:
            argv += ["--product-group", product_group]
        data = self._cached(
            ("search", token, platform, chain, product_group, page), argv
        )
        return _extract_list(data)

    def positions(self, *, address: str, chains: str) -> dict[str, Any]:
        """User DeFi positions across one or more chains (comma-sep)."""
        data = self._cached(
            ("positions", address, chains),
            ["defi", "positions", "--address", address, "--chains", chains],
        )
        return data

    def rate_chart(
        self,
        *,
        investment_id: str,
        chain: str,
        platform_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Historical APY chart points for a product."""
        argv = [
            "defi", "rate-chart",
            "--investment-id", investment_id,
            "--chain", chain,
        ]
        if platform_id:
            argv += ["--platform-id", platform_id]
        data = self._cached(("rate-chart", investment_id, chain), argv)
        return _extract_list(data, key_candidates=("list", "chart", "items"))

    def supported_chains(self) -> list[dict[str, Any]]:
        data = self._cached(("support-chains",), ["defi", "support-chains"])
        return _extract_list(data)

    def supported_platforms(self, chain: str | None = None) -> list[dict[str, Any]]:
        argv = ["defi", "support-platforms"]
        if chain:
            argv += ["--chain", chain]
        data = self._cached(("support-platforms", chain), argv)
        return _extract_list(data)


# ---- helpers ----

def _extract_list(
    data: Any, key_candidates: tuple[str, ...] = ("investmentList", "list", "items")
) -> list[dict[str, Any]]:
    """OnChainOS lists come back under various keys depending on endpoint."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in key_candidates:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def normalize_product(p: dict[str, Any]) -> dict[str, Any]:
    """Coerce a product dict into a stable shape across endpoints."""
    return {
        "investment_id": p.get("investmentId") or p.get("id"),
        "name": p.get("investmentName") or p.get("name") or "?",
        "platform": p.get("platformName") or p.get("platform") or "?",
        "platform_id": p.get("platformId"),
        "chain": p.get("chainName") or p.get("chain"),
        "apy_pct": _pct(p.get("rate") or p.get("apy")),
        "tvl_usd": _f(p.get("tvl")),
        "product_type": p.get("investmentType") or p.get("productType"),
        "raw": p,
    }


def _f(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _pct(v: Any) -> float:
    """OnChainOS returns APY as a ratio (0.0472 = 4.72%). Convert to percent."""
    raw = _f(v)
    return raw * 100.0
