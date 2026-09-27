#!/usr/bin/env bash
# Run the trader CLI against the dev environment (secrets from docker/.env.dev).
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --env-file ../docker/.env.dev trader "$@"
