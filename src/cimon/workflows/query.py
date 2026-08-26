# ruff: noqa: CPY001
"""Composable, efficient filter builder for the workflows Parquet cache.

Filters are expressed as `pyarrow.dataset` predicates, so both the predicate
evaluation and the column selection are pushed down into the Parquet reader:
row groups that cannot match are skipped using their min/max statistics, and
columns that are not requested are never read from disk. Nothing is read
until a `to_*` method is called, so filters can be composed cheaply.

The resulting `pyarrow.Table` is a convenient hand-off point for downstream
visualization or statistics code, e.g. via `table.to_pandas()` or
`pyarrow.compute` aggregations.

Example:
    ```python
    rows = (
        WorkflowQuery(parquet_path)
        .workflow_conclusion("failure")
        .created_between("2026-08-01T00:00:00Z", "2026-08-31T23:59:59Z")
        .columns("run_id", "job_name", "job_duration_sec")
        .to_pylist()
    )
    ```
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.dataset as ds

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


class WorkflowQuery:
    """Fluent, lazily-evaluated filter over the workflows Parquet cache.

    Every filter method returns `self` so calls can be chained, and combines
    its predicate with any previously added ones using logical AND. Use
    `filter()` to add an arbitrary `pyarrow.dataset` expression when the
    built-in shortcuts below don't cover a case, without needing to extend
    this class.
    """

    def __init__(self, path: str | Path) -> None:
        """Open the Parquet file (or dataset directory) at `path` for querying."""
        # NOTE: row-group skipping (the main perf win here) only kicks in once
        # the file spans multiple row groups sorted by the filtered columns;
        # see the PERF note next to save_parquet() in synch.py.
        self._dataset = ds.dataset(path, format="parquet")
        self._expr: ds.Expression | None = None
        self._columns: list[str] | None = None

    def filter(self, expr: ds.Expression) -> WorkflowQuery:
        """Add an arbitrary pyarrow.dataset filter expression (AND-combined)."""
        self._expr = expr if self._expr is None else self._expr & expr
        return self

    def where(self, **equals: Any) -> WorkflowQuery:  # noqa: ANN401
        """Keep rows where every given column equals its given value."""
        for column, value in equals.items():
            self.filter(ds.field(column) == value)
        return self

    def isin(self, column: str, values: Iterable[Any]) -> WorkflowQuery:
        """Keep rows where `column` is one of `values`."""
        return self.filter(ds.field(column).isin(list(values)))

    def between(self, column: str, low: Any, high: Any) -> WorkflowQuery:  # noqa: ANN401
        """Keep rows where `low <= column <= high`."""
        return self.filter((ds.field(column) >= low) & (ds.field(column) <= high))

    def not_null(self, column: str) -> WorkflowQuery:
        """Keep rows where `column` is not null."""
        return self.filter(ds.field(column).is_valid())

    # Domain-specific shortcuts. Kept thin on top of filter()/isin()/between()
    # so adding another one is a one-liner, not a schema change.

    def run_id(self, *run_ids: int | str) -> WorkflowQuery:
        """Keep rows belonging to any of the given run IDs."""
        return self.isin("run_id", [str(value) for value in run_ids])

    def workflow_conclusion(self, *conclusions: str) -> WorkflowQuery:
        """Keep rows whose run concluded with any of the given values (e.g. "failure")."""
        return self.isin("workflow_conclusion", conclusions)

    def job_conclusion(self, *conclusions: str) -> WorkflowQuery:
        """Keep rows whose job concluded with any of the given values."""
        return self.isin("job_conclusion", conclusions)

    def job_name(self, *names: str) -> WorkflowQuery:
        """Keep rows whose job name is any of the given values."""
        return self.isin("job_name", names)

    def created_between(self, start: str, end: str) -> WorkflowQuery:
        """Keep runs created within `[start, end]` (ISO 8601 timestamps)."""
        return self.between("created_at", start, end)

    def columns(self, *names: str) -> WorkflowQuery:
        """Restrict which columns are read from disk (column pruning)."""
        self._columns = list(names) or None
        return self

    # Execution / materialization. Nothing above touches disk until here.

    def to_table(self) -> pa.Table:
        """Execute the query and return the result as an Arrow table."""
        return self._dataset.to_table(filter=self._expr, columns=self._columns)

    def to_pylist(self) -> list[dict[str, Any]]:
        """Execute the query and return the result as a list of dicts."""
        return self.to_table().to_pylist()
