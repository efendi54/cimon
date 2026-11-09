"""Select cached run attempts and plot average job waiting and execution times."""

from __future__ import annotations

import hashlib
import html
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import plotly.graph_objects as go
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from cimon.visualization.renderers.run_timeline import (
    COLORS,
    DEFAULT_TZ,
    WAITING,
    prepare_jobs,
    render,
    spec_for_run,
)
from cimon.workflows.query_spec import build_expression
from cimon.workflows.run_url import parse_workflow_run_url

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)
WORKFLOW_KEYS = ["host", "owner", "repo", "workflow_file"]
RUN_KEYS = ["host", "owner", "repo", "run_id", "run_attempt"]
METRICS = ["queue_min", "dur_min", "total_min"]


def selection_urls(params: Mapping[str, str]) -> list[str]:
    """Read and validate the distinct run references from a Parquet selection."""
    if params.get("individual-html", "true") not in {"true", "false"}:
        msg = "Parameter 'individual-html' must be true or false."
        raise ValueError(msg)
    path = Path(params["run-urls-file"])
    if not path.is_file():
        msg = f"Run selection file does not exist: {path}"
        raise ValueError(msg)
    try:
        schema = pq.read_schema(path)
        required = {
            *RUN_KEYS,
            "workflow_file",
            "workflow_run_url",
            "event",
            "workflow_status",
            "created_at",
            "cache_updated_at",
            "job_name",
            "job_url",
            "job_status",
            "job_conclusion",
            "job_started_at",
            "job_completed_at",
        }
        if "workflow_run_url" not in schema.names:
            msg = f"Cannot read workflow_run_url from {path}: column is missing."
            raise ValueError(msg)
        missing = sorted(required - set(schema.names))
        if missing:
            msg = f"Parquet job data in {path} is missing required column(s): {', '.join(missing)}."
            raise ValueError(msg)
        column = pq.read_table(path, columns=["workflow_run_url"]).column("workflow_run_url")
    except (pa.ArrowException, OSError) as exc:
        msg = f"Cannot read workflow_run_url from {path}: {exc}"
        raise ValueError(msg) from exc
    urls = column.to_pylist()
    if not urls:
        msg = f"No workflow run URLs in {path}."
        raise ValueError(msg)
    if any(not isinstance(url, str) or not url.strip() for url in urls):
        msg = f"Every workflow_run_url in {path} must be a non-empty string."
        raise ValueError(msg)
    # Normalize references, not just strings (base and job links can identify the same run).
    distinct = {}
    for url in urls:
        distinct.setdefault(parse_workflow_run_url(url), url)
    return list(distinct.values())


def select_attempts(table: pa.Table, urls: list[str]) -> pa.Table:
    """Resolve every reference and retain each selected run attempt exactly once."""
    selected = {}
    for url in urls:
        subset = table.filter(build_expression(spec_for_run({"run-url": url})))
        if subset.num_rows == 0:
            msg = f"Run selection {url} has no matching job data in the supplied Parquet file."
            raise ValueError(msg)
        attempt = pc.call_function("max", [subset.column("run_attempt")]).as_py()
        if attempt is None:
            msg = f"No run_attempt in the cache for {url}."
            raise ValueError(msg)
        subset = subset.filter(pc.call_function("equal", [subset.column("run_attempt"), pa.scalar(attempt)]))
        first = subset.slice(0, 1).to_pylist()[0]
        selected[tuple(first[key] for key in RUN_KEYS)] = subset
    return pa.concat_tables(list(selected.values()))


def job_averages(table: pa.Table) -> pd.DataFrame:
    """Mean, median and valid sample count per workflow, full job name and outcome.

    Only completed, non-skipped jobs count. Invalid/missing timestamps are
    excluded per metric; UTC avoids daylight-saving distortions of durations.
    """
    jobs, _snapshot = prepare_jobs(table, "UTC")
    completed = (
        jobs["job_status"].eq("completed") & jobs["job_conclusion"].notna() & jobs["job_conclusion"].ne("skipped")
    )
    execution_valid = jobs["job_started_at"].notna() & jobs["job_completed_at"].notna() & jobs["end"].ge(jobs["start"])
    has_created = (
        jobs["job_created_at"].notna() if "job_created_at" in jobs else pd.Series(data=False, index=jobs.index)
    )
    waiting_valid = has_created & jobs["job_started_at"].notna() & jobs["start"].ge(jobs["eligible"])
    jobs["dur_min"] = jobs["dur_min"].where(execution_valid)
    jobs["queue_min"] = jobs["queue_min"].where(waiting_valid)
    jobs["total_min"] = (jobs["dur_min"] + jobs["queue_min"]).where(execution_valid & waiting_valid)
    jobs = jobs[completed].copy()
    missing = jobs[METRICS].isna().sum()
    if missing.any():
        logger.warning("Excluded missing/invalid duration measurements: %s", missing.to_dict())
    return jobs.groupby([*WORKFLOW_KEYS, "job_name", "job_conclusion"])[METRICS].agg(
        ["mean", "median", "count"],
    )


def average_figure(summary: pd.DataFrame, title: str, run_count: int) -> go.Figure:
    """Stack phase means, with a separate marker for the mean paired total."""
    summary = summary.sort_values(("total_min", "mean"), na_position="first")
    labels = [f"{name} [{conclusion}]" for name, conclusion in summary.index]
    figure = go.Figure()
    for metric, label, color in [
        ("queue_min", WAITING, COLORS[WAITING]),
        ("dur_min", "execution", "#1f77b4"),
    ]:
        figure.add_trace(
            go.Bar(
                x=summary[(metric, "mean")],
                y=labels,
                name=label,
                orientation="h",
                marker_color=color,
                customdata=summary[[(metric, "median"), (metric, "count")]],
                hovertemplate=(
                    "%{y}<br>Mean: %{x:.2f} min<br>Median: %{customdata[0]:.2f} min"
                    "<br>Valid samples: %{customdata[1]}<extra>%{fullData.name}</extra>"
                ),
            ),
        )
    figure.add_trace(
        go.Scatter(
            x=summary[("total_min", "mean")],
            y=labels,
            name="total (paired samples)",
            mode="markers",
            marker={"symbol": "diamond", "color": "#222", "size": 9},
            customdata=summary[[("total_min", "median"), ("total_min", "count")]],
            hovertemplate=(
                "%{y}<br>Mean total: %{x:.2f} min<br>Median: %{customdata[0]:.2f} min"
                "<br>Valid samples: %{customdata[1]}<extra>%{fullData.name}</extra>"
            ),
        ),
    )
    figure.update_layout(
        title=(
            f"Average job phases -- {html.escape(title)} ({run_count} run attempts)"
            "<br><sup>Completed jobs only; outcomes separated. Each phase uses its own valid samples."
            "<br>Total uses paired samples; missing timings are not zero. Hover for median and sample count.</sup>"
        ),
        barmode="stack",
        height=max(450, 30 * len(summary) + 200),
        xaxis_title="Duration (minutes)",
        yaxis_title="Job [outcome]",
    )
    return figure


def render_selection(
    table: pa.Table,
    output_dir: Path,
    params: Mapping[str, str],
    gap_seconds: int,
) -> None:
    """Write one average page per workflow and optionally the existing per-run pages."""
    selected = select_attempts(table, selection_urls(params))
    if params.get("individual-html", "true") == "true":
        prepare_jobs(selected, params.get("tz", DEFAULT_TZ))
    summary = job_averages(selected)
    if summary.empty or not summary.xs("count", axis=1, level=1).to_numpy().any():
        msg = "Selected runs have no completed, non-skipped jobs with valid duration measurements."
        raise ValueError(msg)
    frame = selected.to_pandas()
    output_dir.mkdir(parents=True, exist_ok=True)
    summarized_workflows = set(summary.index.droplevel(["job_name", "job_conclusion"]))
    for identity, workflow_runs in frame.groupby(WORKFLOW_KEYS):
        label = "/".join(identity)
        suffix = hashlib.sha256(label.encode()).hexdigest()[:12]
        workflow_dir = output_dir / f"{Path(identity[-1]).name}-{suffix}"
        if identity not in summarized_workflows:
            logger.warning("No completed-job duration measurements for workflow %s", identity)
        else:
            workflow_summary = pd.DataFrame(summary.xs(identity, level=WORKFLOW_KEYS))
            if not workflow_summary.xs("count", axis=1, level=1).to_numpy().any():
                logger.warning("No valid completed-job duration measurements for workflow %s", identity)
            else:
                run_count = len(workflow_runs[RUN_KEYS].drop_duplicates())
                workflow_dir.mkdir(parents=True, exist_ok=True)
                output_path = workflow_dir / "averages.html"
                average_figure(workflow_summary, label, run_count).write_html(output_path)
                logger.info("Wrote %s", output_path)
        if params.get("individual-html", "true") == "true":
            for run_identity, group in workflow_runs.groupby(RUN_KEYS):
                if (
                    render(
                        pa.Table.from_pandas(group, preserve_index=False),
                        workflow_dir / "runs",
                        tz=params.get("tz", DEFAULT_TZ),
                        gap_seconds=gap_seconds,
                    )
                    is None
                ):
                    logger.warning("No plottable jobs for selected run attempt %s", run_identity)
