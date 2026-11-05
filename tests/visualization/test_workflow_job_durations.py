# ruff: noqa: CPY001, PLR2004
"""Tests for the parametrized `cimon visualize workflow-job-durations` visualization and its renderer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from cimon.__main__ import main
from cimon.visualization.renderers.run_timeline import critical_path, prepare_jobs, union_minutes

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

RUN_URL = "https://example.com/acme/app/actions/runs/100"


def _job(
    name: str,
    start: str | None,
    end: str | None,
    *,
    created: str | None = None,
    status: str | None = None,
    conclusion: str | None = "success",
    attempt: int = 1,
    run_id: str = "100",
) -> dict[str, object]:
    return {
        "host": "example.com",
        "owner": "acme",
        "repo": "app",
        "run_id": run_id,
        "run_attempt": attempt,
        "event": "schedule",
        "workflow_status": "completed" if end else "in_progress",
        "workflow_file": ".github/workflows/nightly.yml",
        "workflow_run_url": f"https://example.com/acme/app/actions/runs/{run_id}/attempts/{attempt}",
        "created_at": "2026-11-04T19:00:00Z",
        "cache_updated_at": "2026-11-05T09:00:00+00:00",
        "job_name": name,
        "job_url": f"https://example.com/acme/app/actions/runs/{run_id}/job/{name}",
        "job_created_at": created,
        "job_started_at": start,
        "job_completed_at": end,
        "job_status": status or ("completed" if end else "in_progress"),
        "job_conclusion": conclusion if end else None,
    }


def _rows() -> list[dict[str, object]]:
    return [
        _job("Build / compile", "2026-11-04T19:01:00Z", "2026-11-04T20:00:00Z", created="2026-11-04T19:00:30Z"),
        _job("Test A / run", "2026-11-04T20:00:30Z", "2026-11-04T20:30:00Z", created="2026-11-04T20:00:00Z"),
        _job("Test B / run", "2026-11-04T20:00:40Z", None, created="2026-11-04T20:00:00Z"),
        _job(
            "Test C / run",
            "2026-11-04T20:00:00Z",
            None,
            created="2026-11-04T20:00:00Z",
            status="queued",
        ),
        _job(
            "Release / publish",
            "2026-11-04T20:30:00Z",
            "2026-11-04T20:30:00Z",
            created="2026-11-04T20:30:00Z",
            conclusion="skipped",
        ),
        _job(
            "Build / compile",
            "2026-11-05T08:00:00Z",
            "2026-11-05T08:10:00Z",
            created="2026-11-05T07:59:00Z",
            attempt=2,
        ),
        _job("Other run", "2026-11-04T19:01:00Z", "2026-11-04T19:02:00Z", run_id="200"),
    ]


def _write_cache(cache_dir: Path, rows: list[dict[str, object]] | None = None) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows or _rows()), cache_dir / "workflows.parquet")


def _visualize(tmp_path: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        main,
        [
            "visualize",
            "workflow-job-durations",
            "-i",
            str(tmp_path / "cache" / "workflows.parquet"),
            "-o",
            str(tmp_path / "out"),
            *extra,
        ],
    )


def test_visualize_defaults_to_latest_attempt(tmp_path: Path) -> None:
    """Without an attempt in the URL the latest cached attempt of exactly that run is plotted."""
    _write_cache(tmp_path / "cache")

    result = _visualize(tmp_path, "-p", f"run-url={RUN_URL}")

    assert result.exit_code == 0, result.output
    html = (tmp_path / "out" / "run-100-attempt-2.html").read_text(encoding="utf-8")
    assert "Build" in html
    assert "Test A" not in html
    assert pq.read_table(tmp_path / "out" / "workflow-job-durations.parquet").num_rows == 6


def test_visualize_with_attempt_in_url_plots_running_and_skips_skipped(tmp_path: Path) -> None:
    """A /attempts/<n> URL selects that attempt; still running jobs are plotted, skipped ones are not."""
    _write_cache(tmp_path / "cache")

    result = _visualize(tmp_path, "-p", f"run-url={RUN_URL}/attempts/1")

    assert result.exit_code == 0, result.output
    html = (tmp_path / "out" / "run-100-attempt-1.html").read_text(encoding="utf-8")
    assert "Test B" in html
    assert "in_progress" in html
    assert "Release" not in html
    # Plotly serializes "/" as \\u002f inside the embedded figure JSON.
    run_url = "https://example.com/acme/app/actions/runs/100/attempts/1".replace("/", "\\u002f")
    assert f'href=\\"{run_url}\\"' in html


def test_visualize_unknown_run_fails(tmp_path: Path) -> None:
    """A run that is not in the cache produces a clear error instead of an empty page."""
    _write_cache(tmp_path / "cache")

    result = _visualize(tmp_path, "-p", "run-url=https://example.com/acme/app/actions/runs/999")

    assert result.exit_code != 0
    assert "Run `cimon sync` first" in result.output


def test_visualize_rejects_non_run_url(tmp_path: Path) -> None:
    """A URL that is not a workflow-run URL is reported with a clear message."""
    _write_cache(tmp_path / "cache")

    result = _visualize(tmp_path, "-p", "run-url=https://example.com/acme/app/pull/1")

    assert result.exit_code != 0
    assert "Not a workflow run URL" in result.output


def test_visualize_requires_run_url_parameter(tmp_path: Path) -> None:
    """The parametrized visualization cannot run without its run-url."""
    _write_cache(tmp_path / "cache")

    result = _visualize(tmp_path)

    assert result.exit_code != 0
    assert "Missing parameter 'run-url'" in result.output


def test_visualize_rejects_unknown_parameter_and_malformed_option(tmp_path: Path) -> None:
    """Unknown parameter names and options without KEY=VALUE fail early."""
    _write_cache(tmp_path / "cache")

    unknown = _visualize(tmp_path, "-p", f"run-url={RUN_URL}", "-p", "foo=1")
    malformed = _visualize(tmp_path, "-p", "run-url")

    assert unknown.exit_code != 0
    assert "does not accept parameter(s) foo" in unknown.output
    assert malformed.exit_code != 0
    assert "expected KEY=VALUE" in malformed.output


def test_visualize_list_shows_parameters() -> None:
    """--list shows the parameters of parametrized visualizations."""
    result = CliRunner().invoke(main, ["visualize", "--list"])

    assert result.exit_code == 0, result.output
    assert "workflow-job-durations" in result.output
    assert "-p run-url=..." in result.output


def test_visualize_without_parameters_rejects_parameters(tmp_path: Path) -> None:
    """Plain visualizations do not silently ignore -p."""
    _write_cache(tmp_path / "cache")

    result = CliRunner().invoke(
        main,
        [
            "visualize",
            "runner-status-trend",
            "-i",
            str(tmp_path / "cache" / "workflows.parquet"),
            "-o",
            str(tmp_path / "out"),
            "-p",
            "x=1",
        ],
    )

    assert result.exit_code != 0
    assert "does not accept parameter(s) x" in result.output


def test_union_minutes_counts_overlaps_once() -> None:
    """Overlapping intervals only count once towards the wall-clock time."""
    starts = pd.Series(pd.to_datetime(["2026-01-01 10:00", "2026-01-01 10:30", "2026-01-01 12:00"]))
    ends = pd.Series(pd.to_datetime(["2026-01-01 11:00", "2026-01-01 11:30", "2026-01-01 12:30"]))

    assert union_minutes(starts, ends) == 90 + 30


def _timed(rows: dict[str, list[str]]) -> pd.DataFrame:
    jobs = pd.DataFrame({"job_name": rows["name"]})
    for column in ("eligible", "start", "end"):
        jobs[column] = pd.to_datetime(rows[column])
    jobs["dur_min"] = (jobs["end"] - jobs["start"]).dt.total_seconds() / 60
    return jobs


def test_critical_path_follows_jobs_that_end_right_before_the_next_starts() -> None:
    """The critical path chains from the last-ending job back through its predecessors, ignoring parallel ones."""
    jobs = _timed(
        {
            "name": ["setup", "build", "side", "test"],
            "eligible": ["2026-01-01 10:00", "2026-01-01 10:10", "2026-01-01 10:10", "2026-01-01 12:00"],
            "start": ["2026-01-01 10:00", "2026-01-01 10:10", "2026-01-01 10:10", "2026-01-01 12:00"],
            "end": ["2026-01-01 10:10", "2026-01-01 12:00", "2026-01-01 10:40", "2026-01-01 13:00"],
        },
    )

    assert critical_path(jobs, gap_seconds=60)["job_name"].tolist() == ["setup", "build", "test"]


def test_critical_path_bridges_runner_wait_via_job_created_at() -> None:
    """A job that waited for a runner is chained to the job that ended when it was created, not to an unrelated one."""
    jobs = _timed(
        {
            "name": ["setup", "unrelated", "build"],
            "eligible": ["2026-01-01 10:00", "2026-01-01 10:00", "2026-01-01 10:10"],
            "start": ["2026-01-01 10:00", "2026-01-01 10:05", "2026-01-01 14:00"],
            "end": ["2026-01-01 10:10", "2026-01-01 13:50", "2026-01-01 15:00"],
        },
    )

    path = critical_path(jobs, gap_seconds=60)

    assert path["job_name"].tolist() == ["setup", "build"]


def test_prepare_jobs_derives_waiting_time_and_treats_queued_jobs_as_waiting() -> None:
    """Waiting time is created -> started; a still queued job waits until the snapshot and has no running time."""
    jobs, snapshot = prepare_jobs(pa.Table.from_pylist(_rows()[:4]), tz="UTC")
    by_name = jobs.set_index("job_name")

    assert snapshot == pd.Timestamp("2026-11-05 09:00")
    assert by_name.loc["Build / compile", "queue_min"] == 0.5
    assert by_name.loc["Test A / run", "queue_min"] == 0.5
    assert by_name.loc["Test C / run", "dur_min"] == 0
    assert (
        by_name.loc["Test C / run", "queue_min"] == (snapshot - pd.Timestamp("2026-11-04 20:00")).total_seconds() / 60
    )


def test_visualize_works_with_cache_without_job_created_at(tmp_path: Path) -> None:
    """A cache written before `job_created_at` existed still plots; waiting times are simply unknown (0)."""
    rows = [{key: value for key, value in row.items() if key != "job_created_at"} for row in _rows()]
    _write_cache(tmp_path / "cache", rows)

    result = _visualize(tmp_path, "-p", f"run-url={RUN_URL}/attempts/1")

    assert result.exit_code == 0, result.output
    assert "queue times are unknown" in result.output
    assert (tmp_path / "out" / "run-100-attempt-1.html").exists()
