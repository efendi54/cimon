# ruff: noqa: CPY001
"""Tests for parsing workflow-run URLs."""

from __future__ import annotations

import pytest

from cimon.workflows.run_url import RunRef, parse_workflow_run_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://cariad.ghe.com/CAS/app-adas-src/actions/runs/382965642",
            RunRef("cariad.ghe.com", "CAS", "app-adas-src", "382965642"),
        ),
        (
            "https://cariad.ghe.com/CAS/app-adas-src/actions/runs/382965642/attempts/2",
            RunRef("cariad.ghe.com", "CAS", "app-adas-src", "382965642", 2),
        ),
        (
            " https://github.com/acme/app/actions/runs/7/job/99?pr=1 ",
            RunRef("github.com", "acme", "app", "7"),
        ),
    ],
)
def test_parse_workflow_run_url(url: str, expected: RunRef) -> None:
    """Run and job URLs are reduced to host, owner, repo, run id and optional attempt."""
    assert parse_workflow_run_url(url) == expected


@pytest.mark.parametrize("url", ["", "https://github.com/acme/app/pull/1", "382965642"])
def test_parse_workflow_run_url_rejects_other_urls(url: str) -> None:
    """Anything that is not a workflow-run URL raises a ValueError."""
    with pytest.raises(ValueError, match="Not a workflow run URL"):
        parse_workflow_run_url(url)
