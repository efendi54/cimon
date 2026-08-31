#!/usr/bin/env bash

set -euo pipefail


# CAUTION: change this later
# -->>
export GH_TOKEN=$(pass show 'keepass.kdbx/CARIAD/NEW-GH-INSTANCE-TOKEN' | head -n 1)
export GH_HOST=cariad.ghe.com
export WORKSPACE_FOLDER=/workspaces/app-adas-src
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CIMON_CACHE_DIR=${HOME}/.cache/cimon
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

# get list of workflow files being triggered in pull_request or merge_group events
mapfile -t wf_files < <(grep -RE "(merge_group|pull_request):" ${WORKSPACE_FOLDER}/.github/workflows/ | cut -d: -f1 |  xargs -n1 basename | sort -u)

# for wf in "${wf_files[@]}"; do
#   uv run cimon sync --workflow "$wf" --from-date "$(date -I)"
# done
uv run cimon sync --workflow pr.yml --from-date "$(date -I)"
uv run cimon sync --workflow qg_cas_build_and_test.yml --from-date "$(date -I)"

uv run cimon quota
uv run --extra viz cimon visualize job-durations -i "${CIMON_CACHE_DIR}/workflows.parquet" -o out/visualizations

popd >/dev/null