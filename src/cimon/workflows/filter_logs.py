# ruff: noqa: CPY001
"""Download job logs referenced by a Parquet file and filter rows by a log pattern.

For each row of a Parquet file with a `job_url` column (e.g. produced by
`cimon query -c job_url`), downloads that job's log into a directory (one
file per job, named `<run_id>_<job_id>.log` so it stays unique even though
all logs land in the same flat directory), then searches the log for a
caller-supplied regular expression.

By default that directory is a temporary one, removed again once this run
finishes. Pass `keep_logs=True` to instead keep it under `output_dir/logs`,
persisted across runs -- so a later run over the same (or an overlapping)
input can skip logs it already downloaded, without consuming GitHub API
quota again.

Every row whose log matched is written to `log-match.parquet` in the output
directory, keeping all of its original columns. If no log matched at all, no
output file is written and a warning is logged instead.
"""

from __future__ import annotations

import contextlib
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq

from cimon.parquet_io import write_table_atomic
from cimon.workflows.build_metrics import (
    WORKFLOW_URL_RE,
    download_job_log,
    ensure_github_auth,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

LOG_MATCH_PARQUET_FILE_NAME = "log-match.parquet"
LOGS_DIR_NAME = "logs"


@contextlib.contextmanager
def _logs_directory(output_dir: Path, *, keep_logs: bool) -> Iterator[Path]:
    """Yield the directory to download logs into: persistent if `keep_logs`, a tempdir otherwise."""
    if keep_logs:
        logs_dir = output_dir / LOGS_DIR_NAME
        logs_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Keeping downloaded logs in {logs_dir}")
        yield logs_dir
        return

    with tempfile.TemporaryDirectory(prefix="cimon-filter-logs-") as tmp_dir:
        yield Path(tmp_dir)


def _download_log(job_url: str, logs_dir: Path, progress: str) -> Path | None:
    """Download `job_url`'s log into `logs_dir`, named uniquely by run_id/job_id.

    `progress` (e.g. "[3/10] (30%)") is prefixed to every log line emitted
    here, so both an actual download and a skipped one produce visible
    output. Skips the download (and thus the GitHub API quota it would
    consume) if that log was already downloaded, e.g. because `job_url`
    appears more than once in the input. Returns `None` (logging a warning)
    if `job_url` doesn't look like a workflow-job URL, or if the log could
    not be downloaded (e.g. a 404 because its retention period expired) --
    either way, that one job is skipped instead of aborting the whole run.
    """
    match = WORKFLOW_URL_RE.match(job_url)
    if not match:
        logger.warning(
            f"{progress} Skipping job URL that doesn't match the expected format: {job_url}",
        )
        return None

    info = match.groupdict()
    log_path = logs_dir / f"{info['run_id']}_{info['job_id']}.log"
    if log_path.exists():
        logger.info(f"{progress} Already downloaded, skipping: {log_path}")
        return log_path

    ensure_github_auth(info["host"])

    logger.info(f"{progress} Downloading {job_url}")
    try:
        downloaded = download_job_log(
            info["host"],
            info["owner"],
            info["repo"],
            info["job_id"],
            logs_dir,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning(f"{progress} Skipping {job_url}: failed to download its log ({exc})")
        # download_job_log() leaves an empty/partial file behind on failure.
        (logs_dir / f"{info['job_id']}.log").unlink(missing_ok=True)
        return None
    downloaded.rename(log_path)
    logger.info(f"{progress} Downloaded to {log_path}")
    return log_path


def run(
    input_path: Path,
    pattern: str,
    output_dir: Path,
    *,
    keep_logs: bool = False,
) -> Path | None:
    """Match every row's job log in `input_path` against `pattern`, writing matches to `output_dir`.

    If `keep_logs` is set, downloaded logs are kept under `output_dir/logs`
    instead of a temporary directory, so a later call can skip logs it
    already downloaded (see `_logs_directory`).

    Returns the path of `log-match.parquet` if at least one log matched,
    `None` otherwise.
    """
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        msg = f"Invalid pattern {pattern!r}: {exc}"
        raise ValueError(msg) from None

    table = pq.read_table(input_path)
    if "job_url" not in table.column_names:
        msg = f"'{input_path}' has no 'job_url' column."
        raise ValueError(msg)

    job_urls = table.column("job_url").to_pylist()
    total = len(job_urls)
    matched_indices: list[int] = []

    with _logs_directory(output_dir, keep_logs=keep_logs) as logs_dir:
        for index, job_url in enumerate(job_urls, start=1):
            if not job_url:
                continue

            progress = f"[{index}/{total}] ({index / total:.0%})"
            log_path = _download_log(job_url, logs_dir, progress)
            if log_path is None:
                continue

            content = log_path.read_text(encoding="utf-8", errors="replace")
            if regex.search(content):
                matched_indices.append(index - 1)

    if not matched_indices:
        logger.warning(
            f"No log matched pattern {pattern!r} across {total} job(s); not writing an output file.",
        )
        return None

    matched_table = table.take(pa.array(matched_indices))
    output_path = output_dir / LOG_MATCH_PARQUET_FILE_NAME
    write_table_atomic(matched_table, output_path)
    logger.info(f"Wrote {matched_table.num_rows} matching row(s) to {output_path}")
    return output_path
