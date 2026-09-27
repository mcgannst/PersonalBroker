#!/usr/bin/env bash
# Container entrypoint (P4-T17, SPEC §15 start-up). Mode: all (default) | api | worker | cron | migrate.
#   all, api, migrate: `alembic upgrade head` as the owner role (MIGRATION_DATABASE_URL), then
#                      `trader create-admin`; worker and cron never migrate.
#   Then MIGRATION_DATABASE_URL and ADMIN_PASSWORD_INITIAL are unset, so no long-running process holds
#   them, and the mode's process is exec'd: supervisord (all), the api, run-worker.sh or supercronic.
#   The unset reaches only this process tree: a `docker exec` shell starts from the container's own
#   environment (the env file), so it still sees both keys. Removing them there needs them out of the
#   runtime env file altogether (e.g. a one-shot migrate container with its own env file): see P4-T19.
# It prints the mode, the Alembic revision and exit codes only: never an environment value (so no xtrace).
set -eu

mode="${1:-all}"
docker_dir="${TRADER_DOCKER_DIR:-/app/docker}"
alembic_ini="/app/alembic.ini"
migrate_tries=5
migrate_delay=5

case "$mode" in
  all | api | worker | cron | migrate) ;;
  *)
    echo "entrypoint: unknown mode '$mode' (use all, api, worker, cron or migrate)" >&2
    exit 64
    ;;
esac
echo "entrypoint: mode $mode"

# Writable places live in /tmp (the root filesystem is read-only). The scraper creates the FinViz cache
# directory itself (mode 0700, and it refuses one it doesn't own), so only its parent is made here.
mkdir -p "${HOME:-/tmp/home}"
if [ -n "${TRADER_FINVIZ_CACHE_DIR:-}" ]; then
  mkdir -p "$(dirname "$TRADER_FINVIZ_CACHE_DIR")"
fi

migrate() {
  local try=1
  while true; do
    if alembic -c "$alembic_ini" upgrade head; then
      return 0
    fi
    if [ "$try" -ge "$migrate_tries" ]; then
      echo "entrypoint: alembic upgrade failed $try times; giving up" >&2
      return 1
    fi
    echo "entrypoint: alembic upgrade failed (try $try of $migrate_tries); retrying in ${migrate_delay}s" >&2
    sleep "$migrate_delay"
    try=$((try + 1))
  done
}

if [ "$mode" != "worker" ] && [ "$mode" != "cron" ]; then
  if ! migrate; then
    exit 1
  fi
  revision="$(alembic -c "$alembic_ini" current 2>/dev/null | tail -n 1 || true)"
  echo "entrypoint: alembic revision ${revision:-unknown}"
  # A rejected or missing admin must never stop the worker and cron from trading: it only means the web
  # login doesn't work yet.
  admin_code=0
  trader create-admin || admin_code=$?
  if [ "$admin_code" -ne 0 ]; then
    echo "entrypoint: trader create-admin exited $admin_code; starting anyway" >&2
    echo "entrypoint: create-admin exit code $admin_code"
  fi
fi

unset MIGRATION_DATABASE_URL ADMIN_PASSWORD_INITIAL

case "$mode" in
  all) exec supervisord -c "$docker_dir/supervisord.conf" ;;
  api) exec python -m trader.api ;;
  worker) exec "$docker_dir/run-worker.sh" ;;
  cron) exec supercronic -passthrough-logs "$docker_dir/crontab" ;;
  migrate) exit 0 ;;
esac
