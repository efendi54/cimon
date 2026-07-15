#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys


def gh_api(endpoint: str):
    result = subprocess.run(
        ["gh", "api", endpoint],
        check=True,
        capture_output=True,
        text=True,
    )

    return json.loads(result.stdout)


def print_quota():
    rate_limit = gh_api("/rate_limit")
    core = rate_limit["resources"]["core"]

    reset_time = dt.datetime.fromtimestamp(
        core["reset"],
        tz=dt.timezone.utc,
    )

    print("GitHub API quota:")
    print(f"  Limit:     {core['limit']}")
    print(f"  Used:      {core['used']}")
    print(f"  Remaining: {core['remaining']}")
    print(f"  Reset:     {reset_time}")


def workflow_info(
    owner: str,
    repo: str,
    workflow_file: str,
):
    return gh_api(
        f"/repos/{owner}/{repo}/actions/workflows/{workflow_file}"
    )


def iter_runs(
    owner: str,
    repo: str,
    workflow_id: int,
    from_date: str,
    to_date: str,
    max_pages: int | None = None,
):
    page = 1

    while True:
        if max_pages and page > max_pages:
            break

        response = gh_api(
            f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs"
            f"?created={from_date}..{to_date}"
            f"&per_page=100&page={page}"
        )

        runs = response["workflow_runs"]

        if not runs:
            break

        yield from runs
        page += 1


def iter_jobs(
    owner: str,
    repo: str,
    run_id: int,
):
    page = 1

    while True:
        response = gh_api(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
            f"?per_page=100&page={page}"
        )

        jobs = response["jobs"]

        if not jobs:
            break

        yield from jobs
        page += 1


def calculate_duration(job):
    if not job.get("started_at") or not job.get("completed_at"):
        return None

    start = dt.datetime.strptime(
        job["started_at"],
        "%Y-%m-%dT%H:%M:%SZ",
    )

    end = dt.datetime.strptime(
        job["completed_at"],
        "%Y-%m-%dT%H:%M:%SZ",
    )

    return int((end - start).total_seconds())


def load_cache(path):
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise RuntimeError(
            f"{path} must contain a JSON list"
        )

    return data


def save_cache(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def job_cache_key(run_id: int, job_name: str):
    return f"{run_id}:{job_name}"


def workflow_cache_key(run_id: int):
    return str(run_id)


def now_iso():
    return dt.datetime.now(
        dt.timezone.utc
    ).isoformat()


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Collect GitHub Actions workflow runs "
            "or workflow jobs."
        )
    )

    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)

    parser.add_argument(
        "--workflow",
        required=True,
        help="Workflow filename",
    )

    parser.add_argument(
        "--job",
        default=None,
        help=(
            "Optional job name. "
            "If omitted workflow runs are collected."
        ),
    )

    parser.add_argument(
        "--from-date",
        required=True,
    )

    parser.add_argument(
        "--to-date",
        required=True,
    )

    parser.add_argument(
        "--workflow-status",
        choices=[
            "success",
            "failure",
            "cancelled",
            "skipped",
        ],
        default=None,
    )

    parser.add_argument(
        "--job-status",
        choices=[
            "success",
            "failure",
            "cancelled",
            "skipped",
        ],
        default="success",
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Maximum workflow run pages to scan",
    )

    parser.add_argument(
        "--quota",
        action="store_true",
    )

    parser.add_argument(
        "--output-dir",
        default=".",
    )

    parser.add_argument(
        "--output-file",
        default=None,
        help=(
            "JSON output file. "
            "Overrides default output filename."
        ),
    )

    args = parser.parse_args()

    if args.quota:
        print_quota()
        return

    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    if args.output_file:
        output_file = os.path.abspath(args.output_file)
    else:
        output_file = os.path.join(
            output_dir,
            "found_jobs.json" if args.job else "found_workflows.json",
        )

    cached_results = load_cache(output_file)

    if args.job:
        cached_keys = {
            job_cache_key(
                item["run_id"],
                item["job_name"],
            )
            for item in cached_results
            if "run_id" in item and "job_name" in item
        }
    else:
        cached_keys = {
            workflow_cache_key(item["run_id"])
            for item in cached_results
            if "run_id" in item
        }

    print(f"Cached entries: {len(cached_results)}")

    workflow = workflow_info(
        args.owner,
        args.repo,
        args.workflow,
    )

    workflow_id_value = workflow["id"]

    print(f"Workflow: {workflow['name']}")
    print(f"Workflow ID: {workflow_id_value}")

    new_results = []

    for run in iter_runs(
        args.owner,
        args.repo,
        workflow_id_value,
        args.from_date,
        args.to_date,
        args.max_pages,
    ):

        if (
            args.workflow_status
            and run["conclusion"] != args.workflow_status
        ):
            continue

        if not args.job:
            cache_key = workflow_cache_key(run["id"])

            if cache_key in cached_keys:
                print(f"Skipping cached workflow {run['id']}")
                continue

            new_results.append(
                {
                    "run_id": run["id"],
                    "run_number": run["run_number"],
                    "workflow_id": workflow_id_value,
                    "workflow_name": workflow["name"],
                    "workflow_file": workflow["path"],
                    "workflow_run_url": run["html_url"],
                    "workflow_conclusion": run["conclusion"],
                    "created_at": run["created_at"],
                    "checked_at": now_iso(),
                }
            )

            continue

        cache_key = job_cache_key(
            run["id"],
            args.job,
        )

        if cache_key in cached_keys:
            print(f"Skipping cached job for workflow run {run['id']}")
            continue

        matching_job = None

        for job in iter_jobs(
            args.owner,
            args.repo,
            run["id"],
        ):
            if args.job in job["name"]:
                matching_job = job
                break

        if matching_job is None:
            continue

        if matching_job["conclusion"] != args.job_status:
            continue

        new_results.append(
            {
                "run_id": run["id"],
                "run_number": run["run_number"],
                "workflow_id": workflow_id_value,
                "workflow_name": workflow["name"],
                "workflow_file": workflow["path"],
                "workflow_run_url": run["html_url"],
                "job_id": matching_job["id"],
                "job_name": matching_job["name"],
                "html_url": matching_job["html_url"],
                "duration": calculate_duration(matching_job),
                "workflow_conclusion": run["conclusion"],
                "job_conclusion": matching_job["conclusion"],
                "checked_at": now_iso(),
            }
        )

        print(f"Adding {matching_job['id']}")

    combined_results = cached_results + new_results

    save_cache(
        output_file,
        combined_results,
    )

    print()
    print(f"New entries: {len(new_results)}")
    print(f"Total entries: {len(combined_results)}")
    print(f"Output: {output_file}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)