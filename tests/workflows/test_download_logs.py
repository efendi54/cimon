# ruff: noqa: CPY001
"""Tests for `cimon.workflows.download_logs`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from cimon.workflows import build_metrics, download_logs

if TYPE_CHECKING:
    from pathlib import Path


def _write_sample(path: Path) -> None:
    table = pa.table(
        {
            "job_url": [
                "https://github.com/acme/app/actions/runs/100/job/1",
                "https://github.com/acme/app/actions/runs/100/job/2",
                "https://github.com/acme/app/actions/runs/101/job/3",
            ],
            "job_name": ["build", "test", "build"],
        },
    )
    pq.write_table(table, path)


def _fake_download_job_log(
    _host: str,
    _owner: str,
    _repo: str,
    job_id: str,
    output_dir: Path,
) -> Path:
    logfile = output_dir / f"{job_id}.log"
    if logfile.is_file() and logfile.stat().st_size > 0:
        return logfile

    contents = {
        "1": "everything fine\n",
        "2": "ERROR: something broke\n",
        "3": "ERROR: something broke\n",
    }
    logfile.write_text(contents[job_id], encoding="utf-8")
    return logfile


def test_run_writes_matching_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only rows whose log matches the pattern are written to log-match.parquet."""
    monkeypatch.setattr(build_metrics, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(build_metrics, "download_job_log", _fake_download_job_log)

    input_path = tmp_path / "jobs.parquet"
    _write_sample(input_path)

    output_dir = tmp_path / "out"
    output_path = download_logs.run(str(input_path), output_dir, "ERROR:")

    assert output_path == output_dir / download_logs.LOG_MATCH_PARQUET_FILE_NAME
    table = pq.read_table(output_path)
    assert table.column("job_name").to_pylist() == ["test", "build"]


def test_run_without_pattern_only_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a pattern, every job's log is downloaded but no output file is written."""
    monkeypatch.setattr(build_metrics, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(build_metrics, "download_job_log", _fake_download_job_log)

    input_path = tmp_path / "jobs.parquet"
    _write_sample(input_path)

    output_dir = tmp_path / "out"
    output_path = download_logs.run(str(input_path), output_dir, keep_logs=True)

    assert output_path is None
    assert not (output_dir / download_logs.LOG_MATCH_PARQUET_FILE_NAME).exists()
    logs_dir = output_dir / download_logs.LOGS_DIR_NAME
    assert sorted(str(p.relative_to(logs_dir)) for p in logs_dir.rglob("*.log")) == [
        "100/1.log",
        "100/2.log",
        "101/3.log",
    ]


def test_run_accepts_single_job_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare job URL is accepted directly, matching cimon build-metrics' input forms."""
    monkeypatch.setattr(build_metrics, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(build_metrics, "download_job_log", _fake_download_job_log)

    job_url = "https://github.com/acme/app/actions/runs/100/job/2"
    output_dir = tmp_path / "out"
    output_path = download_logs.run(job_url, output_dir, "ERROR:")

    assert output_path == output_dir / download_logs.LOG_MATCH_PARQUET_FILE_NAME
    table = pq.read_table(output_path)
    assert table.column("job_url").to_pylist() == [job_url]


def test_run_accepts_json_array_of_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON array of plain URL strings is accepted, matching cimon build-metrics' JSON form."""
    monkeypatch.setattr(build_metrics, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(build_metrics, "download_job_log", _fake_download_job_log)

    input_path = tmp_path / "jobs.json"
    input_path.write_text(
        json.dumps(
            [
                "https://github.com/acme/app/actions/runs/100/job/1",
                "https://github.com/acme/app/actions/runs/100/job/2",
            ]
        ),
        encoding="utf-8",
    )

    output_path = download_logs.run(str(input_path), tmp_path / "out", "ERROR:")

    table = pq.read_table(output_path)
    assert table.column("job_url").to_pylist() == [
        "https://github.com/acme/app/actions/runs/100/job/2",
    ]


def test_run_returns_none_and_logs_warning_when_no_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No output file is written, and a warning is logged, when nothing matched."""
    monkeypatch.setattr(build_metrics, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(build_metrics, "download_job_log", _fake_download_job_log)

    input_path = tmp_path / "jobs.parquet"
    _write_sample(input_path)

    output_dir = tmp_path / "out"
    with caplog.at_level("WARNING"):
        output_path = download_logs.run(str(input_path), output_dir, "NOPE_NOT_FOUND")

    assert output_path is None
    assert not (output_dir / download_logs.LOG_MATCH_PARQUET_FILE_NAME).exists()
    assert "No log matched pattern" in caplog.text


def test_run_rejects_invalid_regex(tmp_path: Path) -> None:
    """An invalid regular expression is reported as a ValueError."""
    input_path = tmp_path / "jobs.parquet"
    _write_sample(input_path)

    with pytest.raises(ValueError, match="Invalid pattern"):
        download_logs.run(str(input_path), tmp_path / "out", "(unclosed")


def test_run_requires_job_url_column(tmp_path: Path) -> None:
    """A Parquet file without a job_url column is rejected."""
    input_path = tmp_path / "jobs.parquet"
    pq.write_table(pa.table({"other_column": ["x"]}), input_path)

    with pytest.raises(ValueError, match="job_url"):
        download_logs.run(str(input_path), tmp_path / "out", "ERROR")


def test_download_log_skips_already_downloaded_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A log already present in the logs directory isn't re-downloaded."""
    calls: list[str] = []

    def _tracking_download(
        _host: str,
        _owner: str,
        _repo: str,
        job_id: str,
        output_dir: Path,
    ) -> Path:
        logfile = output_dir / f"{job_id}.log"
        if not (logfile.is_file() and logfile.stat().st_size > 0):
            calls.append(job_id)
        return _fake_download_job_log(_host, _owner, _repo, job_id, output_dir)

    monkeypatch.setattr(build_metrics, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(build_metrics, "download_job_log", _tracking_download)

    job_url = "https://github.com/acme/app/actions/runs/100/job/1"
    first = download_logs._download_log(job_url, tmp_path, "[1/1] (100%)")  # noqa: SLF001
    second = download_logs._download_log(job_url, tmp_path, "[1/1] (100%)")  # noqa: SLF001

    assert first == second
    assert calls == ["1"]


def test_run_removes_logs_dir_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --keep-logs, the downloaded logs don't survive the run."""
    seen_logs_dirs: list[Path] = []

    def _tracking_download(
        _host: str,
        _owner: str,
        _repo: str,
        job_id: str,
        output_dir: Path,
    ) -> Path:
        seen_logs_dirs.append(output_dir)
        return _fake_download_job_log(_host, _owner, _repo, job_id, output_dir)

    monkeypatch.setattr(build_metrics, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(build_metrics, "download_job_log", _tracking_download)

    input_path = tmp_path / "jobs.parquet"
    _write_sample(input_path)

    download_logs.run(str(input_path), tmp_path / "out", "ERROR:")

    assert not (tmp_path / "out" / download_logs.LOGS_DIR_NAME).exists()
    assert not seen_logs_dirs[0].exists()


def test_run_keeps_logs_and_skips_redownload_across_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With keep_logs=True, logs persist under output_dir/logs/<run_id>/<job_id>.log and are reused."""
    calls: list[str] = []

    def _tracking_download(
        _host: str,
        _owner: str,
        _repo: str,
        job_id: str,
        output_dir: Path,
    ) -> Path:
        logfile = output_dir / f"{job_id}.log"
        if not (logfile.is_file() and logfile.stat().st_size > 0):
            calls.append(job_id)
        return _fake_download_job_log(_host, _owner, _repo, job_id, output_dir)

    monkeypatch.setattr(build_metrics, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(build_metrics, "download_job_log", _tracking_download)

    input_path = tmp_path / "jobs.parquet"
    _write_sample(input_path)

    output_dir = tmp_path / "out"
    download_logs.run(str(input_path), output_dir, "ERROR:", keep_logs=True)

    logs_dir = output_dir / download_logs.LOGS_DIR_NAME
    assert logs_dir.exists()
    assert sorted(str(p.relative_to(logs_dir)) for p in logs_dir.rglob("*.log")) == [
        "100/1.log",
        "100/2.log",
        "101/3.log",
    ]

    calls.clear()
    download_logs.run(str(input_path), output_dir, "ERROR:", keep_logs=True)

    assert calls == []


def test_download_log_skips_job_on_download_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed download (e.g. a 404 for an expired log) is skipped instead of aborting the run."""
    import subprocess  # noqa: PLC0415

    def _failing_download(
        _host: str,
        _owner: str,
        _repo: str,
        job_id: str,  # noqa: ARG001
        output_dir: Path,  # noqa: ARG001
    ) -> Path:
        raise subprocess.CalledProcessError(1, ["gh"])

    monkeypatch.setattr(build_metrics, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(build_metrics, "download_job_log", _failing_download)

    job_url = "https://github.com/acme/app/actions/runs/100/job/1"
    with caplog.at_level("WARNING"):
        result = download_logs._download_log(job_url, tmp_path, "[1/1] (100%)")  # noqa: SLF001

    assert result is None
    assert "failed to download its log" in caplog.text


def test_download_log_skips_url_with_unexpected_format(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A URL that doesn't match the expected workflow-job format is skipped with a warning."""
    with caplog.at_level("WARNING"):
        result = download_logs._download_log("not-a-job-url", tmp_path, "[1/1] (100%)")  # noqa: SLF001

    assert result is None
    assert "doesn't match the expected format" in caplog.text


def test_run_continues_after_a_job_log_fails_to_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One job's log failing to download doesn't abort the rest of the run."""
    import subprocess  # noqa: PLC0415

    def _flaky_download(
        _host: str,
        _owner: str,
        _repo: str,
        job_id: str,
        output_dir: Path,
    ) -> Path:
        if job_id == "2":
            raise subprocess.CalledProcessError(1, ["gh"])
        return _fake_download_job_log(_host, _owner, _repo, job_id, output_dir)

    monkeypatch.setattr(build_metrics, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(build_metrics, "download_job_log", _flaky_download)

    input_path = tmp_path / "jobs.parquet"
    _write_sample(input_path)

    output_path = download_logs.run(str(input_path), tmp_path / "out", "ERROR:")

    table = pq.read_table(output_path)
    assert table.column("job_name").to_pylist() == ["build"]
