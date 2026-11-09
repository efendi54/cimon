# ruff: noqa: CPY001
"""Registry of named visualizations: a filter spec paired with a render callback.

Each entry names a filter spec (see `cimon.workflows.query_spec`) that narrows
the workflows Parquet cache down to the rows one visualization cares about,
and a `render` function that turns the resulting `pa.Table` into the actual
visualization. `cimon.visualization.pipeline` runs the filter, writes the
result out as its own Parquet file, and then calls `render` -- so the
filtered subset stays inspectable/reusable independently of the render step.

Add a new visualization by adding a spec file under `specs/` and calling
`register()` with it below. A visualization that depends on parameters
(`cimon visualize <name> -p KEY=VALUE`) has no spec file: it passes a
`spec_builder` that builds the filter spec in code, plus its accepted `params`
(see `Visualization`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import pyarrow as pa

SPECS_DIR = Path(__file__).parent / "specs"


@dataclass(frozen=True)
class Visualization:
    """A named filter spec paired with the function that renders its result.

    Most visualizations use a static `spec_path` and a `render(table, output_dir)` callback. A *parametrized*
    visualization (e.g. one specific workflow run) declares its accepted `params` (name -> help text, passed via
    `cimon visualize -p NAME=VALUE`), builds its filter spec from them with `spec_builder(params)`, and its `render`
    is called as `render(table, output_dir, params)`. `spec_builder`/`render` raise `ValueError` for bad parameters
    or unusable data.
    """

    name: str
    spec_path: Path | None
    render: Callable[..., None]
    columns: tuple[str, ...] | None = None
    spec_builder: Callable[[Mapping[str, str]], Mapping[str, Any]] | None = None
    params: Mapping[str, str] = field(default_factory=dict)


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


def _render_job_durations(table: pa.Table, output_dir: Path) -> None:
    from cimon.visualization.renderers.job_durations import render  # noqa: PLC0415

    render(table, output_dir)


def _render_merge_group_failures(table: pa.Table, output_dir: Path) -> None:
    from cimon.visualization.renderers.merge_group_failures import (
        render,  # noqa: PLC0415
    )

    render(table, output_dir)


def _render_runner_status_trend(table: pa.Table, output_dir: Path) -> None:
    from cimon.visualization.renderers.runner_status_trend import (
        render,  # noqa: PLC0415
    )

    render(table, output_dir)


def _workflow_job_durations_spec(params: Mapping[str, str]) -> Mapping[str, Any]:
    from cimon.visualization.renderers.run_timeline import spec_for_run  # noqa: PLC0415

    return spec_for_run(params)


def _render_workflow_job_durations(table: pa.Table, output_dir: Path, params: Mapping[str, str]) -> None:
    from cimon.visualization.renderers.run_timeline import render_run  # noqa: PLC0415

    render_run(table, output_dir, params)


register(
    Visualization(
        name="job-durations",
        spec_path=SPECS_DIR / "job_durations.yml",
        render=_render_job_durations,
    ),
)

register(
    Visualization(
        name="merge-group-failures",
        spec_path=SPECS_DIR / "merge_group_failures.yml",
        render=_render_merge_group_failures,
    ),
)

register(
    Visualization(
        name="runner-status-trend",
        spec_path=SPECS_DIR / "runner_status_trend.yml",
        render=_render_runner_status_trend,
    ),
)

register(
    Visualization(
        name="workflow-job-durations",
        spec_path=None,
        spec_builder=_workflow_job_durations_spec,
        render=_render_workflow_job_durations,
        params={
            "run-url": "workflow run URL, alternatively use run-urls-file (default: latest cached attempt)",
            "run-urls-file": "Parquet job data including workflow_run_url; alternative to run-url, no -i needed",
            "individual-html": "write per-run pages for Parquet selection: true (default) or false",
            "tz": "time zone for plotting (default: Europe/Berlin)",
            "gap": "tolerance in seconds when chaining jobs on the critical path (default: 120)",
        },
    ),
)
