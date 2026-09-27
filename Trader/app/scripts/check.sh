#!/usr/bin/env bash
# Quality gate: every builder runs this before committing; the Verifier runs it on trunk.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --quiet
uv run ruff check .
uv run ruff format --check .
uv run mypy trader
uv run pytest -q "$@"
