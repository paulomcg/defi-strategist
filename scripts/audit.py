"""Append-only JSONL audit log.

Single shared file at `state/audit.jsonl` unless overridden by
DEFI_STRATEGIST_AUDIT_PATH. Lines are JSON objects with at least an
`event` and `ts_utc` field.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _audit_path() -> Path:
    override = os.environ.get("DEFI_STRATEGIST_AUDIT_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "state" / "audit.jsonl"


def append(event: dict[str, Any]) -> None:
    if "ts_utc" not in event:
        event = {"ts_utc": datetime.now(timezone.utc).isoformat(), **event}
    p = _audit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(event, default=str) + "\n")


def read(*, limit: int | None = None) -> list[dict[str, Any]]:
    p = _audit_path()
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if limit is not None:
        return out[-limit:]
    return out
