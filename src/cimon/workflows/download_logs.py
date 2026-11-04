# ruff: noqa: CPY001
"""Download job logs, optionally keeping only those whose log matches a pattern.

Accepts a single workflow-job URL, a JSON array of URL strings, or a Parquet
file with a `job_url` column (e.g. produced by `cimon query -c job_url`) --
see `resolve_job_urls`. Each job's log is downloaded via the shared
`download_job_log_for_url` routine into `output_dir/logs/<run_id>/<job_id>.log`,
the same layout used by `cimon build-metrics`.

Downloaded logs are always kept (never silently deleted at the end of a run)
so a later call over the same/overlapping input can skip logs it already
downloaded, without consuming GitHub API quota again. The directory logs are
being written into is always logged, so it's visible even when nothing else
is printed.

If a `pattern` (regular expression) is given, every matched job is written
to `log-match.parquet` in the output directory (Parquet input keeps all of
its original columns; URL/JSON input only produces a `job_url` column); if
nothing matched, a warning is logged and no output file is written. Without
a `pattern`, logs are simply downloaded and no output file is produced.

Pass `clear_logs=True` (only meaningful together with `pattern`) to delete
every downloaded log that did *not* match the pattern once the analysis is
done, keeping only the logs of matching jobs on disk.
"""

from __future__ import annotations

import contextlib
import logging
import re
import subprocess
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from cimon.parquet_io import write_table_atomic
from cimon.workflows.build_metrics import download_job_log_for_url, resolve_job_urls

logger = logging.getLogger(__name__)

LOG_MATCH_PARQUET_FILE_NAME = "log-match.parquet"
LOGS_DIR_NAME = "logs"


def _logs_directory(output_dir: Path) -> Path:
    """Return (creating if needed) the persistent directory to download logs into."""
    logs_dir = output_dir / LOGS_DIR_NAME
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


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
            f"{progress} Skipping {job_url}: failed to download its log ({exc})",
        )
        return None

    return log_path


def _delete_non_matching_logs(log_paths: dict[int, Path], matched_indices: list[int]) -> None:
    """Delete every downloaded log in `log_paths` whose index isn't in `matched_indices`.

    Also removes the per-run directory (`<run_id>/`) once it's left empty,
    so a cleared-out run doesn't leave a trail of empty directories behind.
    """
    matched = set(matched_indices)
    deleted = 0
    for index, log_path in log_paths.items():
        if index in matched:
            continue
        log_path.unlink(missing_ok=True)
        deleted += 1
        # Directory not empty (other jobs of the same run matched) -- keep it.
        with contextlib.suppress(OSError):
            log_path.parent.rmdir()
    if deleted:
        logger.info(f"Deleted {deleted} non-matching log(s).")


def run(
    input_arg: str,
    output_dir: Path,
    pattern: str | None = None,
    *,
    clear_logs: bool = False,
) -> Path | None:
    """Download every job's log resolved from `input_arg`, optionally filtering by `pattern`.

    `input_arg` is a job URL, a JSON array of URL strings, or a Parquet file
    with a `job_url` column (see `resolve_job_urls`). Parquet input keeps all
    of its original columns in `log-match.parquet`; the other two forms only
    produce a `job_url` column.

    Downloaded logs are always kept under `output_dir/logs`, so a later call
    can skip logs it already downloaded (see `_logs_directory`). If
    `clear_logs` is set (only meaningful together with `pattern`), every log
    that did *not* match `pattern` is deleted again once the analysis is
    done.

    If `pattern` is given, returns the path of `log-match.parquet` once at
    least one log matched it, `None` if none did. Without `pattern`, always
    returns `None` -- every job's log is downloaded and nothing is written.
    """
    if clear_logs and pattern is None:
        msg = "clear_logs only has an effect together with a pattern -- pass one, or drop clear_logs."
        raise ValueError(msg)

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
    log_paths: dict[int, Path] = {}

    logs_dir = _logs_directory(output_dir)
    logger.info(f"Downloading logs into {logs_dir}")

    for index, job_url in enumerate(job_urls, start=1):
        if not job_url:
            continue

        progress = f"[{index}/{total}] ({index / total:.0%})"
        log_path = _download_log(job_url, logs_dir, progress)
        if log_path is None:
            continue
        log_paths[index - 1] = log_path

        if regex is None:
            continue
        content = log_path.read_text(encoding="utf-8", errors="replace")
        if regex.search(content):
            matched_indices.append(index - 1)

    if regex is None:
        return None

    if clear_logs:
        _delete_non_matching_logs(log_paths, matched_indices)

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
