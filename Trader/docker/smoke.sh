#!/usr/bin/env bash
# End-to-end smoke on a throwaway local stack (P4-T17; used by P4-T19): builds trader:smoke for the Mac's own
# platform (a separate tag, so it never replaces the amd64 trader:dev), starts it in `api` mode with a
# throwaway PostgreSQL 14 (docker-compose.smoke.yml, ports on 127.0.0.1 only), seeds it, runs the local
# Playwright smoke, and always removes the stack and the temporary env file. It needs no docker/.env.dev:
# every secret is generated for this run and never printed.
#   bash Trader/docker/smoke.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")" # Trader/
project="trader-smoke"
compose_file="$here/docker-compose.smoke.yml"
base_url="http://127.0.0.1:18000"
smoke_user="smoke"
# The smoke database's roles (docker/smoke/init.sql); throwaway passwords that protect nothing.
app_url_host="postgresql+psycopg://trader_smoke_app:smoke-app@127.0.0.1:15432/trader_smoke"

env_file="$(mktemp "${TMPDIR:-/tmp}/trader-smoke-env.XXXXXX")"
export SMOKE_ENV_FILE="$env_file"

cleanup() {
  local code=$?
  if [ "$code" -ne 0 ]; then
    echo "smoke: failed (exit $code); last container logs:" >&2
    docker compose -p "$project" -f "$compose_file" logs --tail 80 trader >&2 || true
  fi
  docker compose -p "$project" -f "$compose_file" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$env_file"
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Fresh secrets for this run only: a Fernet key, a session secret and a random 20-character admin password.
chmod 600 "$env_file"
uv --directory "$root/app" run --quiet python - "$env_file" <<'PY'
import secrets
import string
import sys

from cryptography.fernet import Fernet

alphabet = string.ascii_letters + string.digits
values = {
    "DATABASE_URL": "postgresql+psycopg://trader_smoke_app:smoke-app@db:5432/trader_smoke",
    "MIGRATION_DATABASE_URL": "postgresql+psycopg://trader_smoke_owner:smoke-owner@db:5432/trader_smoke",
    "APP_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "ADMIN_USERNAME": "smoke",
    "ADMIN_PASSWORD_INITIAL": "".join(secrets.choice(alphabet) for _ in range(20)),
    "PUBLIC_BASE_URL": "http://127.0.0.1:18000",
}
with open(sys.argv[1], "w") as fh:
    fh.writelines(f"{key}={value}\n" for key, value in values.items())
PY
smoke_password="$(grep '^ADMIN_PASSWORD_INITIAL=' "$env_file" | cut -d= -f2-)"

echo "smoke: building trader:smoke"
docker build -f "$here/Dockerfile" --build-arg APP_VERSION=smoke -t trader:smoke "$root"

echo "smoke: starting the stack"
docker compose -p "$project" -f "$compose_file" up -d

echo "smoke: waiting for $base_url/api/health"
waited=0
until [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$base_url/api/health" || true)" = "200" ]; do
  if [ "$waited" -ge 60 ]; then
    echo "smoke: the api never became healthy" >&2
    exit 1
  fi
  sleep 2
  waited=$((waited + 2))
done

echo "smoke: seeding"
uv --directory "$root/app" run python -m tests.e2e.seed_smoke --url "$app_url_host"

echo "smoke: running Playwright (local mode)"
SMOKE_MODE=local SMOKE_BASE_URL="$base_url" SMOKE_USER="$smoke_user" SMOKE_PASSWORD="$smoke_password" \
  npm --prefix "$root/web" run e2e -- tests/smoke.spec.ts
echo "smoke: passed"
