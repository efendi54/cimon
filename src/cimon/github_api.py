# ruff: noqa: CPY001
"""Common GitHub API helpers used by cimon commands."""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from collections.abc import Mapping


logger = logging.getLogger(__name__)

MIN_API_QUOTA = 100


def create_session(token: str) -> requests.Session:
    """Create an authenticated GitHub API session."""
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    return session


def api_get(
    session: requests.Session,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    retries: int = 5,
) -> requests.Response:
    """Fetch a URL from the GitHub API, retrying transient failures."""
    for attempt in range(retries):
        try:
            response = session.get(url, params=params, timeout=30)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2**attempt)
            continue

        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining and int(remaining) < MIN_API_QUOTA:
            logger.warning("API quota low: %s", remaining)

        if response.status_code in (429, 500, 502, 503, 504):
            if attempt == retries - 1:
                response.raise_for_status()

            retry_after = response.headers.get("Retry-After")
            delay = int(retry_after) if retry_after else 2**attempt
            logger.warning("Retry %s in %ss", response.status_code, delay)
            time.sleep(delay)
            continue

        response.raise_for_status()
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
