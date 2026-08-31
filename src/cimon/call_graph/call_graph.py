"""
call_graph.py.

Module to generate Mermaid call graphs for GitHub Actions workflows.
"""

import logging
from enum import Enum
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

branch = "main"
blob_url = f"https://git.hub.vwgroup.com/CARIAD/app-adas-src/blob/{branch}/"


class MermaidCallGraphMode(Enum):
    """Defines the mode for generating Mermaid call graphs."""

    DEEP = "deep"
    """Generate a single graph with all workflows and their calls."""

    SHALLOW = "shallow"
    """Generate separate graphs for each workflow, showing only direct calls."""


def sanitize_name(name: str) -> str:
    """Mermaid-safe node id."""
    return name.replace("-", "_").replace(" ", "_").replace("@", "_").replace("/", "_").replace(".", "_")


def generate_mermaid_graph(
    wf_path: Path,
    out_path: Path,
    callgraph_mode: MermaidCallGraphMode = MermaidCallGraphMode.SHALLOW,
) -> None:
    """Generates a Mermaid call graph for the given workflow file."""
    lines = [f"# [{wf_path.name}]({blob_url}{wf_path})"]
    lines.extend(["```mermaid", "flowchart TD"])

    callgraph_lines = build_graph(
        path=wf_path.resolve(),
        out_path=out_path,
        callgraph_mode=callgraph_mode,
        expanded_workflows=set(),
        active_stack=set(),
    )

    lines.extend(callgraph_lines)
    lines.append("```")

    out_path.mkdir(parents=True, exist_ok=True)
    out_file = out_path / f"cg_{wf_path.stem}.md"
    out_file.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"{wf_path} --> {out_file}")


# ruff: noqa: PLR0915
def build_graph(
    path: Path,
    out_path: Path,
    recursion_depth: int = 0,
    callgraph_mode: MermaidCallGraphMode = MermaidCallGraphMode.SHALLOW,
    expanded_workflows: set[str] | None = None,
    active_stack: set[str] | None = None,
) -> list[str]:
    """Recursively builds Mermaid graph lines for the given workflow file."""
    if expanded_workflows is None:
        expanded_workflows = set()

    if active_stack is None:
        active_stack = set()

    workflow_id = str(path.resolve())

    if workflow_id in active_stack:
        logger.warning("Cycle detected: %s", workflow_id)
        return []

    active_stack.add(workflow_id)

    try:
        wf = yaml.safe_load(path.read_text(encoding="utf-8"))
        jobs = wf.get("jobs", {})

        depth_padding = "    " * recursion_depth

        def create_job_id(job_name: str) -> str:
            return f"{sanitize_name(path.stem)}_{sanitize_name(job_name)}"

        all_job_ids = {name: create_job_id(name) for name in jobs}

        used_nodes = {}
        lines = []

        for job_name, job in jobs.items():
            # create node for the job
            job_id = all_job_ids[job_name]
            lines.append(f'{depth_padding}{job_id}["{job_name}"]')

            # handle potential needs
            needs = job.get("needs", [])
            if isinstance(needs, str):
                needs = [needs]

            lines.extend([f"{depth_padding}{all_job_ids[need]} --> {job_id}" for need in needs if need in all_job_ids])

            # handle reusable workflow
            used_wf_file = job.get("uses")
            if not used_wf_file:
                continue

            used_node = sanitize_name(f"used_{used_wf_file}")
            used_nodes[used_node] = used_wf_file
            uses_path = (path.parent / Path(used_wf_file).name).resolve()
            logger.debug(f"Resolved '{used_wf_file}' -> {uses_path} (exists={uses_path.exists()})")

            add_plain_node = True

            if callgraph_mode == MermaidCallGraphMode.DEEP and uses_path and uses_path.exists():
                used_workflow_id = str(uses_path.resolve())

                # only expand if not already expanded in the current call stack
                if used_workflow_id not in expanded_workflows:
                    expanded_workflows.add(used_workflow_id)

                    subgraph_lines = build_graph(
                        path=uses_path,
                        out_path=out_path,
                        recursion_depth=recursion_depth + 1,
                        callgraph_mode=callgraph_mode,
                        expanded_workflows=expanded_workflows,
                        active_stack=active_stack,
                    )

                    lines.append(f'{depth_padding}subgraph {used_node} ["{used_wf_file}"]')

                    lines.extend(subgraph_lines)
                    lines.append(f"{depth_padding}end")
                    add_plain_node = False

            elif callgraph_mode == MermaidCallGraphMode.SHALLOW and uses_path and uses_path.exists():
                generate_mermaid_graph(
                    wf_path=uses_path,
                    out_path=out_path,
                    callgraph_mode=MermaidCallGraphMode.SHALLOW,
                )

            if add_plain_node:
                lines.append(f'{depth_padding}{used_node}["{used_wf_file}"]')

            lines.append(f"{depth_padding}{job_id} -->|{job_name}| {used_node}")

        # style nodes
        if all_job_ids:
            lines.append("")
            lines.append(f"{depth_padding}classDef jobNode fill:#add8e6,stroke:#333,color:#000;")
            lines.append(f"{depth_padding}class {','.join(all_job_ids.values())} jobNode;")

        if used_nodes:
            lines.append(f"{depth_padding}classDef usesNode fill:#ffa500,stroke:#333,color:#000;")
            lines.append(f"{depth_padding}class {','.join(used_nodes.keys())} usesNode;")

        return lines

    finally:
        active_stack.remove(workflow_id)
