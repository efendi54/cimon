# ruff: noqa: CPY001
"""Render job duration over time, one HTML page per workflow, via DuckDB + Plotly.

Requires the `viz` extra (`duckdb`, `pandas`, `plotly`) -- imported lazily by
the registry so the rest of `cimon` keeps working without it installed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import plotly.express as px

if TYPE_CHECKING:
    import pyarrow as pa

logger = logging.getLogger(__name__)

_QUERY = """
    SELECT
        workflow_name,
        workflow_file,
        job_name,
        cast(created_at AS TIMESTAMPTZ) AS created_at,
        job_duration_sec
    FROM jobs
    WHERE job_duration_sec IS NOT NULL
    ORDER BY workflow_file, created_at
"""


def render(table: pa.Table, output_dir: Path) -> None:
    """Write one `output_dir/job-durations/<workflow_file>.html` per workflow, job duration over time."""
    con = duckdb.connect()
    con.register("jobs", table)
    frame = con.execute(_QUERY).df()

    pages_dir = output_dir / "job-durations"
    pages_dir.mkdir(parents=True, exist_ok=True)

    if frame.empty:
        output_path = pages_dir / "none.html"
        px.scatter(title="No completed, successful jobs in range").write_html(output_path)
        logger.info(f"Wrote {output_path}")
        return

    for workflow_file, group in frame.groupby("workflow_file"):
        workflow_name = group["workflow_name"].iloc[0]
        # `.name` guards against a workflow_file value containing path separators.
        output_path = pages_dir / f"{Path(str(workflow_file)).name}.html"

        figure = px.scatter(
            group,
            x="created_at",
            y="job_duration_sec",
            color="job_name",
            title=f"Job duration (completed, successful jobs) -- {workflow_name}",
            labels={"created_at": "time", "job_duration_sec": "duration (s)", "job_name": "job"},
        )
        figure.write_html(output_path)
        logger.info(f"Wrote {output_path}")
