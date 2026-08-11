# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests>=2.34.2",
# ]
# ///

# todo:
# adapt caching so that all workflow runs are cached, not just the ones with
# matching jobs. This will allow for more efficient re-runs of the script
# without having to re-fetch all workflow runs.


from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import time

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


def get_token():

    token = (
        os.getenv("GH_TOKEN")
        or os.getenv("GITHUB_TOKEN")
    )

    if not token:
        raise RuntimeError("GH_TOKEN or GITHUB_TOKEN must be set")

    return token


def get_api_base_url():

    host = os.getenv("GH_HOST")

    if not host:
        raise RuntimeError("GH_HOST must be set")

    return f"https://{host}/api/v3"


def create_session(token):

    session = requests.Session()

    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
    )

    return session


def api_get(
    session,
    url,
    *,
    params=None,
    retries=5,
):

    for attempt in range(retries):

        try:
            response = session.get(
                url,
                params=params,
                timeout=30,
            )

        except requests.RequestException:

            if attempt == retries - 1:
                raise

            time.sleep(2 ** attempt)
            continue

        remaining = response.headers.get("X-RateLimit-Remaining")

        if remaining and int(remaining) < 100:
            print(f"WARNING: API quota low: {remaining}")

        if response.status_code in (
            429,
            500,
            502,
            503,
            504,
        ):

            if attempt == retries - 1:
                response.raise_for_status()

            retry_after = response.headers.get("Retry-After")

            delay = (
                int(retry_after)
                if retry_after
                else 2 ** attempt
            )

            print(f"Retry {response.status_code} in {delay}s")

            time.sleep(delay)
            continue

        response.raise_for_status()

        return response

    raise RuntimeError("GitHub API failed")


def print_quota(
    session,
    base_url,
):

    data = api_get(
        session,
        f"{base_url}/rate_limit",
    ).json()

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


def workflow_info(
    session,
    base_url,
    owner,
    repo,
    workflow_file,
):

    return api_get(
        session,
        (
            f"{base_url}"
            f"/repos/{owner}/{repo}"
            f"/actions/workflows/"
            f"{workflow_file}"
        ),
    ).json()


def iter_runs(
    session,
    base_url,
    owner,
    repo,
    workflow_id,
    from_date,
    to_date,
    workflow_status=None,
    workflow_conclusion=None,
    max_pages=None,
):

    page = 1

    while True:

        if max_pages and page > max_pages:
            break

        params = {
            "created": f"{from_date}..{to_date}",
            "per_page": 100,
            "page": page,
        }

        if workflow_status and workflow_status != "any":
            params["status"] = workflow_status

        response = api_get(
            session,
            (
                f"{base_url}"
                f"/repos/{owner}/{repo}"
                f"/actions/workflows/"
                f"{workflow_id}/runs"
            ),
            params=params,
        )

        runs = response.json()["workflow_runs"]

        if not runs:
            break

        for run in runs:

            if workflow_status != "any":
                if (workflow_status and run["status"] != workflow_status):
                    continue
            if workflow_conclusion != "any":
                if (workflow_conclusion and run["conclusion"] != workflow_conclusion):
                    continue

            yield run

        page += 1


def iter_jobs(
    session,
    base_url,
    owner,
    repo,
    run_id,
):

    jobs = []
    page = 1

    while True:

        response = api_get(
            session,
            (
                f"{base_url}"
                f"/repos/{owner}/{repo}"
                f"/actions/runs/{run_id}"
                f"/jobs"
            ),
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


def find_matching_job(
    session,
    base_url,
    owner,
    repo,
    run_id,
    job_name,
    job_status,
):

    for job in iter_jobs(
        session,
        base_url,
        owner,
        repo,
        run_id,
    ):

        if job_name not in job["name"]:
            continue

        if (job_status != "any" and job["conclusion"] != job_status):
            continue

        return job

    return None


def calculate_job_duration_in_seconds(job):

    if (
        not job.get("started_at")
        or not job.get("completed_at")
    ):
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


def now_iso():

    return dt.datetime.now(
        dt.timezone.utc
    ).isoformat()


def load_cache(path):

    if not os.path.exists(path):
        return {
            "workflow": None,
            "runs": [],
        }

    with open(
        path,
        "r",
        encoding="utf-8",
    ) as f:

        data = json.load(f)

    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object")

    if "runs" not in data:
        data["runs"] = []
    return data


def save_cache(
    path,
    data,
):

    directory = os.path.dirname(os.path.abspath(path))

    fd, tmp_path = tempfile.mkstemp(
        dir=directory,
        prefix=".cache_",
        suffix=".json",
    )

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)

    finally:

        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def create_workflow_cache_entry(
    workflow,
):

    return {
        "id": workflow["id"],
        "name": workflow["name"],
        "file": workflow["path"],
    }


def create_run_cache_entry(
    run,
):

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
            "duration_sec": (
                calculate_job_duration_in_seconds(job)
            ),
            "status": job["status"],
            "conclusion": job["conclusion"],
        }
    }


def process_job_run(
    session,
    base_url,
    owner,
    repo,
    run,
    job_name,
    job_status,
):

    job = find_matching_job(
        session,
        base_url,
        owner,
        repo,
        run["id"],
        job_name,
        job_status,
    )

    if job is None:
        return None

    return create_job_cache_entry(job)


def main():

    parser = argparse.ArgumentParser(
        description=(
            "Collect GitHub Actions workflow runs "
            "or workflow jobs."
        )
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    # ------------------------------------------------------------------
    # quota
    # ------------------------------------------------------------------
    subparsers.add_parser(
        "quota",
        help="Show the GitHub API quota.",
    )

    # ------------------------------------------------------------------
    # collect
    # ------------------------------------------------------------------
    today = dt.date.today().isoformat()
    collect_runs_parser = subparsers.add_parser(
        "runs",
        help=(
            "Collect GitHub Actions workflow runs "
            "or workflow jobs."
        ),
    )

    collect_runs_parser.add_argument(
        "--owner",
        default="CARIAD",
        help="Repository owner"
    )

    collect_runs_parser.add_argument(
        "--repo",
        default="app-adas-src",
        help="Repository name"
    )

    collect_runs_parser.add_argument(
        "--workflow",
        help="Workflow filename (e.g. pr.yml)",
        required=True
    )

    collect_runs_parser.add_argument(
        "--job",
        default=None,
        help=(
            "Optional job name filter. "
            "If omitted workflow runs are collected."
        ),
    )

    collect_runs_parser.add_argument(
        "--from-date",
        default=today,
        help="Relevant start date (YYYY-MM-DD)"
    )

    collect_runs_parser.add_argument(
        "--to-date",
        default=today,
        help="Relevant end date (YYYY-MM-DD)"
    )

    collect_runs_parser.add_argument(
        "--workflow-status",
        choices=[
            "completed",
            "in_progress",
            "queued",
            "waiting",
            "any"
        ],
        default="any",
    )

    collect_runs_parser.add_argument(
        "--workflow-conclusion",
        choices=[
            "success",
            "failure",
            "cancelled",
            "skipped",
            "neutral",
            "timed_out",
            "action_required",
            "stale",
            "any"
        ],
        default="any",
        help="Workflow conclusion filter"
    )

    collect_runs_parser.add_argument(
        "--job-status",
        choices=[
            "success",
            "failure",
            "cancelled",
            "skipped",
            "any"
        ],
        default="any",
    )

    collect_runs_parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help=(
            "Number of concurrent worker threads "
            "for job processing"
        )
    )

    collect_runs_parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help=(
            "Maximum number of pages to fetch "
            "for workflow runs"
        )
    )

    collect_runs_parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to save output files"
    )

    collect_runs_parser.add_argument(
        "--output-file",
        default=None,
        help=(
            "Output file name. "
            "If not provided, the workflow filename is used."
        )
    )

    args = parser.parse_args()

    token = get_token()
    base_url = get_api_base_url()
    session = create_session(token)

    if args.command == "quota":
        print_quota(
            session,
            base_url,
        )
        return

    workflow = workflow_info(
        session,
        base_url,
        args.owner,
        args.repo,
        args.workflow,
    )

    print(f"Workflow: {workflow['name']}")
    print(f"Workflow ID: {workflow['id']}")
    print(f"Workflow status: {args.workflow_status}")
    print(f"Workflow conclusion: {args.workflow_conclusion}")

    output_dir = os.path.abspath(args.output_dir)

    os.makedirs(output_dir, exist_ok=True)

    if args.output_file:
        output_file = os.path.abspath(args.output_file)
    else:
        output_file = os.path.join(
            output_dir,
            f"{os.path.basename(args.workflow)}.json",
        )

    cache = load_cache(output_file)

    cache["workflow"] = create_workflow_cache_entry(workflow)    

    cached_runs = {
        str(item["run_id"]): item
        for item in cache["runs"]
        if "run_id" in item
    }

    print(f"Cached workflow runs: {len(cached_runs)}")
    runs = list(
        iter_runs(
            session,
            base_url,
            args.owner,
            args.repo,
            workflow["id"],
            args.from_date,
            args.to_date,
            args.workflow_status,
            args.workflow_conclusion,
            args.max_pages,
        )
    )

    print(f"Workflow runs found: {len(runs)}")

    runs_to_process = []

    for run in runs:

        run_id = str(run["id"])

        if run_id not in cached_runs:
            cached_runs[run_id] = create_run_cache_entry(run)
            print(f"Caching workflow run {run_id}")

        else:
            cached = cached_runs[run_id]
            cached["run_number"] = run["run_number"]
            cached["workflow_run_url"] = run["html_url"]
            cached["workflow_status"] = run["status"]
            cached["workflow_conclusion"] = run["conclusion"]
            cached["created_at"] = run["created_at"]

        if not args.job:
            continue

        cached = cached_runs[run_id]

        # Bei abgeschlossenen Workflows wird die Job-API nur einmal
        # aufgerufen. Sobald "jobs" existiert, ist die Abfrage gecacht.
        if (
            run["status"] == "completed"
            and "jobs" in cached
        ):
            print(f"Using cached jobs for workflow run {run_id}")
            continue

        runs_to_process.append(run)


    if args.job and runs_to_process:

        print(f"Workflow runs to process for jobs: {len(runs_to_process)}")
        with ThreadPoolExecutor(
            max_workers=args.workers
        ) as executor:

            futures = {
                executor.submit(
                    process_job_run,
                    session,
                    base_url,
                    args.owner,
                    args.repo,
                    run,
                    args.job,
                    args.job_status,
                ): run
                for run in runs_to_process
            }

            for future in as_completed(
                futures
            ):

                run = futures[future]
                run_id = str(run["id"])
                result = future.result()
                cached = cached_runs[run_id]

                # Die Job-Abfrage wurde durchgeführt.
                # Daher wird "jobs" auch dann gesetzt, wenn kein
                # passender Job gefunden wurde.
                jobs = cached.setdefault("jobs",[])

                if result:

                    job_id = result["job"]["id"]

                    if not any(
                        item["job"]["id"]
                        == job_id
                        for item in jobs
                    ):
                        jobs.append(result)
                        print(f"Adding {result['job']['html_url']}")

                    else:

                        print(
                            f"Job {job_id} "
                            f"already cached for "
                            f"workflow run {run_id}"
                        )
                else:
                    print(f"No matching job found for workflow run {run_id}")

    cache["runs"] = list(cached_runs.values())

    save_cache(output_file, cache)

    print()

    print(
        "Total cached workflow runs: "
        f"{len(cache['runs'])}"
    )

    print(f"Output: {output_file}")


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:
        sys.exit(130)
