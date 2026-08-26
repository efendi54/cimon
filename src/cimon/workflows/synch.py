# ruff: noqa: CPY001
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyarrow>=21.0.0",
#     "pydantic>=2.9.0",
#     "requests>=2.34.2",
# ]
# ///

"""Utilities for collecting GitHub Actions workflow and job metrics."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import tempfile
import threading
from collections import ChainMap
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as pq

import requests
from cimon.github_api import api_get, create_session, load_etag_cache, save_etag_cache
from cimon.workflows.models import JobEntry, JobInfo, RunEntry, WorkflowCache, WorkflowInfo

if TYPE_CHECKING:
    from collections.abc import Iterator, MutableMapping


logger = logging.getLogger(__name__)

RESPONSE_JSON_FILE_NAME = "response.json"
WORKFLOWS_PARQUET_FILE_NAME = "workflows.parquet"
ETAG_CACHE_FILE_NAME = "etag_cache.json"
HTTP_NOT_FOUND = 404
GITHUB_API_PER_PAGE = 100

PARQUET_SCHEMA = pa.schema(
    [
        ("owner", pa.string()),
        ("repo", pa.string()),
        ("host", pa.string()),
        ("workflow_id", pa.string()),
        ("workflow_name", pa.string()),
        ("workflow_file", pa.string()),
        ("run_id", pa.string()),
        ("run_number", pa.int64()),
        ("workflow_run_url", pa.string()),
        ("workflow_status", pa.string()),
        ("workflow_conclusion", pa.string()),
        ("created_at", pa.string()),
        ("cached_at", pa.string()),
        ("updated_at", pa.string()),
        ("jobs_cached_at", pa.string()),
        ("job_id", pa.string()),
        ("job_name", pa.string()),
        ("job_url", pa.string()),
        ("job_started_at", pa.string()),
        ("job_completed_at", pa.string()),
        ("job_duration_sec", pa.int64()),
        ("job_status", pa.string()),
        ("job_conclusion", pa.string()),
        ("job_runner_name", pa.string()),
        ("job_runner_labels", pa.list_(pa.string())),
    ],
)


def workflow_info(
    session: requests.Session,
    base_url: str,
    owner: str,
    repo: str,
    workflow_file: str,
    etag_cache: MutableMapping[str, dict[str, Any]] | None = None,
) -> dict:
    """Fetch workflow metadata from the GitHub API."""
    return api_get(
        session,
        f"{base_url}/repos/{owner}/{repo}/actions/workflows/{workflow_file}",
        etag_cache=etag_cache,
    ).json()


def iter_runs(
    session: requests.Session,
    base_url: str,
    owner: str,
    repo: str,
    workflow_id: int | str,
    from_date: str,
    to_date: str,
    max_pages: int | None = None,
    etag_cache: MutableMapping[str, dict[str, Any]] | None = None,
) -> Iterator[dict]:
    """
    Fetch all workflow runs within the requested date range.

    No workflow status or conclusion filter is applied here.
    The cache always receives all workflow runs.
    """
    page = 1

    while True:
        if max_pages and page > max_pages:
            break

        params = {
            "created": f"{from_date}..{to_date}",
            "per_page": GITHUB_API_PER_PAGE,
            "page": page,
        }

        response = api_get(
            session,
            f"{base_url}/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs",
            params=params,
            etag_cache=etag_cache,
        )

        runs = response.json()["workflow_runs"]

        if not runs:
            break

        yield from runs

        if len(runs) < GITHUB_API_PER_PAGE:
            break

        page += 1


def iter_jobs(
    session: requests.Session,
    base_url: str,
    owner: str,
    repo: str,
    run_id: int | str,
    etag_cache: MutableMapping[str, dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Fetch all jobs associated with a workflow run."""
    jobs = []
    page = 1

    while True:
        response = api_get(
            session,
            f"{base_url}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
            params={
                "per_page": GITHUB_API_PER_PAGE,
                "page": page,
            },
            etag_cache=etag_cache,
        )

        page_jobs = response.json()["jobs"]

        if not page_jobs:
            break

        jobs.extend(page_jobs)

        if len(page_jobs) < GITHUB_API_PER_PAGE:
            break

        page += 1

    yield from jobs


def calculate_job_duration_in_seconds(job: dict[str, Any]) -> int | None:
    """Calculate a job duration in seconds."""
    if not job.get("started_at") or not job.get("completed_at"):
        return None

    start = dt.datetime.strptime(
        job["started_at"], "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=dt.timezone.utc)
    end = dt.datetime.strptime(
        job["completed_at"], "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=dt.timezone.utc)

    return int((end - start).total_seconds())


def now_iso() -> str:
    """Return the current UTC time in ISO 8601 format."""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def format_file_size(path: str | Path) -> str:
    """Return a human-readable file size, or "missing" if the file doesn't exist."""
    file_path = Path(path)
    if not file_path.exists():
        return "missing"

    size = float(file_path.stat().st_size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:  # noqa: PLR2004
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024

    return f"{size:.1f} TB"


def load_cache(path: str) -> WorkflowCache:
    """Load a JSON cache, creating an empty structure when absent."""
    cache_path = Path(path)
    if not cache_path.exists():
        return WorkflowCache()

    return WorkflowCache.model_validate_json(cache_path.read_text(encoding="utf-8"))


def save_cache(path: str, data: WorkflowCache) -> None:
    """Atomically save cache data as formatted JSON."""
    cache_path = Path(path)
    directory = cache_path.resolve().parent
    directory.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=directory,
        prefix=".cache_",
        suffix=".json",
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data.model_dump_json(indent=2))

        Path(tmp_path).replace(cache_path)

    finally:
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()


def cache_to_parquet_rows(data: WorkflowCache) -> list[dict[str, Any]]:
    """Flatten the current JSON snapshot into one row per run and job."""
    workflow = data.workflow
    rows = []

    for run in data.runs:
        run_fields = {
            "owner": workflow.owner if workflow else None,
            "repo": workflow.repo if workflow else None,
            "host": workflow.host if workflow else None,
            "workflow_id": str(workflow.id) if workflow else None,
            "workflow_name": workflow.name if workflow else None,
            "workflow_file": workflow.file if workflow else None,
            "run_id": str(run.run_id),
            "run_number": run.run_number,
            "workflow_run_url": run.workflow_run_url,
            "workflow_status": run.workflow_status,
            "workflow_conclusion": run.workflow_conclusion,
            "created_at": run.created_at,
            "cached_at": run.cached_at,
            "updated_at": run.updated_at,
            "jobs_cached_at": run.jobs_cached_at,
        }

        if not run.jobs:
            rows.append(
                {
                    **run_fields,
                    "job_id": None,
                    "job_name": None,
                    "job_url": None,
                    "job_started_at": None,
                    "job_completed_at": None,
                    "job_duration_sec": None,
                    "job_status": None,
                    "job_conclusion": None,
                    "job_runner_name": None,
                    "job_runner_labels": None,
                },
            )
            continue

        for job_entry in run.jobs:
            job = job_entry.job
            rows.append(
                {
                    **run_fields,
                    "job_id": str(job.id),
                    "job_name": job.name,
                    "job_url": job.html_url,
                    "job_started_at": job.started_at,
                    "job_completed_at": job.completed_at,
                    "job_duration_sec": job.duration_sec,
                    "job_status": job.status,
                    "job_conclusion": job.conclusion,
                    "job_runner_name": job.runner_name,
                    "job_runner_labels": job.runner_labels,
                },
            )

    return rows


def load_parquet(path: str) -> list[dict[str, Any]]:
    """Load existing Parquet rows, or return an empty cache."""
    parquet_path = Path(path)
    if not parquet_path.exists():
        return []
    return pq.read_table(parquet_path).to_pylist()


def cached_jobs_from_parquet(rows: list[dict[str, Any]]) -> tuple[set[str], dict[str, list[JobEntry]]]:
    """Build a run index and reusable job snapshots from Parquet rows."""
    existing_run_ids = set()
    jobs_by_run: dict[str, list[JobEntry]] = {}

    for row in rows:
        run_id = row.get("run_id")
        if not run_id:
            continue

        existing_run_ids.add(str(run_id))

        # A run without cached jobs must still be recognized as "checked",
        # otherwise runs with genuinely zero jobs get refetched forever.
        jobs_by_run.setdefault(str(run_id), [])

        job_id = row.get("job_id")
        if job_id is None:
            continue

        job_entry = JobEntry(
            job=JobInfo(
                id=int(job_id) if str(job_id).isdigit() else 0,
                name=row.get("job_name"),
                html_url=row.get("job_url"),
                started_at=row.get("job_started_at"),
                completed_at=row.get("job_completed_at"),
                duration_sec=row.get("job_duration_sec"),
                status=row.get("job_status"),
                conclusion=row.get("job_conclusion"),
                runner_name=row.get("job_runner_name"),
                runner_labels=row.get("job_runner_labels"),
            ),
        )

        jobs_by_run[str(run_id)].append(job_entry)

    return existing_run_ids, jobs_by_run


def merge_parquet_cache(path: str, request_data: WorkflowCache) -> None:
    """Replace runs in the persistent Parquet cache with the JSON snapshot."""
    current_rows = cache_to_parquet_rows(request_data)
    request_run_ids = {
        row["run_id"] for row in current_rows if row.get("run_id") is not None
    }
    existing_rows = [
        row for row in load_parquet(path)
        if row.get("run_id") not in request_run_ids
    ]
    save_parquet(path, existing_rows + current_rows)


def save_parquet(path: str, rows: list[dict[str, Any]]) -> None:
    """Atomically save Parquet cache data."""
    parquet_path = Path(path)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=parquet_path.parent,
        prefix=".cache_",
        suffix=".parquet",
    )
    os.close(fd)

    try:
        table = pa.Table.from_pylist(rows, schema=PARQUET_SCHEMA)
        pq.write_table(table, tmp_path)
        Path(tmp_path).replace(parquet_path)
    finally:
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()


def create_workflow_cache_entry(
    workflow: dict[str, Any],
    owner: str,
    repo: str,
    host: str,
) -> WorkflowInfo:
    """Create a cache entry for workflow metadata."""
    return WorkflowInfo(
        owner=owner,
        repo=repo,
        host=host,
        id=workflow["id"],
        name=workflow["name"],
        file=workflow["path"],
    )


def create_run_cache_entry(run: dict[str, Any]) -> RunEntry:
    """Create a cache entry for a workflow run."""
    timestamp = now_iso()
    return RunEntry(
        run_id=run["id"],
        run_number=run["run_number"],
        workflow_run_url=run["html_url"],
        workflow_status=run["status"],
        workflow_conclusion=run["conclusion"],
        created_at=run["created_at"],
        cached_at=timestamp,
        updated_at=timestamp,
    )


def create_job_cache_entry(job: dict[str, Any]) -> JobEntry:
    """Create a cache entry for a workflow job."""
    return JobEntry(
        job=JobInfo(
            id=job["id"],
            name=job["name"],
            html_url=job["html_url"],
            started_at=job.get("started_at"),
            completed_at=job.get("completed_at"),
            duration_sec=calculate_job_duration_in_seconds(job),
            status=job["status"],
            conclusion=job["conclusion"],
            runner_name=job.get("runner_name"),
            runner_labels=job.get("labels"),
        ),
    )


def fetch_jobs(
    session: requests.Session,
    base_url: str,
    owner: str,
    repo: str,
    run_id: int | str,
    etag_cache: MutableMapping[str, dict[str, Any]] | None = None,
) -> list[JobEntry]:
    """
    Fetch and cache all jobs for a workflow run.

    No job-name or job-status filtering is performed.
    """
    return [
        create_job_cache_entry(job)
        for job in iter_jobs(session, base_url, owner, repo, run_id, etag_cache=etag_cache)
    ]


def process_job_run(
    session: requests.Session,
    base_url: str,
    owner: str,
    repo: str,
    run: dict[str, Any],
    etag_cache: MutableMapping[str, dict[str, Any]] | None = None,
) -> list[JobEntry]:
    """Fetch jobs for a workflow run."""
    return fetch_jobs(session, base_url, owner, repo, run["id"], etag_cache=etag_cache)


def update_run_cache_entry(
    cached: RunEntry,
    run: dict[str, Any],
) -> None:
    """
    Update the workflow-run information from GitHub.

    Job information is intentionally not modified here.
    """
    cached.run_number = run["run_number"]
    cached.workflow_run_url = run["html_url"]
    cached.workflow_status = run["status"]
    cached.workflow_conclusion = run["conclusion"]
    cached.created_at = run["created_at"]
    cached.updated_at = now_iso()


def main(argv: list[str] | None = None) -> None:  # noqa: PLR0915
    """Run the command-line metrics collector."""
    parser = argparse.ArgumentParser(
        description="Update a GitHub Actions workflow-run cache.",
    )

    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 1.0.0",
    )

    parser.add_argument(
        "--token",
        default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"),
        help="GitHub API token (will be read out from environment variables GH_TOKEN or GITHUB_TOKEN if not provided)",
    )

    parser.add_argument(
        "--host",
        default=os.environ.get("GH_HOST"),
        help="GitHub API host, e.g. git.hub.vwgroup.com (will be read out from "
        "environment variable GH_HOST if not provided)",
    )

    today = dt.datetime.now(tz=dt.timezone.utc).date().isoformat()

    parser.add_argument(
        "--owner",
        default="CAS",
        help="Repository owner",
    )

    parser.add_argument(
        "--repo",
        default="app-adas-src",
        help="Repository name",
    )

    parser.add_argument(
        "--workflow",
        help="Workflow filename (e.g. pr.yml)",
    )

    parser.add_argument(
        "--from-date",
        default=today,
        help="Start date (YYYY-MM-DD)",
    )

    parser.add_argument(
        "--to-date",
        default=today,
        help="End date (YYYY-MM-DD)",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Number of concurrent worker threads for job processing",
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Maximum number of pages to fetch for workflow runs",
    )

    parser.add_argument(
        "--cache-dir",
        default=Path.home() / ".cache/cimon",
        type=Path,
        help="Directory where workflows.json and workflows.parquet are written.",
    )

    args = parser.parse_args(argv)

    token = args.token

    if not args.token:
        parser.error(
            "No token provided and no environment variable set. Please set "
            "GH_TOKEN or GITHUB_TOKEN.",
        )

    host = args.host

    if not host:
        parser.error(
            "No host provided and no environment variable set. Please set "
            "GH_HOST.",
        )

    base_url = f"https://{host}/api/v3"

    session = create_session(token)

    # ------------------------------------------------------------------
    # Cache paths
    # ------------------------------------------------------------------

    cache_dir = Path(args.cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    parquet_file = cache_dir / WORKFLOWS_PARQUET_FILE_NAME
    response_file = str(cache_dir / RESPONSE_JSON_FILE_NAME)
    etag_cache_file = str(cache_dir / ETAG_CACHE_FILE_NAME)

    # Entries only get carried over to the next run if they are actually
    # read or written this run (via the ChainMap below). This prunes stale
    # entries for runs/pages that are no longer queried, e.g. once a run
    # is fully cached as completed or the date range moves on.
    persisted_etag_cache = load_etag_cache(etag_cache_file)
    etag_cache: dict[str, dict[str, Any]] = {}
    etag_cache_view = ChainMap(etag_cache, persisted_etag_cache)

    # ------------------------------------------------------------------
    # Workflow information
    # ------------------------------------------------------------------

    try:
        workflow = workflow_info(
            session,
            base_url,
            args.owner,
            args.repo,
            args.workflow,
            etag_cache=etag_cache_view,
        )
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == HTTP_NOT_FOUND:
            parser.error(
                "Workflow not found (404). Please check --owner, --repo, --workflow and --host. "
                f"Requested: {args.owner}/{args.repo} workflow {args.workflow}",
            )
        raise

    # ------------------------------------------------------------------
    # Load cache
    # ------------------------------------------------------------------

    existing_parquet_rows = load_parquet(str(parquet_file))
    existing_run_ids, cached_jobs_by_run = cached_jobs_from_parquet(existing_parquet_rows)

    # The JSON file is deliberately a snapshot of this request only.
    cache = WorkflowCache(
        workflow=create_workflow_cache_entry(workflow, args.owner, args.repo, host),
    )

    cached_runs: dict[str, RunEntry] = {}

    # ------------------------------------------------------------------
    # Fetch workflow runs
    #
    # IMPORTANT:
    # Only the date range is used as a GitHub API filter.
    # No status/conclusion filter is applied.
    # ------------------------------------------------------------------

    logger.info(f"Fetching workflow runs for {args.owner}/{args.repo} workflow {workflow['name']} ({args.workflow})")
    logger.info(f"Date range: {args.from_date} .. {args.to_date} ...")

    runs = None
    fetch_error = None
    fetch_done = threading.Event()

    def fetch_workflow_runs() -> None:
        nonlocal runs, fetch_error

        try:
            runs = list(
                iter_runs(
                    session,
                    base_url,
                    args.owner,
                    args.repo,
                    workflow["id"],
                    args.from_date,
                    args.to_date,
                    args.max_pages,
                    etag_cache=etag_cache_view,
                ),
            )
        except (requests.RequestException, RuntimeError) as error:
            fetch_error = error
        finally:
            fetch_done.set()

    fetch_thread = threading.Thread(
        target=fetch_workflow_runs,
        name="workflow-run-fetch",
        daemon=True,
    )

    fetch_thread.start()

    # ------------------------------------------------------------------
    # Spinner
    # ------------------------------------------------------------------

    spinner = "|/-\\"
    spinner_index = 0

    while not fetch_done.wait(0.05):
        char = spinner[spinner_index % len(spinner)]
        spinner_index += 1

        sys.stdout.write(f"\rFetching workflow runs ... {char}")
        sys.stdout.flush()

    fetch_thread.join()

    if fetch_error is not None:
        sys.stdout.write("\rFetching workflow runs ... FAILED\n")
        sys.stdout.flush()
        raise fetch_error

    sys.stdout.write(f"\rFetching workflow runs ... done ({len(runs)} runs found)\n")
    sys.stdout.flush()

    # ------------------------------------------------------------------
    # Update workflow-run cache
    # ------------------------------------------------------------------

    runs_to_process = []

    new_runs = 0
    known_runs = 0
    cached_completed_runs = 0

    for run in runs:
        run_id = str(run["id"])
        cached = create_run_cache_entry(run)
        cached_runs[run_id] = cached

        if run_id not in existing_run_ids:
            new_runs += 1
            logger.info(f"Caching new workflow run {run_id}")

        else:
            update_run_cache_entry(cached, run)
            known_runs += 1

        # --------------------------------------------------------------
        # Completed runs with already cached jobs are immutable for
        # our purposes. The jobs API does not need to be queried again.
        # --------------------------------------------------------------

        if run["status"] == "completed" and run_id in cached_jobs_by_run:
            cached.jobs = cached_jobs_by_run[run_id]
            cached.jobs_cached_at = now_iso()
            cached_completed_runs += 1
            continue

        # --------------------------------------------------------------
        # New runs and non-completed runs need their complete job list.
        #
        # No job filter is applied.
        # --------------------------------------------------------------

        runs_to_process.append(run)

    # ------------------------------------------------------------------
    # Update jobs concurrently
    # ------------------------------------------------------------------

    logger.info(f"Workflow runs requiring job update: {len(runs_to_process)}")

    if runs_to_process:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    process_job_run,
                    session,
                    base_url,
                    args.owner,
                    args.repo,
                    run,
                    etag_cache=etag_cache_view,
                ): run
                for run in runs_to_process
            }

            for future in as_completed(futures):
                run = futures[future]
                run_id = str(run["id"])
                jobs = future.result()
                cached = cached_runs[run_id]

                # Replace the complete job snapshot.
                #
                # This is important for runs that are still in
                # progress because their jobs can change between
                # cache updates.

                cached.jobs = jobs
                cached.jobs_cached_at = now_iso()

                logger.debug(f"Updated {len(jobs)} jobs for workflow run {run_id}")

    # ------------------------------------------------------------------
    # Save complete cache
    # ------------------------------------------------------------------

    cache.runs = list(cached_runs.values())
    cache.updated_at = now_iso()

    save_cache(response_file, cache)
    merge_parquet_cache(str(parquet_file), cache)
    save_etag_cache(etag_cache_file, etag_cache)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    logger.info("Cache update completed.")
    logger.info(f"  ETag cache:              {etag_cache_file} ({format_file_size(etag_cache_file)})")
    logger.info(f"  Response:                {response_file} ({format_file_size(response_file)})")
    logger.info(f"      Runs In Response:    {len(runs)}")
    logger.info(f"      New Runs:            {new_runs}")
    logger.info(f"      Known Runs:          {known_runs}")
    logger.info(f"  Parquet cache:           {parquet_file} ({format_file_size(parquet_file)})")
    logger.info(f"      Completed:           {cached_completed_runs}")
    logger.info(f"      Runs With Job Update:{len(runs_to_process)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
