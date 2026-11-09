# ruff: noqa: D103, PLR2004
"""Batch selection, duration statistics and HTML output regression tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from cimon.__main__ import main
from cimon.visualization.renderers.run_averages import average_figure, job_averages, select_attempts
from cimon.workflows.run_url import parse_workflow_run_url

from tests.visualization.test_workflow_job_durations import RUN_URL, _job, _write_cache

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result


def _visualize(tmp_path: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        main,
        ["visualize", "workflow-job-durations", "-o", str(tmp_path / "out"), *extra],
    )


def _selection(tmp_path: Path, urls: list[str | None]) -> str:
    path = tmp_path / "selected.parquet"
    cache = pq.read_table(tmp_path / "cache" / "workflows.parquet")
    rows = cache.to_pylist()
    selected = []
    for url in dict.fromkeys(urls):
        matches = []
        if url:
            try:
                ref = parse_workflow_run_url(url)
            except ValueError:
                ref = None
            if ref:
                matches = [
                    row
                    for row in rows
                    if (row["host"], row["owner"], row["repo"], row["run_id"])
                    == (ref.host, ref.owner, ref.repo, ref.run_id)
                    and (ref.attempt is None or row["run_attempt"] == ref.attempt)
                ]
        selected.extend([{**row, "workflow_run_url": url} for row in (matches or rows[:1])])
    table = pa.Table.from_pylist(selected, schema=cache.schema) if selected else cache.slice(0, 0)
    pq.write_table(table, path)
    return f"run-urls-file={path}"


def _completed(run_id: str, *, attempt: int = 1) -> dict[str, object]:
    return _job(
        "Build / compile",
        "2026-11-04T19:02:00Z",
        "2026-11-04T19:12:00Z",
        created="2026-11-04T19:00:00Z",
        run_id=run_id,
        attempt=attempt,
    )


@pytest.mark.parametrize("individual", [True, False])
def test_batch_writes_summary_and_optional_individual_pages(tmp_path: Path, *, individual: bool) -> None:
    """Repeated URLs do not multiply samples or per-run output."""
    _write_cache(tmp_path / "cache", [_completed("100"), _completed("200")])
    selection = _selection(tmp_path, [RUN_URL, RUN_URL, RUN_URL.replace("100", "200")])
    result = _visualize(tmp_path, "-p", selection, "-p", f"individual-html={str(individual).lower()}")

    assert result.exit_code == 0, (result.output, result.exception)
    pages = list((tmp_path / "out").glob("*/averages.html"))
    assert len(pages) == 1
    content = pages[0].read_text()
    assert "2 run attempts" in content
    assert "waiting for runner" in content
    assert "total (paired samples)" in content
    assert len(list((tmp_path / "out").rglob("run-*-attempt-*.html"))) == (2 if individual else 0)
    runs_dir = pages[0].parent / "runs"
    assert runs_dir.exists() is individual
    if individual:
        assert {page.name for page in runs_dir.glob("*.html")} == {
            "run-100-attempt-1.html",
            "run-200-attempt-1.html",
        }
    assert pq.read_table(tmp_path / "out" / "workflow-job-durations.parquet").num_rows == 2


def test_batch_defaults_to_individual_pages(tmp_path: Path) -> None:
    _write_cache(tmp_path / "cache", [_completed("100")])
    result = _visualize(tmp_path, "-p", _selection(tmp_path, [RUN_URL]))

    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "out").rglob("run-100-attempt-1.html"))) == 1


def test_selection_resolves_latest_explicit_and_overlapping_attempts() -> None:
    """A base URL and its latest explicit attempt count once; older explicit attempts remain selected."""
    table = pa.Table.from_pylist([_completed("100"), _completed("100", attempt=2), _completed("200")])
    selected = select_attempts(table, [RUN_URL, f"{RUN_URL}/attempts/2", f"{RUN_URL}/attempts/1"])

    assert selected.num_rows == 2
    assert set(selected.column("run_attempt").to_pylist()) == {1, 2}


def test_averages_exclude_incomplete_and_missing_timings_per_metric() -> None:
    """Missing waits are not zero; failed jobs are not mixed with successes."""
    second = _completed("200")
    second.update(job_started_at="2026-11-04T19:04:00Z", job_completed_at="2026-11-04T19:24:00Z")
    unknown_wait = _completed("300")
    unknown_wait["job_created_at"] = None
    failure = _completed("400")
    failure["job_conclusion"] = "failure"
    running = _completed("500")
    running.update(job_status="in_progress", job_completed_at=None, job_conclusion=None)
    skipped = _completed("600")
    skipped["job_conclusion"] = "skipped"
    invalid = _completed("700")
    invalid.update(job_started_at="2026-11-04T18:00:00Z", job_completed_at="2026-11-04T17:00:00Z")
    summary = job_averages(
        pa.Table.from_pylist([_completed("100"), second, unknown_wait, failure, running, skipped, invalid]),
    ).droplevel(["host", "owner", "repo", "workflow_file"])
    success = summary.loc[("Build / compile", "success")]

    assert success[("queue_min", "mean")] == 3
    assert success[("queue_min", "median")] == 3
    assert success[("queue_min", "count")] == 2
    assert success[("dur_min", "mean")] == pytest.approx(40 / 3)
    assert success[("dur_min", "median")] == 10
    assert success[("dur_min", "count")] == 3
    assert success[("total_min", "mean")] == 18
    assert success[("total_min", "count")] == 2
    assert summary.loc[("Build / compile", "failure"), ("dur_min", "count")] == 1
    assert len(summary) == 2
    figure = average_figure(summary, "nightly", 7)
    assert list(figure.data[0].x) == [2, 3]
    assert list(figure.data[2].x) == [12, 18]
    assert figure.layout.barmode == "stack"


def test_averages_use_utc_elapsed_time_across_dst() -> None:
    row = _completed("100")
    row.update(
        job_created_at="2026-10-25T00:00:00Z",
        job_started_at="2026-10-25T00:30:00Z",
        job_completed_at="2026-10-25T01:30:00Z",
    )
    summary = job_averages(pa.Table.from_pylist([row]))
    assert summary[("dur_min", "mean")].iloc[0] == 60


def test_batch_separates_workflows(tmp_path: Path) -> None:
    other = _completed("200")
    other["workflow_file"] = ".github/workflows/pr.yml"
    _write_cache(tmp_path / "cache", [_completed("100"), other])
    result = _visualize(
        tmp_path,
        "-p",
        _selection(tmp_path, [RUN_URL, RUN_URL.replace("100", "200")]),
        "-p",
        "individual-html=false",
    )

    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "out").glob("*/averages.html"))) == 2


def test_workflow_directories_group_averages_with_their_own_runs(tmp_path: Path) -> None:
    """Each workflow average shares its directory only with that workflow's run pages."""
    other = _completed("200")
    other["workflow_file"] = ".github/workflows/pr.yml"
    _write_cache(tmp_path / "cache", [_completed("100"), other])
    result = _visualize(
        tmp_path,
        "-p",
        _selection(tmp_path, [RUN_URL, RUN_URL.replace("100", "200")]),
    )

    assert result.exit_code == 0, result.output
    for filename, run_id in [("nightly.yml", "100"), ("pr.yml", "200")]:
        [directory] = (tmp_path / "out").glob(f"{filename}-*")
        assert (directory / "averages.html").is_file()
        assert {page.name for page in (directory / "runs").glob("*.html")} == {
            f"run-{run_id}-attempt-1.html",
        }


@pytest.mark.parametrize("urls", [[], [None], [""], ["https://example.com/pull/1"]])
def test_batch_rejects_invalid_selection(tmp_path: Path, urls: list[str | None]) -> None:
    _write_cache(tmp_path / "cache")
    result = _visualize(tmp_path, "-p", _selection(tmp_path, urls))

    assert result.exit_code != 0
    assert not list((tmp_path / "out").rglob("*.html"))


def test_batch_rejects_missing_column(tmp_path: Path) -> None:
    _write_cache(tmp_path / "cache")
    path = tmp_path / "selection.parquet"
    pq.write_table(pa.table({"wrong": ["test"]}), path)
    result = _visualize(tmp_path, "-p", f"run-urls-file={path}")
    assert result.exit_code != 0
    assert "Cannot read workflow_run_url" in result.output


@pytest.mark.parametrize("extra", ["individual-html=maybe", f"run-url={RUN_URL}"])
def test_batch_rejects_conflicting_or_invalid_options(tmp_path: Path, extra: str) -> None:
    _write_cache(tmp_path / "cache")
    result = _visualize(tmp_path, "-p", _selection(tmp_path, [RUN_URL]), "-p", extra)
    assert result.exit_code != 0


def test_batch_reports_missing_run_before_writing_html(tmp_path: Path) -> None:
    _write_cache(tmp_path / "cache")
    missing = RUN_URL.replace("100", "999")
    result = _visualize(tmp_path, "-p", _selection(tmp_path, [RUN_URL, missing]))

    assert result.exit_code != 0
    assert missing in result.output
    assert not list((tmp_path / "out").rglob("*.html"))


def test_batch_with_only_running_jobs_has_no_average(tmp_path: Path) -> None:
    row = _completed("100")
    row.update(job_status="in_progress", job_completed_at=None, job_conclusion=None)
    _write_cache(tmp_path / "cache", [row])
    result = _visualize(tmp_path, "-p", _selection(tmp_path, [RUN_URL]))

    assert result.exit_code != 0
    assert "no completed" in result.output


def test_missing_created_column_does_not_contribute_zero_wait() -> None:
    row = _completed("100")
    del row["job_created_at"]
    summary = job_averages(pa.Table.from_pylist([row]))
    assert summary[("queue_min", "count")].iloc[0] == 0
    assert pd.isna(summary[("queue_min", "mean")].iloc[0])
    assert summary[("dur_min", "mean")].iloc[0] == 10


def test_batch_separates_same_run_id_in_different_repositories(tmp_path: Path) -> None:
    other = _completed("100")
    other["repo"] = "other"
    other["workflow_run_url"] = RUN_URL.replace("/app/", "/other/")
    _write_cache(tmp_path / "cache", [_completed("100"), other])
    result = _visualize(
        tmp_path,
        "-p",
        _selection(tmp_path, [RUN_URL, str(other["workflow_run_url"])]),
    )

    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "out").glob("*/averages.html"))) == 2
    assert len(list((tmp_path / "out").rglob("run-100-attempt-1.html"))) == 2


def test_batch_logs_workflow_without_completed_samples(tmp_path: Path) -> None:
    other = _completed("200")
    other.update(
        workflow_file=".github/workflows/pr.yml",
        job_status="in_progress",
        job_completed_at=None,
        job_conclusion=None,
    )
    _write_cache(tmp_path / "cache", [_completed("100"), other])
    result = _visualize(
        tmp_path,
        "-p",
        _selection(tmp_path, [RUN_URL, RUN_URL.replace("100", "200")]),
        "-p",
        "individual-html=false",
    )

    assert result.exit_code == 0, result.output
    assert "No completed-job duration measurements" in result.output
    assert len(list((tmp_path / "out").glob("*/averages.html"))) == 1


def test_selection_missing_file_is_reported(tmp_path: Path) -> None:
    _write_cache(tmp_path / "cache")
    result = _visualize(tmp_path, "-p", f"run-urls-file={tmp_path / 'missing.parquet'}")

    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_zero_duration_is_a_valid_sample() -> None:
    row = _completed("100")
    row.update(job_started_at=row["job_created_at"], job_completed_at=row["job_created_at"])
    summary = job_averages(pa.Table.from_pylist([row]))

    assert summary[("queue_min", "mean")].iloc[0] == 0
    assert summary[("dur_min", "mean")].iloc[0] == 0
    assert summary[("total_min", "count")].iloc[0] == 1


def test_batch_uses_supplied_job_data_without_any_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A standalone Parquet file is sufficient and its durations are the source of truth."""
    path = tmp_path / "jobs.parquet"
    row = _completed("100")
    row["job_completed_at"] = "2026-11-04T19:32:00Z"
    pq.write_table(pa.Table.from_pylist([row]), path)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    result = _visualize(
        tmp_path,
        "-p",
        f"run-urls-file={path}",
        "-p",
        "individual-html=false",
    )

    assert result.exit_code == 0, (result.output, result.exception)
    intermediate = pq.read_table(tmp_path / "out" / "workflow-job-durations.parquet")
    assert intermediate.to_pylist() == [row]
    assert job_averages(intermediate)[("dur_min", "mean")].iloc[0] == 30
    assert len(list((tmp_path / "out").rglob("*.html"))) == 1


def test_batch_rejects_additional_input(tmp_path: Path) -> None:
    _write_cache(tmp_path / "cache", [_completed("100")])
    result = _visualize(
        tmp_path,
        "-p",
        _selection(tmp_path, [RUN_URL]),
        "-i",
        str(tmp_path / "cache" / "workflows.parquet"),
    )

    assert result.exit_code != 0
    assert "do not pass -i" in result.output


def test_batch_rejects_url_only_file_with_clear_schema_error(tmp_path: Path) -> None:
    path = tmp_path / "urls.parquet"
    pq.write_table(pa.table({"workflow_run_url": [RUN_URL]}), path)
    result = _visualize(tmp_path, "-p", f"run-urls-file={path}")

    assert result.exit_code != 0
    assert "missing required column(s)" in result.output
    assert "job_started_at" in result.output
