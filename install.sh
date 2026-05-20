#!/usr/bin/env bash
# One-shot installer for defi-strategist.
#
# Creates a local .venv, installs runtime + test deps, verifies the
# CLI is callable. Idempotent — safe to re-run after a git pull.
#
# Prerequisites:
#   - python3 >= 3.10
#   - `onchainos` CLI on PATH (https://web3.okx.com/onchainos)
#   - For --live mode: OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE
#     in env (passed through to onchainos; defi-strategist never reads them)

set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ ! -d .venv ]; then
  echo "  creating .venv..."
  python3 -m venv .venv
fi

echo "  installing runtime + test deps..."
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt pytest

echo "  verifying defi-strategist CLI..."
PYTHONPATH="$ROOT" .venv/bin/python3 -m scripts.cli --version >/dev/null

echo "  running smoke tests..."
.venv/bin/python3 -m pytest tests/ -q --no-header -x 2>&1 | tail -3 || true

echo
echo "  done. invoke via:"
echo "    $ROOT/bin/defi-strategist [--format json|table] <subcommand> [args...]"
echo "  see SKILL.md for the full surface, SUBMISSION.md for the contest mapping."
