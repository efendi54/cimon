#!/usr/bin/env bash

set -euo pipefail

# Pause before the script (and thus an auto-closing terminal) exits, no matter
# how it exits, so quota/other errors stay readable instead of vanishing.
# Skipped when run non-interactively (e.g. from
# backfill_static_runner_workflows.sh), so it doesn't hang waiting for a
# keypress that will never come.
if [ -z "${CIMON_SYNC_NONINTERACTIVE:-}" ]; then
  trap 'echo; read -n 1 -s -r -p "Press any key to close this window..."; echo' EXIT
fi

# CAUTION: change this later
# -->>
export GH_TOKEN=$(pass show 'keepass.kdbx/CARIAD/NEW-GH-INSTANCE-TOKEN' | head -n 1)
export GH_HOST=cariad.ghe.com
export WORKSPACE_FOLDER=/workspaces/app-adas-src
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CIMON_CACHE_DIR=${HOME}/.cache/cimon

workflows=(
  "nightly.yml"
  "pr.yml"
  "cas_build_and_test.yml"
  "cas_nightly_and_release.yml"
  "cas_ota_deploy.yml"
  "pr_test_monitor.yml"
  "qg_cas_build_and_test.yml"
  "test_vehicle_ready.yml"
  "virtual_ncap.yml"
  "build_infra_team_debug.yml"
  "snitch_commit_finder.yml"
  "private_deployment_build.yml"
  "priority_runners.yml"
)

export FROM_DATE="$(date -I)"
export TO_DATE="$(date -I)"
if [ $# -ge 1 ]; then
  export FROM_DATE="$1"
fi
if [ $# -ge 2 ]; then
  export TO_DATE="$2"
fi

for wf in "${workflows[@]}"; do
  rc=0
  # Capture the real exit code via "cmd || rc=$?" rather than "if ! cmd;
  # then rc=$?; fi" -- `$?` right after a negated ("!") condition reflects
  # the *negated* status (always 0 on failure), not cimon's actual exit
  # code, which would make a quota failure here look like a success to any
  # caller (e.g. backfill_static_runner_workflows.sh) checking this script's
  # own exit status.
  uv run cimon sync --workflow "$wf" --from-date "$FROM_DATE" --to-date "$TO_DATE" --workers 10 || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "cimon sync failed for workflow '$wf' (exit code $rc, likely quota limit reached). Stopping sync." >&2
    exit "$rc"
  fi
done
