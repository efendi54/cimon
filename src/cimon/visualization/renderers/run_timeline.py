# ruff: noqa: CPY001
"""Render the timeline and runtime contribution of the jobs of ONE workflow run (attempt) via Plotly.

Produces a single HTML page with three plots:

1. A timeline with one row per stage (the first ` / `-separated part of the job name) and one bar per job phase:
   orange while the job waits for a runner (job `created_at` -> `started_at`), then coloured by status while it
   runs. Jobs that are still running/queued end at the cache snapshot time.
2. Per stage: wall-clock time with at least one job waiting for a runner / running, and the summed running time
   (effort), each with its share of the run.
3. A heuristic critical path: from the job that ended last, step back to the job that ended right before it was
   created (i.e. became eligible to run), including the time each job waited for a runner.
   The real `needs:` dependencies are not part of the cache, hence "heuristic".

Caches written before `job_created_at` existed have no waiting times (they are 0) until `cimon sync` refetches the run.
Skipped jobs are left out. Only needs the core dependencies (`pandas`, `plotly`).
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pyarrow.compute as pc

from cimon.workflows.run_url import parse_workflow_run_url

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import pyarrow as pa

logger = logging.getLogger(__name__)

DEFAULT_TZ = "Europe/Berlin"
DEFAULT_GAP_SECONDS = 120
WAITING = "waiting for runner"
COLORS = {
    "success": "#2ca02c",
    "failure": "#d62728",
    "in_progress": "#1f77b4",
    "cancelled": "#7f7f7f",
    WAITING: "#ff7f0e",
}
# Jobs shorter than this (flag/setup jobs) are not taken into account as predecessors on the critical path.
MIN_CRITICAL_JOB_MINUTES = 0.5
# Opens the job URL in a new tab when a timeline bar is clicked; `{plot_id}` is substituted by Plotly.
_CLICK_TO_OPEN_JOB_URL = """
document.getElementById('{plot_id}').on('plotly_click', function(data) {
    var url = data.points[0].customdata[0];
    if (url) { window.open(url, '_blank'); }
});
"""


def _to_local(column: pd.Series, tz: str) -> pd.Series:
    """Parse ISO timestamps (UTC) and convert them to naive timestamps in `tz`."""
    return pd.to_datetime(column, utc=True).dt.tz_convert(tz).dt.tz_localize(None)


def prepare_jobs(table: pa.Table, tz: str) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Return the started, non-skipped jobs of the run with their timing columns and the snapshot time.

    - `eligible`: when the job was created, i.e. started waiting for a runner (`job_started_at` if the cache has
      no `job_created_at` yet),
    - `start`/`end`: runner assignment and completion (the snapshot time while still running),
    - `queue_min`/`dur_min`: waiting time for a runner and running time.

    GitHub reports `started_at == created_at` for jobs that are still queued: those are treated as waiting until
    the snapshot with no running time.
    """
    frame = table.to_pandas()
    snapshot = _to_local(frame["cache_updated_at"], tz).max()
    started = _to_local(frame["job_started_at"], tz)
    has_created = "job_created_at" in frame.columns
    frame["eligible"] = _to_local(frame["job_created_at"], tz).fillna(started) if has_created else started
    queued = frame["job_status"] == "queued"
    frame["start"] = started.mask(queued, snapshot)
    frame["end"] = _to_local(frame["job_completed_at"], tz).fillna(snapshot)
    frame["created"] = _to_local(frame["created_at"], tz)
    frame["state"] = frame["job_conclusion"].fillna(frame["job_status"])
    frame["stage"] = frame["job_name"].str.split(" / ").str[0]
    frame["queue_min"] = ((frame["start"] - frame["eligible"]).dt.total_seconds() / 60).clip(lower=0)
    frame["dur_min"] = ((frame["end"] - frame["start"]).dt.total_seconds() / 60).clip(lower=0)
    jobs = frame[(frame["job_conclusion"] != "skipped") & frame["start"].notna()]
    if not has_created or frame["job_created_at"].isna().all():
        logger.warning("No job_created_at in the cache for this run - queue times are unknown (run `cimon sync`).")
    return jobs.copy(), snapshot


def union_minutes(starts: pd.Series, ends: pd.Series) -> float:
    """Minutes covered by at least one of the intervals `starts[i]..ends[i]` (overlaps are counted once)."""
    total, current_end = 0.0, None
    for start, end in sorted(zip(starts, ends, strict=True)):
        if current_end is None or start > current_end:
            total += (end - start).total_seconds()
            current_end = end
        elif end > current_end:
            total += (end - current_end).total_seconds()
            current_end = end
    return total / 60


def stage_contribution(jobs: pd.DataFrame, wall_minutes: float) -> pd.DataFrame:
    """Per stage: job count, summed/wall-clock running time, wall-clock waiting time and their shares of the run."""
    rows = {}
    for stage, group in jobs.groupby("stage"):
        wall = union_minutes(group["start"], group["end"])
        queue_wall = union_minutes(group["eligible"], group["start"])
        rows[stage] = {
            "jobs": len(group),
            "sum_h": group["dur_min"].sum() / 60,
            "wall_h": wall / 60,
            "wall_share_pct": wall / wall_minutes * 100 if wall_minutes else 0.0,
            "queue_sum_h": group["queue_min"].sum() / 60,
            "queue_wall_h": queue_wall / 60,
            "queue_share_pct": queue_wall / wall_minutes * 100 if wall_minutes else 0.0,
            "span_h": (group["end"].max() - group["eligible"].min()).total_seconds() / 3600,
        }
    return pd.DataFrame.from_dict(rows, orient="index").sort_values("span_h")


def critical_path(jobs: pd.DataFrame, gap_seconds: int) -> pd.DataFrame:
    """Heuristic critical path, see the module docstring."""
    path = []
    current = jobs.loc[jobs["end"].idxmax()]
    while current is not None:
        path.append(current)
        candidates = jobs[
            (jobs["end"] <= current["eligible"] + pd.Timedelta(seconds=gap_seconds))
            & (jobs["start"] < current["eligible"])
            & (jobs["dur_min"] >= MIN_CRITICAL_JOB_MINUTES)
        ]
        current = candidates.loc[candidates["end"].idxmax()] if not candidates.empty else None
    return pd.DataFrame(path[::-1])


def _segments(jobs: pd.DataFrame) -> pd.DataFrame:
    """One row per waiting phase (created -> started) and per running phase (started -> completed) of each job."""
    waiting = jobs[jobs["queue_min"] > 0].assign(seg_start=jobs["eligible"], seg_end=jobs["start"], seg=WAITING)
    running = jobs[~((jobs["job_status"] == "queued") | (jobs["start"] == jobs["end"]))].assign(
        seg_start=jobs["start"],
        seg_end=jobs["end"],
        seg=jobs["state"],
    )
    return pd.concat([waiting, running], ignore_index=True)


def _timeline_figure(jobs: pd.DataFrame, title: str, run_url: str | None, snapshot: pd.Timestamp, tz: str) -> go.Figure:
    order = jobs.groupby("stage")["eligible"].min().sort_values(ascending=False).index.tolist()
    link = (
        f'<br><a href="{html.escape(run_url, quote=True)}" target="_blank">{html.escape(run_url)}</a>'
        if run_url
        else ""
    )
    subtitle = f"Time zone {tz}; running jobs end at the cache snapshot ({snapshot:%d.%m. %H:%M})"
    figure = px.timeline(
        _segments(jobs),
        x_start="seg_start",
        x_end="seg_end",
        y="stage",
        color="seg",
        color_discrete_map=COLORS,
        hover_name="job_name",
        custom_data=["job_url"],
        hover_data={
            "queue_min": ":.1f",
            "dur_min": ":.1f",
            "job_conclusion": True,
            "stage": False,
            "seg": False,
        },
        category_orders={"stage": order},
        title=f"{title}{link}<br><sup>{subtitle}</sup>",
    )
    figure.update_traces(opacity=0.75)
    figure.update_layout(barmode="overlay", height=max(450, 24 * len(order) + 150), legend_title="Status")
    if (jobs["job_completed_at"].isna()).any():
        figure.add_shape(
            type="line",
            x0=snapshot,
            x1=snapshot,
            y0=0,
            y1=1,
            yref="paper",
            line={"dash": "dash", "color": "black"},
        )
        figure.add_annotation(x=snapshot, y=1, yref="paper", text="Snapshot", showarrow=False, yanchor="bottom")
    return figure


def _contribution_figure(stages: pd.DataFrame, run_id: str) -> go.Figure:
    figure = go.Figure()
    figure.add_bar(
        y=stages.index,
        x=stages["queue_wall_h"],
        orientation="h",
        name="Time with >= 1 job waiting for a runner (wall-clock)",
        marker_color=COLORS[WAITING],
        customdata=stages[["queue_share_pct", "jobs", "queue_sum_h"]],
        hovertemplate=(
            "%{y}<br>%{x:.2f} h waiting = %{customdata[0]:.0f}% of the run"
            "<br>%{customdata[1]:.0f} jobs, %{customdata[2]:.2f} h waiting summed"
        ),
    )
    figure.add_bar(
        y=stages.index,
        x=stages["wall_h"],
        orientation="h",
        name="Time with >= 1 running job (wall-clock)",
        customdata=stages[["wall_share_pct", "jobs"]],
        hovertemplate="%{y}<br>%{x:.2f} h = %{customdata[0]:.0f}% of the run<br>%{customdata[1]:.0f} jobs",
    )
    figure.add_bar(y=stages.index, x=stages["sum_h"], orientation="h", name="Summed running time (effort)")
    figure.update_layout(
        title=f"Contribution per stage - run {run_id}",
        barmode="group",
        xaxis_title="hours",
        height=max(450, 44 * len(stages) + 150),
    )
    return figure


def _critical_path_figure(path: pd.DataFrame, wall_minutes: float) -> go.Figure:
    waiting_h = path["queue_min"].sum() / 60
    path_h = (path["queue_min"] + path["dur_min"]).sum() / 60
    figure = px.bar(
        _segments(path).assign(seg_min=lambda d: (d["seg_end"] - d["seg_start"]).dt.total_seconds() / 60),
        x="seg_min",
        y="job_name",
        orientation="h",
        color="seg",
        color_discrete_map=COLORS,
        title=(
            f"Critical path (heuristic) - {path_h:.1f} h of {wall_minutes / 60:.1f} h wall-clock, "
            f"{waiting_h:.1f} h of it waiting for a runner"
        ),
        labels={"seg_min": "minutes", "job_name": "", "seg": "Status"},
        category_orders={"job_name": path["job_name"].tolist()},
    )
    figure.update_layout(height=max(300, 40 * len(path) + 150), yaxis_autorange="reversed")
    return figure


def render(
    table: pa.Table,
    output_dir: Path,
    *,
    tz: str = DEFAULT_TZ,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
) -> Path | None:
    """Write `output_dir/run-<run_id>-attempt-<n>.html` for the single run attempt in `table`.

    Returns the written path, or `None` if the run has no started, non-skipped jobs (nothing to plot).
    """
    if table.num_rows == 0:
        return None
    jobs, snapshot = prepare_jobs(table, tz)
    first = table.slice(0, 1).to_pylist()[0]
    run_id, attempt = first["run_id"], first["run_attempt"]
    if jobs.empty:
        logger.warning(f"Run {run_id} has no started, non-skipped jobs - nothing to plot.")
        return None

    # A rerun attempt keeps the original `created_at`, so measure it from its first job instead.
    run_start = jobs["created"].min() if attempt == 1 else jobs["eligible"].min()
    wall_minutes = (jobs["end"].max() - run_start).total_seconds() / 60
    title = (
        f"Run {run_id} attempt {attempt} ({first['event']}, {first['workflow_status']}) - "
        f"{first['workflow_file']} - wall-clock {wall_minutes / 60:.1f} h"
    )

    stages = stage_contribution(jobs, wall_minutes)
    path = critical_path(jobs, gap_seconds)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"run-{run_id}-attempt-{attempt}.html"
    figures = [
        _timeline_figure(jobs, title, first.get("workflow_run_url"), snapshot, tz),
        _contribution_figure(stages, run_id),
        _critical_path_figure(path, wall_minutes),
    ]
    with output_path.open("w", encoding="utf-8") as handle:
        handle.write("<html><head><meta charset='utf-8'></head><body>")
        for index, figure in enumerate(figures):
            handle.write(
                figure.to_html(
                    full_html=False,
                    include_plotlyjs="cdn" if index == 0 else False,
                    post_script=_CLICK_TO_OPEN_JOB_URL if index == 0 else None,
                ),
            )
        handle.write("</body></html>")

    logger.info(f"{title}")
    logger.info(f"Contribution per stage:\n{stages.sort_values('span_h', ascending=False).round(2).to_string()}")
    logger.info(
        "Critical path (heuristic, %.1f h, thereof %.1f h waiting for a runner):\n%s",
        (path["queue_min"] + path["dur_min"]).sum() / 60,
        path["queue_min"].sum() / 60,
        path.round({"queue_min": 1, "dur_min": 1})[
            ["job_name", "eligible", "start", "end", "queue_min", "dur_min"]
        ].to_string(index=False),
    )
    logger.info(f"Wrote {output_path}")
    return output_path


def spec_for_run(params: Mapping[str, str]) -> dict[str, Any]:
    """Select a run URL or the run references from a Parquet selection."""
    if params.get("run-urls-file"):
        from cimon.visualization.renderers.run_averages import selection_urls  # noqa: PLC0415

        if params.get("run-url"):
            msg = "Use either 'run-url' or 'run-urls-file', not both."
            raise ValueError(msg)
        return {"any": [spec_for_run({"run-url": url}) for url in selection_urls(params)]}
    if "individual-html" in params:
        msg = "Parameter 'individual-html' requires 'run-urls-file'."
        raise ValueError(msg)
    url = params.get("run-url")
    if not url:
        msg = "Missing parameter 'run-url' or 'run-urls-file', e.g. -p run-url=https://<host>/<owner>/<repo>/actions/runs/<run_id>"
        raise ValueError(msg)
    ref = parse_workflow_run_url(url)
    conditions = [
        {"column": "host", "op": "eq", "value": ref.host},
        {"column": "owner", "op": "eq", "value": ref.owner},
        {"column": "repo", "op": "eq", "value": ref.repo},
        {"column": "run_id", "op": "eq", "value": ref.run_id},
    ]
    if ref.attempt is not None:
        conditions.append({"column": "run_attempt", "op": "eq", "value": ref.attempt})
    return {"all": conditions}


def render_run(table: pa.Table, output_dir: Path, params: Mapping[str, str]) -> None:
    """Render a single attempt or batch averages, honouring the `tz` and `gap` parameters."""
    try:
        gap_seconds = int(params.get("gap", DEFAULT_GAP_SECONDS))
    except ValueError:
        msg = f"Parameter 'gap' must be an integer number of seconds, got {params['gap']!r}"
        raise ValueError(msg) from None

    if params.get("run-urls-file"):
        from cimon.visualization.renderers.run_averages import render_selection  # noqa: PLC0415

        render_selection(table, output_dir, params, gap_seconds)
        return

    attempt = pc.max(table.column("run_attempt")).as_py()
    attempt_table = table.filter(pc.equal(table.column("run_attempt"), attempt))
    if render(attempt_table, output_dir, tz=params.get("tz", DEFAULT_TZ), gap_seconds=gap_seconds) is None:
        msg = f"Run attempt {attempt} has no started, non-skipped jobs in the cache - nothing to plot."
        raise ValueError(msg)
