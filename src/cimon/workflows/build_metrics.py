"""Script for retrieving and processing build metrics towards github workflow jobs that initiated bazel builds.

- Dependency: gh (GitHub CLI) must be installed and authenticated with a token that has access to the repository via
  the GH_TOKEN environment variable.

- Usage:
    python build_metrics.py <workflow-job-url> [output-dir]

The script downloads the job log, extracts build metrics, and generates a JSON file and a Markdown summary table.
Additionally it checks for the existence of a 'build-profiles' artifact and downloads it too if available.
Non-empty job logs already present below the output directory are reused;
new downloads are written atomically so interrupted downloads are never cached.

When given a JSON file listing multiple job runs instead of a single URL (see
`process_input`), it additionally aggregates every (target, config) data point
across all those runs into an HTML trend chart of cache-hit rate and build
duration over time.

A Parquet file with a `job_url` string column (e.g. produced by
`cimon query -c job_url`) is accepted the same way as the JSON array. If it
also has a `job_runner_name` column, an additional `runner-cache-health.html`
chart is generated, breaking cache-hit rate down per runner.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

WORKFLOW_URL_RE = re.compile(
    r"^https://(?P<host>[^/]+)/"
    r"(?P<owner>[^/]+)/"
    r"(?P<repo>[^/]+)/"
    r"actions/runs/(?P<run_id>\d+)/job/(?P<job_id>\d+)/?$"
)


BUILD_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T[0-9:.]+Z)\s*"
    r"(?:##\[group\]\s*)?"
    r"(?:\x1B\[[0-?]*[ -/]*[@-~])*"
    r"bazel build\b.*//[^ ]+.*$"
)


INFO_RE = re.compile(r"^(?P<ts>.*) INFO: (?P<body>\d+ process(?:es)?: .*cache hit,.*)$")


CACHE_RE = {
    "action": re.compile(r"(\d+)\s+action cache hit"),
    "remote": re.compile(r"(\d+)\s+remote cache hit"),
    "internal": re.compile(r"(\d+)\s+internal"),
    "sandbox": re.compile(r"(\d+)\s+processwrapper-sandbox"),
    "local": re.compile(r"(\d+)\s+local"),
}


def parse_ts(ts: str) -> datetime:
    ts = ts.replace("Z", "+00:00")

    if "." in ts:
        base, rest = ts.split(".", 1)
        frac, tz = re.split(r"(?=[+-])", rest)
        frac = frac[:6]
        ts = f"{base}.{frac}{tz}"

    return datetime.fromisoformat(ts)


def fmt(sec: float) -> str:
    sec = round(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"


def extract(body: str, key: str) -> int:
    m = CACHE_RE[key].search(body)
    return int(m.group(1)) if m else 0


def ensure_github_auth(host: str) -> None:
    result = subprocess.run(
        ["gh", "auth", "status", "--hostname", host],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    if result.returncode == 0:
        return

    token = os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("Missing GH_TOKEN")

    subprocess.run(
        ["gh", "auth", "login", "--hostname", host, "--with-token"],
        input=token,
        text=True,
        check=True,
    )


def get_job_info(
    host: str,
    owner: str,
    repo: str,
    job_id: str,
) -> dict[str, Any]:
    result = subprocess.run(
        [
            "gh",
            "api",
            "--hostname",
            host,
            f"/repos/{owner}/{repo}/actions/jobs/{job_id}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    job = json.loads(result.stdout)

    started = parse_ts(job["started_at"])
    completed = parse_ts(job["completed_at"])

    return {
        "job_name": job["name"],
        "job_active_duration_sec": (completed - started).total_seconds(),
    }


def download_job_log_for_url(
    workflow_url: str,
    output_root: Path,
) -> tuple[Path, dict[str, str]]:
    """Parse a workflow-job URL and download its log into output_root/<run_id>/<job_id>.log.

    Shared by `process_job_run` (build-metrics) and
    `cimon.workflows.download_logs`, so both reuse the same per-run directory
    layout, cached-log reuse, and GitHub auth handling. Returns the log path
    plus the URL's regex-parsed components (host/owner/repo/run_id/job_id),
    so callers needing more job data don't have to re-parse the URL.
    """
    match = WORKFLOW_URL_RE.match(workflow_url)

    if not match:
        raise ValueError("Invalid workflow URL")

    info = match.groupdict()
    output_dir = output_root / info["run_id"]
    output_dir.mkdir(parents=True, exist_ok=True)

    ensure_github_auth(info["host"])

    log_file = download_job_log(
        info["host"],
        info["owner"],
        info["repo"],
        info["job_id"],
        output_dir,
    )

    return log_file, info


def download_job_log(
    host: str,
    owner: str,
    repo: str,
    job_id: str,
    output_dir: Path,
) -> Path:
    logfile = output_dir / f"{job_id}.log"
    partial_logfile = logfile.with_suffix(".log.part")

    if logfile.is_file() and logfile.stat().st_size > 0:
        print(f"Using cached job log {logfile}")
        return logfile

    print(f"Downloading job log {job_id}")
    partial_logfile.unlink(missing_ok=True)

    try:
        with open(partial_logfile, "wb") as f:
            subprocess.run(
                [
                    "gh",
                    "api",
                    "--hostname",
                    host,
                    f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                ],
                stdout=f,
                check=True,
            )
        partial_logfile.replace(logfile)
    finally:
        partial_logfile.unlink(missing_ok=True)

    return logfile


def download_build_profiles_if_exists(
    host,
    owner,
    repo,
    run_id,
    output_dir: Path,
) -> None:
    print("Checking artifact 'build-profiles'...")

    result = subprocess.run(
        [
            "gh",
            "api",
            "--hostname",
            host,
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    artifacts = json.loads(result.stdout).get("artifacts", [])
    artifact = next(
        (a for a in artifacts if a["name"] == "build-profiles"),
        None,
    )

    if not artifact:
        print("No build-profiles artifact found")
        return

    target = output_dir / "build-profiles"
    target.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            "gh",
            "run",
            "download",
            run_id,
            "-n",
            "build-profiles",
            "--dir",
            str(target),
        ],
        check=True,
    )

    print(f"Downloaded artifact to {target}")


def parse_bazel_build_args(command_line: str) -> tuple[list[str], list[str]]:
    """Extract the target patterns and `--config` values from a 'bazel build ...' command line."""
    args = shlex.split(command_line)

    try:
        build_index = args.index("build")
    except ValueError:
        raise ValueError("No 'bazel build' command found.")

    args = args[build_index + 1 :]

    targets: list[str] = []
    configs: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]

        # --target_pattern_file=<file>
        if arg.startswith("--target_pattern_file="):
            return [arg.split("=", 1)[1]], configs

        # --target_pattern_file <file>
        if arg == "--target_pattern_file":
            if i + 1 >= len(args):
                raise ValueError("--target_pattern_file specified without filename.")
            return [args[i + 1]], configs

        # --config=<name>
        if arg.startswith("--config="):
            configs.append(arg.split("=", 1)[1])

        # --config <name>
        elif arg == "--config":
            if i + 1 >= len(args):
                raise ValueError("--config specified without a value.")
            configs.append(args[i + 1])
            i += 1

        # Bazel target
        elif arg.startswith("//") or arg.startswith("@"):
            targets.append(arg)

        i += 1

    return targets, configs


def parse_log(logfile: Path) -> list[dict[str, Any]]:
    """Parse a job log into one entry per 'bazel build' invocation.

    A single invocation can emit several 'INFO: ... cache hit' lines (e.g. one
    per retry after a transient remote-cache error), so the entry is only
    finalized once the next 'bazel build' line (or EOF) is reached, using the
    last INFO line seen as the actual end timestamp and cache-hit numbers.
    """
    builds: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def finalize(entry: dict[str, Any]) -> None:
        if "info_ts" not in entry:
            raise RuntimeError("Missing INFO for build:\n" + entry["build_line"])
        builds.append(entry)

    with open(logfile, encoding="utf-8", errors="replace") as f:
        for line in f:
            bm = BUILD_RE.search(line)

            if bm:
                if current is not None:
                    finalize(current)

                current = {
                    "start_ts": parse_ts(bm.group("ts")).isoformat(),
                    "build_line": line.strip(),
                }
                current["targets"], current["configs"] = parse_bazel_build_args(line)
                continue

            im = INFO_RE.search(line)

            if im and current:
                body = im.group("body")

                start = datetime.fromisoformat(current["start_ts"])
                info_ts = parse_ts(im.group("ts")).isoformat()
                info = datetime.fromisoformat(info_ts)

                # Overwrite (not append) so a later retry's output replaces
                # an earlier one instead of prematurely closing the entry.
                current.update(
                    {
                        "duration_sec": (info - start).total_seconds(),
                        "info_ts": info_ts,
                        "info_line": line.strip(),
                        "cache": {
                            "action": extract(body, "action"),
                            "remote": extract(body, "remote"),
                            "internal": extract(body, "internal"),
                            "sandbox": extract(body, "sandbox"),
                            "local": extract(body, "local"),
                        },
                    }
                )

    if current is not None:
        finalize(current)

    return builds


def compute_cache_rates(cache: dict[str, int]) -> dict[str, float | int | None]:
    """Derive cache-hit ratios from one build's raw Bazel process-strategy counts.

    `hit_rate` is an overall, build-level view (`action`+`remote` hits over
    all processes, including `internal`). It is *not* a runner signal: the
    shared `remote` cache is reachable by every runner alike, and `internal`
    bookkeeping processes are never cache candidates in the first place, so
    both dilute it for cross-runner comparisons.

    `local_cache_effectiveness` isolates the runner's own on-disk cache: of
    the processes that were *not* satisfied by the shared remote cache (so
    only this runner's own cache or a real execution could have resolved
    them), the share this runner's cache already had.

    `executed_share` is the share of those same non-remote, non-internal
    processes that had to be genuinely executed on this runner -- the direct
    cost of a cold local cache. Both are `None` (not a percentage of 0) when
    there was nothing to measure, e.g. a build entirely resolved remotely.
    """
    executed = cache["local"] + cache["sandbox"]
    hits = cache["action"] + cache["remote"]
    total = hits + cache["internal"] + executed
    hit_rate = hits / total * 100.0 if total else 0.0

    runner_total = cache["action"] + executed
    local_cache_effectiveness = (
        cache["action"] / runner_total * 100.0 if runner_total else None
    )

    non_internal_total = hits + executed
    executed_share = (
        executed / non_internal_total * 100.0 if non_internal_total else None
    )

    return {
        "hits": hits,
        "total": total,
        "hit_rate": hit_rate,
        "local_cache_effectiveness": local_cache_effectiveness,
        "executed_share": executed_share,
    }


def _fmt_rate(rate: float | None) -> str:
    """Format an optional percentage for the per-build Markdown table."""
    return f"{rate:.1f}%" if rate is not None else "n/a"


def generate_md_table_entries(
    builds: list[dict[str, Any]],
    job_info: dict[str, Any],
) -> str:
    md = [
        f"# {job_info['job_name']}\n",
        f"**URL:** <{job_info['job_url']}>\n",
        f"**Duration:** {fmt(job_info['job_active_duration_sec'])}\n",
        "",
        "Target | Config | Duration | Cache Hit Rate | Local Cache Eff. | Executed | Action | Remote | Internal | Local | Sandbox | Hits | Total | Build-Logs |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for b in builds:
        duration = b["duration_sec"]
        c = b["cache"]
        config = "+".join(b["configs"]) if b["configs"] else "-"

        rates = compute_cache_rates(c)

        cell = f"<code>{b['build_line']}</code><br><code>{b['info_line']}</code>"

        # Bazel reports cache stats per invocation, not per target, so every
        # target of a multi-target build shares the same duration/rate/cache.
        for target in b["targets"]:
            md.append(
                f"| {target} "
                f"| {config} "
                f"| {fmt(duration)} "
                f"| **{rates['hit_rate']:.1f}%** "
                f"| {_fmt_rate(rates['local_cache_effectiveness'])} "
                f"| {_fmt_rate(rates['executed_share'])} "
                f"| {c['action']} "
                f"| {c['remote']} "
                f"| {c['internal']} "
                f"| {c['local']} "
                f"| {c['sandbox']} "
                f"| {rates['hits']} "
                f"| {rates['total']} "
                f"| {cell} |"
            )

    return "\n".join(md)


def flatten_metric_rows(
    builds: list[dict[str, Any]],
    job_info: dict[str, Any],
    workflow_url: str,
    runner_name: str | None = None,
) -> list[dict[str, Any]]:
    """Flatten parsed builds into one row per (target, config) data point, for trend analysis."""
    rows: list[dict[str, Any]] = []

    for b in builds:
        rates = compute_cache_rates(b["cache"])
        config = "+".join(b["configs"]) if b["configs"] else "-"

        for target in b["targets"]:
            rows.append(
                {
                    "job_run_url": workflow_url,
                    "job_name": job_info["job_name"],
                    "runner_name": runner_name,
                    "start_ts": b["start_ts"],
                    "target": target,
                    "config": config,
                    "duration_sec": b["duration_sec"],
                    "hit_rate": rates["hit_rate"],
                    "local_cache_effectiveness": rates["local_cache_effectiveness"],
                    "executed_share": rates["executed_share"],
                    "hits": rates["hits"],
                    "total": rates["total"],
                },
            )

    return rows


def render_hit_rate_trend(rows: list[dict[str, Any]], output_path: Path) -> None:
    """Plot cache-hit rate and build duration over time, one line per Bazel config."""
    from itertools import cycle  # noqa: PLC0415

    import pandas as pd  # noqa: PLC0415
    import plotly.express as px  # noqa: PLC0415
    import plotly.graph_objects as go  # noqa: PLC0415
    from plotly.subplots import make_subplots  # noqa: PLC0415

    if not rows:
        go.Figure(layout={"title": "No build data in range"}).write_html(output_path)
        print(f"Wrote {output_path}")
        return

    frame = pd.DataFrame(rows)
    frame["start_ts"] = pd.to_datetime(frame["start_ts"])

    # A single `bazel build` invocation covers one config but potentially many
    # targets; flatten_metric_rows() emits one row per target sharing the same
    # duration/cache counts. Collapse those back into one point per
    # invocation here, otherwise every target would incorrectly show up as
    # its own (duplicate, overlapping) trend series instead of by config.
    frame = (
        frame.groupby(["job_run_url", "start_ts", "config"], as_index=False)
        .agg(
            targets=("target", lambda s: ", ".join(sorted(set(s)))),
            hit_rate=("hit_rate", "first"),
            duration_sec=("duration_sec", "first"),
            runner_name=("runner_name", "first"),
        )
        .sort_values("start_ts")
    )

    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        subplot_titles=("Cache hit rate (%)", "Build duration (min)"),
    )

    colors = dict(
        zip(sorted(frame["config"].unique()), cycle(px.colors.qualitative.Dark24))
    )

    for config, group in frame.groupby("config"):
        customdata = (
            group[["job_run_url", "runner_name", "targets"]]
            .fillna("(unknown)")
            .to_numpy()
        )
        figure.add_trace(
            go.Scatter(
                x=group["start_ts"],
                y=group["hit_rate"],
                mode="lines+markers",
                name=config,
                legendgroup=config,
                marker={"color": colors[config]},
                customdata=customdata,
                hovertemplate="%{y:.1f}%<br>runner: %{customdata[1]}<br>targets: %{customdata[2]}<br>%{customdata[0]}<extra>%{fullData.name}</extra>",
            ),
            row=1,
            col=1,
        )
        figure.add_trace(
            go.Scatter(
                x=group["start_ts"],
                y=group["duration_sec"] / 60.0,
                mode="lines+markers",
                name=config,
                legendgroup=config,
                showlegend=False,
                marker={"color": colors[config]},
                customdata=customdata,
                hovertemplate="%{y:.1f} min<br>runner: %{customdata[1]}<br>targets: %{customdata[2]}<br>%{customdata[0]}<extra>%{fullData.name}</extra>",
            ),
            row=2,
            col=1,
        )

    figure.update_layout(title="Bazel cache-hit rate & duration over time")
    figure.update_yaxes(title_text="hit rate (%)", row=1, col=1)
    figure.update_yaxes(title_text="duration (min)", row=2, col=1)
    figure.write_html(
        output_path,
        post_script=(
            "document.querySelectorAll('.plotly-graph-div').forEach(function(div) {"
            "div.on('plotly_click', function(data) {"
            "var url = data.points[0].customdata[0];"
            "if (url) { window.open(url, '_blank'); }"
            "});"
            "});"
        ),
    )
    print(f"Wrote {output_path}")


def render_runner_cache_health(rows: list[dict[str, Any]], output_path: Path) -> None:
    """Plot per-runner distributions to spot runners with a cold/ineffective local cache.

    Three box plots side by side (no time axis -- an overall distribution):

    - `local_cache_effectiveness` -- of the processes *not* already covered by
      the shared remote cache, the share this runner's own on-disk cache
      already had. This is the actual runner-quality signal.
    - `hit_rate` -- the overall (action+remote) rate, shown only for context;
      it is dominated by the shared remote cache and is roughly
      runner-independent, so a runner standing out only in the first plot
      points at that runner's own cache rather than a remote-cache-wide issue.
    - `executed_share` -- the share of non-remote, non-internal processes this
      runner actually had to execute (the direct cost of a cold local cache).

    Below that, a runner x time-bucket heatmap of `local_cache_effectiveness`
    adds the time dimension the box plots lack. Unlike a line-per-runner time
    series (see `render_runner_cache_trend`), this scales to environments with
    many (often short-lived) runners, since each runner is just one row.
    """
    from itertools import cycle  # noqa: PLC0415

    import pandas as pd  # noqa: PLC0415
    import plotly.express as px  # noqa: PLC0415
    import plotly.graph_objects as go  # noqa: PLC0415
    from plotly.subplots import make_subplots  # noqa: PLC0415

    if not rows:
        go.Figure(layout={"title": "No build data in range"}).write_html(output_path)
        print(f"Wrote {output_path}")
        return

    frame = pd.DataFrame(rows)
    frame["runner_name"] = frame["runner_name"].fillna("(unknown)")
    frame["start_ts"] = pd.to_datetime(frame["start_ts"])
    frame["local_cache_effectiveness"] = pd.to_numeric(
        frame["local_cache_effectiveness"],
        errors="coerce",
    )

    runners = sorted(frame["runner_name"].unique())
    colors = dict(zip(runners, cycle(px.colors.qualitative.Dark24)))

    figure = make_subplots(
        rows=2,
        cols=3,
        specs=[[{}, {}, {}], [{"type": "heatmap", "colspan": 3}, None, None]],
        row_heights=[0.4, 0.6],
        vertical_spacing=0.1,
        subplot_titles=(
            "Local cache effectiveness (%)",
            "Overall cache hit rate (%)",
            "Executed (%)",
            "Local cache effectiveness over time, by runner (%)",
        ),
    )

    for runner, group in frame.groupby("runner_name"):
        for col, column in enumerate(
            ("local_cache_effectiveness", "hit_rate", "executed_share"),
            start=1,
        ):
            figure.add_trace(
                go.Box(
                    y=group[column],
                    name=runner,
                    legendgroup=runner,
                    # The x-axis already labels each box by runner name, so a
                    # legend would only duplicate that -- and with many
                    # runners it grows tall enough to cover the heatmap's
                    # colorbar below.
                    showlegend=False,
                    marker={"color": colors[runner]},
                    boxpoints="all",
                    jitter=0.4,
                    pointpos=0,
                ),
                row=1,
                col=col,
            )

    # Bucket size adapts to the covered time range, so the heatmap stays
    # readable whether it spans a few hours or several weeks of builds.
    span = frame["start_ts"].max() - frame["start_ts"].min()
    if span <= pd.Timedelta(hours=6):
        freq = "15min"
    elif span <= pd.Timedelta(days=2):
        freq = "1h"
    elif span <= pd.Timedelta(days=14):
        freq = "1D"
    else:
        freq = "7D"

    frame["time_bucket"] = frame["start_ts"].dt.floor(freq)
    pivot = frame.pivot_table(
        index="runner_name",
        columns="time_bucket",
        values="local_cache_effectiveness",
        aggfunc="mean",
    ).reindex(runners)

    figure.add_trace(
        go.Heatmap(
            z=pivot.to_numpy(),
            x=pivot.columns,
            y=pivot.index,
            colorscale="RdYlGn",
            zmin=0,
            zmax=100,
            colorbar={"title": "eff. (%)", "len": 0.5, "y": 0.2},
            hovertemplate="runner: %{y}<br>%{x}<br>local cache eff.: %{z:.1f}%<extra></extra>",
        ),
        row=2,
        col=1,
    )

    figure.update_layout(
        title="Bazel cache hit rate by runner",
        height=max(600, 22 * len(runners) + 300),
    )
    figure.update_yaxes(title_text="rate (%)", range=[0, 100], row=1)
    figure.update_xaxes(title_text="build start time", row=2, col=1)
    figure.write_html(output_path)
    print(f"Wrote {output_path}")


def render_runner_cache_trend(rows: list[dict[str, Any]], output_path: Path) -> None:
    """Plot local cache effectiveness, overall hit rate, and executed share over time, one line per runner."""
    from itertools import cycle  # noqa: PLC0415

    import pandas as pd  # noqa: PLC0415
    import plotly.express as px  # noqa: PLC0415
    import plotly.graph_objects as go  # noqa: PLC0415
    from plotly.subplots import make_subplots  # noqa: PLC0415

    if not rows:
        go.Figure(layout={"title": "No build data in range"}).write_html(output_path)
        print(f"Wrote {output_path}")
        return

    frame = pd.DataFrame(rows)
    frame["runner_name"] = frame["runner_name"].fillna("(unknown)")
    frame["start_ts"] = pd.to_datetime(frame["start_ts"])

    # A single `bazel build` invocation covers one config but potentially many
    # targets; flatten_metric_rows() emits one row per target sharing the
    # same metric values. Collapse those back into one point per invocation
    # here, otherwise only one arbitrary target would survive deduplication
    # instead of all the targets actually built together showing in the hover.
    frame = (
        frame.groupby(
            ["runner_name", "start_ts", "job_run_url", "config"],
            as_index=False,
        )
        .agg(
            targets=("target", lambda s: ", ".join(sorted(set(s)))),
            local_cache_effectiveness=("local_cache_effectiveness", "first"),
            hit_rate=("hit_rate", "first"),
            executed_share=("executed_share", "first"),
        )
        .sort_values("start_ts")
    )

    runners = sorted(frame["runner_name"].unique())
    colors = dict(zip(runners, cycle(px.colors.qualitative.Dark24)))
    metrics = (
        ("local_cache_effectiveness", "Local cache effectiveness (%)"),
        ("hit_rate", "Overall cache hit rate (%)"),
        ("executed_share", "Executed (%)"),
    )
    figure = make_subplots(
        rows=len(metrics),
        cols=1,
        shared_xaxes=True,
        subplot_titles=[title for _, title in metrics],
    )

    for runner, group in frame.groupby("runner_name"):
        customdata = group[["job_run_url", "targets", "config"]].to_numpy()
        hovertemplate = (
            "%{y:.1f}%<br>%{x}<br>targets: %{customdata[1]}"
            "<br>config: %{customdata[2]}<br>%{customdata[0]}"
            "<extra>%{fullData.name}</extra>"
        )
        for row, (column, _) in enumerate(metrics, start=1):
            figure.add_trace(
                go.Scatter(
                    x=group["start_ts"],
                    y=group[column],
                    mode="lines+markers",
                    name=runner,
                    legendgroup=runner,
                    showlegend=row == 1,
                    marker={"color": colors[runner]},
                    customdata=customdata,
                    hovertemplate=hovertemplate,
                ),
                row=row,
                col=1,
            )

    figure.update_layout(
        title="Bazel cache hit rate by runner over time",
        hovermode="x unified",
    )
    for row in range(1, len(metrics) + 1):
        figure.update_yaxes(title_text="rate (%)", range=[0, 100], row=row, col=1)
    figure.update_xaxes(title_text="build start time", row=len(metrics), col=1)
    figure.write_html(output_path)
    print(f"Wrote {output_path}")


def process_job_run(
    workflow_url: str,
    output_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Download, parse and write per-job JSON/MD outputs for one job run.

    Returns its parsed `builds` and `job_info`, so callers processing several
    job runs (see `process_input`) can aggregate them without re-parsing.
    """
    log_file, info = download_job_log_for_url(workflow_url, output_root)
    output_dir = log_file.parent

    builds = parse_log(log_file)

    job_info = get_job_info(
        info["host"],
        info["owner"],
        info["repo"],
        info["job_id"],
    )
    job_info["job_url"] = workflow_url

    build_metrics = {
        "job_url": workflow_url,
        "job_name": job_info["job_name"],
        "job_active_duration_sec": job_info["job_active_duration_sec"],
        "build_info": builds,
    }

    json_file = output_dir / f"{info['job_id']}.json"

    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(
            build_metrics,
            f,
            indent=2,
        )

    md_file = output_dir / f"{info['job_id']}.md"

    with open(md_file, "w", encoding="utf-8") as f:
        f.write(generate_md_table_entries(builds, job_info))

    # download_build_profiles_if_exists(
    #     info["host"],
    #     info["owner"],
    #     info["repo"],
    #     info["run_id"],
    #     output_dir,
    # )

    print("\n====================")
    print("OUTPUT")
    print("====================")
    print(f"Log : {log_file}")
    print(f"JSON: {json_file}")
    print(f"MD  : {md_file}")
    print(f"DIR : {output_dir}")

    return builds, job_info


def main(
    workflow_url: str,
    output_root: Path = Path("/tmp"),
) -> int:
    process_job_run(workflow_url, output_root)
    return 0


def _job_entries_from_json(input_path: Path) -> list[dict[str, Any]]:
    """Read job run URLs (and runner_name, if present) from a JSON array of `{"job": {...}}` entries."""
    with open(input_path, encoding="utf-8") as f:
        entries = json.load(f)

    if not isinstance(entries, list):
        raise ValueError("JSON must contain an array.")

    jobs: list[dict[str, Any]] = []

    for info in entries:
        if (
            not isinstance(info, dict)
            or not isinstance(info.get("job"), dict)
            or "job_url" not in info["job"]
        ):
            continue

        job = info["job"]
        url = job["job_url"]
        if not isinstance(url, str):
            raise ValueError(f"Job run URL {url!r} is not a string.")

        jobs.append({"job_url": url, "runner_name": job.get("runner_name")})

    return jobs


def _job_entries_from_parquet(
    input_path: Path,
    url_column: str = "job_url",
    runner_column: str = "job_runner_name",
) -> list[dict[str, Any]]:
    """Read job run URLs (and runner_name, if present) from a Parquet file (e.g. from `cimon query`)."""
    import pyarrow.parquet as pq  # noqa: PLC0415

    has_runner_column = runner_column in pq.ParquetFile(input_path).schema_arrow.names
    columns = [url_column, runner_column] if has_runner_column else [url_column]

    table = pq.read_table(input_path, columns=columns)
    urls = table.column(url_column).to_pylist()
    runner_names = (
        table.column(runner_column).to_pylist()
        if has_runner_column
        else [None] * len(urls)
    )

    return [
        {"job_url": url, "runner_name": runner_name}
        for url, runner_name in zip(urls, runner_names)
        if url
    ]


def resolve_job_entries(input_arg: str) -> list[dict[str, Any]]:
    """Resolve INPUT (a job URL, JSON array file, or Parquet file) into a list of job entries.

    Used by `process_input` (build-metrics), which also needs runner_name for
    its runner-comparison charts. A bare job URL resolves to a single entry
    with no runner_name; see `_job_entries_from_json`/`_job_entries_from_parquet`
    for the file formats. `cimon.workflows.download_logs` only needs job URLs,
    so it uses the simpler `resolve_job_urls` instead.
    """
    if WORKFLOW_URL_RE.match(input_arg):
        return [{"job_url": input_arg, "runner_name": None}]

    input_path = Path(input_arg)

    if not input_path.is_file():
        raise ValueError(
            f"'{input_arg}' is neither a workflow URL, a JSON file, nor a Parquet file."
        )

    return (
        _job_entries_from_parquet(input_path)
        if input_path.suffix == ".parquet"
        else _job_entries_from_json(input_path)
    )


def _job_urls_from_json(input_path: Path) -> list[str]:
    """Read job run URLs from a top-level JSON array of URL strings."""
    with open(input_path, encoding="utf-8") as f:
        entries = json.load(f)

    if not isinstance(entries, list):
        raise ValueError("JSON must contain an array.")

    urls: list[str] = []

    for url in entries:
        if not isinstance(url, str):
            raise ValueError(f"Job run URL {url!r} is not a string.")
        urls.append(url)

    return urls


def _job_urls_from_parquet(input_path: Path, url_column: str = "job_url") -> list[str]:
    """Read job run URLs from a Parquet file's job_url column (e.g. from `cimon query`)."""
    import pyarrow.parquet as pq  # noqa: PLC0415

    table = pq.read_table(input_path, columns=[url_column])
    return [url for url in table.column(url_column).to_pylist() if url]


def resolve_job_urls(input_arg: str) -> list[str]:
    """Resolve INPUT (a job URL, JSON array of URL strings, or Parquet file) into a list of job URLs.

    Used by `cimon.workflows.download_logs`, which -- unlike `build-metrics` --
    doesn't need runner_name, so its JSON form is a plain array of URL strings
    instead of `resolve_job_entries`' `{"job": {...}}` entries.
    """
    if WORKFLOW_URL_RE.match(input_arg):
        return [input_arg]

    input_path = Path(input_arg)

    if not input_path.is_file():
        raise ValueError(
            f"'{input_arg}' is neither a workflow URL, a JSON file, nor a Parquet file."
        )

    return (
        _job_urls_from_parquet(input_path)
        if input_path.suffix == ".parquet"
        else _job_urls_from_json(input_path)
    )


def process_input(
    input_arg: str,
    output_root: Path,
) -> int:
    if WORKFLOW_URL_RE.match(input_arg):
        return main(input_arg, output_root)

    all_jobs = resolve_job_entries(input_arg)

    rows: list[dict[str, Any]] = []
    total = len(all_jobs)

    if total == 0:
        print("No job run URLs found in input.")
        return 0

    for index, job in enumerate(all_jobs, start=1):
        workflow_url = job["job_url"]
        runner_name = job.get("runner_name")

        print("=" * 80)
        print(f"Processing {workflow_url}")
        print(f"[{index}/{total}] {index / total * 100.0:.1f}% complete")
        print("=" * 80)

        builds, job_info = process_job_run(workflow_url, output_root)
        rows.extend(flatten_metric_rows(builds, job_info, workflow_url, runner_name))

    trend_file = output_root / "cache-hit-rate-trend.html"
    render_hit_rate_trend(rows, trend_file)
    print(f"\nTrend: {trend_file}")

    runner_health_file = output_root / "runner-cache-health.html"
    render_runner_cache_health(rows, runner_health_file)
    print(f"Runner cache health: {runner_health_file}")

    runner_trend_file = output_root / "runner-cache-trend.html"
    render_runner_cache_trend(rows, runner_trend_file)
    print(f"Runner cache trend: {runner_trend_file}")

    return 0


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print(
            f"Usage: {sys.argv[0]} "
            "<job-run-url | json-file | parquet-file> [output-dir]"
        )
        sys.exit(1)

    input_arg = sys.argv[1]

    out = Path(sys.argv[2]) if len(sys.argv) == 3 else Path("/tmp")

    sys.exit(process_input(input_arg, out))
