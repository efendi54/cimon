#!/usr/bin/env bash

# Drives sync_static_runner_workflows.sh across a range of one-day windows,
# e.g. --from-date 2026-09-20 --to-date 2026-09-21, then
# --from-date 2026-09-21 --to-date 2026-09-22, and so on, up to and including
# a window starting on END_DATE itself (--from-date END_DATE --to-date
# END_DATE+1day).
#
# If a window's run fails (most likely because the GitHub API quota got
# exhausted, which typically resets after an hour), this script stops that
# run, waits RETRY_DELAY_SECONDS (default: 3600, i.e. one hour) and then
# retries the *same* window from scratch -- it does not advance to the next
# day until the current one succeeds.
#
# Each window's sync is also bounded by WINDOW_TIMEOUT_SECONDS (default:
# 1800, i.e. 30 minutes). This guards against the sync hanging indefinitely
# (observed e.g. when a GitHub API connection gets stuck in CLOSE_WAIT after
# a server-side reset, without sync_static_runner_workflows.sh's own retry
# logic ever seeing an exception to react to). Such a timeout is unrelated to
# the GitHub API quota, so it's retried after the much shorter
# TIMEOUT_RETRY_DELAY_SECONDS (default: 60) instead of the full
# RETRY_DELAY_SECONDS -- waiting a full hour after killing a stuck connection
# would needlessly delay the retry (e.g. past an already-happened quota
# reset) for a failure that has nothing to do with the quota.
#
# On a real (non-timeout) failure, the actual GitHub API quota reset time is
# queried via `cimon quota --json` (reusing cimon's own, already-authenticated
# quota lookup) and only that remaining time (+ a small buffer) is waited,
# instead of blindly sleeping the full RETRY_DELAY_SECONDS -- e.g. restarting
# a few minutes before the quota resets no longer costs up to another full
# hour. RETRY_DELAY_SECONDS is still used as a fallback if that lookup fails
# (e.g. network issue) and as an upper bound against implausible reset
# values.
#
# Usage: backfill_static_runner_workflows.sh [START_DATE] [END_DATE]
#   START_DATE defaults to 2026-09-20, END_DATE defaults to 2026-10-19.

set -euo pipefail

# Pause before the script (and thus an auto-closing terminal) exits, no matter
# how it exits, so quota/other errors stay readable instead of vanishing.
trap 'echo; read -n 1 -s -r -p "Press any key to close this window..."; echo' EXIT

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SYNC_SCRIPT="$SCRIPT_DIR/sync_static_runner_workflows.sh"

START_DATE="${1:-2026-09-20}"
END_DATE="${2:-2026-10-19}"
RETRY_DELAY_SECONDS="${RETRY_DELAY_SECONDS:-3600}"
WINDOW_TIMEOUT_SECONDS="${WINDOW_TIMEOUT_SECONDS:-300}"
TIMEOUT_RETRY_DELAY_SECONDS="${TIMEOUT_RETRY_DELAY_SECONDS:-60}"

# CAUTION: change this later (same credentials sync_static_runner_workflows.sh
# uses; only fetched here, lazily, if not already set in the environment).
# -->>
export GH_TOKEN="${GH_TOKEN:-$(pass show 'keepass.kdbx/CARIAD/NEW-GH-INSTANCE-TOKEN' | head -n 1)}"
export GH_HOST="${GH_HOST:-cariad.ghe.com}"

# Seconds to wait until the GitHub API core quota actually resets (+ a small
# buffer), capped at RETRY_DELAY_SECONDS. Falls back to RETRY_DELAY_SECONDS
# itself if the reset time can't be determined (cimon/network issue,
# unexpected output, ...). Relies on `cimon quota --json`, which prints a
# single clean JSON line on stdout for exactly this purpose (same /rate_limit
# data cimon itself uses internally) -- run from the same working directory
# as sync_static_runner_workflows.sh (the cimon project root), same
# assumption it already relies on for `uv run`.
quota_retry_delay() {
  local reset_epoch now_epoch delay
  reset_epoch="$(uv run cimon quota --json 2>/dev/null \
    | jq -r '.reset // empty' 2>/dev/null || true)"

  if [[ "$reset_epoch" =~ ^[0-9]+$ ]]; then
    now_epoch="$(date +%s)"
    delay=$(( reset_epoch - now_epoch + 10 ))
    if (( delay > 0 )); then
      if (( delay > RETRY_DELAY_SECONDS )); then
        delay="$RETRY_DELAY_SECONDS"
      fi
      echo "$delay"
      return 0
    fi
  fi

  echo "$RETRY_DELAY_SECONDS"
  return 0
}

current_date="$START_DATE"
while [[ ! "$current_date" > "$END_DATE" ]]; do
  next_date="$(date -I -d "$current_date + 1 day")"

  echo "=== Syncing window $current_date -> $next_date ==="

  while true; do
    rc=0
    # --kill-after ensures a SIGKILL follows the initial SIGTERM in case the
    # sync process is stuck in a way that ignores/doesn't see the TERM (it
    # normally does react to plain SIGTERM, but this is a safety net).
    CIMON_SYNC_NONINTERACTIVE=1 timeout --kill-after=30 "$WINDOW_TIMEOUT_SECONDS" \
      bash "$SYNC_SCRIPT" "$current_date" "$next_date" || rc=$?

    if [ "$rc" -eq 0 ]; then
      break
    fi

    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
      echo "Window $current_date -> $next_date timed out after" \
        "${WINDOW_TIMEOUT_SECONDS}s (likely a stuck GitHub API connection, unrelated to quota)." \
        "Waiting ${TIMEOUT_RETRY_DELAY_SECONDS}s before retrying the same window..." >&2
      sleep "$TIMEOUT_RETRY_DELAY_SECONDS"
    else
      delay="$(quota_retry_delay)"
      echo "Window $current_date -> $next_date failed (likely GitHub API quota)." \
        "Waiting ${delay}s before retrying the same window..." >&2
      sleep "$delay"
    fi
  done

  current_date="$next_date"
done

echo "Done: synced all windows from $START_DATE to $END_DATE."
