# ruff: noqa: CPY001
"""Render job duration over time, one HTML page per workflow, via DuckDB + Plotly.

Requires the `viz` extra (`duckdb`, `pandas`, `plotly`) -- imported lazily by
the registry so the rest of `cimon` keeps working without it installed.
"""

from __future__ import annotations

import logging
from itertools import cycle
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa

logger = logging.getLogger(__name__)

_QUERY = """
    SELECT
        workflow_name,
        workflow_file,
        job_name,
        job_status,
        job_url,
        cast(created_at AS TIMESTAMPTZ) AS created_at,
        job_active_duration_sec
    FROM jobs
    WHERE job_active_duration_sec IS NOT NULL
    ORDER BY workflow_file, created_at
"""

# Opens the job URL in a new tab when a data point is clicked; `{plot_id}` is
# substituted by Plotly with the actual chart div id at write_html() time.
_CLICK_TO_OPEN_JOB_URL = """
document.getElementById('{plot_id}').on('plotly_click', function(data) {
    var url = data.points[0].customdata;
    if (url) { window.open(url, '_blank'); }
});
"""


def _add_duration_traces(
    figure: go.Figure,
    subset: pd.DataFrame,
    col: int,
    colors: dict[str, str],
    legend_shown: set[str],
) -> None:
    """Add one marker trace per job name in `subset` to `figure`'s subplot at column `col`."""
    for job_name, sub in subset.groupby("job_name"):
        figure.add_trace(
            go.Scatter(
                x=sub["created_at"],
                y=sub["job_active_duration_sec"],
                mode="markers",
                name=job_name,
                legendgroup=job_name,
                showlegend=job_name not in legend_shown,
                marker={"size": 9, "color": colors[job_name], "line": {"width": 1, "color": "black"}},
                customdata=sub["job_url"],
                hovertemplate="%{y} s<br>%{customdata}<extra>%{fullData.name}</extra>",
            ),
            row=1,
            col=col,
        )
        legend_shown.add(job_name)


def render(table: pa.Table, output_dir: Path) -> None:
    """Write one `output_dir/job-durations/<workflow_file>.html` per workflow, job duration over time."""
    con = duckdb.connect()
    con.register("jobs", table)
    frame = con.execute(_QUERY).df()

    pages_dir = output_dir / "job-durations"
    pages_dir.mkdir(parents=True, exist_ok=True)

    if frame.empty:
        output_path = pages_dir / "none.html"
        px.scatter(title="No completed, successful or in-progress jobs in range").write_html(output_path)
        logger.info(f"Wrote {output_path}")
        return

    for workflow_file, group in frame.groupby("workflow_file"):
        workflow_name = group["workflow_name"].iloc[0]
        # `.name` guards against a workflow_file value containing path separators.
        output_path = pages_dir / f"{Path(str(workflow_file)).name}.html"

        in_progress = group[group["job_status"] == "in_progress"]
        completed = group[group["job_status"] == "completed"]

        # Same color per job across both subplots; legend entry shown only once.
        colors = dict(zip(sorted(group["job_name"].unique()), cycle(px.colors.qualitative.Dark24)))
        legend_shown: set[str] = set()

        figure = make_subplots(
            rows=1,
            cols=2,
            subplot_titles=("Still running (in_progress)", "Completed (success)"),
        )
        _add_duration_traces(figure, in_progress, 1, colors, legend_shown)
        _add_duration_traces(figure, completed, 2, colors, legend_shown)

        # Higher contrast against the plot background.
        figure.update_layout(title=f"Job duration -- {workflow_name}", plot_bgcolor="#e5e5e5")
        figure.update_xaxes(title_text="time")
        figure.update_yaxes(title_text="active duration (s)", col=1)
        figure.write_html(output_path, post_script=_CLICK_TO_OPEN_JOB_URL)
        logger.info(f"Wrote {output_path}")
