# ruff: noqa: CPY001
"""Tests for the `cimon query` CLI command."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml
from click.testing import CliRunner

from cimon.__main__ import main

from tests.workflows.test_query import _write_sample

if TYPE_CHECKING:
    from click.testing import Result


def test_query_command_writes_filtered_parquet(tmp_path: Path) -> None:
    """The `query` command filters the input Parquet file per the spec and writes a new one."""
    input_path = tmp_path / "workflows.parquet"
    _write_sample(input_path)

    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        yaml.dump({"column": "workflow_conclusion", "op": "eq", "value": "failure"}),
        encoding="utf-8",
    )

    output_path = tmp_path / "filtered.parquet"

    result = CliRunner().invoke(
        main,
        [
            "query",
            str(spec_path),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--column",
            "run_id",
        ],
    )

    assert result.exit_code == 0, result.output
    table = pq.read_table(output_path)
    assert table.to_pylist() == [{"run_id": "101"}]


def test_query_command_accepts_multiple_specs_and_combines_them(tmp_path: Path) -> None:
    """Given several spec files, only rows matching all of them (AND-combined) are kept."""
    input_path = tmp_path / "workflows.parquet"
    _write_sample(input_path)

    spec_a_path = tmp_path / "spec_a.yaml"
    spec_a_path.write_text(
        yaml.dump({"column": "workflow_status", "op": "eq", "value": "completed"}),
        encoding="utf-8",
    )
    spec_b_path = tmp_path / "spec_b.yaml"
    spec_b_path.write_text(
        yaml.dump({"column": "workflow_conclusion", "op": "eq", "value": "failure"}),
        encoding="utf-8",
    )

    output_path = tmp_path / "filtered.parquet"

    result = CliRunner().invoke(
        main,
        [
            "query",
            str(spec_a_path),
            str(spec_b_path),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--column",
            "run_id",
        ],
    )

    assert result.exit_code == 0, result.output
    table = pq.read_table(output_path)
    assert table.to_pylist() == [{"run_id": "101"}]


def _run_sql(tmp_path: Path, sql: str, *args: str) -> Result:
    sql_path = tmp_path / "query.sql"
    sql_path.write_text(sql, encoding="utf-8")
    return CliRunner().invoke(
        main,
        [
            "query",
            "--sql-file",
            str(sql_path),
            "--input",
            str(tmp_path / "workflows.parquet"),
            "--output",
            str(tmp_path / "result.parquet"),
            *args,
        ],
    )


def test_query_sql_writes_aggregated_parquet(tmp_path: Path) -> None:
    """Run the documented SQL against job rows and check its result schema and order."""
    pytest.importorskip("duckdb")
    row = {
        "job_name": "build",
        "job_duration_sec": 12.345,
        "job_started_at": "2026-11-20T03:00:00Z",
        "event": "merge_group",
        "job_conclusion": "failure",
        "workflow_conclusion": "failure",
    }
    pq.write_table(
        pa.Table.from_pylist([
            {**row, "job_name": "test", "job_duration_sec": 20.0},
            row,
            {**row, "job_duration_sec": None},
            {**row, "job_started_at": "2026-11-20T02:59:59Z"},
            {**row, "event": "push"},
            {**row, "job_conclusion": "success"},
            {**row, "workflow_conclusion": "success"},
        ]),
        tmp_path / "workflows.parquet",
    )
    sql = (Path(__file__).parents[1] / "example_specs" / "filter_merge_group_failues.sql").read_text(
        encoding="utf-8",
    )

    result = _run_sql(tmp_path, sql)

    assert result.exit_code == 0, result.output
    table = pq.read_table(tmp_path / "result.parquet")
    assert table.column_names == ["job_name", "failure_count", "avg_duration_sec"]
    assert table.to_pylist() == [
        {"job_name": "build", "failure_count": 3, "avg_duration_sec": 12.35},
        {"job_name": "test", "failure_count": 1, "avg_duration_sec": 20.0},
    ]


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT 1; SELECT 2", "exactly one SELECT"),
        ("CREATE TABLE scratch (id INTEGER)", "exactly one SELECT"),
        ("SELECT * FROM read_parquet('workflows.parquet')", "file system operations are disabled"),
    ],
)
def test_query_sql_rejects_unsupported_operations(tmp_path: Path, sql: str, message: str) -> None:
    """Invalid SQL modes fail without overwriting an existing output."""
    pytest.importorskip("duckdb")
    _write_sample(tmp_path / "workflows.parquet")
    output = tmp_path / "result.parquet"
    output.write_bytes(b"existing output")

    result = _run_sql(tmp_path, sql)

    assert result.exit_code != 0
    assert message in result.output
    assert output.read_bytes() == b"existing output"


def test_query_sql_rejects_column_option(tmp_path: Path) -> None:
    """Only SQL controls the output columns in SQL mode."""
    _write_sample(tmp_path / "workflows.parquet")

    result = _run_sql(tmp_path, "SELECT * FROM source", "--column", "job_name")

    assert result.exit_code == 2  # noqa: PLR2004
    assert "cannot be combined with SPECS or --column" in result.output
