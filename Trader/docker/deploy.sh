#!/usr/bin/env bash
# Build, ship and recreate Trader on the shared Docker host (P4-T17, SPEC §15 and §15.1; FinanceTracker's
# build-and-ship pattern). One command, from any directory (every path is resolved from this file):
#   bash Trader/docker/deploy.sh dev
#   TRADER_TAG=<tag> bash Trader/docker/deploy.sh prod
# Env file: $TRADER_ENV_FILE when set (an absolute path; a worktree has no docker/.env.dev, so pass the main
# checkout's), else docker/.env.<env>. It is checked for the required key NAMES only (values are never read
# or printed) and handed to compose as TRADER_ENV_FILE; compose reads it on the Mac, at run time only.
# Optional: TRADER_HEALTH_URL, TRADER_HEALTH_TIMEOUT (120 s), TRADER_HEALTH_INTERVAL (3 s),
# TRADER_DEPLOY_SSH (stephen@192.168.68.73).
set -euo pipefail

usage() {
  echo "usage: [TRADER_ENV_FILE=/abs/path] [TRADER_TAG=tag] bash docker/deploy.sh dev|prod" >&2
  exit 2
}

[ "$#" -eq 1 ] || usage
env_name="$1"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")" # Trader/: the build context

case "$env_name" in
  dev)
    tag="dev"
    host="trader-dev.sunspinner.ca"
    ;;
  prod)
    if [ -z "${TRADER_TAG:-}" ]; then
      echo "deploy: prod needs TRADER_TAG (the image tag to build and run)" >&2
      exit 1
    fi
    tag="$TRADER_TAG"
    host="trader.sunspinner.ca"
    ;;
  *) usage ;;
esac

env_file="${TRADER_ENV_FILE:-$here/.env.$env_name}"
if [ ! -f "$env_file" ]; then
  echo "deploy: env file not found: $env_file" >&2
  exit 1
fi
env_file="$(cd "$(dirname "$env_file")" && pwd)/$(basename "$env_file")"

has_key() {
  grep -Eq "^[[:space:]]*(export[[:space:]]+)?$1=[^[:space:]]" "$env_file"
}
for key in DATABASE_URL MIGRATION_DATABASE_URL APP_ENCRYPTION_KEY SESSION_SECRET; do
  if ! has_key "$key"; then
    echo "deploy: $env_file has no $key" >&2
    exit 1
  fi
done
for key in ADMIN_USERNAME ADMIN_PASSWORD_INITIAL; do
  if ! has_key "$key"; then
    echo "deploy: warning: $env_file has no $key (needed only until the first web user exists)" >&2
  fi
done

export TRADER_ENV_FILE="$env_file"
export TRADER_TAG="$tag"
image="trader:$tag"
app_version="$(git -C "$root" describe --always --dirty 2>/dev/null || echo unknown)"
ssh_target="${TRADER_DEPLOY_SSH:-stephen@192.168.68.73}"
health_url="${TRADER_HEALTH_URL:-https://$host/api/health}"
health_timeout="${TRADER_HEALTH_TIMEOUT:-120}"
health_interval="${TRADER_HEALTH_INTERVAL:-3}"

echo "deploy: building $image ($app_version) for linux/amd64"
DOCKER_DEFAULT_PLATFORM=linux/amd64 docker --context desktop-linux build --platform linux/amd64 \
  -f "$here/Dockerfile" --build-arg "APP_VERSION=$app_version" -t "$image" "$root"

echo "deploy: shipping $image to $ssh_target"
docker --context desktop-linux save "$image" | ssh "$ssh_target" docker load

echo "deploy: recreating the trader service ($env_name)"
docker --context shared-docker-server compose -f "$here/docker-compose.$env_name.yml" \
  up -d --no-build --no-deps --force-recreate trader

echo "deploy: waiting for $health_url (up to ${health_timeout}s)"
waited=0
while true; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$health_url" || true)"
  if [ "$code" = "200" ]; then
    echo "deploy: health $code after ${waited}s"
    exit 0
  fi
  if [ "$waited" -ge "$health_timeout" ]; then
    echo "deploy: health never returned 200 (last status ${code:-none})" >&2
    exit 1
  fi
  sleep "$health_interval"
  waited=$((waited + health_interval))
done
