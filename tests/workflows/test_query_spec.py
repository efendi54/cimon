# ruff: noqa: CPY001
"""Tests for declarative filter specs (query_spec.build_expression / WorkflowQuery.filter_spec)."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import pytest

from cimon.workflows.query import WorkflowQuery
from cimon.workflows.query_spec import _resolve_relative_date, build_expression

from tests.workflows.test_query import _write_sample

if TYPE_CHECKING:
    from pathlib import Path

LONG_JOB_DURATION_SEC = 60


def test_leaf_condition(tmp_path: Path) -> None:
    """A single leaf condition filters on one column."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    spec = {"column": "workflow_conclusion", "op": "eq", "value": "failure"}
    result = WorkflowQuery(path).filter_spec(spec).to_pylist()

    assert [row["run_id"] for row in result] == ["101"]


def test_all_and_any_nesting(tmp_path: Path) -> None:
    """'all' and 'any' groups combine nested conditions with AND / OR."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    spec = {
        "all": [
            {"column": "workflow_status", "op": "eq", "value": "completed"},
            {
                "any": [
                    {"column": "job_conclusion", "op": "eq", "value": "failure"},
                    {"column": "job_duration_sec", "op": "gt", "value": LONG_JOB_DURATION_SEC},
                ],
            },
        ],
    }
    result = WorkflowQuery(path).filter_spec(spec).to_pylist()

    assert [row["run_id"] for row in result] == ["101"]


def test_not_negates_group(tmp_path: Path) -> None:
    """'not' negates the nested condition or group."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    spec = {"not": {"column": "workflow_conclusion", "op": "eq", "value": "failure"}}
    result = WorkflowQuery(path).filter_spec(spec).to_pylist()

    assert [row["run_id"] for row in result] == ["100"]


def test_starts_with_matches_job_name_prefix(tmp_path: Path) -> None:
    """'starts_with' matches jobs whose name starts with the given prefix."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    spec = {"column": "job_name", "op": "starts_with", "value": "bui"}
    result = WorkflowQuery(path).filter_spec(spec).to_pylist()

    assert [row["run_id"] for row in result] == ["100"]


def test_contains_matches_job_name_substring(tmp_path: Path) -> None:
    """'contains' matches jobs whose name contains the given substring anywhere."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    spec = {"column": "job_name", "op": "contains", "value": "es"}
    result = WorkflowQuery(path).filter_spec(spec).to_pylist()

    assert [row["run_id"] for row in result] == ["101"]


def test_any_combines_alternative_job_name_prefixes(tmp_path: Path) -> None:
    """'any' of 'starts_with' conditions matches several alternative name prefixes."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    spec = {
        "any": [
            {"column": "job_name", "op": "starts_with", "value": "bui"},
            {"column": "job_name", "op": "starts_with", "value": "tes"},
        ],
    }
    result = WorkflowQuery(path).filter_spec(spec).to_pylist()

    assert sorted(row["run_id"] for row in result) == ["100", "101"]


def test_list_contains_matches_exact_runner_label(tmp_path: Path) -> None:
    """'list_contains' matches rows whose list column contains the value exactly."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    spec = {"column": "job_runner_labels", "op": "list_contains", "value": "self-hosted"}
    result = WorkflowQuery(path).filter_spec(spec).to_pylist()

    assert [row["run_id"] for row in result] == ["102"]


def test_list_contains_does_not_partial_match(tmp_path: Path) -> None:
    """'list_contains' doesn't match a label that is only a substring of another."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    spec = {"column": "job_runner_labels", "op": "list_contains", "value": "self-host"}
    result = WorkflowQuery(path).filter_spec(spec).to_pylist()

    assert result == []


def test_in_progress_jobs_with_runner_label(tmp_path: Path) -> None:
    """Combining job_status and runner_label finds still-running jobs on a given runner."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    result = WorkflowQuery(path).job_status("in_progress").runner_label("linux").to_pylist()

    assert [row["run_id"] for row in result] == ["102"]


def test_unknown_operator_raises() -> None:
    """An unsupported operator raises a clear error instead of failing silently."""
    with pytest.raises(ValueError, match="Unsupported filter operator"):
        build_expression({"column": "run_id", "op": "nope", "value": "1"})


def test_missing_key_raises() -> None:
    """A condition missing 'column' or 'op' raises a clear error."""
    with pytest.raises(ValueError, match="missing required key"):
        build_expression({"column": "run_id"})


def test_resolve_relative_date_today_variants() -> None:
    """'today'/'today_start'/'today_end' resolve to today's date at the expected time of day."""
    today = dt.datetime.now(tz=dt.timezone.utc).date().isoformat()

    assert _resolve_relative_date("today") == f"{today}T00:00:00Z"
    assert _resolve_relative_date("today_start") == f"{today}T00:00:00Z"
    assert _resolve_relative_date("today_end") == f"{today}T23:59:59Z"


def test_resolve_relative_date_day_offsets() -> None:
    """'today[+-]Nd[_start|_end]' resolves to a date N days from today."""
    today = dt.datetime.now(tz=dt.timezone.utc).date()

    assert _resolve_relative_date("today-4d") == f"{today - dt.timedelta(days=4)}T00:00:00Z"
    assert _resolve_relative_date("today-4d_end") == f"{today - dt.timedelta(days=4)}T23:59:59Z"
    assert _resolve_relative_date("today+2d") == f"{today + dt.timedelta(days=2)}T00:00:00Z"


def test_resolve_relative_date_now() -> None:
    """'now' resolves to the current UTC instant, not just a date."""
    resolved = _resolve_relative_date("now")

    assert dt.datetime.strptime(resolved, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def test_resolve_relative_date_leaves_other_strings_untouched() -> None:
    """Strings that aren't relative-date placeholders (e.g. real timestamps, statuses) pass through unchanged."""
    assert _resolve_relative_date("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"
    assert _resolve_relative_date("success") == "success"


def test_between_with_relative_date_placeholders(tmp_path: Path) -> None:
    """A 'between' condition resolves 'today'/'today_end' placeholders before filtering."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    # A wide, far-past lower bound keeps this independent of the sample's fixed
    # 2026-08 dates and today's actual date -- only the upper bound matters here.
    spec = {"column": "created_at", "op": "between", "value": ["today-3650d", "today_end"]}
    result = WorkflowQuery(path).filter_spec(spec).to_pylist()

    assert {row["run_id"] for row in result} == {"100", "101", "102"}
