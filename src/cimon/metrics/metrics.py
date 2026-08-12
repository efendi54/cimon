# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests>=2.34.2",
# ]
# ///

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import requests
import sys
import tempfile
import time

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def create_session(token):
    session = requests.Session()

    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
    )

    return session


def api_get(session, url, *, params=None, retries=5):
    for attempt in range(retries):
        try:
            response = session.get(url, params=params, timeout=30)

        except requests.RequestException:
            if attempt == retries - 1:
                raise

            time.sleep(2 ** attempt)
            continue

        remaining = response.headers.get("X-RateLimit-Remaining")

        if remaining and int(remaining) < 100:
            print(f"WARNING: API quota low: {remaining}")

        if response.status_code in (429, 500, 502, 503, 504):
            if attempt == retries - 1:
                response.raise_for_status()

            retry_after = response.headers.get("Retry-After")
            delay = int(retry_after) if retry_after else 2 ** attempt

            print(f"Retry {response.status_code} in {delay}s")
            time.sleep(delay)
            continue

        response.raise_for_status()
        return response

    raise RuntimeError("GitHub API failed")


def print_quota(session, base_url):
    try:
        data = api_get(session, f"{base_url}/rate_limit", retries=1).json()
    except requests.RequestException as e:
        print(f"Failed to fetch GitHub API quota: {e}")
        sys.exit(1)

    core = data["resources"]["core"]

    reset = dt.datetime.fromtimestamp(
        core["reset"],
        tz=dt.timezone.utc,
    )

    print("GitHub API quota:")
    print(f"  Limit:     {core['limit']}")
    print(f"  Used:      {core['used']}")
    print(f"  Remaining: {core['remaining']}")
    print(f"  Reset:     {reset}")


def workflow_info(session, base_url, owner, repo, workflow_file):
    return api_get(
        session,
        f"{base_url}/repos/{owner}/{repo}/actions/workflows/{workflow_file}",
    ).json()


def iter_runs(
    session,
    base_url,
    owner,
    repo,
    workflow_id,
    from_date,
    to_date,
    max_pages=None,
):
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
            "per_page": 100,
            "page": page,
        }

        response = api_get(
            session,
            f"{base_url}/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs",
            params=params,
        )

        runs = response.json()["workflow_runs"]

        if not runs:
            break

        yield from runs
        page += 1


def iter_jobs(session, base_url, owner, repo, run_id):
    jobs = []
    page = 1

    while True:
        response = api_get(
            session,
            f"{base_url}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
            params={
                "per_page": 100,
                "page": page,
            },
        )

        page_jobs = response.json()["jobs"]

        if not page_jobs:
            break

        jobs.extend(page_jobs)
        page += 1

    yield from jobs


def calculate_job_duration_in_seconds(job):
    if not job.get("started_at") or not job.get("completed_at"):
        return None

    start = dt.datetime.strptime(job["started_at"], "%Y-%m-%dT%H:%M:%SZ")
    end = dt.datetime.strptime(job["completed_at"], "%Y-%m-%dT%H:%M:%SZ")

    return int((end - start).total_seconds())


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_cache(path):
    if not os.path.exists(path):
        return {
            "workflow": None,
            "runs": [],
        }

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object")

    if "runs" not in data:
        data["runs"] = []

    return data


def save_cache(path, data):
    directory = os.path.dirname(os.path.abspath(path))

    fd, tmp_path = tempfile.mkstemp(
        dir=directory,
        prefix=".cache_",
        suffix=".json",
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        os.replace(tmp_path, path)

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def create_workflow_cache_entry(workflow):
    return {
        "id": workflow["id"],
        "name": workflow["name"],
        "file": workflow["path"],
    }


def create_run_cache_entry(run):
    return {
        "run_id": run["id"],
        "run_number": run["run_number"],
        "workflow_run_url": run["html_url"],
        "workflow_status": run["status"],
        "workflow_conclusion": run["conclusion"],
        "created_at": run["created_at"],
        "cached_at": now_iso(),
    }


def create_job_cache_entry(job):
    return {
        "job": {
            "id": job["id"],
            "name": job["name"],
            "html_url": job["html_url"],
            "duration_sec": calculate_job_duration_in_seconds(job),
            "status": job["status"],
            "conclusion": job["conclusion"],
        }
    }


def fetch_jobs(session, base_url, owner, repo, run_id):
    """
    Fetch and cache all jobs for a workflow run.

    No job-name or job-status filtering is performed.
    """

    return [
        create_job_cache_entry(job)
        for job in iter_jobs(session, base_url, owner, repo, run_id)
    ]


def process_job_run(session, base_url, owner, repo, run):
    return fetch_jobs(session, base_url, owner, repo, run["id"])


def update_run_cache_entry(cached, run):
    """
    Update the workflow-run information from GitHub.

    Job information is intentionally not modified here.
    """

    cached["run_number"] = run["run_number"]
    cached["workflow_run_url"] = run["html_url"]
    cached["workflow_status"] = run["status"]
    cached["workflow_conclusion"] = run["conclusion"]
    cached["created_at"] = run["created_at"]
    cached["updated_at"] = now_iso()


def main():
    parser = argparse.ArgumentParser(
        description="Update a GitHub Actions workflow-run cache."
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

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
        help="GitHub API host, e.g. git.hub.vwgroup.com (will be read out from environment variable GH_HOST if not provided)",
    )

    # ------------------------------------------------------------------
    # quota
    # ------------------------------------------------------------------

    subparsers.add_parser(
        "quota",
        help="Show the GitHub API quota.",
    )

    # ------------------------------------------------------------------
    # runs
    # ------------------------------------------------------------------

    today = dt.date.today().isoformat()

    collect_runs_parser = subparsers.add_parser(
        "runs",
        help="Update the GitHub Actions workflow-run cache.",
    )

    collect_runs_parser.add_argument(
        "--owner",
        default="CARIAD",
        help="Repository owner",
    )

    collect_runs_parser.add_argument(
        "--repo",
        default="app-adas-src",
        help="Repository name",
    )

    collect_runs_parser.add_argument(
        "--workflow",
        required=True,
        help="Workflow filename (e.g. pr.yml)",
    )

    collect_runs_parser.add_argument(
        "--from-date",
        default=today,
        help="Start date (YYYY-MM-DD)",
    )

    collect_runs_parser.add_argument(
        "--to-date",
        default=today,
        help="End date (YYYY-MM-DD)",
    )

    collect_runs_parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Number of concurrent worker threads for job processing",
    )

    collect_runs_parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Maximum number of pages to fetch for workflow runs",
    )

    collect_runs_parser.add_argument(
        "--output-dir",
        default=Path(os.path.expanduser("~")) / ".cache/cimon",
        help="Directory to save the cache file",
    )

    collect_runs_parser.add_argument(
        "--output-file",
        default=None,
        help=(
            "Cache file name. "
            "If not provided, the workflow filename is used."
        ),
    )

    args = parser.parse_args()

    token = args.token
    if not args.token:
        parser.error(
            "No token provided and no environment variable set. Please set "
            "GH_TOKEN or GITHUB_TOKEN."
        )

    host = args.host
    if not host:
        parser.error(
            "No host provided and no environment variable set. Please set "
            "GH_HOST."
        )
    base_url = f"https://{host}/api/v3"

    session = create_session(token)

    if args.command == "quota":
        print_quota(session, base_url)
        return

    # ------------------------------------------------------------------
    # Workflow information
    # ------------------------------------------------------------------

    workflow = workflow_info(
        session,
        base_url,
        args.owner,
        args.repo,
        args.workflow,
    )

    # ------------------------------------------------------------------
    # Cache path
    # ------------------------------------------------------------------

    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    if args.output_file:
        cache_file = os.path.abspath(args.output_file)
    else:
        cache_file = os.path.join(
            output_dir,
            f"{os.path.basename(args.workflow)}.json",
        )

    # ------------------------------------------------------------------
    # Load cache
    # ------------------------------------------------------------------
    cache = load_cache(cache_file)
    cache["workflow"] = {
        "owner": args.owner,
        "repo": args.repo,
        "host": host,
        "id": workflow["id"],
        "name": workflow["name"],
        "file": workflow["path"],
    }

    cached_runs = {
        str(item["run_id"]): item
        for item in cache["runs"]
        if "run_id" in item
    }


    # ------------------------------------------------------------------
    # Fetch workflow runs
    #
    # IMPORTANT:
    # Only the date range is used as a GitHub API filter.
    # No status/conclusion filter is applied.
    # ------------------------------------------------------------------
    print(f"Fetching workflow runs for {args.owner}/{args.repo} workflow {workflow['name']} ({args.workflow})")
    print(f"Date range: {args.from_date} .. {args.to_date} ...")

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
        )
    )

    # print(f"Workflow: {workflow['name']}")
    # print(f"Workflow ID: {workflow['id']}")
    # print(f"Date range: {args.from_date} .. {args.to_date} ({len(runs)} runs found)")
    # print(f"Currently cached workflow runs: {len(cached_runs)}")

    # ------------------------------------------------------------------
    # Update workflow-run cache
    # ------------------------------------------------------------------

    runs_to_process = []

    new_runs = 0
    updated_runs = 0
    cached_completed_runs = 0

    for run in runs:
        run_id = str(run["id"])

        if run_id not in cached_runs:
            cached_runs[run_id] = create_run_cache_entry(run)
            new_runs += 1
            print(f"Caching new workflow run {run_id}")

        else:
            cached = cached_runs[run_id]
            update_run_cache_entry(cached, run)
            updated_runs += 1

        cached = cached_runs[run_id]

        # --------------------------------------------------------------
        # Completed runs with already cached jobs are immutable for
        # our purposes. The jobs API does not need to be queried again.
        # --------------------------------------------------------------

        if run["status"] == "completed" and "jobs" in cached:
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

    print(f"Workflow runs requiring job update: {len(runs_to_process)}")

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

                cached["jobs"] = jobs
                cached["jobs_cached_at"] = now_iso()

                print(f"Updated {len(jobs)} jobs for workflow run {run_id}")

    # ------------------------------------------------------------------
    # Save complete cache
    # ------------------------------------------------------------------

    cache["runs"] = list(cached_runs.values())
    cache["updated_at"] = now_iso()

    save_cache(cache_file, cache)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    print()
    print("Cache update completed.")
    print(f"  Runs found:             {len(runs)}")
    print(f"  New runs:               {new_runs}")
    print(f"  Updated runs:           {updated_runs}")
    print(f"  Completed runs cached:  {cached_completed_runs}")
    print(f"  Runs with job update:   {len(runs_to_process)}")
    print(f"  Total cached runs:      {len(cache['runs'])}")
    print(f"  Cache:                  {cache_file}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
