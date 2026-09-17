# ruff: noqa: CPY001
"""Render each runner's recorded status/busy history over time via Plotly.

Requires the `viz` extra (`pandas`, `plotly`) -- imported lazily by the
registry so the rest of `cimon` keeps working without it installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pyarrow as pa

_COLORS = {
    "online (idle)": "#2ca02c",
    "online (busy)": "#ff7f0e",
    "offline": "#d62728",
}


def render(table: pa.Table, output_dir: Path) -> None:
    """Write `output_dir/runner-status-trend.html`, one row per runner name.

    Reads the snapshot history built up by `append_runner_snapshot()`. One
    row (y-category) per runner name, one marker per poll colored by state
    (offline/online-idle/online-busy) and connected by a thin line, so each
    runner's own timeline reads at a glance.
    """
    import pandas as pd  # noqa: PLC0415
    import plotly.graph_objects as go  # noqa: PLC0415

    output_path = output_dir / "runner-status-trend.html"

    frame = table.to_pandas()

    if frame.empty:
        go.Figure(
            layout={"title": "No runner-status snapshots recorded yet"}
        ).write_html(output_path)
        return

    frame["polled_at"] = pd.to_datetime(frame["polled_at"])
    frame["state"] = [
        "offline"
        if status != "online"
        else ("online (busy)" if busy else "online (idle)")
        for status, busy in zip(frame["status"], frame["busy"], strict=True)
    ]
    frame = frame.sort_values("polled_at")

    figure = go.Figure()

    runner_names = sorted(frame["runner_name"].unique())
    for runner_name in runner_names:
        runner_rows = frame[frame["runner_name"] == runner_name]
        figure.add_trace(
            go.Scatter(
                x=runner_rows["polled_at"],
                y=[runner_name] * len(runner_rows),
                mode="lines+markers",
                line={"color": "lightgray", "width": 1},
                marker={
                    "color": [_COLORS[state] for state in runner_rows["state"]],
                    "size": 10,
                },
                showlegend=False,
                hovertemplate="%{y}<br>%{x}<extra></extra>",
            ),
        )

    # Invisible points, only to add a color-key legend for the states above.
    for state, color in _COLORS.items():
        figure.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                marker={"color": color, "size": 10},
                name=state,
            ),
        )

    figure.update_layout(
        title="Runner status over time",
        height=max(300, 24 * len(runner_names) + 150),
        yaxis={"type": "category"},
    )
    figure.write_html(output_path)
