# ruff: noqa: CPY001
"""Registry of named visualizations: a filter spec paired with a render callback.

Each entry names a filter spec (see `cimon.workflows.query_spec`) that narrows
the workflows Parquet cache down to the rows one visualization cares about,
and a `render` function that turns the resulting `pa.Table` into the actual
visualization. `cimon.visualization.pipeline` runs the filter, writes the
result out as its own Parquet file, and then calls `render` -- so the
filtered subset stays inspectable/reusable independently of the render step.

Add a new visualization by adding a spec file under `specs/` and calling
`register()` with it below.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    import pyarrow as pa

SPECS_DIR = Path(__file__).parent / "specs"


@dataclass(frozen=True)
class Visualization:
    """A named filter spec paired with the function that renders its result."""

    name: str
    spec_path: Path
    render: Callable[[pa.Table, Path], None]
    columns: tuple[str, ...] | None = None


_REGISTRY: dict[str, Visualization] = {}


def register(visualization: Visualization) -> None:
    """Add `visualization` to the registry, keyed by its name."""
    _REGISTRY[visualization.name] = visualization


def get(name: str) -> Visualization:
    """Look up a registered visualization by name."""
    try:
        return _REGISTRY[name]
    except KeyError:
        msg = f"Unknown visualization {name!r}. Available: {', '.join(sorted(_REGISTRY)) or '(none registered)'}"
        raise KeyError(msg) from None


def available() -> list[str]:
    """List the names of all registered visualizations."""
    return sorted(_REGISTRY)


def _not_implemented(_table: pa.Table, _output_dir: Path) -> None:
    """Placeholder render callback for visualizations that don't have one yet."""
    msg = "No render function implemented yet -- replace `_not_implemented` with your own in this module."
    raise NotImplementedError(msg)


def _render_runner_utilization(table: pa.Table, output_dir: Path) -> None:
    from cimon.visualization.renderers.runner_utilization import render  # noqa: PLC0415

    render(table, output_dir)


register(
    Visualization(
        name="runner-utilization",
        spec_path=SPECS_DIR / "runner_utilization.yml",
        render=_render_runner_utilization,
    ),
)
