"""
GitHub Workflow Visualizer.

A tool to visualize GitHub Actions workflows by generating call graphs in Mermaid syntax.
"""

import logging
import sys
from pathlib import Path

import click

from gh_wf_visu.call_graph import MermaidCallGraphMode, generate_mermaid_graph

logger = logging.getLogger(__name__)


@click.group()
@click.option(
    "-l",
    "--log-level",
    default="info",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
)
def main(log_level: str) -> None:
    """Main entry point for the GitHub Workflow Visualizer CLI."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(levelname)s: %(message)s",
    )
    logger.info(f"Command line: {' '.join(sys.argv)}")


@main.command()
@click.option(
    "-h",
    "--host_url",
    envvar="GH_HOST_URL",
    type=str,
    required=False,
    default="git.hub.vwgroup.com",
    help="""GitHub host URL, e.g., 'git.hub.vwgroup.com'.
    The value can be set via the 'GH_HOST_URL' environment variable.""",
)
@click.option(
    "-r",
    "--repo_name",
    envvar="GH_REPO_NAME",
    type=str,
    required=False,
    default="CARIAD/app-adas-src",
    help="""GitHub repository name in the format 'owner/repo'.
    The value can be set via the 'GH_REPO_NAME' environment variable.""",
)
@click.option(
    "-w",
    "--workflow_path",
    envvar="WORKFLOW_PATH",
    type=click.Path(exists=True, file_okay=True, dir_okay=True, readable=True, path_type=Path),
    default=Path(".github/workflows"),
    help="""Relevant Workflow directory or file to use.
    The value can be set via the 'WORKFLOW_PATH' environment variable.""",
)
@click.option(
    "-s",
    "--subgraphs",
    is_flag=True,
    help="""Whether to generate subgraphs for reusable workflows.""",
)
@click.option(
    "-o",
    "--output_folder",
    envvar="OUTPUT_FOLDER",
    type=click.Path(exists=False, file_okay=False, dir_okay=True, readable=True, path_type=Path),
    default=Path("./out/call_graphs"),
    help="""Output folder where the generated markdown file(s) will be put into.""",
)
def callgraph(
    host_url: str,
    repo_name: str,
    workflow_path: str,
    *,
    subgraphs: bool,
    output_folder: Path,
) -> None:
    """Command to show a call graph of the workflow."""
    logger.debug("Using GitHub host URL: %s", host_url)
    logger.debug("Using repository name: %s", repo_name)

    callgraph_mode = MermaidCallGraphMode.DEEP if subgraphs else MermaidCallGraphMode.SHALLOW
    generate_mermaid_graph(wf_path=Path(workflow_path), out_path=output_folder, callgraph_mode=callgraph_mode)


if __name__ == "__main__":
    main()
