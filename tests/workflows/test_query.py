# ruff: noqa: CPY001
"""Tests for the WorkflowQuery filter builder over the Parquet cache."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from cimon.workflows.query import WorkflowQuery
from cimon.workflows.synch import PARQUET_SCHEMA

if TYPE_CHECKING:
    from pathlib import Path

LONG_JOB_DURATION_SEC = 60


def _write_sample(path: Path) -> None:
    rows = [
        {
            "owner": "acme",
            "repo": "app",
            "host": "github.com",
            "workflow_id": "1",
            "workflow_name": "CI",
            "workflow_file": "ci.yml",
            "run_id": "100",
            "run_number": 1,
            "workflow_run_url": "https://example/100",
            "workflow_status": "completed",
            "workflow_conclusion": "success",
            "created_at": "2026-08-01T00:00:00Z",
            "cached_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z",
            "jobs_cached_at": None,
            "job_id": "1",
            "job_name": "build",
            "job_url": "https://example/100/job/1",
            "job_started_at": None,
            "job_completed_at": None,
            "job_duration_sec": 30,
            "job_status": "completed",
            "job_conclusion": "success",
            "job_runner_name": None,
            "job_runner_labels": None,
        },
        {
            "owner": "acme",
            "repo": "app",
            "host": "github.com",
            "workflow_id": "1",
            "workflow_name": "CI",
            "workflow_file": "ci.yml",
            "run_id": "101",
            "run_number": 2,
            "workflow_run_url": "https://example/101",
            "workflow_status": "completed",
            "workflow_conclusion": "failure",
            "created_at": "2026-08-15T00:00:00Z",
            "cached_at": "2026-08-15T00:00:00Z",
            "updated_at": "2026-08-15T00:00:00Z",
            "jobs_cached_at": None,
            "job_id": "2",
            "job_name": "test",
            "job_url": "https://example/101/job/2",
            "job_started_at": None,
            "job_completed_at": None,
            "job_duration_sec": 90,
            "job_status": "completed",
            "job_conclusion": "failure",
            "job_runner_name": None,
            "job_runner_labels": None,
        },
        {
            "owner": "acme",
            "repo": "app",
            "host": "github.com",
            "workflow_id": "1",
            "workflow_name": "CI",
            "workflow_file": "ci.yml",
            "run_id": "102",
            "run_number": 3,
            "workflow_run_url": "https://example/102",
            "workflow_status": "in_progress",
            "workflow_conclusion": None,
            "created_at": "2026-08-20T00:00:00Z",
            "cached_at": "2026-08-20T00:00:00Z",
            "updated_at": "2026-08-20T00:00:00Z",
            "jobs_cached_at": None,
            "job_id": "3",
            "job_name": "deploy",
            "job_url": "https://example/102/job/3",
            "job_started_at": "2026-08-20T00:00:00Z",
            "job_completed_at": None,
            "job_duration_sec": None,
            "job_status": "in_progress",
            "job_conclusion": None,
            "job_runner_name": None,
            "job_runner_labels": ["self-hosted", "linux"],
        },
    ]
    table = pa.Table.from_pylist(rows, schema=PARQUET_SCHEMA)
    pq.write_table(table, path)


def test_workflow_conclusion_filter(tmp_path: Path) -> None:
    """It keeps only rows matching the requested workflow conclusion."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    result = WorkflowQuery(path).workflow_conclusion("failure").to_pylist()

    assert [row["run_id"] for row in result] == ["101"]


def test_chained_filters_and_column_pruning(tmp_path: Path) -> None:
    """Chained filters combine with AND and `columns()` restricts the output."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    result = (
        WorkflowQuery(path)
        .created_between("2026-08-01T00:00:00Z", "2026-08-10T00:00:00Z")
        .job_conclusion("success")
        .columns("run_id", "job_name")
        .to_pylist()
    )

    assert result == [{"run_id": "100", "job_name": "build"}]


def test_generic_filter_escape_hatch(tmp_path: Path) -> None:
    """filter() accepts arbitrary pyarrow.dataset expressions for uncovered cases."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    result = WorkflowQuery(path).filter(ds.field("job_duration_sec") > LONG_JOB_DURATION_SEC).to_pylist()

    assert [row["run_id"] for row in result] == ["101"]
