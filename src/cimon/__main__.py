# ruff: noqa: CPY001
"""CI Monitor CLI tool for tracking GitHub Actions workflow runs and other stuff."""

import logging
import sys
from pathlib import Path

import click
import yaml

import requests
from cimon.github_api import QuotaLimitReachedError, create_session, print_quota
from cimon.parquet_io import write_table_atomic
from cimon.visualization import pipeline, registry
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
    try:
        synch.main(list(args))
    except QuotaLimitReachedError:
        logger.exception("Sync aborted")
        raise click.Abort from None


@main.command("cache-info")
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path.home() / ".cache/cimon",
    help="Directory where caching related files are stored.",
)
def cache_info(cache_dir: Path) -> None:
    """Show the current workflow-run Parquet cache: per workflow_file created_at range, status and conclusion counts."""
    synch.print_cache_info(cache_dir)


@main.command("repair-cache")
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path.home() / ".cache/cimon",
    help="Directory where caching related files are stored.",
)
def repair_cache(cache_dir: Path) -> None:
    """Recompute job_duration_sec/job_active_duration_sec for the whole Parquet cache locally (no GitHub API calls)."""
    parquet_file = Path(cache_dir).resolve() / synch.WORKFLOWS_PARQUET_FILE_NAME
    changed = synch.repair_job_durations(str(parquet_file))
    logger.info(f"Repaired {changed} value(s) in {parquet_file}")


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
    write_table_atomic(table, output_path)

    logger.info(f"Wrote {table.num_rows} filtered row(s) to {output_path}")


@main.command("visualize")
@click.argument("names", nargs=-1)
@click.option(
    "-i",
    "--input",
    "input_path",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="Path to the workflows Parquet cache to filter.",
)
@click.option(
    "-o",
    "--output-dir",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./out/visualizations"),
    help="""Directory to write the filtered Parquet file (and any rendered output) into.""",
)
@click.option(
    "-l",
    "--list",
    "list_only",
    is_flag=True,
    help="List the registered visualization names and exit.",
)
def visualize(names: tuple[str, ...], input_path: Path | None, output_dir: Path, *, list_only: bool) -> None:
    """Filter the workflows Parquet cache per one or more registered visualizations' specs, then render them.

    NAMES can be given more than once (e.g. `cimon visualize job-durations
    merge-group-failures`) to run several visualizations in one call.

    See cimon.visualization.registry for the list of registered visualizations
    and how to add new ones.
    """
    if list_only:
        click.echo("\n".join(registry.available()))
        return

    if not names:
        msg = "Missing argument 'NAMES'. Use --list to see available visualizations."
        raise click.UsageError(msg)
    if not input_path:
        msg = "Missing option '-i' / '--input'."
        raise click.UsageError(msg)

    try:
        for name in names:
            pipeline.run(name, input_path, output_dir)
    except KeyError as exc:
        raise click.UsageError(str(exc)) from None


if __name__ == "__main__":
    main()
