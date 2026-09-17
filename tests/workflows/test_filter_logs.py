# ruff: noqa: CPY001
"""Tests for `cimon.workflows.filter_logs`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from cimon.workflows import filter_logs

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
    monkeypatch.setattr(filter_logs, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(filter_logs, "download_job_log", _fake_download_job_log)

    input_path = tmp_path / "jobs.parquet"
    _write_sample(input_path)

    output_dir = tmp_path / "out"
    output_path = filter_logs.run(input_path, "ERROR:", output_dir)

    assert output_path == output_dir / filter_logs.LOG_MATCH_PARQUET_FILE_NAME
    table = pq.read_table(output_path)
    assert table.column("job_name").to_pylist() == ["test", "build"]


def test_run_returns_none_and_logs_warning_when_no_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No output file is written, and a warning is logged, when nothing matched."""
    monkeypatch.setattr(filter_logs, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(filter_logs, "download_job_log", _fake_download_job_log)

    input_path = tmp_path / "jobs.parquet"
    _write_sample(input_path)

    output_dir = tmp_path / "out"
    with caplog.at_level("WARNING"):
        output_path = filter_logs.run(input_path, "NOPE_NOT_FOUND", output_dir)

    assert output_path is None
    assert not (output_dir / filter_logs.LOG_MATCH_PARQUET_FILE_NAME).exists()
    assert "No log matched pattern" in caplog.text


def test_run_rejects_invalid_regex(tmp_path: Path) -> None:
    """An invalid regular expression is reported as a ValueError."""
    input_path = tmp_path / "jobs.parquet"
    _write_sample(input_path)

    with pytest.raises(ValueError, match="Invalid pattern"):
        filter_logs.run(input_path, "(unclosed", tmp_path / "out")


def test_run_requires_job_url_column(tmp_path: Path) -> None:
    """A Parquet file without a job_url column is rejected."""
    input_path = tmp_path / "jobs.parquet"
    pq.write_table(pa.table({"other_column": ["x"]}), input_path)

    with pytest.raises(ValueError, match="job_url"):
        filter_logs.run(input_path, "ERROR", tmp_path / "out")


def test_download_log_skips_already_downloaded_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A log already present in the logs directory isn't re-downloaded, and the skip is still logged."""
    calls: list[str] = []

    def _tracking_download(
        _host: str,
        _owner: str,
        _repo: str,
        job_id: str,
        output_dir: Path,
    ) -> Path:
        calls.append(job_id)
        return _fake_download_job_log(_host, _owner, _repo, job_id, output_dir)

    monkeypatch.setattr(filter_logs, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(filter_logs, "download_job_log", _tracking_download)

    job_url = "https://github.com/acme/app/actions/runs/100/job/1"
    with caplog.at_level("INFO"):
        first = filter_logs._download_log(job_url, tmp_path, "[1/1] (100%)")  # noqa: SLF001
        caplog.clear()
        second = filter_logs._download_log(job_url, tmp_path, "[1/1] (100%)")  # noqa: SLF001

    assert first == second
    assert calls == ["1"]
    assert "Already downloaded, skipping" in caplog.text


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

    monkeypatch.setattr(filter_logs, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(filter_logs, "download_job_log", _tracking_download)

    input_path = tmp_path / "jobs.parquet"
    _write_sample(input_path)

    filter_logs.run(input_path, "ERROR:", tmp_path / "out")

    assert not (tmp_path / "out" / filter_logs.LOGS_DIR_NAME).exists()
    assert not seen_logs_dirs[0].exists()


def test_run_keeps_logs_and_skips_redownload_across_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With keep_logs=True, logs persist under output_dir/logs and are reused by a later run."""
    calls: list[str] = []

    def _tracking_download(
        _host: str,
        _owner: str,
        _repo: str,
        job_id: str,
        output_dir: Path,
    ) -> Path:
        calls.append(job_id)
        return _fake_download_job_log(_host, _owner, _repo, job_id, output_dir)

    monkeypatch.setattr(filter_logs, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(filter_logs, "download_job_log", _tracking_download)

    input_path = tmp_path / "jobs.parquet"
    _write_sample(input_path)

    output_dir = tmp_path / "out"
    filter_logs.run(input_path, "ERROR:", output_dir, keep_logs=True)

    logs_dir = output_dir / filter_logs.LOGS_DIR_NAME
    assert logs_dir.exists()
    assert sorted(p.name for p in logs_dir.iterdir()) == [
        "100_1.log",
        "100_2.log",
        "101_3.log",
    ]

    calls.clear()
    filter_logs.run(input_path, "ERROR:", output_dir, keep_logs=True)

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
        job_id: str,
        output_dir: Path,
    ) -> Path:
        # download_job_log() leaves this behind before the subprocess call fails.
        (output_dir / f"{job_id}.log").touch()
        raise subprocess.CalledProcessError(1, ["gh"])

    monkeypatch.setattr(filter_logs, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(filter_logs, "download_job_log", _failing_download)

    job_url = "https://github.com/acme/app/actions/runs/100/job/1"
    with caplog.at_level("WARNING"):
        result = filter_logs._download_log(job_url, tmp_path, "[1/1] (100%)")  # noqa: SLF001

    assert result is None
    assert "failed to download its log" in caplog.text
    assert list(tmp_path.iterdir()) == []


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

    monkeypatch.setattr(filter_logs, "ensure_github_auth", lambda _host: None)
    monkeypatch.setattr(filter_logs, "download_job_log", _flaky_download)

    input_path = tmp_path / "jobs.parquet"
    _write_sample(input_path)

    output_path = filter_logs.run(input_path, "ERROR:", tmp_path / "out")

    table = pq.read_table(output_path)
    assert table.column("job_name").to_pylist() == ["build"]
