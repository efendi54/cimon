"""Script for retrieving and processing build metrics towards github workflow jobs that initiated bazel builds.

- Dependency: gh (GitHub CLI) must be installed and authenticated with a token that has access to the repository via
  the GH_TOKEN environment variable.

- Usage:
    python build_metrics.py <workflow-job-url> [output-dir]

The script downloads the job log, extracts build metrics, and generates a JSON file and a Markdown summary table.
Additionally it checks for the existence of a 'build-profiles' artifact and downloads it too if available.
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
        "job-name": job["name"],
        "duration_sec": (completed - started).total_seconds(),
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


def get_bazel_targets_argument(command_line: str) -> str:
    args = shlex.split(command_line)

    try:
        build_index = args.index("build")
    except ValueError:
        raise ValueError("No 'bazel build' command found.")

    args = args[build_index + 1:]

    targets = []

    i = 0
    while i < len(args):
        arg = args[i]

        # --target_pattern_file=<file>
        if arg.startswith("--target_pattern_file="):
            return arg.split("=", 1)[1]

        # --target_pattern_file <file>
        if arg == "--target_pattern_file":
            if i + 1 >= len(args):
                raise ValueError("--target_pattern_file specified without filename.")
            return args[i + 1]

        # Bazel target
        if arg.startswith("//") or arg.startswith("@"):
            targets.append(arg)

        i += 1

    return " ".join(targets)


def parse_log(logfile: Path) -> list[dict[str, Any]]:
    builds: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    with open(logfile, encoding="utf-8", errors="replace") as f:
        for line in f:
            bm = BUILD_RE.search(line)

            if bm:
                if current is not None:
                    raise RuntimeError(
                        "Missing INFO for previous build:\n"
                        + current["build_line"]
                    )

                current = {
                    "start_ts": parse_ts(bm.group("ts")).isoformat(),
                    "build_line": line.strip(),
                }
                targets = get_bazel_targets_argument(line)
                current["targets"] = targets
                continue

            im = INFO_RE.search(line)

            if im and current:
                body = im.group("body")

                start = datetime.fromisoformat(current["start_ts"])
                info_ts = parse_ts(im.group("ts")).isoformat()
                info = datetime.fromisoformat(info_ts)

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

                builds.append(current)
                current = None

    if current is not None:
        raise RuntimeError(
            "Missing INFO for last build:\n"
            + current["build_line"]
        )

    return builds


def generate_md_table_entries(
    builds: list[dict[str, Any]],
    job_info: dict[str, Any],
) -> str:
    md = [
        f"# {job_info['job-name']}\n",
        f'**URL:** <{job_info["job-html-url"]}>\n',
        f'**Duration:** {fmt(job_info["duration_sec"])}\n',
        "",
        "Target(s) | Duration | Cache Hit Rate | Action | Remote | Internal | Local | Sandbox | Hits | Total | Build-Logs |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for b in builds:
        duration = b["duration_sec"]
        c = b["cache"]

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

        md.append(
            f"| {b['targets']} "
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


def main(
    workflow_url: str,
    output_root: Path = Path("/tmp"),
) -> int:
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
    job_info["job-html-url"] = workflow_url

    build_metrics = {
        "job-html-url": workflow_url,
        "job-name": job_info["job-name"],
        "duration_sec": job_info["duration_sec"],
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

    download_build_profiles_if_exists(
        info["host"],
        info["owner"],
        info["repo"],
        info["run_id"],
        output_dir,
    )

    print("\n====================")
    print("OUTPUT")
    print("====================")
    print(f"Log : {log_file}")
    print(f"JSON: {json_file}")
    print(f"MD  : {md_file}")
    print(f"DIR : {output_dir}")

    return 0


def process_input(
    input_arg: str,
    output_root: Path,
) -> int:
    if WORKFLOW_URL_RE.match(input_arg):
        return main(input_arg, output_root)

    input_path = Path(input_arg)

    if not input_path.is_file():
        raise ValueError(
            f"'{input_arg}' is neither a workflow URL nor a JSON file."
        )

    with open(input_path, encoding="utf-8") as f:
        entries = json.load(f)

    if not isinstance(entries, list):
        raise ValueError("JSON must contain an array.")

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"Entry {i} is not an object.")

        workflow_url = entry.get("html_url")

        if not workflow_url:
            raise ValueError(
                f"Entry {i} has no 'html_url' property."
            )

        print("=" * 80)
        print(f"Processing {workflow_url}")
        print("=" * 80)

        main(workflow_url, output_root)

    return 0

if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print(
            f"Usage: {sys.argv[0]} "
            "<workflow-job-url | json-file> [output-dir]"
        )
        sys.exit(1)

    input_arg = sys.argv[1]

    out = (
        Path(sys.argv[2])
        if len(sys.argv) == 3
        else Path("/tmp")
    )

    sys.exit(process_input(input_arg, out))
