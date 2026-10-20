#!/usr/bin/env python3
"""
Erzeugt einen Markdown-Report über den Festplattenverbrauch von GitHub-Actions
Runnern, basierend auf Job-Log-Dateien unter ~/tmp/logs/<run_id>/<job_id>.log.

Jede Log-Datei enthält u.a.:
  - eine Zeile "Runner name: '<name>'"
  - eine Zeile "Complete job name: <job_name> (<runner>)"
  - genau zwei "df"-Zeilen für "/dev/root ... /" (vor und nach dem Job)

Aufruf:
    python3 disk_usage_report.py [--logs-dir DIR] [--output FILE]
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d+)Z\s+(.*)$")
RUNNER_NAME_RE = re.compile(r"Runner name:\s*'([^']+)'")
JOB_NAME_RE = re.compile(r"Complete job name:\s*(.*?)\s*\(([^()]*)\)\s*$")
DISK_LINE_RE = re.compile(
    r"^(?P<filesystem>\S+)\s+(?P<size>\S+)\s+(?P<used>\S+)\s+"
    r"(?P<avail>\S+)\s+(?P<pct>\d+)%\s+(?P<mount>\S+)$"
)


SIZE_UNIT_FACTORS = {
    "K": 1 / (1024 * 1024),
    "M": 1 / 1024,
    "G": 1.0,
    "T": 1024.0,
}


def parse_size_to_gb(raw: str) -> float:
    """Wandelt df-Größenangaben wie '235G', '512M' oder '1.2T' in GB (als
    Gleitkommazahl) um, damit präzisere Deltas als über das gerundete
    Prozent-Feld berechnet werden können."""
    match = re.match(r"^([\d.]+)([KMGT]?)$", raw.strip(), re.IGNORECASE)
    if not match:
        raise ValueError(f"Unerwartetes Größenformat: {raw!r}")
    value, unit = match.groups()
    factor = SIZE_UNIT_FACTORS.get(unit.upper(), 1.0)
    return float(value) * factor


@dataclass
class DiskSample:
    timestamp: datetime
    size: str
    used: str
    avail: str
    percent: int
    mount: str

    @property
    def used_gb(self) -> float:
        return parse_size_to_gb(self.used)


GHE_RUN_URL_TEMPLATE = (
    "https://cariad.ghe.com/CAS/app-adas-src/actions/runs/{run_id}/job/{job_id}"
)


@dataclass
class JobEntry:
    run_id: str
    job_id: str
    runner_name: str
    job_name: str | None
    before: DiskSample
    after: DiskSample
    log_path: Path

    @property
    def delta_percent(self) -> int:
        return self.after.percent - self.before.percent

    @property
    def delta_used_gb(self) -> float:
        return self.after.used_gb - self.before.used_gb

    @property
    def url(self) -> str:
        return GHE_RUN_URL_TEMPLATE.format(run_id=self.run_id, job_id=self.job_id)


def parse_timestamp(base: str, frac: str) -> datetime:
    """Baut aus Datum/Zeit-Basis und Nachkommastellen (ggf. >6 Stellen, da
    die Logs teils Sub-Mikrosekunden-Präzision verwenden) ein datetime."""
    frac6 = (frac + "000000")[:6]
    return datetime.fromisoformat(f"{base}.{frac6}")


def parse_log_file(path: Path) -> JobEntry | None:
    runner_name: str | None = None
    job_name: str | None = None
    disk_samples: list[DiskSample] = []

    text = path.read_text(encoding="utf-8", errors="replace")
    for raw_line in text.splitlines():
        ts_match = TIMESTAMP_RE.match(raw_line)
        if not ts_match:
            continue
        base, frac, content = ts_match.groups()

        if runner_name is None:
            rn_match = RUNNER_NAME_RE.search(content)
            if rn_match:
                runner_name = rn_match.group(1)
                continue

        jn_match = JOB_NAME_RE.search(content)
        if jn_match:
            job_name = jn_match.group(1).strip()
            continue

        disk_match = DISK_LINE_RE.match(content.strip())
        if disk_match and disk_match.group("mount") == "/" and disk_match.group("filesystem") == "/dev/root":
            disk_samples.append(
                DiskSample(
                    timestamp=parse_timestamp(base, frac),
                    size=disk_match.group("size"),
                    used=disk_match.group("used"),
                    avail=disk_match.group("avail"),
                    percent=int(disk_match.group("pct")),
                    mount=disk_match.group("mount"),
                )
            )

    if runner_name is None or len(disk_samples) < 2:
        # Unvollständige/unerwartete Log-Datei überspringen.
        return None

    before, after = disk_samples[0], disk_samples[-1]
    return JobEntry(
        run_id=path.parent.name,
        job_id=path.stem,
        runner_name=runner_name,
        job_name=job_name,
        before=before,
        after=after,
        log_path=path,
    )


def collect_entries(logs_dir: Path) -> list[JobEntry]:
    entries: list[JobEntry] = []
    for log_path in sorted(logs_dir.glob("**/*.log")):
        entry = parse_log_file(log_path)
        if entry is not None:
            entries.append(entry)
    return entries


def format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_delta(delta: int) -> str:
    if delta > 0:
        return f"+{delta}%"
    if delta < 0:
        return f"{delta}%"
    return "±0%"


def format_delta_gb(delta_gb: float) -> str:
    sign = "+" if delta_gb > 0 else ("" if delta_gb < 0 else "±")
    return f"{sign}{delta_gb:.1f} GB"


def format_days(start: datetime, end: datetime) -> str:
    days = (end - start).total_seconds() / 86400
    return f"{days:.1f}"


def format_timespan(start: datetime, end: datetime) -> str:
    total_seconds = int((end - start).total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days} Tag{'e' if days != 1 else ''}")
    if hours or days:
        parts.append(f"{hours} Stunde{'n' if hours != 1 else ''}")
    parts.append(f"{minutes} Minute{'n' if minutes != 1 else ''}")
    return ", ".join(parts)


def build_markdown(entries: list[JobEntry]) -> str:
    by_runner: dict[str, list[JobEntry]] = defaultdict(list)
    for entry in entries:
        by_runner[entry.runner_name].append(entry)
    for runner_entries in by_runner.values():
        runner_entries.sort(key=lambda e: e.before.timestamp)

    oldest = min(entry.before.timestamp for entry in entries)
    newest = max(entry.before.timestamp for entry in entries)

    lines: list[str] = []
    lines.append("# Festplattenverbrauch-Report pro Runner")
    lines.append("")
    lines.append(
        f"Ausgewertete Jobs: **{len(entries)}** "
        f"über **{len(by_runner)}** Runner."
    )
    lines.append(
        f"Betrachteter Zeitraum: **{format_dt(oldest)}** bis **{format_dt(newest)}** "
        f"(Dauer: {format_timespan(oldest, newest)})."
    )
    lines.append("")

    # Gesamtübersicht
    lines.append("## Übersicht pro Runner")
    lines.append("")
    lines.append(
        "| Runner | Anzahl Jobs | Erster Job | Letzter Job | Zeitspanne (Tage) | "
        "Max. Belegung | Max. Delta (%) | Max. Delta (GB) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for runner_name in sorted(by_runner):
        runner_entries = by_runner[runner_name]
        first_ts = runner_entries[0].before.timestamp
        last_ts = runner_entries[-1].before.timestamp
        max_pct = max(max(e.before.percent, e.after.percent) for e in runner_entries)
        max_delta_pct = max(runner_entries, key=lambda e: abs(e.delta_percent))
        max_delta_gb = max(runner_entries, key=lambda e: abs(e.delta_used_gb))
        lines.append(
            f"| `{runner_name}` | {len(runner_entries)} "
            f"| {format_dt(first_ts)} "
            f"| {format_dt(last_ts)} "
            f"| {format_days(first_ts, last_ts)} "
            f"| {max_pct}% "
            f"| {format_delta(max_delta_pct.delta_percent)} "
            f"| {format_delta_gb(max_delta_gb.delta_used_gb)} |"
        )
    lines.append("")

    # Detailtabellen pro Runner
    lines.append("## Details pro Runner")
    lines.append("")
    for runner_name in sorted(by_runner):
        runner_entries = by_runner[runner_name]
        lines.append(f"### Runner: `{runner_name}`")
        lines.append("")
        lines.append(f"Anzahl ausgeführter Jobs: **{len(runner_entries)}**")
        lines.append("")
        lines.append(
            "| # | Zeitpunkt (Start) | Job | Belegung vorher | Belegung nachher | "
            "Delta (%) | Belegt vorher | Belegt nachher | Delta (GB) | Workflow-Run / Job |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for idx, entry in enumerate(runner_entries, start=1):
            lines.append(
                f"| {idx} "
                f"| {format_dt(entry.before.timestamp)} "
                f"| {entry.job_name or '–'} "
                f"| {entry.before.percent}% "
                f"| {entry.after.percent}% "
                f"| {format_delta(entry.delta_percent)} "
                f"| {entry.before.used} / {entry.before.size} "
                f"| {entry.after.used} / {entry.after.size} "
                f"| {format_delta_gb(entry.delta_used_gb)} "
                f"| [{entry.run_id}/{entry.job_id}]({entry.url}) |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=Path.home() / "tmp" / "logs",
        help="Verzeichnis mit den Job-Log-Dateien (Standard: ~/tmp/logs)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / "tmp" / "disk_usage_report.md",
        help="Pfad der zu erzeugenden Markdown-Datei",
    )
    args = parser.parse_args()

    if not args.logs_dir.is_dir():
        raise SystemExit(f"Logs-Verzeichnis nicht gefunden: {args.logs_dir}")

    entries = collect_entries(args.logs_dir)
    if not entries:
        raise SystemExit(f"Keine auswertbaren Log-Dateien in {args.logs_dir} gefunden.")

    markdown = build_markdown(entries)
    args.output.write_text(markdown, encoding="utf-8")
    print(f"Report geschrieben: {args.output} ({len(entries)} Jobs, "
          f"{len({e.runner_name for e in entries})} Runner)")


if __name__ == "__main__":
    main()
