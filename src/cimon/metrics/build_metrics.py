"""Script for retrieving and processing build metrics towards github workflow jobs that initiated bazel builds.

- Dependency: gh (GitHub CLI) must be installed and authenticated with a token that has access to the repository via
the GH_TOKEN environment variable.

- Usage:
    python build_metrics.py <workflow-job-url> [output-dir]

The <workflow-job-url> should be a URL to a specific job in a GitHub Actions workflow run.
The optional [output-dir] specifies where to save the output files (default is /tmp).

The script downloads the job log, extracts build metrics, and generates a JSON file and a Markdown summary table.
Additionally it checks for the existence of a 'build-profiles' artifact and downloads it too if available.

"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# -----------------------------
# WORKFLOW URL
# -----------------------------
WORKFLOW_URL_RE = re.compile(
    r"^https://(?P<host>[^/]+)/"
    r"(?P<owner>[^/]+)/"
    r"(?P<repo>[^/]+)/"
    r"actions/runs/(?P<run_id>\d+)/job/(?P<job_id>\d+)/?$"
)

# -----------------------------
# BUILD LINE
# -----------------------------
BUILD_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T[0-9:.]+Z)\s*"
    r"(?:##\[group\]\s*)?"
    r"(?:\x1B\[[0-?]*[ -/]*[@-~])*"
    r"bazel build\b.*//[^ ]+.*$"
)

# -----------------------------
# INFO LINE
# -----------------------------
INFO_RE = re.compile(r"^(?P<ts>.*) INFO: (?P<body>\d+ process(?:es)?: .*cache hit,.*)$")

# -----------------------------
# CACHE METRICS
# -----------------------------
CACHE_RE = {
    "action": re.compile(r"(\d+)\s+action cache hit"),
    "remote": re.compile(r"(\d+)\s+remote cache hit"),
    "internal": re.compile(r"(\d+)\s+internal"),
    "sandbox": re.compile(r"(\d+)\s+processwrapper-sandbox"),
    "local": re.compile(r"(\d+)\s+local"),
}


# -----------------------------
# UTIL
# -----------------------------
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


# -----------------------------
# GITHUB AUTH
# -----------------------------
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


# -----------------------------
# DOWNLOAD LOG
# -----------------------------
def download_job_log(host, owner, repo, job_id, output_dir: Path) -> Path:
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


# -----------------------------
# DOWNLOAD ARTIFACT
# -----------------------------
def download_build_profiles_if_exists(host, owner, repo, run_id, output_dir: Path) -> None:
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
    artifact = next((a for a in artifacts if a["name"] == "build-profiles"), None)

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


# -----------------------------
# PARSE LOG
# -----------------------------
def parse_log(logfile: Path) -> list[dict[str, Any]]:
    builds: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    with open(logfile, encoding="utf-8", errors="replace") as f:
        for line in f:
            bm = BUILD_RE.search(line)

            if bm:
                if current is not None:
                    raise RuntimeError("Missing INFO for previous build:\n" + current["build_line"])

                current = {
                    "start_ts": parse_ts(bm.group("ts")).isoformat(),
                    "build_line": line.strip(),
                }
                # print(f"Found build: {current['build_line']}")
                continue

            im = INFO_RE.search(line)

            if im and current:
                body = im.group("body")

                start = datetime.fromisoformat(current["start_ts"])
                info_ts = parse_ts(im.group("ts")).isoformat()
                info = datetime.fromisoformat(info_ts)
                duration = (info - start).total_seconds()

                current.update(
                    {
                        "duration_sec": duration,
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
        raise RuntimeError("Missing INFO for last build:\n" + current["build_line"])

    return builds


# -----------------------------
# MARKDOWN
# -----------------------------
def generate_md_table_entries(builds: list[dict[str, Any]], workflow_url: str) -> str:
    md = [
        "# Bazel Build Summary",
        f"{workflow_url}",
        "",
        "| Build / INFO | Duration | Cache Hit Rate | Action | Remote | Internal | Local | Sandbox | Hits | Total |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for b in builds:
        # start = datetime.fromisoformat(b["start_ts"])
        # info = datetime.fromisoformat(b["info_ts"])
        # duration = (info - start).total_seconds()
        duration = b["duration_sec"]

        c = b["cache"]
        total = c["action"] + c["remote"] + c["internal"] + c["sandbox"] + c["local"]
        hits = c["action"] + c["remote"]
        rate = (hits / total * 100.0) if total else 0.0

        cell = f"<code>{b['build_line']}</code><br><code>{b['info_line']}</code>"

        md.append(
            f"| {cell} "
            f"| {fmt(duration)} "
            f"| **{rate:.1f}%** "
            f"| {c['action']} "
            f"| {c['remote']} "
            f"| {c['internal']} "
            f"| {c['local']} "
            f"| {c['sandbox']} "
            f"| {hits} "
            f"| {total} |"
        )

    return "\n".join(md)


# -----------------------------
# MAIN
# -----------------------------
def main(workflow_url: str, output_root: Path = Path("/tmp")) -> int:  # noqa: S108
    info = WORKFLOW_URL_RE.match(workflow_url)
    if not info:
        raise ValueError("Invalid workflow URL")

    info = info.groupdict()

    output_dir = output_root / info["run_id"]
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ensure_github_auth(info["host"])

    log_file = download_job_log(
        info["host"],
        info["owner"],
        info["repo"],
        info["job_id"],
        output_dir,
    )

    builds = parse_log(log_file)

    json_file = output_dir / f"{info['job_id']}.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(builds, f, indent=2)

    md_file = output_dir / f"{info['job_id']}.md"
    with open(md_file, "w", encoding="utf-8") as f:
        f.write(generate_md_table_entries(builds, workflow_url))

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


# -----------------------------
# ENTRYPOINT
# -----------------------------
if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print(f"Usage: {sys.argv[0]} <workflow-job-url> [output-dir]")
        print("The <workflow-job-url> should be a URL to a specific job in a GitHub Actions workflow run.")
        print(f"Example: {sys.argv[0]} https://github.com/owner/repo/actions/runs/123456789/job/5678910 /tmp/output")
        sys.exit(1)

    workflow_url = sys.argv[1]
    out = Path(sys.argv[2]) if len(sys.argv) == 3 else Path("/tmp")  # noqa: PLR2004, S108

    sys.exit(main(workflow_url, out))
