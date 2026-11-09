# ruff: noqa: ANN001, D100, D103, PLR2004

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
import requests
from click.testing import CliRunner

from cimon.__main__ import main
from cimon.github_api import QuotaLimitReachedError
from cimon.workflows import synch

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def sync_api(mocker) -> dict[str, Any]:
    mocker.patch("cimon.workflows.synch.resolve_token", return_value="test-token")
    session = mocker.patch("cimon.workflows.synch.create_session").return_value
    response = mocker.Mock()
    response.json.return_value = {"resources": {"core": {"limit": 5000, "remaining": 4000}}}
    quota = mocker.patch("cimon.workflows.synch.api_get", return_value=response)
    workflows = {
        name: {"id": index, "name": name, "path": f".github/workflows/{name}"}
        for index, name in enumerate(
            ["cas_nightly_and_release.yml", "pr.yml", "other.yml"],
            start=1,
        )
    }
    runs = {
        workflow["id"]: {
            "id": workflow["id"] * 100,
            "run_number": 1,
            "run_attempt": 1,
            "html_url": f"https://example.test/actions/runs/{workflow['id'] * 100}",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-11-09T08:00:00Z",
        }
        for workflow in workflows.values()
    }

    def workflow_info(_session, _url, _owner, _repo, name, *, etag_cache) -> dict:
        etag_cache[name] = {"etag": name, "body": workflows[name]}
        return workflows[name]

    info = mocker.patch("cimon.workflows.synch.workflow_info", side_effect=workflow_info)
    fetch = mocker.patch(
        "cimon.workflows.synch.iter_runs",
        side_effect=lambda *args, **_kwargs: iter([runs[args[4]]]),
    )
    active = mocker.patch("cimon.workflows.synch.scan_active_runs", return_value=[])
    jobs = mocker.patch(
        "cimon.workflows.synch.process_job_run",
        side_effect=lambda *args, **_kwargs: [
            synch.create_job_cache_entry(
                {
                    "id": args[4]["id"] + 1,
                    "name": "build",
                    "html_url": "https://example.test/jobs/1",
                    "run_attempt": 1,
                    "created_at": "2026-11-09T08:00:00Z",
                    "started_at": "2026-11-09T08:01:00Z",
                    "completed_at": "2026-11-09T08:02:00Z",
                    "status": "completed",
                    "conclusion": "success",
                },
            ),
        ],
    )
    return {
        "info": info,
        "fetch": fetch,
        "active": active,
        "jobs": jobs,
        "quota": quota,
        "session": session,
        "runs": runs,
    }


def sync_args(tmp_path, workflows) -> list[str]:
    args = [
        "--log-file",
        str(tmp_path / "cimon.log"),
        "sync",
        "--host",
        "example.test",
        "--cache-dir",
        str(tmp_path),
    ]
    for workflow in workflows:
        args.extend(["--workflow", workflow])
    return args


@pytest.mark.parametrize(
    "workflows",
    [
        ["cas_nightly_and_release.yml"],
        ["cas_nightly_and_release.yml", "pr.yml"],
    ],
)
def test_sync_cli_persists_every_workflow_and_reuses_cached_jobs(
    tmp_path,
    sync_api,
    workflows,
) -> None:
    args = sync_args(tmp_path, workflows)
    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0, (result.output, result.exception)
    assert [call.args[4] for call in sync_api["info"].call_args_list] == workflows
    assert [call.args[4] for call in sync_api["active"].call_args_list] == list(
        range(1, len(workflows) + 1),
    )
    rows = synch.load_parquet(str(tmp_path / "workflows.parquet"))
    assert len(rows) == len(workflows)
    assert {(row["workflow_file"], row["run_id"], row["job_id"]) for row in rows} == {
        (f".github/workflows/{name}", str(index * 100), str(index * 100 + 1))
        for index, name in enumerate(workflows, start=1)
    }
    assert set(json.loads((tmp_path / "etag_cache.json").read_text())) == set(workflows)
    snapshot = json.loads((tmp_path / "response.json").read_text())
    assert snapshot["workflow"]["file"] == f".github/workflows/{workflows[-1]}"
    assert len(snapshot["runs"]) == 1
    sync_api["quota"].assert_called_once()
    assert sync_api["session"].quota_limit == 500

    sync_api["jobs"].reset_mock()
    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0, (result.output, result.exception)
    sync_api["jobs"].assert_not_called()
    assert len(synch.load_parquet(str(tmp_path / "workflows.parquet"))) == len(workflows)


def test_sync_cli_saves_partial_progress_and_stops_at_shared_quota(
    tmp_path,
    sync_api,
) -> None:
    error = QuotaLimitReachedError("quota exhausted")

    def fetch_runs(
        _session,
        _url,
        _owner,
        _repo,
        workflow_id,
        _from,
        _to,
        _pages,
        *,
        etag_cache,
    ) -> Iterator[dict]:
        assert etag_cache
        yield sync_api["runs"][workflow_id]
        if workflow_id == 2:
            raise error

    sync_api["fetch"].side_effect = fetch_runs
    result = CliRunner().invoke(
        main,
        sync_args(tmp_path, ["cas_nightly_and_release.yml", "pr.yml", "other.yml"]),
    )

    assert result.exit_code != 0
    assert [call.args[4] for call in sync_api["info"].call_args_list] == [
        "cas_nightly_and_release.yml",
        "pr.yml",
    ]
    rows = synch.load_parquet(str(tmp_path / "workflows.parquet"))
    assert {row["run_id"] for row in rows} == {"100", "200"}
    assert {row["job_id"] for row in rows if row["run_id"] == "100"} == {"101"}
    assert {row["job_id"] for row in rows if row["run_id"] == "200"} == {None}
    assert set(json.loads((tmp_path / "etag_cache.json").read_text())) == {
        "cas_nightly_and_release.yml",
        "pr.yml",
    }


def test_sync_cli_reports_full_missing_workflow_without_losing_previous_cache(
    tmp_path,
    sync_api,
) -> None:
    response = requests.Response()
    response.status_code = 404
    info = sync_api["info"]
    success = info.side_effect

    def workflow_info(session, url, owner, repo, name, *, etag_cache) -> dict:
        if name == "missing.yml":
            raise requests.HTTPError(response=response)
        return success(session, url, owner, repo, name, etag_cache=etag_cache)

    info.side_effect = workflow_info
    result = CliRunner().invoke(
        main,
        sync_args(tmp_path, ["cas_nightly_and_release.yml", "missing.yml"]),
    )

    assert result.exit_code == 2
    assert "workflow missing.yml" in result.output
    rows = synch.load_parquet(str(tmp_path / "workflows.parquet"))
    assert {row["run_id"] for row in rows} == {"100"}


def test_sync_cli_requires_workflow_before_accessing_api(tmp_path, sync_api) -> None:
    result = CliRunner().invoke(main, sync_args(tmp_path, []))

    assert result.exit_code == 2
    assert "No workflow specified" in result.output
    sync_api["quota"].assert_not_called()
