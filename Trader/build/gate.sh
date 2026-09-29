#!/usr/bin/env bash
# Shared test lane: runs the full quality gate (app/scripts/check.sh) for THIS checkout, but at most
# TRADER_GATE_SLOTS (default 2) full gates run at once across every worktree on the machine. Each gate runs
# pytest with TRADER_TEST_WORKERS (default 4) xdist workers, each with its own PostgreSQL container, so one
# gate uses about 4 of the Mac's 8 cores: 2 lanes x 4 workers keep the machine busy without overload.
# Other callers wait for a free slot.
# Usage (from a worktree root): bash Trader/build/gate.sh
# Exit code is check.sh's exit code.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
CHECK="$HERE/../app/scripts/check.sh"
BASE="${TRADER_GATE_LOCK:-/private/tmp/claude-501/trader-gate.lock}"
SLOTS="${TRADER_GATE_SLOTS:-2}"
STALE_MINUTES="${TRADER_GATE_STALE_MINUTES:-45}"

mkdir -p "$(dirname "$BASE")"
waited=0
LOCK=""
while [ -z "$LOCK" ]; do
  for i in $(seq 1 "$SLOTS"); do
    cand="$BASE.$i"
    # Clear a slot left by a crashed run (older than STALE_MINUTES).
    if [ -n "$(find "$cand" -maxdepth 0 -mmin "+$STALE_MINUTES" 2>/dev/null)" ]; then
      echo "gate: removing stale slot $i (older than ${STALE_MINUTES} min)"
      rm -rf "$cand"
    fi
    if mkdir "$cand" 2>/dev/null; then
      LOCK="$cand"
      break
    fi
  done
  if [ -z "$LOCK" ]; then
    if [ $((waited % 60)) -eq 0 ]; then
      echo "gate: all $SLOTS test lanes busy, waited ${waited}s"
    fi
    sleep 5
    waited=$((waited + 5))
  fi
done
echo "$(pwd) pid $$ since $(TZ=America/Edmonton date +%H:%M:%S) MT" > "$LOCK/owner"
trap 'rm -rf "$LOCK"' EXIT INT TERM
echo "gate: running check.sh for $(pwd) in $(basename "$LOCK") after waiting ${waited}s"
bash "$CHECK"
