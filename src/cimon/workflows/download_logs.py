# ruff: noqa: CPY001
"""Download job logs, optionally keeping only those whose log matches a pattern.

Accepts a single workflow-job URL, a JSON array of URL strings, or a Parquet
file with a `job_url` column (e.g. produced by `cimon query -c job_url`) --
see `resolve_job_urls`. Each job's log is downloaded via the shared
`download_job_log_for_url` routine into `<run_id>/<job_id>.log`, the same
layout used by `cimon build-metrics`.

By default that directory is a temporary one, removed again once this run
finishes. Pass `keep_logs=True` to instead keep it under `output_dir/logs`,
persisted across runs -- so a later run over the same (or an overlapping)
input can skip logs it already downloaded, without consuming GitHub API
quota again.

If a `pattern` (regular expression) is given, every matched job is written
to `log-match.parquet` in the output directory (Parquet input keeps all of
its original columns; URL/JSON input only produces a `job_url` column); if
nothing matched, a warning is logged and no output file is written. Without
a `pattern`, logs are simply downloaded and no output file is produced.
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
from cimon.workflows.build_metrics import download_job_log_for_url, resolve_job_urls

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

    with tempfile.TemporaryDirectory(prefix="cimon-download-logs-") as tmp_dir:
        yield Path(tmp_dir)


def _download_log(job_url: str, logs_dir: Path, progress: str) -> Path | None:
    """Download `job_url`'s log into `logs_dir`/<run_id>/<job_id>.log via `download_job_log_for_url`.

    `progress` (e.g. "[3/10] (30%)") is prefixed to every log line emitted
    here, so both an actual download and a skipped one produce visible
    output. Already-downloaded, non-empty logs are reused (see
    `download_job_log_for_url`). Returns `None` (logging a warning) if
    `job_url` doesn't look like a workflow-job URL, or if the log could not
    be downloaded (e.g. a 404 because its retention period expired) --
    either way, that one job is skipped instead of aborting the whole run.
    """
    logger.info(f"{progress} {job_url}")

    try:
        log_path, _ = download_job_log_for_url(job_url, logs_dir)
    except ValueError:
        logger.warning(
            f"{progress} Skipping job URL that doesn't match the expected format: {job_url}",
        )
        return None
    except subprocess.CalledProcessError as exc:
        logger.warning(
            f"{progress} Skipping {job_url}: failed to download its log ({exc})"
        )
        return None

    return log_path


def run(
    input_arg: str,
    output_dir: Path,
    pattern: str | None = None,
    *,
    keep_logs: bool = False,
) -> Path | None:
    """Download every job's log resolved from `input_arg`, optionally filtering by `pattern`.

    `input_arg` is a job URL, a JSON array of URL strings, or a Parquet file
    with a `job_url` column (see `resolve_job_urls`). Parquet input keeps all
    of its original columns in `log-match.parquet`; the other two forms only
    produce a `job_url` column.

    If `keep_logs` is set, downloaded logs are kept under `output_dir/logs`
    instead of a temporary directory, so a later call can skip logs it
    already downloaded (see `_logs_directory`).

    If `pattern` is given, returns the path of `log-match.parquet` once at
    least one log matched it, `None` if none did. Without `pattern`, always
    returns `None` -- every job's log is downloaded and nothing is written.
    """
    regex = None
    if pattern is not None:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            msg = f"Invalid pattern {pattern!r}: {exc}"
            raise ValueError(msg) from None

    input_path = Path(input_arg)

    if input_path.is_file() and input_path.suffix == ".parquet":
        table = pq.read_table(input_path)
        if "job_url" not in table.column_names:
            msg = f"'{input_arg}' has no 'job_url' column."
            raise ValueError(msg)
        job_urls = table.column("job_url").to_pylist()
    else:
        job_urls = resolve_job_urls(input_arg)
        table = pa.table({"job_url": job_urls})

    total = len(job_urls)
    matched_indices: list[int] = []

    with _logs_directory(output_dir, keep_logs=keep_logs) as logs_dir:
        for index, job_url in enumerate(job_urls, start=1):
            if not job_url:
                continue

            progress = f"[{index}/{total}] ({index / total:.0%})"
            log_path = _download_log(job_url, logs_dir, progress)
            if log_path is None or regex is None:
                continue

            content = log_path.read_text(encoding="utf-8", errors="replace")
            if regex.search(content):
                matched_indices.append(index - 1)

    if regex is None:
        return None

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
