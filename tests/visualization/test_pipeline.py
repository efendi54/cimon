# ruff: noqa: CPY001
"""Tests for the visualization spec -> filtered Parquet -> render pipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow.parquet as pq
import pytest
import yaml

from cimon.visualization import pipeline, registry
from cimon.visualization.registry import Visualization

from tests.workflows.test_query import _write_sample

if TYPE_CHECKING:
    from pathlib import Path

    import pyarrow as pa


def test_run_writes_intermediate_parquet_and_calls_render(tmp_path: Path) -> None:
    """It filters the cache per the registered spec, persists it, then renders it."""
    input_path = tmp_path / "workflows.parquet"
    _write_sample(input_path)

    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.dump({"column": "job_status", "op": "eq", "value": "in_progress"}), encoding="utf-8")

    rendered: list[pa.Table] = []
    registry.register(
        Visualization(name="test-viz", spec_path=spec_path, render=lambda table, _output_dir: rendered.append(table)),
    )

    output_dir = tmp_path / "out"
    pipeline.run("test-viz", input_path, output_dir)

    intermediate = pq.read_table(output_dir / "test-viz.parquet")
    assert [row["run_id"] for row in intermediate.to_pylist()] == ["102"]
    assert len(rendered) == 1
    assert rendered[0].to_pylist() == intermediate.to_pylist()


def test_get_unknown_visualization_raises() -> None:
    """It raises with a helpful message when the name isn't registered."""
    with pytest.raises(KeyError, match="unknown-viz"):
        registry.get("unknown-viz")
