"""Script for retrieving and processing build metrics towards github workflow jobs that initiated bazel builds.

- Dependency: gh (GitHub CLI) must be installed and authenticated with a token that has access to the repository via
  the GH_TOKEN environment variable.

- Usage:
    python build_metrics.py <workflow-job-url> [output-dir]

The script downloads the job log, extracts build metrics, and generates a JSON file and a Markdown summary table.
Additionally it checks for the existence of a 'build-profiles' artifact and downloads it too if available.

When given a JSON file listing multiple job runs instead of a single URL (see
`process_input`), it additionally aggregates every (target, config) data point
across all those runs into an HTML trend chart of cache-hit rate and build
duration over time. Requires the `viz` extra (`pandas`, `plotly`) for that
chart; the rest of the script works without it.

A Parquet file with a `job_url` string column (e.g. produced by
`cimon query -c job_url`) is accepted the same way as the JSON array. If it
also has a `job_runner_name` column, an additional `runner-cache-health.html`
chart is generated, breaking cache-hit rate down per runner.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import shlex
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


INFO_RE = re.compile(
    r"^(?P<ts>.*) INFO: (?P<body>\d+ process(?:es)?: .*cache hit,.*)$"
)


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


def download_job_log(
    host: str,
    owner: str,
    repo: str,
    job_id: str,
    output_dir: Path,
) -> Path:
    logfile = output_dir / f"{job_id}.log"

    print(f"Downloading job log {job_id}")

    with open(logfile, "w", encoding="utf-8") as f:
        subprocess.run(
            [
                "gh",
                "api",
                "--hostname",
                host,
                f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                "--allow-escape-sequences"
            ],
            stdout=f,
            check=True,
        )

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

    args = args[build_index + 1:]

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
            raise RuntimeError(
                "Missing INFO for build:\n" + entry["build_line"]
            )
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
                        "duration_sec": (
                            info - start
                        ).total_seconds(),
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


def generate_md_table_entries(
    builds: list[dict[str, Any]],
    job_info: dict[str, Any],
) -> str:
    md = [
        f"# {job_info['job_name']}\n",
        f'**URL:** <{job_info["job_url"]}>\n',
        f'**Duration:** {fmt(job_info["job_active_duration_sec"])}\n',
        "",
        "Target | Config | Duration | Cache Hit Rate | Action | Remote | Internal | Local | Sandbox | Hits | Total | Build-Logs |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for b in builds:
        duration = b["duration_sec"]
        c = b["cache"]
        config = "+".join(b["configs"]) if b["configs"] else "-"

        total = (
            c["action"]
            + c["remote"]
            + c["internal"]
            + c["sandbox"]
            + c["local"]
        )

        hits = c["action"] + c["remote"]

        rate = (
            hits / total * 100.0
            if total
            else 0.0
        )

        cell = (
            f"<code>{b['build_line']}</code>"
            f"<br><code>{b['info_line']}</code>"
        )

        # Bazel reports cache stats per invocation, not per target, so every
        # target of a multi-target build shares the same duration/rate/cache.
        for target in b["targets"]:
            md.append(
                f"| {target} "
                f"| {config} "
                f"| {fmt(duration)} "
                f"| **{rate:.1f}%** "
                f"| {c['action']} "
                f"| {c['remote']} "
                f"| {c['internal']} "
                f"| {c['local']} "
                f"| {c['sandbox']} "
                f"| {hits} "
                f"| {total} "
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
        c = b["cache"]
        total = c["action"] + c["remote"] + c["internal"] + c["sandbox"] + c["local"]
        hits = c["action"] + c["remote"]
        rate = hits / total * 100.0 if total else 0.0
        # Only the `action` (local, on-disk) cache is tied to the runner's own
        # disk state; `remote` is a shared cache, so this rate is what
        # actually reflects a given runner's local-cache health.
        action_hit_rate = c["action"] / total * 100.0 if total else 0.0
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
                    "hit_rate": rate,
                    "action_hit_rate": action_hit_rate,
                    "hits": hits,
                    "total": total,
                },
            )

    return rows


def render_hit_rate_trend(rows: list[dict[str, Any]], output_path: Path) -> None:
    """Plot cache-hit rate and build duration over time, one line per (target, config).

    Requires the `viz` extra (`pandas`, `plotly`), imported lazily so the rest
    of this script keeps working without it installed.
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
    frame["start_ts"] = pd.to_datetime(frame["start_ts"])
    frame["series"] = frame["target"] + " [" + frame["config"] + "]"
    frame = frame.sort_values("start_ts")

    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        subplot_titles=("Cache hit rate (%)", "Build duration (min)"),
    )

    colors = dict(zip(sorted(frame["series"].unique()), cycle(px.colors.qualitative.Dark24)))

    for series, group in frame.groupby("series"):
        customdata = group[["job_run_url", "runner_name"]].fillna("(unknown)").to_numpy()
        figure.add_trace(
            go.Scatter(
                x=group["start_ts"],
                y=group["hit_rate"],
                mode="lines+markers",
                name=series,
                legendgroup=series,
                marker={"color": colors[series]},
                customdata=customdata,
                hovertemplate="%{y:.1f}%<br>runner: %{customdata[1]}<br>%{customdata[0]}<extra>%{fullData.name}</extra>",
            ),
            row=1,
            col=1,
        )
        figure.add_trace(
            go.Scatter(
                x=group["start_ts"],
                y=group["duration_sec"] / 60.0,
                mode="lines+markers",
                name=series,
                legendgroup=series,
                showlegend=False,
                marker={"color": colors[series]},
                customdata=customdata,
                hovertemplate="%{y:.1f} min<br>runner: %{customdata[1]}<br>%{customdata[0]}<extra>%{fullData.name}</extra>",
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
    """Plot cache-hit rate distributions per runner, to spot runners with a cold/ineffective local cache.

    Two box plots side by side: the `action` (local, on-disk) cache-hit rate,
    which is the component actually tied to a specific runner's disk state,
    and the overall (action+remote) rate for comparison -- the latter should
    be roughly runner-independent since it comes from the shared remote
    cache, so a runner standing out only in the first plot points at that
    runner's local cache rather than a remote-cache-wide issue.

    Requires the `viz` extra (`pandas`, `plotly`), imported lazily so the rest
    of this script keeps working without it installed.
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

    runners = sorted(frame["runner_name"].unique())
    colors = dict(zip(runners, cycle(px.colors.qualitative.Dark24)))

    figure = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=("Action (local) cache hit rate (%)", "Overall cache hit rate (%)"),
    )

    for runner, group in frame.groupby("runner_name"):
        figure.add_trace(
            go.Box(
                y=group["action_hit_rate"],
                name=runner,
                legendgroup=runner,
                marker={"color": colors[runner]},
                boxpoints="all",
                jitter=0.4,
                pointpos=0,
            ),
            row=1,
            col=1,
        )
        figure.add_trace(
            go.Box(
                y=group["hit_rate"],
                name=runner,
                legendgroup=runner,
                showlegend=False,
                marker={"color": colors[runner]},
                boxpoints="all",
                jitter=0.4,
                pointpos=0,
            ),
            row=1,
            col=2,
        )

    figure.update_layout(title="Bazel cache hit rate by runner")
    figure.update_yaxes(title_text="hit rate (%)", col=1)
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
    match = WORKFLOW_URL_RE.match(workflow_url)

    if not match:
        raise ValueError("Invalid workflow URL")

    info = match.groupdict()

    output_dir = output_root / info["run_id"]

    if output_dir.exists():
        shutil.rmtree(output_dir)

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    ensure_github_auth(info["host"])

    log_file = download_job_log(
        info["host"],
        info["owner"],
        info["repo"],
        info["job_id"],
        output_dir,
    )

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
        f.write(
            generate_md_table_entries(builds, job_info)
            )

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
        if not isinstance(info, dict) or not isinstance(info.get("job"), dict) or "html_url" not in info["job"]:
            continue

        job = info["job"]
        url = job["html_url"]
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
    runner_names = table.column(runner_column).to_pylist() if has_runner_column else [None] * len(urls)

    return [
        {"job_url": url, "runner_name": runner_name}
        for url, runner_name in zip(urls, runner_names)
        if url
    ]


def process_input(
    input_arg: str,
    output_root: Path,
) -> int:
    if WORKFLOW_URL_RE.match(input_arg):
        return main(input_arg, output_root)

    input_path = Path(input_arg)

    if not input_path.is_file():
        raise ValueError(
            f"'{input_arg}' is neither a workflow URL, a JSON file, nor a Parquet file."
        )

    all_jobs = (
        _job_entries_from_parquet(input_path)
        if input_path.suffix == ".parquet"
        else _job_entries_from_json(input_path)
    )

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

    return 0

if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print(
            f"Usage: {sys.argv[0]} "
            "<job-run-url | json-file | parquet-file> [output-dir]"
        )
        sys.exit(1)

    input_arg = sys.argv[1]

    out = (
        Path(sys.argv[2])
        if len(sys.argv) == 3
        else Path("/tmp")
    )

    sys.exit(process_input(input_arg, out))
