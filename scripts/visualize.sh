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
    exit 1
  fi
done

uv run --extra viz cimon visualize job-durations merge-group-failures -i "${CIMON_CACHE_DIR}/workflows.parquet" -o "${CIMON_VIZ_OUTPUT_DIR}"
uv run cimon runners --org CAS -o "${CIMON_VIZ_OUTPUT_DIR}"/runner-status.html
uv run --extra viz cimon runner-status-trend -o "${CIMON_VIZ_OUTPUT_DIR}/runner-status-trend.html"
  
popd >/dev/null