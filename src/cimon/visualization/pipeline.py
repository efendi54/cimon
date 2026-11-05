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
    from collections.abc import Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)


def run(name: str, input_path: Path, output_dir: Path, params: Mapping[str, str] | None = None) -> None:
    """Filter `input_path` per the visualization `name`'s spec, then render it.

    The filtered rows are written to `output_dir/{name}.parquet` before
    `render` is called with the resulting table and `output_dir`.

    `params` are only accepted by parametrized visualizations (see `Visualization`); they build the filter spec and
    are handed to `render` as a third argument. Raises `ValueError` for unknown/invalid parameters or if nothing
    matches.
    """
    visualization = get(name)
    params = dict(params or {})
    unknown = sorted(set(params) - set(visualization.params))
    if unknown:
        accepted = ", ".join(visualization.params) or "none"
        msg = f"Visualization {name!r} does not accept parameter(s) {', '.join(unknown)}. Accepted: {accepted}."
        raise ValueError(msg)

    if visualization.spec_builder:
        spec = visualization.spec_builder(params)
    else:
        spec = yaml.safe_load(visualization.spec_path.read_text(encoding="utf-8"))

    query = WorkflowQuery(input_path).filter_spec(spec)
    if visualization.columns:
        query = query.columns(*visualization.columns)
    table = query.to_table()
    if visualization.spec_builder and table.num_rows == 0:
        msg = f"Nothing in {input_path} matches {params}. Is it in the cache? Run `cimon sync` first."
        raise ValueError(msg)

    output_dir.mkdir(parents=True, exist_ok=True)
    intermediate_path = output_dir / f"{name}.parquet"
    write_table_atomic(table, intermediate_path)
    logger.info(f"Wrote {table.num_rows} filtered row(s) to {intermediate_path}")

    if visualization.params:
        visualization.render(table, output_dir, params)
    else:
        visualization.render(table, output_dir)
