# ruff: noqa: CPY001
"""Shared helper for atomically writing Arrow tables to Parquet."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow.parquet as pq

if TYPE_CHECKING:
    import pyarrow as pa


def write_table_atomic(table: pa.Table, path: Path) -> None:
    """Write `table` to `path` as Parquet, replacing any existing file only on success."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".query_", suffix=".parquet")
    os.close(fd)
    try:
        pq.write_table(table, tmp_path)
        Path(tmp_path).replace(path)
    finally:
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()
