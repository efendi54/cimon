# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests>=2.34.2",
# ]
# ///

# following environment variables must be set:
# - GH_TOKEN or GITHUB_TOKEN: GitHub personal access token
# - GH_HOST: GitHub host (default: github.com)
# additionally the following environment variables can be set:
# - GH_API_BASE_URL: GitHub API base URL (default: https://github.com/api/v3)
# in order to have the requests library use system certificates, you can set the REQUESTS_CA_BUNDLE environment variable to the path of the certificate bundle file. For example:
# export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import threading
import time

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


JOB_CACHE = {}
JOB_CACHE_LOCK = threading.Lock()


def get_token():
    token = (
        os.getenv("GH_TOKEN")
        or os.getenv("GITHUB_TOKEN")
    )

    if not token:
        raise RuntimeError(
            "GH_TOKEN or GITHUB_TOKEN must be set"
        )

    return token


def get_api_base_url():
    host = os.getenv(
        "GH_HOST",
        "github.com",
    )

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

            time.sleep(
                2 ** attempt
            )

            continue


        remaining = response.headers.get(
            "X-RateLimit-Remaining"
        )

        if remaining and int(remaining) < 100:

            print(
                f"WARNING: API quota low: {remaining}"
            )


        if response.status_code in (
            429,
            500,
            502,
            503,
            504,
        ):

            if attempt == retries - 1:
                response.raise_for_status()

            retry_after = response.headers.get(
                "Retry-After"
            )

            delay = (
                int(retry_after)
                if retry_after
                else 2 ** attempt
            )

            print(
                f"Retry {response.status_code} "
                f"in {delay}s"
            )

            time.sleep(delay)

            continue


        response.raise_for_status()

        return response


    raise RuntimeError(
        "GitHub API failed"
    )


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
    max_pages=None,
):

    page = 1

    while True:

        if max_pages and page > max_pages:
            break


        response = api_get(
            session,
            (
                f"{base_url}"
                f"/repos/{owner}/{repo}"
                f"/actions/workflows/"
                f"{workflow_id}/runs"
            ),
            params={
                "created": (
                    f"{from_date}..{to_date}"
                ),
                "per_page": 100,
                "page": page,
            },
        )


        runs = response.json()["workflow_runs"]


        if not runs:
            break


        for run in runs:

            if (
                workflow_status
                and run["conclusion"]
                != workflow_status
            ):
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

    with JOB_CACHE_LOCK:
        cached = JOB_CACHE.get(run_id)


    if cached is not None:
        yield from cached
        return


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


    with JOB_CACHE_LOCK:
        JOB_CACHE[run_id] = jobs


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


        if (
            job_status
            and job["conclusion"]
            != job_status
        ):
            return None


        return job


    return None



def calculate_duration(job):

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


    return int(
        (end - start).total_seconds()
    )



def now_iso():

    return dt.datetime.now(
        dt.timezone.utc
    ).isoformat()



def process_job_run(
    session,
    base_url,
    owner,
    repo,
    run,
    workflow,
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


    return {
        "run_id": run["id"],
        "run_number": run["run_number"],

        "workflow_id": workflow["id"],
        "workflow_name": workflow["name"],
        "workflow_file": workflow["path"],

        "workflow_run_url": run["html_url"],

        "job_id": job["id"],
        "job_name": job["name"],
        "html_url": job["html_url"],

        "duration": calculate_duration(job),

        "workflow_conclusion": (
            run["conclusion"]
        ),

        "job_conclusion": (
            job["conclusion"]
        ),

        "added_at": now_iso(),
    }



def job_cache_key(
    run_id,
    job_name,
):

    return f"{run_id}:{job_name}"



def workflow_cache_key(
    run_id,
):

    return str(run_id)



def load_cache(path):

    if not os.path.exists(path):
        return []


    with open(
        path,
        "r",
        encoding="utf-8",
    ) as f:

        data = json.load(f)


    if not isinstance(data, list):

        raise RuntimeError(
            f"{path} must contain JSON list"
        )


    return data



def save_cache(
    path,
    data,
):

    with open(
        path,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            data,
            f,
            indent=2,
        )

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Collect GitHub Actions workflow runs "
            "or workflow jobs."
        )
    )

    parser.add_argument(
        "--owner",
        required=True,
    )

    parser.add_argument(
        "--repo",
        required=True,
    )

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
        "--workers",
        type=int,
        default=5,
        help=(
            "Number of parallel job API requests."
        ),
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
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
            "Overrides default filename."
        ),
    )

    args = parser.parse_args()


    token = get_token()

    base_url = get_api_base_url()

    session = create_session(
        token
    )


    if args.quota:

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


    print(
        f"Workflow: {workflow['name']}"
    )

    print(
        f"Workflow ID: {workflow['id']}"
    )


    output_dir = os.path.abspath(
        args.output_dir
    )

    os.makedirs(
        output_dir,
        exist_ok=True,
    )


    if args.output_file:

        output_file = os.path.abspath(
            args.output_file
        )

    else:

        output_file = os.path.join(
            output_dir,
            (
                "found_jobs.json"
                if args.job
                else "found_workflows.json"
            ),
        )


    cached_results = load_cache(
        output_file
    )


    if args.job:

        cached_keys = {
            job_cache_key(
                item["run_id"],
                item["job_name"],
            )
            for item in cached_results
            if (
                "run_id" in item
                and "job_name" in item
            )
        }

    else:

        cached_keys = {
            workflow_cache_key(
                item["run_id"]
            )
            for item in cached_results
            if "run_id" in item
        }


    print(
        f"Cached entries: {len(cached_results)}"
    )


    runs_to_process = []


    for run in iter_runs(
        session,
        base_url,
        args.owner,
        args.repo,
        workflow["id"],
        args.from_date,
        args.to_date,
        args.workflow_status,
        args.max_pages,
    ):

        if args.job:

            key = job_cache_key(
                run["id"],
                args.job,
            )

        else:

            key = workflow_cache_key(
                run["id"]
            )


        if key in cached_keys:

            print(
                f"Skipping cached {key}"
            )

            continue


        runs_to_process.append(
            run
        )


    print(
        f"Runs to process: {len(runs_to_process)}"
    )


    new_results = []


    if args.job:

        with ThreadPoolExecutor(
            max_workers=args.workers
        ) as executor:


            futures = [
                executor.submit(
                    process_job_run,
                    session,
                    base_url,
                    args.owner,
                    args.repo,
                    run,
                    workflow,
                    args.job,
                    args.job_status,
                )
                for run in runs_to_process
            ]


            for future in as_completed(
                futures
            ):

                result = future.result()


                if result:

                    new_results.append(
                        result
                    )

                    print(
                        f"Adding {result['html_url']}"
                    )


    else:

        for run in runs_to_process:

            new_results.append(
                {
                    "run_id": run["id"],
                    "run_number": run["run_number"],

                    "workflow_id": workflow["id"],
                    "workflow_name": workflow["name"],
                    "workflow_file": workflow["path"],

                    "workflow_run_url": run["html_url"],

                    "workflow_conclusion": (
                        run["conclusion"]
                    ),

                    "created_at": run["created_at"],

                    "checked_at": now_iso(),
                }
            )


    combined_results = (
        cached_results
        + new_results
    )


    save_cache(
        output_file,
        combined_results,
    )


    print()

    print(
        f"New entries: {len(new_results)}"
    )

    print(
        f"Total entries: {len(combined_results)}"
    )

    print(
        f"Output: {output_file}"
    )



if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        sys.exit(130)

