"""DeFi action executor.

Wraps the two-step OnChainOS write flow:
    1) `onchainos defi {claim,invest,deposit,redeem,collect,withdraw}`
       returns transaction calldata (NOT broadcast).
    2) `onchainos wallet contract-call --input-data | --unsigned-tx ...`
       signs via the Agentic Wallet (TEE-backed) AND broadcasts.

The executor is intentionally narrow in v0.1.5 — it supports the two
actions you need to close the simplest non-trivial DeFi loop:

    - `claim`    — claim pending rewards from a position
    - `reinvest` — deposit a token amount into an investment product
                   (used immediately after `claim` to auto-compound)

Future actions (`redeem`, `withdraw`, `rotate`) follow the same pattern;
they're explicitly out of scope for v0.1.5 to keep the surface tight
and the safety story simple.

## Safety model

Every executor instance has TWO modes:

    - `dry_run=True`  — default; calldata is built but NEVER submitted.
                        Returned Fill records have `submitted=False`
                        and carry the prepared calldata for inspection.
    - `dry_run=False` — live; calldata is built then submitted via
                        `wallet contract-call`. Returned Fill records
                        have `submitted=True` and a `tx_hash`.

The watch loop refuses to set `dry_run=False` unless `--live` was
explicitly passed on the CLI. The same flag also has to coexist with
the wallet-equity kill-switch (sibling of PM's) before any submit
fires.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any

CLI_BIN = "onchainos"
DEFAULT_TIMEOUT_SEC = 30


class DefiExecutorError(Exception):
    """Wraps subprocess / API failures during action execution."""


@dataclass
class DefiExecutor:
    address: str
    chain: str = "solana"
    cli_bin: str = CLI_BIN
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    dry_run: bool = True

    # ---- public verbs ----

    def claim(
        self,
        *,
        investment_id: str | None = None,
        platform_id: str | None = None,
        reward_type: str = "REWARD_PLATFORM",
        expect_output: list[dict[str, Any]] | None = None,
        biz_type: str = "defi",
    ) -> dict[str, Any]:
        """Claim rewards. Returns a Fill record.

        REWARD_PLATFORM requires `--platform-id`; REWARD_INVESTMENT
        requires `--id`. Pass `expect_output` to bypass the auto-fetch
        and lock in expected output tokens at decision time (preferred
        — protects against in-flight reward drift).
        """
        argv = self._claim_argv(
            investment_id=investment_id,
            platform_id=platform_id,
            reward_type=reward_type,
            expect_output=expect_output,
        )
        return self._build_and_submit(
            action="claim",
            build_argv=argv,
            biz_type=biz_type,
            meta={
                "investment_id": investment_id,
                "platform_id": platform_id,
                "reward_type": reward_type,
            },
        )

    def reinvest(
        self,
        *,
        investment_id: str,
        token: str,
        amount_minimal_units: str,
        slippage: str = "0.01",
        biz_type: str = "defi",
    ) -> dict[str, Any]:
        """Deposit `token` (amount in minimal units) into `investment_id`.

        Uses `defi invest` (the high-level convenience that resolves
        tokens + builds calldata in one call). Returns a Fill record.
        """
        argv = [
            "defi", "invest",
            "--investment-id", investment_id,
            "--address", self.address,
            "--token", token,
            "--amount", amount_minimal_units,
            "--chain", self.chain,
            "--slippage", slippage,
        ]
        return self._build_and_submit(
            action="reinvest",
            build_argv=argv,
            biz_type=biz_type,
            meta={
                "investment_id": investment_id,
                "token": token,
                "amount_minimal_units": amount_minimal_units,
                "slippage": slippage,
            },
        )

    # ---- internals ----

    def _claim_argv(
        self,
        *,
        investment_id: str | None,
        platform_id: str | None,
        reward_type: str,
        expect_output: list[dict[str, Any]] | None,
    ) -> list[str]:
        argv = [
            "defi", "claim",
            "--address", self.address,
            "--chain", self.chain,
            "--reward-type", reward_type,
        ]
        if investment_id:
            argv += ["--id", investment_id]
        if platform_id:
            argv += ["--platform-id", platform_id]
        if expect_output:
            argv += ["--expect-output", json.dumps(expect_output)]
        return argv

    def _build_and_submit(
        self,
        *,
        action: str,
        build_argv: list[str],
        biz_type: str,
        meta: dict[str, Any],
    ) -> dict[str, Any]:
        # Step 1 — build calldata.
        calldata_payload = self._run(build_argv)

        # Extract the submit-ready fields. Defi commands return varied
        # shapes; we look for the common keys and surface what we find.
        to_addr, input_data, unsigned_tx = _extract_submit_fields(calldata_payload)

        fill: dict[str, Any] = {
            "ok": True,
            "action": action,
            "address": self.address,
            "chain": self.chain,
            "submitted": False,
            "tx_hash": None,
            "calldata": calldata_payload,
            "to": to_addr,
            "input_data": input_data,
            "unsigned_tx": unsigned_tx,
            "meta": meta,
            "executor": "defi-strategist",
        }

        if self.dry_run:
            fill["dry_run_reason"] = "dry_run=True (default); set dry_run=False or pass --live to submit"
            return fill

        if not (input_data or unsigned_tx):
            raise DefiExecutorError(
                f"submit_calldata_missing — {action} returned no input_data / unsigned_tx; "
                "this product may require a different submit path"
            )
        if not to_addr and input_data:
            raise DefiExecutorError(
                f"submit_to_missing — EVM input_data requires a `to` address; got none from build step"
            )

        # Step 2 — submit via wallet contract-call.
        submit_argv = self._submit_argv(
            to=to_addr,
            input_data=input_data,
            unsigned_tx=unsigned_tx,
            biz_type=biz_type,
        )
        submit_payload = self._run(submit_argv)
        fill["submitted"] = True
        fill["submit_response"] = submit_payload
        fill["tx_hash"] = _extract_tx_hash(submit_payload)
        return fill

    def _submit_argv(
        self,
        *,
        to: str | None,
        input_data: str | None,
        unsigned_tx: str | None,
        biz_type: str,
    ) -> list[str]:
        argv = [
            "wallet", "contract-call",
            "--chain", self.chain,
            "--biz-type", biz_type,
            "--from", self.address,
            "--force",
        ]
        if to:
            argv += ["--to", to]
        if unsigned_tx:
            argv += ["--unsigned-tx", unsigned_tx]
        elif input_data:
            argv += ["--input-data", input_data]
        return argv

    def _run(self, argv: list[str]) -> dict[str, Any]:
        # Coerce all argv elements to str — investment_id and other ids
        # may arrive as int from the OnChainOS adapter; subprocess.run
        # rejects non-string argv elements with an opaque TypeError.
        argv = [str(a) for a in argv]
        try:
            res = subprocess.run(
                [self.cli_bin, *argv],
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
        except FileNotFoundError as e:
            raise DefiExecutorError(f"cli_not_found {e.filename}") from e
        except subprocess.TimeoutExpired as e:
            raise DefiExecutorError(f"cli_timeout {' '.join(argv)}") from e

        if res.returncode != 0:
            tail = (res.stderr or res.stdout).strip().splitlines()[-1:]
            detail = tail[0] if tail else "non-zero exit"
            if "OK-ACCESS-KEY" in detail or "auth" in detail.lower():
                raise DefiExecutorError("wallet_not_logged_in")
            raise DefiExecutorError(f"cli_error {detail}")

        try:
            payload = json.loads(res.stdout)
        except json.JSONDecodeError as e:
            raise DefiExecutorError(f"cli_output_invalid {e}") from e

        if not payload.get("ok"):
            raise DefiExecutorError(f"api_error {payload.get('error') or payload}")
        return payload.get("data") or {}


# ---- response-shape helpers ----

def _extract_submit_fields(
    data: Any,
) -> tuple[str | None, str | None, str | None]:
    """Pull (to, input_data, unsigned_tx) out of a defi-build response.

    Real shapes vary by product type:
      - EVM lending/earn: { to, data | inputData, value }
      - V3 add liquidity: { to, data, value, ... }
      - Solana: { unsignedTx | encodedTx | base58Tx }
    This helper is best-effort; new shapes get fallthroughs added as
    they're observed.
    """
    if not isinstance(data, dict):
        return None, None, None
    # Sometimes the calldata is nested under "txData" / "transaction" / "tx"
    candidates: list[dict[str, Any]] = [data]
    for nest_key in ("txData", "transaction", "tx", "txs"):
        v = data.get(nest_key)
        if isinstance(v, dict):
            candidates.append(v)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            candidates.extend(v)

    for c in candidates:
        to_addr = c.get("to") or c.get("contractAddress") or c.get("contract")
        input_data = (
            c.get("data")
            or c.get("inputData")
            or c.get("calldata")
        )
        unsigned_tx = (
            c.get("unsignedTx")
            or c.get("encodedTx")
            or c.get("base58Tx")
            or c.get("solanaUnsignedTx")
        )
        if input_data or unsigned_tx or to_addr:
            return to_addr, input_data, unsigned_tx
    return None, None, None


def _extract_tx_hash(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    return (
        data.get("txHash")
        or data.get("orderId")
        or data.get("transactionHash")
        or data.get("hash")
    )
