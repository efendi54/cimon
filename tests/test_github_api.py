# ruff: noqa: CPY001
"""Tests for `cimon.github_api.netrc_token` and its precedence over env tokens."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from cimon import github_api
from cimon.workflows import build_metrics

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def netrc_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a private netrc file containing test credentials."""
    path = tmp_path / ".netrc"
    path.write_text("machine ghe.example login me password netrc-secret\n")
    path.chmod(0o600)
    monkeypatch.setenv("NETRC", str(path))
    return path


@pytest.mark.usefixtures("netrc_file")
def test_netrc_token_returns_password_for_matching_host() -> None:
    """A matching netrc host returns its password."""
    assert github_api.netrc_token("ghe.example") == "netrc-secret"


@pytest.mark.usefixtures("netrc_file")
def test_netrc_token_is_none_for_other_host() -> None:
    """Credentials for another host are not returned."""
    assert github_api.netrc_token("other.example") is None


@pytest.mark.usefixtures("netrc_file")
def test_github_cli_environment_uses_netrc_without_changing_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subprocess environment uses netrc without mutating the parent."""
    monkeypatch.setenv("GH_TOKEN", "env-token")
    env = build_metrics.github_cli_environment("ghe.example")

    assert env["GH_ENTERPRISE_TOKEN"] == "netrc-secret"  # noqa: S105
    assert env["GH_HOST"] == "ghe.example"
    assert build_metrics.os.environ["GH_TOKEN"] == "env-token"  # noqa: S105


@pytest.mark.parametrize("host", ["github.com", "tenant.ghe.com", "ghe.example"])
def test_ensure_github_auth_uses_tokens_without_login(
    host: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing credentials avoid invoking the GitHub CLI."""
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
    """Report a missing token and failed CLI authentication."""
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
    """Accept an existing authenticated GitHub CLI session."""
    monkeypatch.setattr(build_metrics, "resolve_token", lambda _host, _token: None)
    monkeypatch.delenv("GH_ENTERPRISE_TOKEN", raising=False)
    calls = []

    def run_gh(args: list[str], *, env: dict[str, str], **_kwargs: object) -> SimpleNamespace:
        calls.append((args, env["GH_HOST"]))
        return SimpleNamespace(returncode=0)

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
@pytest.mark.usefixtures("netrc_file")
def test_github_cli_environment_supports_token_fallbacks(
    monkeypatch: pytest.MonkeyPatch, host: str, source: str, target: str,
) -> None:
    """Environment tokens work when netrc has no matching host."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(source, "env-token")
    assert build_metrics.github_cli_environment(host)[target] == "env-token"


@pytest.mark.usefixtures("netrc_file")
def test_resolve_token_logs_netrc_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log the netrc source without exposing the credential."""
    github_api._log_credential_source.cache_clear()  # noqa: SLF001
    with caplog.at_level("INFO"):
        assert github_api.resolve_token("ghe.example", "env-token") == "netrc-secret"

    assert "from .netrc (overrides environment token)" in caplog.text
    assert "netrc-secret" not in caplog.text


@pytest.mark.usefixtures("netrc_file")
def test_resolve_token_logs_env_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log when the environment token is used."""
    github_api._log_credential_source.cache_clear()  # noqa: SLF001
    with caplog.at_level("INFO"):
        assert github_api.resolve_token("other.example", "env-token") == "env-token"

    assert "from environment token (no matching .netrc entry)" in caplog.text
