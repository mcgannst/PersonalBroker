#!/usr/bin/env bash
# supervisord's `worker` program (P4-T17): runs `python -m trader.worker` as a child and exits with its code.
#   - A stop signal (TERM from supervisord, or INT) is forwarded to the worker as TERM (a background child of
#     a non-interactive shell ignores INT), and the script waits for it to finish its shutdown.
#   - Exit 2 (another worker holds the lock) or 3 (the lock was lost): wait WORKER_RESTART_DELAY seconds
#     (default 30) before exiting, unless the script is being stopped, so supervisord never restarts a second
#     worker in a tight loop.
#   - Exit 4 (the live run changed) and every other code return at once: supervisord restarts it now.
set -u

delay="${WORKER_RESTART_DELAY:-30}"
stopping=0
child=""
sleeper=""

on_stop() {
  stopping=1
  if [ -n "$child" ]; then
    kill -TERM "$child" 2>/dev/null || true
  fi
  if [ -n "$sleeper" ]; then
    kill -TERM "$sleeper" 2>/dev/null || true
  fi
}
trap on_stop TERM INT # installed before the child starts, so no stop signal is lost

python -m trader.worker &
child=$!
# A stop that arrived between the fork and `child=$!` found no child to forward to: forward it now.
if [ "$stopping" -eq 1 ]; then
  kill -TERM "$child" 2>/dev/null || true
fi

# `wait` returns early (above 128) when a trapped signal arrives; keep waiting until the worker has exited.
while true; do
  code=0
  wait "$child" || code=$?
  if [ "$code" -gt 128 ] && kill -0 "$child" 2>/dev/null; then
    continue
  fi
  break
done
child=""

if [ "$stopping" -eq 0 ] && { [ "$code" -eq 2 ] || [ "$code" -eq 3 ]; }; then
  echo "run-worker: the worker exited $code (single-instance lock); restarting in ${delay}s"
  sleep "$delay" &
  sleeper=$!
  wait "$sleeper" || true
  sleeper=""
fi
exit "$code"
