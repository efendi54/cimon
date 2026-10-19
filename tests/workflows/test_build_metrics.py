# ruff: noqa: CPY001
"""Tests for `cimon.workflows.build_metrics` job-info/log caching behaviour."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from cimon.workflows import build_metrics

if TYPE_CHECKING:
    from pathlib import Path

JOB_URL = "https://github.com/acme/app/actions/runs/100/job/1"


def _write_log(output_root: Path, run_id: str, job_id: str, contents: str = "") -> Path:
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_file = run_dir / f"{job_id}.log"
    log_file.write_text(contents, encoding="utf-8")
    return log_file


def test_process_job_run_fetches_job_info_when_nothing_is_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a cached log or JSON, both the log and job info are fetched from GitHub."""
    auth_calls: list[str] = []
    job_info_calls: list[str] = []

    monkeypatch.setattr(build_metrics, "ensure_github_auth", auth_calls.append)
    monkeypatch.setattr(
        build_metrics,
        "download_job_log",
        lambda _host, _owner, _repo, job_id, output_dir: _write_log(
            output_dir.parent, output_dir.name, job_id,
        ),
    )
    monkeypatch.setattr(
        build_metrics,
        "get_job_info",
        lambda _host, _owner, _repo, job_id: job_info_calls.append(job_id)
        or {"job_name": "Build", "job_active_duration_sec": 42.0},
    )

    builds, job_info = build_metrics.process_job_run(JOB_URL, tmp_path)

    assert builds == []
    assert job_info["job_name"] == "Build"
    assert job_info["job_active_duration_sec"] == 42.0
    assert job_info_calls == ["1"]
    assert auth_calls == ["github.com"]


def test_process_job_run_reuses_cached_log_and_job_info(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A previously downloaded log + written JSON must not trigger any GitHub call."""
    auth_calls: list[str] = []
    job_info_calls: list[str] = []

    monkeypatch.setattr(build_metrics, "ensure_github_auth", auth_calls.append)
    monkeypatch.setattr(
        build_metrics,
        "download_job_log",
        lambda *_args, **_kwargs: pytest.fail("download_job_log must not be called"),
    )
    monkeypatch.setattr(
        build_metrics,
        "get_job_info",
        lambda *_args, **_kwargs: job_info_calls.append("called") or pytest.fail(
            "get_job_info must not be called when <job_id>.json is already cached",
        ),
    )

    _write_log(tmp_path, "100", "1", contents="placeholder log content\n")
    json_file = tmp_path / "100" / "1.json"
    json_file.write_text(
        json.dumps(
            {
                "job_url": JOB_URL,
                "job_name": "Build",
                "job_active_duration_sec": 42.0,
                "build_info": [],
            },
        ),
        encoding="utf-8",
    )

    builds, job_info = build_metrics.process_job_run(JOB_URL, tmp_path)

    assert builds == []
    assert job_info["job_name"] == "Build"
    assert job_info["job_active_duration_sec"] == 42.0
    assert job_info_calls == []
    assert auth_calls == []


def test_load_cached_job_info_returns_none_for_missing_file(tmp_path: Path) -> None:
    """A JSON file that doesn't exist yet is reported as a cache miss, not an error."""
    assert build_metrics.load_cached_job_info(tmp_path / "missing.json") is None


def test_load_cached_job_info_returns_none_for_malformed_file(tmp_path: Path) -> None:
    """A corrupted/partial JSON file falls back to a cache miss instead of raising."""
    json_file = tmp_path / "1.json"
    json_file.write_text("{not valid json", encoding="utf-8")

    assert build_metrics.load_cached_job_info(json_file) is None


def test_load_cached_job_info_returns_none_for_incomplete_file(tmp_path: Path) -> None:
    """A JSON file missing the expected keys falls back to a cache miss."""
    json_file = tmp_path / "1.json"
    json_file.write_text(json.dumps({"job_url": JOB_URL}), encoding="utf-8")

    assert build_metrics.load_cached_job_info(json_file) is None


def test_load_cached_job_info_returns_cached_fields(tmp_path: Path) -> None:
    """A well-formed JSON file yields its job_name/job_active_duration_sec."""
    json_file = tmp_path / "1.json"
    json_file.write_text(
        json.dumps(
            {
                "job_url": JOB_URL,
                "job_name": "Build",
                "job_active_duration_sec": 12.5,
                "build_info": [],
            },
        ),
        encoding="utf-8",
    )

    assert build_metrics.load_cached_job_info(json_file) == {
        "job_name": "Build",
        "job_active_duration_sec": 12.5,
    }
