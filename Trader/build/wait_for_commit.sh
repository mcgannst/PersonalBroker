#!/usr/bin/env bash
# Usage: wait_for_commit.sh "<grep pattern>"  -- exits when a matching commit is on origin/trunk (max 60 min)
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
for _ in $(seq 1 360); do
  git -C "$REPO" fetch -q origin trunk 2>/dev/null || true
  hit=$(git -C "$REPO" log origin/trunk --oneline --grep "$1" | head -1)
  if [ -n "$hit" ]; then
    echo "landed: $hit"
    exit 0
  fi
  sleep 10
done
echo "timeout waiting for $1"
exit 1
