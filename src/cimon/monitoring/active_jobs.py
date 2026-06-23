from textual.app import App, ComposeResult
from textual.widgets import DataTable, Header, Static
from textual.containers import Container, Vertical

import asyncio
import json
import webbrowser
import time

from datetime import datetime, timezone

REPO = "CARIAD/app-adas-src"

# --------------------------------------------------
# CACHE
# --------------------------------------------------

RUN_CACHE = {}
RUN_EVENT_CACHE = {}

ACTIVE_RUN_IDS = []
ACTIVE_RUN_IDS_LAST_REFRESH = 0
RUN_DISCOVERY_INTERVAL = 60

# --------------------------------------------------
# SORT STATE
# --------------------------------------------------

SORT_COLUMN = 0
SORT_REVERSE = True

# --------------------------------------------------
# RATE LIMIT STATE
# --------------------------------------------------

RATE_LIMIT_LIMIT = None
RATE_LIMIT_REMAINING = None
RATE_LIMIT_RESET = None


# --------------------------------------------------
# COMMAND EXECUTION
# --------------------------------------------------

async def run_cmd(*cmd):

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await proc.communicate()

    text = stdout.decode()

    headers = {}
    body = []

    in_headers = True

    for line in text.splitlines():

        if line.startswith("HTTP/"):
            continue

        if line.strip() == "":
            in_headers = False
            continue

        if in_headers and ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        else:
            body.append(line)

    return "\n".join(body), headers


# --------------------------------------------------
# RATE LIMIT
# --------------------------------------------------

def update_rate_limit(headers):

    global RATE_LIMIT_LIMIT, RATE_LIMIT_REMAINING, RATE_LIMIT_RESET

    if "x-ratelimit-limit" in headers:
        RATE_LIMIT_LIMIT = headers["x-ratelimit-limit"]

    if "x-ratelimit-remaining" in headers:
        RATE_LIMIT_REMAINING = headers["x-ratelimit-remaining"]

    if "x-ratelimit-reset" in headers:
        RATE_LIMIT_RESET = headers["x-ratelimit-reset"]


def rate_limit_reset_string():

    if not RATE_LIMIT_RESET:
        return "?"

    try:
        remaining = int(RATE_LIMIT_RESET) - int(time.time())
        return f"{max(0, remaining//60)}m{max(0, remaining%60):02}s"
    except:
        return "?"


def rate_limit_color():

    if not RATE_LIMIT_REMAINING or not RATE_LIMIT_LIMIT:
        return "white"

    try:
        r = int(RATE_LIMIT_REMAINING)
        l = int(RATE_LIMIT_LIMIT)

        ratio = r / l

        if ratio > 0.5:
            return "green"
        if ratio > 0.2:
            return "yellow"
        return "red"
    except:
        return "white"


# --------------------------------------------------
# TIME
# --------------------------------------------------

def calc_duration_seconds(started_at: str) -> int:
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return int((now - started).total_seconds())


# --------------------------------------------------
# RUN DISCOVERY
# --------------------------------------------------

async def fetch_active_run_ids():

    page = 1
    active_run_ids = []

    while True:

        out, headers = await run_cmd(
            "gh",
            "api",
            "--include",
            f"repos/{REPO}/actions/runs?per_page=100&page={page}",
            "--jq",
            ".workflow_runs[] | {id: .id, status: .status}"
        )

        update_rate_limit(headers)

        page_runs = []
        for line in out.splitlines():
            try:
                page_runs.append(json.loads(line))
            except:
                pass

        if not page_runs:
            break

        page_active = [
            str(r["id"])
            for r in page_runs
            if r["status"] in ["in_progress", "queued"]
        ]

        active_run_ids.extend(page_active)

        if not page_active:
            break

        page += 1

    return active_run_ids


# --------------------------------------------------
# CACHE
# --------------------------------------------------

async def get_active_run_ids_cached():

    global ACTIVE_RUN_IDS, ACTIVE_RUN_IDS_LAST_REFRESH

    now = time.time()

    if ACTIVE_RUN_IDS and now - ACTIVE_RUN_IDS_LAST_REFRESH < RUN_DISCOVERY_INTERVAL:
        return ACTIVE_RUN_IDS

    ACTIVE_RUN_IDS = await fetch_active_run_ids()
    ACTIVE_RUN_IDS_LAST_REFRESH = now

    return ACTIVE_RUN_IDS


# --------------------------------------------------
# EVENT
# --------------------------------------------------

async def fetch_run_event(run_id):

    if run_id in RUN_EVENT_CACHE:
        return RUN_EVENT_CACHE[run_id]

    out, headers = await run_cmd(
        "gh",
        "api",
        "--include",
        f"repos/{REPO}/actions/runs/{run_id}",
        "--jq",
        ".event"
    )

    update_rate_limit(headers)

    event = out.strip().strip('"')
    RUN_EVENT_CACHE[run_id] = event
    return event


# --------------------------------------------------
# JOBS
# --------------------------------------------------

async def fetch_jobs_for_run(run_id):

    if run_id in RUN_CACHE:
        return RUN_CACHE[run_id]

    event = await fetch_run_event(run_id)

    out, headers = await run_cmd(
        "gh",
        "api",
        "--include",
        f"repos/{REPO}/actions/runs/{run_id}/jobs",
        "--jq",
        ".jobs[] | select(.status==\"in_progress\" or .status==\"queued\") | {job_id: .id, name: .name, status: .status, started_at: .started_at, runner: (.runner_name // \"unknown\")}"
    )

    update_rate_limit(headers)

    jobs = []
    for line in out.splitlines():
        try:
            job = json.loads(line)
            job["run_id"] = run_id
            job["event"] = event
            jobs.append(job)
        except:
            pass

    RUN_CACHE[run_id] = jobs
    return jobs


async def fetch_jobs():

    run_ids = await get_active_run_ids_cached()
    active_set = set(run_ids)

    for k in list(RUN_CACHE.keys()):
        if k not in active_set:
            del RUN_CACHE[k]

    missing = [r for r in run_ids if r not in RUN_CACHE]

    if missing:
        await asyncio.gather(*(fetch_jobs_for_run(r) for r in missing))

    jobs = []

    for run_id in run_ids:
        for job in RUN_CACHE.get(run_id, []):

            started = job.get("started_at") or datetime.now(timezone.utc).isoformat()

            jobs.append({
                **job,
                "duration_seconds": calc_duration_seconds(started),
                "url": f"https://git.hub.vwgroup.com/{REPO}/actions/runs/{run_id}"
            })

    return jobs


# --------------------------------------------------
# SORT
# --------------------------------------------------

def sort_key(job, col):

    if col == 0:
        return job["duration_seconds"]
    if col == 1:
        return job["name"].lower()
    if col == 2:
        return job.get("status", "")
    if col == 3:
        return job.get("event", "")
    if col == 4:
        return job.get("runner", "")
    if col == 5:
        return job["url"].lower()

    return ""


# --------------------------------------------------
# APP
# --------------------------------------------------

class CIHtop(App):

    BINDINGS = [
        ("q", "quit"),
        ("r", "refresh"),
        ("enter", "open_run"),

        ("1", "sort0"),
        ("2", "sort1"),
        ("3", "sort2"),
        ("4", "sort3"),
        ("5", "sort4"),
        ("6", "sort5"),
    ]

    def _set_sort(self, col):

        global SORT_COLUMN, SORT_REVERSE

        if SORT_COLUMN == col:
            SORT_REVERSE = not SORT_REVERSE
        else:
            SORT_COLUMN = col
            SORT_REVERSE = False

        asyncio.create_task(self.refresh_data())

    def action_sort0(self): self._set_sort(0)
    def action_sort1(self): self._set_sort(1)
    def action_sort2(self): self._set_sort(2)
    def action_sort3(self): self._set_sort(3)
    def action_sort4(self): self._set_sort(4)
    def action_sort5(self): self._set_sort(5)

    # ------------------------------
    # UI
    # ------------------------------

    def compose(self) -> ComposeResult:

        yield Header()

        with Vertical():
            self.table = DataTable()
            yield self.table

            self.status = Static("")
            yield self.status

    async def on_mount(self):

        self.table.add_columns(
            "DURATION",
            "JOB",
            "STATUS",
            "EVENT",
            "RUNNER",
            "URL",
        )

        self.table.cursor_type = "row"

        self.set_interval(15, self.refresh_data)
        self.set_interval(60, self.refresh_data)

        await self.refresh_data()

    async def refresh_data(self):

        try:

            jobs = await fetch_jobs()

            jobs.sort(
                key=lambda j: sort_key(j, SORT_COLUMN),
                reverse=SORT_REVERSE
            )

            self.table.clear()

            for job in jobs:

                d = job["duration_seconds"]
                h, m, s = d // 3600, (d % 3600)//60, d % 60

                self.table.add_row(
                    f"{h}:{m:02}:{s:02}",
                    job["name"],
                    job.get("status", ""),
                    job.get("event", ""),
                    job.get("runner", ""),
                    job["url"],
                )

            # ---------------- status bar ----------------
            color = rate_limit_color()

            quota = ""
            if RATE_LIMIT_REMAINING and RATE_LIMIT_LIMIT:
                quota = f" gh={RATE_LIMIT_REMAINING}/{RATE_LIMIT_LIMIT} reset={rate_limit_reset_string()}"

            self.status.update(
                f"[{color}]jobs={len(jobs)} sort={SORT_COLUMN} reverse={SORT_REVERSE}{quota}[/{color}]"
            )

        except Exception as e:
            self.status.update(f"[red]ERROR: {e}[/red]")

    def action_open_run(self):

        row = self.table.cursor_row
        if row is None:
            return

        webbrowser.open(self.table.get_row_at(row)[5])

def active_jobs():
    CIHtop().run()
