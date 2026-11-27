#!/usr/bin/env bash

set -euo pipefail

# Pause before the script (and thus an auto-closing terminal) exits, no matter
# how it exits, so quota/other errors stay readable instead of vanishing.
trap 'echo; read -n 1 -s -r -p "Press any key to close this window..."; echo' EXIT

export GH_HOST=cariad.ghe.com
export WORKSPACE_FOLDER=/workspaces/app-adas-src
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CIMON_CACHE_DIR=${HOME}/.cache/cimon
CIMON_VIZ_OUTPUT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)/out/visualizations"

pushd "$SCRIPT_DIR" >/dev/null

required_vars=(GH_HOST WORKSPACE_FOLDER)

for var in "${required_vars[@]}"; do
  if [ -z "${!var:-}" ]; then
    echo "$var environment variable is not set" >&2
    read -n 1 -s -r -p "Press any key to exit..."
    echo
    read -n 1 -s -r -p "Press any key to continue..."
    echo
    exit 1
  fi
done

# get list of workflow files being triggered in pull_request or merge_group events
mapfile -t wf_files < <(grep -RE "(merge_group|pull_request):" ${WORKSPACE_FOLDER}/.github/workflows/ | cut -d: -f1 |  xargs -n1 basename | sort -u)

export FROM_DATE="$(date -I)"
export TO_DATE="$(date -I)"
if [ $# -ge 1 ]; then
  export FROM_DATE="$1"
fi
if [ $# -ge 2 ]; then
  export TO_DATE="$2"
fi

for wf in "${wf_files[@]}"; do
  if uv run cimon sync --cache-dir "$CIMON_CACHE_DIR" --workflow "$wf" --from-date "$FROM_DATE" --to-date "$TO_DATE"; then
    :
  else
    rc=$?
    echo "cimon sync failed for workflow '$wf' (exit code $rc). Stopping sync." >&2
    break
  fi
done

# "$SCRIPT_DIR/filter_merge_queue_failures.sh"


popd >/dev/null
