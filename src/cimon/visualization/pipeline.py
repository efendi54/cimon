# ruff: noqa: CPY001
"""Spec -> filtered Parquet -> render pipeline for named visualizations.

Filtering and rendering are deliberately kept as two separate steps: the
filtered rows are written out as their own Parquet file before the render
function ever sees them, so the intermediate result can be inspected (e.g.
with a Parquet viewer) or re-rendered without re-running the filter against
the full cache.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import yaml

from cimon.parquet_io import write_table_atomic
from cimon.visualization.registry import get
from cimon.workflows.query import WorkflowQuery

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def run(name: str, input_path: Path, output_dir: Path) -> None:
    """Filter `input_path` per the visualization `name`'s spec, then render it.

    The filtered rows are written to `output_dir/{name}.parquet` before
    `render` is called with the resulting table and `output_dir`.
    """
    visualization = get(name)
    spec = yaml.safe_load(visualization.spec_path.read_text(encoding="utf-8"))

    query = WorkflowQuery(input_path).filter_spec(spec)
    if visualization.columns:
        query = query.columns(*visualization.columns)
    table = query.to_table()

    output_dir.mkdir(parents=True, exist_ok=True)
    intermediate_path = output_dir / f"{name}.parquet"
    write_table_atomic(table, intermediate_path)
    logger.info(f"Wrote {table.num_rows} filtered row(s) to {intermediate_path}")

    visualization.render(table, output_dir)
