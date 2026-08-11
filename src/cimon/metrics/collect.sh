#!/usr/bin/env bash

OUTPUT_DIR="${1:-output}"


uv run src/cimon/metrics/metrics.py runs --owner CAS --repo app-adas-src --workflow pr.yml --from-date 2026-07-28 --to-date 2026-08-11
#  --job "Build 8650 / [Build] Platform 8650" \
#  --workers 10 \
#  --job-status success \
# | tee /dev/tty \
# | jq -r '.[].job.html_url' found_jobs.json \
# | xargs -r -n 1 -P 10 sh -c \
#   'uv run src/cimon/metrics/build_metrics.py "$1" "'"$OUTPUT_DIR"'"' _