#!/usr/bin/env bash

OUTPUT_DIR="${1:-output}"

uv run src/cimon/metrics/metrics.py runs \
  --workflow qg_cas_build_and_test.yml \
  --job "Build 8650 / [Build] Platform 8650" \
  --workers 10 \
  --job-status success \
# | tee /dev/tty \
# | jq -r '.[].job.html_url' found_jobs.json \
# | xargs -r -n 1 -P 10 sh -c \
#   'uv run src/cimon/metrics/build_metrics.py "$1" "'"$OUTPUT_DIR"'"' _