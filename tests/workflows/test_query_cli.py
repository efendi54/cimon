# ruff: noqa: CPY001
"""Tests for the `cimon query` CLI command."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow.parquet as pq
import yaml
from click.testing import CliRunner

from cimon.__main__ import main

from tests.workflows.test_query import _write_sample

if TYPE_CHECKING:
    from pathlib import Path


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
            "--input",
            str(input_path),
            "--spec",
            str(spec_path),
            "--output",
            str(output_path),
            "--column",
            "run_id",
        ],
    )

    assert result.exit_code == 0, result.output
    table = pq.read_table(output_path)
    assert table.to_pylist() == [{"run_id": "101"}]
