# ruff: noqa: CPY001
"""Tests for the `cimon prune-runs` CLI command."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow.parquet as pq
from cimon.__main__ import main
from click.testing import CliRunner

from tests.workflows.test_query import _write_sample

if TYPE_CHECKING:
    from pathlib import Path


def test_prune_runs_removes_matching_runs_in_place(tmp_path: Path) -> None:
    """Runs created within [--from-date, --to-date] are removed; others are kept."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)
    before = pq.read_table(path).to_pylist()

    result = CliRunner().invoke(
        main,
        [
            "prune-runs",
            str(path),
            "--from-date",
            "2026-08-10",
            "--to-date",
            "2026-08-16",
        ],
    )

    assert result.exit_code == 0, result.output
    assert pq.read_table(path).to_pylist() == [row for row in before if row["run_id"] != "101"]


def test_prune_runs_dry_run_does_not_modify_file(tmp_path: Path) -> None:
    """--dry-run reports matching rows without writing the filtered table."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)
    before = pq.read_table(path).to_pylist()

    result = CliRunner().invoke(
        main,
        [
            "prune-runs",
            str(path),
            "--from-date",
            "2026-08-10",
            "--to-date",
            "2026-08-16",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Dry run: would remove 1 row(s)" in result.output
    assert "2 row(s) would remain" in result.output
    assert pq.read_table(path).to_pylist() == before


def test_prune_runs_plain_date_window_covers_the_whole_day(tmp_path: Path) -> None:
    """--from-date X --to-date X+1 (same convention as `cimon sync`) removes all of day X.

    created_at is compared as a plain string: a run created at
    "2026-08-01T00:00:00Z" sorts before the bare date "2026-08-02" but not
    before "2026-08-01" itself -- so covering day X fully requires
    --to-date to be the *next* day, exactly like `cimon sync` and
    cyclic_sync.sh already does.
    """
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    result = CliRunner().invoke(
        main,
        [
            "prune-runs",
            str(path),
            "--from-date",
            "2026-08-01",
            "--to-date",
            "2026-08-02",
        ],
    )

    assert result.exit_code == 0, result.output
    remaining_run_ids = [row["run_id"] for row in pq.read_table(path).to_pylist()]
    assert remaining_run_ids == ["101", "102"]


def test_prune_runs_defaults_from_date_to_beginning_of_time(tmp_path: Path) -> None:
    """Omitting --from-date removes all runs through --to-date."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)

    result = CliRunner().invoke(
        main,
        [
            "prune-runs",
            str(path),
            "--to-date",
            "2026-08-16T23:59:59Z",
        ],
    )

    assert result.exit_code == 0, result.output
    remaining_run_ids = [row["run_id"] for row in pq.read_table(path).to_pylist()]
    assert remaining_run_ids == ["102"]


def test_prune_runs_no_matches_keeps_everything(tmp_path: Path) -> None:
    """A date range matching nothing leaves the cache unchanged."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)
    before = pq.read_table(path).to_pylist()

    result = CliRunner().invoke(
        main,
        [
            "prune-runs",
            str(path),
            "--from-date",
            "2027-01-01",
            "--to-date",
            "2027-01-02",
        ],
    )

    assert result.exit_code == 0, result.output
    assert pq.read_table(path).to_pylist() == before


def test_prune_runs_rejects_invalid_date(tmp_path: Path) -> None:
    """An invalid --from-date/--to-date value fails with a usage error, nothing is modified."""
    path = tmp_path / "workflows.parquet"
    _write_sample(path)
    before = pq.read_table(path).to_pylist()

    result = CliRunner().invoke(
        main,
        [
            "prune-runs",
            str(path),
            "--from-date",
            "not-a-date",
            "--to-date",
            "2026-08-16",
        ],
    )

    assert result.exit_code != 0
    assert "Invalid date/time" in result.output
    assert pq.read_table(path).to_pylist() == before


def test_prune_runs_rejects_missing_file() -> None:
    """A non-existent PARQUET_FILE fails with a usage error instead of a traceback."""
    result = CliRunner().invoke(
        main,
        [
            "prune-runs",
            "/does/not/exist.parquet",
            "--from-date",
            "2026-08-01",
            "--to-date",
            "2026-08-02",
        ],
    )

    assert result.exit_code != 0
    assert "does not exist" in result.output
