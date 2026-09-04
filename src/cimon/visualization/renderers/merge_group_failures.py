# ruff: noqa: CPY001
"""Render failing merge_group jobs, one HTML page per workflow, via DuckDB + Plotly.

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
        job_url,
        run_id,
        head_branch,
        cast(job_started_at AS TIMESTAMPTZ) AS started_at
    FROM jobs
    WHERE job_started_at IS NOT NULL
    ORDER BY workflow_file, job_name, started_at
"""

# Opens the job URL in a new tab when a data point is clicked; `{plot_id}` is
# substituted by Plotly with the actual chart div id at write_html() time.
_CLICK_TO_OPEN_JOB_URL = """
document.getElementById('{plot_id}').on('plotly_click', function(data) {
    var url = data.points[0].customdata;
    if (url) { window.open(url, '_blank'); }
});
"""


def render(table: pa.Table, output_dir: Path) -> None:
    """Write one `output_dir/merge-group-failures/<workflow_file>.html` per workflow."""
    con = duckdb.connect()
    con.register("jobs", table)
    frame = con.execute(_QUERY).df()

    pages_dir = output_dir / "merge-group-failures"
    pages_dir.mkdir(parents=True, exist_ok=True)

    if frame.empty:
        output_path = pages_dir / "none.html"
        px.scatter(title="No failing merge_group jobs in range").write_html(output_path)
        logger.info(f"Wrote {output_path}")
        return

    for workflow_file, group in frame.groupby("workflow_file"):
        workflow_name = group["workflow_name"].iloc[0]
        # `.name` guards against a workflow_file value containing path separators.
        output_path = pages_dir / f"{Path(str(workflow_file)).name}.html"
        _render_workflow_page(group, workflow_name, output_path)
        logger.info(f"Wrote {output_path}")


def _render_workflow_page(group: pd.DataFrame, workflow_name: str, output_path: Path) -> None:
    """Render one workflow's failure-count bar chart + failure-timeline scatter."""
    # Most-failing job first, both to rank it and to keep both subplots' job
    # order (and thus their shared color mapping) aligned.
    counts = group["job_name"].value_counts()
    job_order = list(counts.index)
    colors = dict(zip(job_order, cycle(px.colors.qualitative.Dark24)))

    figure = make_subplots(
        rows=2,
        cols=1,
        row_heights=[0.35, 0.65],
        vertical_spacing=0.15,
        subplot_titles=("Failures per job", "Failures over time"),
    )

    figure.add_trace(
        go.Bar(
            y=job_order,
            x=[counts[name] for name in job_order],
            orientation="h",
            marker={"color": [colors[name] for name in job_order]},
            showlegend=False,
        ),
        row=1,
        col=1,
    )

    for job_name, sub in group.groupby("job_name"):
        figure.add_trace(
            go.Scatter(
                x=sub["started_at"],
                y=sub["job_name"],
                mode="markers",
                name=job_name,
                marker={"size": 9, "color": colors[job_name], "line": {"width": 1, "color": "black"}},
                customdata=sub["job_url"],
                hovertemplate="%{x}<br>%{customdata}<extra>%{fullData.name}</extra>",
            ),
            row=2,
            col=1,
        )

    # Keep both subplots' job rows in the same (most-failing-first) order.
    figure.update_yaxes(categoryorder="array", categoryarray=list(reversed(job_order)))
    figure.update_layout(
        title=f"merge_group failures -- {workflow_name}",
        plot_bgcolor="#e5e5e5",
        height=350 + 25 * len(job_order),
    )
    figure.update_xaxes(title_text="failure count", row=1)
    figure.update_xaxes(title_text="time", row=2)
    figure.write_html(output_path, post_script=_CLICK_TO_OPEN_JOB_URL)
