#!/usr/bin/env bash

set -euo pipefail


# CAUTION: change this later
# -->>
export GH_TOKEN=$(pass show 'keepass.kdbx/CARIAD/NEW-GH-INSTANCE-TOKEN' | head -n 1)
export GH_HOST=cariad.ghe.com
export WORKSPACE_FOLDER=/workspaces/app-adas-src
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CIMON_CACHE_DIR=${HOME}/.cache/cimon
# Absolute (not pushd-relative), so it doesn't silently drift to scripts/out/...
CIMON_VIZ_OUTPUT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)/out/visualizations"
# <<--

pushd "$SCRIPT_DIR" >/dev/null


required_vars=(GH_TOKEN GH_HOST WORKSPACE_FOLDER)

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
  uv run cimon sync --workflow "$wf" --from-date "$FROM_DATE" --to-date "$TO_DATE"  
done
# uv run cimon sync --workflow pr.yml --from-date "$(date -I)"
# uv run cimon sync --workflow qg_cas_build_and_test.yml --from-date "$(date -I)"
  
popd >/dev/null