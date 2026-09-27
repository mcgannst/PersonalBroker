#!/usr/bin/env bash
# Shared test lane: runs the full quality gate (app/scripts/check.sh) for THIS checkout, but only one
# gate runs at a time across every worktree on the machine. Other callers wait their turn.
# Usage (from a worktree root): bash Trader/build/gate.sh
# Exit code is check.sh's exit code.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
CHECK="$HERE/../app/scripts/check.sh"
LOCK="${TRADER_GATE_LOCK:-/private/tmp/claude-501/trader-gate.lock}"
STALE_MINUTES="${TRADER_GATE_STALE_MINUTES:-45}"

mkdir -p "$(dirname "$LOCK")"
waited=0
until mkdir "$LOCK" 2>/dev/null; do
  # Clear a lock left by a crashed run (older than STALE_MINUTES).
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin "+$STALE_MINUTES" 2>/dev/null)" ]; then
    echo "gate: removing stale lock (older than ${STALE_MINUTES} min)"
    rm -rf "$LOCK"
    continue
  fi
  if [ $((waited % 60)) -eq 0 ]; then
    holder="$(cat "$LOCK/owner" 2>/dev/null || echo unknown)"
    echo "gate: waiting for the test lane (held by: $holder, waited ${waited}s)"
  fi
  sleep 5
  waited=$((waited + 5))
done
echo "$(pwd) pid $$ since $(TZ=America/Edmonton date +%H:%M:%S) MT" > "$LOCK/owner"
trap 'rm -rf "$LOCK"' EXIT INT TERM
echo "gate: running check.sh for $(pwd) after waiting ${waited}s"
bash "$CHECK"
