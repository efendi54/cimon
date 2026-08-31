#!/usr/bin/bash env

set -euo pipefail

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

for wf in "${wf_files[@]}"; do
  uv run cimon sync --workflow "$wf" --from-date "$(date -I)"
done
