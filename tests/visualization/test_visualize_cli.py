# ruff: noqa: CPY001
"""Tests for the `cimon visualize` CLI command."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow.parquet as pq
from click.testing import CliRunner

from cimon.__main__ import main
from cimon.visualization import registry
from cimon.visualization.registry import Visualization

from tests.workflows.test_query import _write_sample

if TYPE_CHECKING:
    from pathlib import Path


def test_visualize_command_writes_intermediate_and_calls_render(tmp_path: Path) -> None:
    """The `visualize` command filters per the registered spec and renders the result."""
    input_path = tmp_path / "workflows.parquet"
    _write_sample(input_path)

    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text("column: job_status\nop: eq\nvalue: in_progress\n", encoding="utf-8")

    rendered_run_ids: list[str] = []
    registry.register(
        Visualization(
            name="cli-test-viz",
            spec_path=spec_path,
            render=lambda table, _output_dir: rendered_run_ids.extend(table.column("run_id").to_pylist()),
        ),
    )

    output_dir = tmp_path / "out"
    result = CliRunner().invoke(
        main,
        ["visualize", "cli-test-viz", "--input", str(input_path), "--output-dir", str(output_dir)],
    )

    assert result.exit_code == 0, result.output
    assert rendered_run_ids == ["102"]
    assert pq.read_table(output_dir / "cli-test-viz.parquet").num_rows == 1


def test_visualize_command_list_flag() -> None:
    """`--list` prints the registered visualization names without requiring --input."""
    result = CliRunner().invoke(main, ["visualize", "--list"])

    assert result.exit_code == 0, result.output
    assert "runner-utilization" in result.output


def test_visualize_command_unknown_name(tmp_path: Path) -> None:
    """An unregistered visualization name produces a usage error, not a crash."""
    input_path = tmp_path / "workflows.parquet"
    _write_sample(input_path)

    result = CliRunner().invoke(
        main,
        ["visualize", "does-not-exist", "--input", str(input_path)],
    )

    assert result.exit_code != 0
    assert "does-not-exist" in result.output
