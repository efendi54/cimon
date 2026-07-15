#!/usr/bin/env bash

source .venv/bin/activate

LEGACY_JOBS_FILE="legacy-jobs.json"
NEW_JOBS_FILE="new-jobs.json"

# collect legacy jobs for the given date range and workflow/job
uv run find_workflows.py --owner CARIAD --repo app-adas-src --from-date 2026-07-08 --to-date 2026-07-08 --workflow qg_cas_build_and_test.yml  --job "[Build] Deployment / [Build] Build and Package" --output-file "$LEGACY_JOBS_FILE" 
# uv run find_workflows.py --owner CARIAD --repo app-adas-src --from-date 2026-07-14 --to-date 2026-07-14 --workflow qg_cas_build_and_test.yml  --job "/ [Build] Platform " --output-file "$NEW_JOBS_FILE"

