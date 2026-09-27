#!/usr/bin/env bash
# Quality gate: every builder runs this before committing; the Verifier runs it on trunk.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --quiet
uv run ruff check .
uv run ruff format --check .
uv run mypy trader
uv run pytest -q "$@"

# The web app (Phase 4): type check and Vitest, whenever the web project is present. With it present, a
# missing npm is a failure, so the gate is never silently incomplete.
if [ -f ../web/package.json ]; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "check.sh: npm is required for the web check (Trader/web/package.json exists)" >&2
    exit 1
  fi
  if [ ! -d ../web/node_modules ] || [ ../web/package-lock.json -nt ../web/node_modules ]; then
    npm --prefix ../web ci
  fi
  npm --prefix ../web run -s check
fi
