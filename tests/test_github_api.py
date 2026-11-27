# ruff: noqa: CPY001
"""Tests for `cimon.github_api.netrc_token` and its precedence over env tokens."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from cimon import github_api
from cimon.workflows import build_metrics

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def netrc_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".netrc"
    path.write_text("machine ghe.example login me password netrc-secret\n")
    path.chmod(0o600)
    monkeypatch.setenv("NETRC", str(path))
    return path


def test_netrc_token_returns_password_for_matching_host(netrc_file: Path) -> None:
    assert github_api.netrc_token("ghe.example") == "netrc-secret"


def test_netrc_token_is_none_for_other_host(netrc_file: Path) -> None:
    assert github_api.netrc_token("other.example") is None


def test_github_cli_environment_uses_netrc_without_changing_parent(
    netrc_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "env-token")
    env = build_metrics.github_cli_environment("ghe.example")

    assert env["GH_ENTERPRISE_TOKEN"] == "netrc-secret"
    assert env["GH_HOST"] == "ghe.example"
    assert build_metrics.os.environ["GH_TOKEN"] == "env-token"


@pytest.mark.parametrize("host", ["github.com", "tenant.ghe.com", "ghe.example"])
def test_ensure_github_auth_uses_tokens_without_login(
    host: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_metrics, "resolve_token", lambda _host, _token: "token")
    monkeypatch.setattr(
        build_metrics.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("A token must not trigger gh auth login"),
    )

    build_metrics.ensure_github_auth(host)


def test_ensure_github_auth_reports_missing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_metrics, "resolve_token", lambda _host, _token: None)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GH_ENTERPRISE_TOKEN", raising=False)
    monkeypatch.setattr(
        build_metrics.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 1})(),
    )

    with pytest.raises(RuntimeError, match=r"No credentials for ghe\.example"):
        build_metrics.ensure_github_auth("ghe.example")


def test_ensure_github_auth_accepts_saved_gh_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_metrics, "resolve_token", lambda _host, _token: None)
    monkeypatch.delenv("GH_ENTERPRISE_TOKEN", raising=False)
    calls = []

    def run_gh(args, **kwargs):
        calls.append((args, kwargs["env"]["GH_HOST"]))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(build_metrics.subprocess, "run", run_gh)
    build_metrics.ensure_github_auth("ghe.example")

    assert calls == [(["gh", "auth", "status", "--hostname", "ghe.example"], "ghe.example")]


@pytest.mark.parametrize(
    ("host", "source", "target"),
    [
        ("github.com", "GITHUB_TOKEN", "GH_TOKEN"),
        ("other.example", "GITHUB_ENTERPRISE_TOKEN", "GH_ENTERPRISE_TOKEN"),
        ("other.example", "GH_TOKEN", "GH_ENTERPRISE_TOKEN"),
    ],
)
def test_github_cli_environment_supports_token_fallbacks(
    netrc_file: Path, monkeypatch: pytest.MonkeyPatch, host: str, source: str, target: str,
) -> None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(source, "env-token")
    assert build_metrics.github_cli_environment(host)[target] == "env-token"


def test_resolve_token_logs_netrc_source(
    netrc_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    github_api._log_credential_source.cache_clear()
    with caplog.at_level("INFO"):
        assert github_api.resolve_token("ghe.example", "env-token") == "netrc-secret"

    assert "from .netrc (overrides environment token)" in caplog.text
    assert "netrc-secret" not in caplog.text


def test_resolve_token_logs_env_source(
    netrc_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    github_api._log_credential_source.cache_clear()
    with caplog.at_level("INFO"):
        assert github_api.resolve_token("other.example", "env-token") == "env-token"

    assert "from environment token (no matching .netrc entry)" in caplog.text
