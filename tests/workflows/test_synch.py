# ruff: noqa: ANN001, CPY001, D100, D103, PLR2004

from cimon.workflows.models import (
    JobEntry,
    JobInfo,
    RunAttemptInfo,
    RunEntry,
    WorkflowCache,
)
from cimon.workflows.synch import (
    cache_to_parquet_rows,
    cached_attempt_has_job_snapshot,
    cached_data_from_parquet,
    create_run_cache_entry,
    fetch_run_attempt_info,
)


def test_cache_to_parquet_rows_uses_matching_attempt_metadata() -> None:
    run = RunEntry(
        run_id=272378712,
        run_number=42,
        run_attempt=3,
        workflow_run_url="https://example.test/actions/runs/272378712",
        workflow_status="completed",
        workflow_conclusion="success",
        created_at="2026-09-29T10:00:00Z",
        cache_updated_at="2026-09-29T11:00:00+00:00",
        attempts=[
            RunAttemptInfo(
                run_attempt=2,
                workflow_run_url="https://example.test/actions/runs/272378712/attempts/2",
                workflow_status="completed",
                workflow_conclusion="failure",
            ),
        ],
        jobs=[
            JobEntry(
                job=JobInfo(
                    id=123,
                    name="build",
                    html_url="https://example.test/jobs/123",
                    run_attempt=2,
                    status="completed",
                    conclusion="failure",
                ),
            ),
        ],
    )

    [row] = cache_to_parquet_rows(WorkflowCache(runs=[run]))

    assert row["run_attempt"] == 2
    assert row["workflow_run_url"].endswith("/attempts/2")
    assert row["workflow_status"] == "completed"
    assert row["workflow_conclusion"] == "failure"


def test_create_run_cache_entry_includes_current_attempt() -> None:
    run = {
        "id": 272378712,
        "run_number": 42,
        "run_attempt": 3,
        "html_url": "https://example.test/actions/runs/272378712",
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-09-29T10:00:00Z",
    }

    entry = create_run_cache_entry(run, "CAS", "app-adas-src", "example.test")

    assert len(entry.attempts) == 1
    assert entry.attempts[0].run_attempt == 3
    assert entry.workflow_run_url.endswith("/actions/runs/272378712/attempts/3")
    assert entry.attempts[0].workflow_conclusion == "success"


def test_fetch_run_attempt_info_uses_attempt_endpoint(mocker) -> None:
    response = mocker.Mock()
    response.json.return_value = {"status": "completed", "conclusion": "failure"}
    api_get = mocker.patch("cimon.workflows.synch.api_get", return_value=response)

    attempt = fetch_run_attempt_info(
        mocker.Mock(),
        "https://example.test/api/v3",
        "CAS",
        "app-adas-src",
        "example.test",
        272378712,
        2,
    )

    assert attempt.workflow_run_url.endswith("/actions/runs/272378712/attempts/2")
    assert attempt.workflow_conclusion == "failure"
    assert api_get.call_args.args[1].endswith(
        "/repos/CAS/app-adas-src/actions/runs/272378712/attempts/2",
    )


def test_cached_data_from_parquet_deduplicates_attempts() -> None:
    rows = [
        {
            "run_id": "272378712",
            "run_attempt": 2,
            "workflow_run_url": "https://example.test/actions/runs/272378712/attempts/2",
            "workflow_status": "completed",
            "workflow_conclusion": "failure",
        },
        {
            "run_id": "272378712",
            "run_attempt": 2,
            "workflow_run_url": "https://example.test/actions/runs/272378712/attempts/2",
            "workflow_status": "completed",
            "workflow_conclusion": "failure",
        },
    ]

    _, _, attempts = cached_data_from_parquet(rows)

    assert len(attempts["272378712"]) == 1
    assert attempts["272378712"][0].workflow_conclusion == "failure"


def test_new_attempt_requires_fresh_job_snapshot() -> None:
    attempts = [
        RunAttemptInfo(
            run_attempt=1,
            workflow_run_url="https://example.test/actions/runs/1",
            workflow_status="completed",
            workflow_conclusion="failure",
        ),
    ]
    jobs = [
        JobEntry(
            job=JobInfo(
                id=1,
                name="build",
                html_url="https://example.test/jobs/1",
                run_attempt=1,
                status="completed",
                conclusion="failure",
            ),
        ),
    ]

    assert cached_attempt_has_job_snapshot(1, attempts, jobs)
    assert not cached_attempt_has_job_snapshot(2, attempts, jobs)
