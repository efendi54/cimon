# ruff: noqa: CPY001
"""Parsing of GitHub Actions workflow-run URLs into the keys used by the workflows Parquet cache."""

from __future__ import annotations

import re
from dataclasses import dataclass

# e.g. https://host/owner/repo/actions/runs/123, .../runs/123/attempts/2 or .../runs/123/job/456
_RUN_URL = re.compile(
    r"^https?://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/runs/(?P<run_id>\d+)(?:/attempts/(?P<attempt>\d+))?",
)


@dataclass(frozen=True)
class RunRef:
    """Identifies one workflow run (and optionally one attempt of it) in the cache."""

    host: str
    owner: str
    repo: str
    run_id: str
    attempt: int | None = None


def parse_workflow_run_url(url: str) -> RunRef:
    """Parse a workflow-run (or job) URL; raises `ValueError` if it is not one."""
    match = _RUN_URL.match(url.strip())
    if not match:
        msg = f"Not a workflow run URL: {url!r} (expected https://<host>/<owner>/<repo>/actions/runs/<run_id>[/attempts/<n>])"
        raise ValueError(msg)
    attempt = match["attempt"]
    return RunRef(
        host=match["host"],
        owner=match["owner"],
        repo=match["repo"],
        run_id=match["run_id"],
        attempt=int(attempt) if attempt else None,
    )
