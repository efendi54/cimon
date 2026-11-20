#!/usr/bin/env bash

set -euo pipefail

# Pause before the script (and thus an auto-closing terminal) exits, no matter
# how it exits, so quota/other errors stay readable instead of vanishing.
trap 'echo; read -n 1 -s -r -p "Press any key to close this window..."; echo' EXIT
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$SCRIPT_DIR/../out"
CIMON_CACHE_DIR=${HOME}/.cache/cimon

mkdir -p "$OUT_DIR"

pushd "$SCRIPT_DIR" >/dev/null

# extract today's workflows from the workflows parquet file
uv run cimon query "$SCRIPT_DIR/../tests/example_specs/filter_today.yml" -i "$CIMON_CACHE_DIR/workflows.parquet" -o "$OUT_DIR/today.parquet"

# extract merge group failures from the workflows parquet file
uv run --extra viz cimon query \
  --sql-file "$SCRIPT_DIR/../tests/example_specs/merge_group_failures.sql" \
  --input "$OUT_DIR/today.parquet" \
  --output "$OUT_DIR/merge-group-failures.parquet"

popd >/dev/null
