# ruff: noqa: CPY001
"""CI Monitor CLI tool for generating call graphs of GitHub workflows and other stuff."""

import logging
import os
import sys
import tempfile
from pathlib import Path

import click
import pyarrow.parquet as pq
import yaml

import requests
from cimon.call_graph.call_graph import MermaidCallGraphMode, generate_mermaid_graph
from cimon.github_api import create_session, print_quota
from cimon.monitoring.active_jobs import active_jobs
from cimon.workflows import synch
from cimon.workflows.query import WorkflowQuery

logger = logging.getLogger(__name__)


@click.group()
@click.option(
    "-l",
    "--log-level",
    default="info",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
)
@click.option(
    "--log-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default="cimon.log",
    help="Also write log messages to this UTF-8 file. Applies to all cimon commands.",
)
def main(log_level: str, log_file: Path | None) -> None:
    """Main entry point for the GitHub Workflow Visualizer CLI."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s: %(message)s",
        handlers=handlers,
        force=True,
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
    "-d",
    "--deep",
    is_flag=True,
    help="""If true will generate subgraphs for reusable workflows, otherwise will generate a shallow call-graph.""",
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
    deep: bool,
    output_folder: Path,
) -> None:
    """Command to show a call graph of the workflow."""
    logger.debug("Using GitHub host URL: %s", host_url)
    logger.debug("Using repository name: %s", repo_name)

    callgraph_mode = MermaidCallGraphMode.DEEP if deep else MermaidCallGraphMode.SHALLOW
    generate_mermaid_graph(wf_path=Path(workflow_path), out_path=output_folder, callgraph_mode=callgraph_mode)

@main.command()
def jobmon() -> None:
    """Showing actively running jobs in the CI."""
    logger.debug("Job monitoring is not yet implemented.")
    active_jobs()


@main.command()
@click.option(
    "--token",
    envvar=["GH_TOKEN", "GITHUB_TOKEN"],
    required=False,
    help="GitHub API token. Defaults to GH_TOKEN or GITHUB_TOKEN.",
)
@click.option(
    "--host",
    envvar="GH_HOST",
    required=False,
    help="GitHub API host, e.g. git.hub.vwgroup.com. Defaults to GH_HOST.",
)
def quota(token: str | None, host: str | None) -> None:
    """Show the GitHub API quota."""
    if not token:
        msg = (
            "No token provided and no environment variable set. "
            "Please set GH_TOKEN or GITHUB_TOKEN."
        )
        raise click.UsageError(msg)

    if not host:
        msg = "No host provided and no environment variable set. Please set GH_HOST."
        raise click.UsageError(msg)

    try:
        session = create_session(token)
        print_quota(session, f"https://{host}/api/v3")
    except requests.RequestException:
        logger.exception("Failed to fetch GitHub API quota")
        raise click.Abort from None


@main.command(
    "sync",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    add_help_option=False,
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def sync(args: tuple[str, ...]) -> None:
    """Run the workflow-run cache synchronizer."""
    synch.main(list(args))


@main.command("query")
@click.option(
    "-i",
    "--input",
    "input_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="Path to the workflows Parquet cache to filter.",
)
@click.option(
    "-s",
    "--spec",
    "spec_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="YAML or JSON file describing the filter (see cimon.workflows.query_spec).",
)
@click.option(
    "-o",
    "--output",
    "output_path",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where to write the filtered rows as a new Parquet file.",
)
@click.option(
    "-c",
    "--column",
    "output_columns",
    multiple=True,
    help="Restrict the output to this column. Can be given multiple times.",
)
def query(
    input_path: Path,
    spec_path: Path,
    output_path: Path,
    output_columns: tuple[str, ...],
) -> None:
    """Filter the workflows Parquet cache with a declarative spec file into a new Parquet file."""
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    workflow_query = WorkflowQuery(input_path).filter_spec(spec)
    if output_columns:
        workflow_query = workflow_query.columns(*output_columns)

    table = workflow_query.to_table()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=output_path.parent, prefix=".query_", suffix=".parquet")
    os.close(fd)
    try:
        pq.write_table(table, tmp_path)
        Path(tmp_path).replace(output_path)
    finally:
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()

    logger.info(f"Wrote {table.num_rows} filtered row(s) to {output_path}")


if __name__ == "__main__":
    main()
