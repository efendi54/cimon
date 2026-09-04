# ruff: noqa: CPY001
"""Render runner occupancy over time per exact runner-label combination, via DuckDB + Plotly.

Requires the `viz` extra (`duckdb`, `pandas`, `plotly`) -- imported lazily by
the registry so the rest of `cimon` keeps working without it installed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import duckdb
import plotly.express as px

if TYPE_CHECKING:
    from pathlib import Path

    import pyarrow as pa

logger = logging.getLogger(__name__)

# Width of the time buckets occupancy is sampled at.
_BUCKET_WIDTH = "15 MINUTE"

_QUERY_TEMPLATE = """
    WITH intervals AS (
        SELECT
            job_runner_name,
            list_sort(job_runner_labels) AS labels,
            cast(job_started_at AS TIMESTAMPTZ) AS started,
            coalesce(cast(job_completed_at AS TIMESTAMPTZ), now()) AS ended
        FROM jobs
        WHERE job_started_at IS NOT NULL
    ),
    bounds AS (
        SELECT min(started) AS lo, max(ended) AS hi FROM intervals
    ),
    buckets AS (
        SELECT unnest(generate_series(lo, hi, INTERVAL {bucket_width})) AS bucket FROM bounds
    )
    SELECT b.bucket, i.labels, count(DISTINCT i.job_runner_name) AS active_runner_count
    FROM buckets b
    JOIN intervals i ON b.bucket >= i.started AND b.bucket < i.ended
    GROUP BY b.bucket, i.labels
    ORDER BY b.bucket, i.labels
"""


def render(table: pa.Table, output_dir: Path) -> None:
    """Write `output_dir/runner-utilization.html`: active runner count per exact label combination, over time."""
    con = duckdb.connect()
    con.register("jobs", table)

    output_path = output_dir / "runner-utilization.html"
    query = _QUERY_TEMPLATE.format(bucket_width=_BUCKET_WIDTH)
    frame = con.execute(query).df()
    if frame.empty:
        px.line(title="No runner activity in range").write_html(output_path)
        logger.info(f"Wrote {output_path}")
        return

    frame["labels"] = frame["labels"].apply(", ".join)

    figure = px.line(
        frame,
        x="bucket",
        y="active_runner_count",
        color="labels",
        title="Runner occupancy over time per runner-label-list",
        labels={"bucket": "time", "active_runner_count": "active runners", "labels": "job_runner_labels"},
        color_discrete_sequence=px.colors.qualitative.Dark24,
        markers=True,
    )
    # Higher contrast against the plot background: bolder lines/markers, light-gray plot area.
    figure.update_traces(line={"width": 3}, marker={"size": 7, "line": {"width": 1, "color": "black"}})
    figure.update_layout(plot_bgcolor="#e5e5e5")
    figure.write_html(output_path)
