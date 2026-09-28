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
# prod (P6-T6, plan D5) refuses before any build: TRADER_TAG dev or latest, a dirty working tree, or a HEAD
# that is not exactly the tag TRADER_TAG.
# Downtime (P6-T6, plan D3): prints `deploy: down from <UTC>` just before the recreate and `deploy: up at <UTC>`
# when health returns 200, then the cron_gap.py command that lists the cron lines to catch up by hand. If the
# recreate or the health wait fails, the `down from` stamp and the hint are still printed.
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
    container="trader-dev"
    ;;
  prod)
    if [ -z "${TRADER_TAG:-}" ]; then
      echo "deploy: prod needs TRADER_TAG (the image tag to build and run)" >&2
      exit 1
    fi
    tag="$TRADER_TAG"
    host="trader.sunspinner.ca"
    container="trader"
    ;;
  *) usage ;;
esac

# The prod guards: prod is built only from a clean checkout of exactly the git tag it is named after.
if [ "$env_name" = "prod" ]; then
  if [ "$tag" = "dev" ] || [ "$tag" = "latest" ]; then
    echo "deploy: prod refused: TRADER_TAG=$tag is not a release tag (use the git tag, e.g. v1.0.0)" >&2
    exit 1
  fi
  if ! porcelain="$(git -C "$root" status --porcelain)"; then
    echo "deploy: prod refused: git status failed in $root" >&2
    exit 1
  fi
  if [ -n "$porcelain" ]; then
    echo "deploy: prod refused: the working tree is dirty (commit or remove the changes)" >&2
    exit 1
  fi
  # --match keeps another tag on the same commit (e.g. a phase tag) from hiding this one.
  exact="$(git -C "$root" describe --exact-match --tags --match "$tag" HEAD 2>/dev/null || true)"
  if [ "$exact" != "$tag" ]; then
    head="$(git -C "$root" describe --always --tags HEAD 2>/dev/null || echo unknown)"
    echo "deploy: prod refused: HEAD is $head, not the tag $tag (check out the tag first)" >&2
    exit 1
  fi
fi

env_file="${TRADER_ENV_FILE:-$here/.env.$env_name}"
if [ ! -f "$env_file" ]; then
  echo "deploy: env file not found: $env_file" >&2
  exit 1
fi
env_file="$(cd "$(dirname "$env_file")" && pwd)/$(basename "$env_file")"

# A key counts when it has a non-empty value: `KEY=v`, `KEY= v`, `export KEY=v`, `KEY="v"`; never `KEY=`,
# `KEY=""`, `KEY= ` or a commented line.
has_key() {
  grep -Eq "^[[:space:]]*(export[[:space:]]+)?$1=[[:space:]]*[\"']?[^[:space:]\"']" "$env_file"
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

utc_now() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}
catch_up_hint() {
  echo "deploy: run uv --directory Trader/app run python ../build/cron_gap.py --from $1 --to $2 --container $container"
}
down_at=""
healthy=0
# On any failure after the service went down, still say when it went down and how to find what was missed.
on_exit() {
  if [ -n "$down_at" ] && [ "$healthy" -ne 1 ]; then
    echo "deploy: not healthy; it was down from $down_at. Once it is healthy again, catch up with --to set to"
    echo "deploy: that time (below: now), then run each listed command:"
    catch_up_hint "$down_at" "$(utc_now)"
  fi
}
trap on_exit EXIT

echo "deploy: recreating the trader service ($env_name)"
down_at="$(utc_now)"
echo "deploy: down from $down_at"
docker --context shared-docker-server compose -f "$here/docker-compose.$env_name.yml" \
  up -d --no-build --no-deps --force-recreate trader

echo "deploy: waiting for $health_url (up to ${health_timeout}s)"
waited=0
while true; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$health_url" || true)"
  if [ "$code" = "200" ]; then
    healthy=1
    up_at="$(utc_now)"
    echo "deploy: health $code after ${waited}s"
    echo "deploy: up at $up_at"
    catch_up_hint "$down_at" "$up_at"
    exit 0
  fi
  if [ "$waited" -ge "$health_timeout" ]; then
    echo "deploy: health never returned 200 (last status ${code:-none})" >&2
    exit 1
  fi
  sleep "$health_interval"
  waited=$((waited + health_interval))
done
