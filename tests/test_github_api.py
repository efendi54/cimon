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


def test_ensure_github_auth_exports_netrc_token_over_env(
    netrc_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "env-token")
    monkeypatch.setattr(
        build_metrics.subprocess,
        "run",
        lambda *_a, **_k: type("R", (), {"returncode": 0})(),
    )

    build_metrics.ensure_github_auth("ghe.example")

    assert build_metrics.os.environ["GH_TOKEN"] == "netrc-secret"


def test_resolve_token_logs_netrc_source(
    netrc_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    github_api._log_credential_source.cache_clear()
    with caplog.at_level("INFO"):
        assert github_api.resolve_token("ghe.example", "env-token") == "netrc-secret"

    assert "from .netrc (overrides GH_TOKEN/GITHUB_TOKEN)" in caplog.text
    assert "netrc-secret" not in caplog.text


def test_resolve_token_logs_env_source(
    netrc_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    github_api._log_credential_source.cache_clear()
    with caplog.at_level("INFO"):
        assert github_api.resolve_token("other.example", "env-token") == "env-token"

    assert "from GH_TOKEN/GITHUB_TOKEN (no matching .netrc entry)" in caplog.text
