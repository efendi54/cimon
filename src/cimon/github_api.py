# ruff: noqa: CPY001
"""Common GitHub API helpers used by cimon commands."""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import pyarrow as pa
import pyarrow.parquet as pq
import requests

from cimon.parquet_io import write_table_atomic

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping


logger = logging.getLogger(__name__)

MIN_API_QUOTA = 100
HTTP_NOT_MODIFIED = 304
HTTP_FORBIDDEN = 403
# Upper bound for any single retry sleep (including server-provided
# Retry-After values), so secondary rate limiting can't block for minutes.
MAX_RETRY_DELAY_SECONDS = 30


class QuotaLimitReachedError(RuntimeError):
    """Raised when the remaining GitHub API quota drops to/below a configured limit."""


class _CachedResponse:
    """Stand-in for `requests.Response` reused when the server returns 304."""

    def __init__(self, body: Any, headers: Mapping[str, str]) -> None:  # noqa: ANN401
        self._body = body
        self.headers = headers
        self.status_code = HTTP_NOT_MODIFIED

    def json(self) -> Any:  # noqa: ANN401
        """Return the cached JSON body from the last 200 response."""
        return self._body


def _etag_cache_key(url: str, params: Mapping[str, Any] | None) -> str:
    """Build a stable cache key for a GET request, including its query params."""
    if not params:
        return url
    return f"{url}?{urlencode(sorted(params.items()))}"


def load_etag_cache(path: str) -> dict[str, dict[str, Any]]:
    """Load a persisted ETag cache used for conditional GitHub API requests."""
    cache_path = Path(path)
    if not cache_path.exists():
        return {}

    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_etag_cache(path: str, cache: dict[str, dict[str, Any]]) -> None:
    """Persist the ETag cache to disk."""
    cache_path = Path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache), encoding="utf-8")


def create_session(token: str, *, quota_limit: int | None = None) -> requests.Session:
    """
    Create an authenticated GitHub API session.

    `quota_limit`, if given, is checked by `api_get()` on every response: once
    the remaining quota (`X-RateLimit-Remaining`) drops to/below it, further
    requests raise `QuotaLimitReachedError` instead of being sent.
    """
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    session.quota_limit = quota_limit
    return session


def api_get(
    session: requests.Session,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    retries: int = 5,
    etag_cache: MutableMapping[str, dict[str, Any]] | None = None,
) -> requests.Response:
    """
    Fetch a URL from the GitHub API, retrying transient failures.

    When `etag_cache` is provided, conditional requests (If-None-Match) are
    used so that unchanged responses (304) don't count against the quota.
    """
    cache_key = _etag_cache_key(url, params)
    cached = etag_cache.get(cache_key) if etag_cache is not None else None
    quota_limit = getattr(session, "quota_limit", None)

    for attempt in range(retries):
        # Another thread sharing this session may have already confirmed the
        # quota is gone (possibly via a secondary-rate-limit response that
        # carries no X-RateLimit-Remaining header, see below) -- stop here
        # instead of burning through more requests/retries first.
        if quota_limit is not None and getattr(session, "_quota_exceeded", False):
            msg = f"GitHub API quota limit reached: configured limit {quota_limit}"
            raise QuotaLimitReachedError(msg)

        headers = {"If-None-Match": cached["etag"]} if cached else {}

        try:
            response = session.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(min(2**attempt, MAX_RETRY_DELAY_SECONDS))
            continue

        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining and int(remaining) < MIN_API_QUOTA:
            logger.warning("API quota low: %s", remaining)

        if remaining and quota_limit is not None and int(remaining) <= quota_limit:
            session._quota_exceeded = True
            msg = f"GitHub API quota limit reached: {remaining} remaining <= configured limit {quota_limit}"
            raise QuotaLimitReachedError(msg)

        if response.status_code == HTTP_NOT_MODIFIED and cached is not None:
            logger.debug("ETag cache HIT (304, no quota used): %s", url)
            if etag_cache is not None:
                # re-write so callers pruning "untouched" entries keep this one
                etag_cache[cache_key] = cached
            return _CachedResponse(cached["body"], response.headers)

        # Secondary rate limiting (abuse detection) is commonly a 403 with a
        # Retry-After header but *without* X-RateLimit-Remaining, so the
        # quota check above can silently miss it -- treat it like 429/5xx.
        is_secondary_rate_limit = (
            response.status_code == HTTP_FORBIDDEN and "Retry-After" in response.headers
        )
        if response.status_code in (429, 500, 502, 503, 504) or is_secondary_rate_limit:
            if attempt == retries - 1:
                response.raise_for_status()

            retry_after = response.headers.get("Retry-After")
            delay = min(
                int(retry_after) if retry_after else 2**attempt, MAX_RETRY_DELAY_SECONDS
            )
            logger.warning("Retry %s in %ss", response.status_code, delay)
            time.sleep(delay)
            continue

        response.raise_for_status()

        if etag_cache is not None:
            etag = response.headers.get("ETag")
            if etag:
                reason = "changed" if cached is not None else "not cached yet"
                logger.debug("ETag cache MISS (%s, quota used): %s", reason, url)
                etag_cache[cache_key] = {"etag": etag, "body": response.json()}

        return response

    msg = "GitHub API failed"
    raise RuntimeError(msg)


def print_quota(session: requests.Session, base_url: str) -> None:
    """Log the current GitHub API quota information."""
    data = api_get(session, f"{base_url}/rate_limit", retries=1).json()
    core = data["resources"]["core"]

    reset = dt.datetime.fromtimestamp(core["reset"], tz=dt.timezone.utc)
    logger.info(
        "GitHub API quota:\n"
        "  Limit:     %s\n"
        "  Used:      %s\n"
        "  Remaining: %s\n"
        "  Reset:     %s",
        core["limit"],
        core["used"],
        core["remaining"],
        reset,
    )


RUNNERS_API_PER_PAGE = 100


def list_runners(
    session: requests.Session,
    base_url: str,
    *,
    org: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch all self-hosted runners for an org or repo, paginating as needed.

    Either `org`, or both `owner` and `repo`, must be given. Each runner dict
    has (among others) `name`, `status` ("online"/"offline") and `busy`
    (whether it's currently executing a job).
    """
    if org:
        path = f"{base_url}/orgs/{org}/actions/runners"
    elif owner and repo:
        path = f"{base_url}/repos/{owner}/{repo}/actions/runners"
    else:
        msg = "Either 'org' or both 'owner' and 'repo' must be given."
        raise ValueError(msg)

    runners: list[dict[str, Any]] = []
    page = 1

    while True:
        params = {"per_page": RUNNERS_API_PER_PAGE, "page": page}
        page_runners = api_get(session, path, params=params).json().get("runners", [])
        runners.extend(page_runners)

        if len(page_runners) < RUNNERS_API_PER_PAGE:
            break

        page += 1

    return runners


def print_runner_status(runners: list[dict[str, Any]]) -> None:
    """Log a summary of runner online/offline/busy counts, plus a per-runner listing."""
    online = [r for r in runners if r.get("status") == "online"]
    offline = [r for r in runners if r.get("status") == "offline"]
    busy = [r for r in online if r.get("busy")]

    logger.info(
        "Runner status:\n"
        "  Total:   %s\n"
        "  Online:  %s (busy: %s, idle: %s)\n"
        "  Offline: %s",
        len(runners),
        len(online),
        len(busy),
        len(online) - len(busy),
        len(offline),
    )

    for runner in sorted(
        runners,
        key=lambda r: (
            r.get("status", ""),
            not r.get("busy", False),
            r.get("name", ""),
        ),
    ):
        state = (
            "busy"
            if runner.get("busy")
            else "idle"
            if runner.get("status") == "online"
            else "-"
        )
        logger.info("  %-8s %-4s %s", runner.get("status"), state, runner.get("name"))


RUNNER_STATUS_PARQUET_FILE_NAME = "runner_status.parquet"
RUNNER_STATUS_SNAPSHOTS_DIR_NAME = "runner_status_snapshots"


def save_runner_snapshot(runners: list[dict[str, Any]], snapshots_dir: Path) -> Path:
    """Package a runner-status poll as JSON (`{polled_at, runners}`) and write it to its own file.

    Kept as a lossless, timestamp-named archive under `snapshots_dir` -- one
    file per poll -- separate from the flattened Parquet index, so later
    analyses (e.g. reintroducing a time-series view) aren't limited to
    whatever columns the Parquet cache happens to have today.
    """
    snapshot = {
        "polled_at": dt.datetime.now(tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runners": runners,
    }

    snapshots_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshots_dir / f"{snapshot['polled_at'].replace(':', '')}.json"
    snapshot_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    return snapshot_path


def append_runner_snapshot(
    runners: list[dict[str, Any]], cache_path: Path, snapshots_dir: Path
) -> Path:
    """Persist a runner-status poll as JSON, then fold that same JSON into the Parquet cache.

    Each call adds one row per runner (`polled_at`, `runner_name`, `status`,
    `busy`, `labels`) to `cache_path`, creating it on first use. Kept separate
    from workflows.parquet: unlike workflow-run history, GitHub does not
    retain past runner status, so this can only ever be built up going
    forward from repeated `cimon runners` calls, one snapshot at a time.

    Returns the path of the JSON snapshot file written for this poll.
    """
    snapshot_path = save_runner_snapshot(runners, snapshots_dir)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    new_rows = pa.table(
        {
            "polled_at": [snapshot["polled_at"]] * len(snapshot["runners"]),
            "runner_name": [r.get("name") for r in snapshot["runners"]],
            "status": [r.get("status") for r in snapshot["runners"]],
            "busy": [bool(r.get("busy")) for r in snapshot["runners"]],
            "labels": [
                [label.get("name") for label in (r.get("labels") or [])]
                for r in snapshot["runners"]
            ],
        },
    )

    if cache_path.exists():
        new_rows = pa.concat_tables(
            [pq.read_table(cache_path), new_rows], promote_options="default"
        )

    write_table_atomic(new_rows, cache_path)

    return snapshot_path


def _runner_state(runner: Mapping[str, Any]) -> str:
    """Classify a runner's current state as offline/online-idle/online-busy."""
    if runner.get("status") != "online":
        return "offline"
    return "online (busy)" if runner.get("busy") else "online (idle)"


def render_runner_status_chart(
    runners: list[dict[str, Any]], output_path: Path
) -> None:
    """Plot a treemap of runner labels -> runner names, colored by current status.

    One row per (label, runner) pair, so a runner with several labels shows
    up under each of them. This is a live snapshot (not a time series).
    Requires the `viz` extra (`pandas`, `plotly`), imported lazily so the rest
    of this module keeps working without it installed.
    """
    import pandas as pd  # noqa: PLC0415
    import plotly.express as px  # noqa: PLC0415

    rows = [
        {
            "label": label.get("name", "(no label)"),
            "runner_name": runner.get("name", "(unknown)"),
            "state": _runner_state(runner),
        }
        for runner in runners
        for label in (runner.get("labels") or [{"name": "(no label)"}])
    ]

    if not rows:
        px.scatter(title="No runners found").write_html(output_path)
        return

    figure = px.treemap(
        pd.DataFrame(rows),
        path=["label", "runner_name"],
        color="state",
        color_discrete_map={
            "online (idle)": "#2ca02c",
            "online (busy)": "#ff7f0e",
            "offline": "#d62728",
        },
        title="Runner status by label",
    )
    figure.write_html(output_path)
