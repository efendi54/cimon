# ruff: noqa: CPY001
"""CI Monitor CLI tool for tracking GitHub Actions workflow runs and other stuff."""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click
import pyarrow.dataset as ds
import requests
import yaml

from cimon.github_api import (
    RUNNER_STATUS_PARQUET_FILE_NAME,
    RUNNER_STATUS_SNAPSHOTS_DIR_NAME,
    QuotaLimitReachedError,
    append_runner_snapshot,
    create_session,
    fetch_quota,
    list_runners,
    print_quota,
    print_runner_status,
    render_runner_status_chart,
    resolve_token,
)
from cimon.parquet_io import write_table_atomic
from cimon.visualization import pipeline
from cimon.workflows import synch
from cimon.workflows.build_metrics import process_input as run_build_metrics
from cimon.workflows.download_logs import run as run_download_logs
from cimon.workflows.query import WorkflowQuery

if TYPE_CHECKING:
    from pyarrow import Table

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
    # `cimon quota --json` must print a single, clean JSON line on stdout for
    # scripts to parse (e.g. `cimon quota --json | jq -r '.reset'`) -- keep
    # this invocation's log messages (including the "Command line: ..." line
    # right below) off stdout entirely in that case, still writing them to
    # --log-file as usual.
    json_quota_mode = "quota" in sys.argv and "--json" in sys.argv
    handlers: list[logging.Handler] = (
        [] if json_quota_mode else [logging.StreamHandler(sys.stdout)]
    )
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
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Print the quota as a single JSON object on stdout (keys: limit, "
    "used, remaining, reset [Unix timestamp]) instead of a human-readable "
    "summary -- e.g. for `cimon quota --json | jq -r '.reset'` in scripts.",
)
def quota(token: str | None, host: str | None, *, as_json: bool) -> None:
    """Show the GitHub API quota."""
    if not host:
        msg = "No host provided and no environment variable set. Please set GH_HOST."
        raise click.UsageError(msg)

    token = resolve_token(host, token)
    if not token:
        msg = (
            "No ~/.netrc entry for the host and no token provided. "
            "Please set GH_TOKEN or GITHUB_TOKEN."
        )
        raise click.UsageError(msg)

    try:
        session = create_session(token)
        if as_json:
            # No stdout log handler is configured for this invocation (see
            # main()), so this is the only thing printed on stdout.
            click.echo(json.dumps(fetch_quota(session, f"https://{host}/api/v3")))
        else:
            print_quota(session, f"https://{host}/api/v3")
    except requests.RequestException:
        logger.exception("Failed to fetch GitHub API quota")
        raise click.Abort from None


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
@click.option("--org", help="Organization to list self-hosted runners for, e.g. CAS.")
@click.option(
    "--owner", help="Repository owner, used with --repo if --org is not given."
)
@click.option(
    "--repo", help="Repository name, used with --owner if --org is not given."
)
@click.option(
    "-o",
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Also write an HTML treemap of runner labels/names colored by status to this file.",
)
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path.home() / ".cache/cimon",
    help="Directory to record this call's runner-status snapshot in "
    "(runner_status.parquet plus a timestamped JSON file under runner_status_snapshots/).",
)
def runners(
    token: str | None,
    host: str | None,
    org: str | None,
    owner: str | None,
    repo: str | None,
    output_path: Path | None,
    cache_dir: Path,
) -> None:
    """Show self-hosted runner status: online/offline counts, and which online runners are busy."""
    if not host:
        msg = "No host provided and no environment variable set. Please set GH_HOST."
        raise click.UsageError(msg)

    token = resolve_token(host, token)
    if not token:
        msg = (
            "No ~/.netrc entry for the host and no token provided. "
            "Please set GH_TOKEN or GITHUB_TOKEN."
        )
        raise click.UsageError(msg)

    if not org and not (owner and repo):
        msg = "Provide either --org, or both --owner and --repo."
        raise click.UsageError(msg)

    try:
        session = create_session(token)
        runner_list = list_runners(
            session, f"https://{host}/api/v3", org=org, owner=owner, repo=repo
        )
        print_runner_status(runner_list)

        snapshot_file = append_runner_snapshot(
            runner_list,
            cache_dir / RUNNER_STATUS_PARQUET_FILE_NAME,
            cache_dir / RUNNER_STATUS_SNAPSHOTS_DIR_NAME,
        )
        logger.info(f"Wrote snapshot {snapshot_file}")

        if output_path:
            render_runner_status_chart(runner_list, output_path)
            logger.info(f"Wrote {output_path}")
    except requests.RequestException:
        logger.exception("Failed to fetch runner status")
        raise click.Abort from None


@main.group("metrics")
def metrics() -> None:
    """Build-cache and workflow-run metrics: extract or visualize them."""


@metrics.command("build-metrics")
@click.argument("input_arg", metavar="INPUT")
@click.argument(
    "output_dir",
    metavar="[OUTPUT_DIR]",
    required=False,
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("/tmp"),
)
def build_metrics(input_arg: str, output_dir: Path) -> None:
    """Extract bazel build cache-hit-rate/duration metrics from job log(s).

    INPUT is a workflow-job URL, a JSON file listing job runs, or a Parquet
    file with a job_url column (e.g. from 'cimon query -c job_url'). Given
    multiple jobs, also writes an aggregated cache-hit-rate-trend.html
    and runner-cache-health.html. See cimon.workflows.build_metrics for
    details. OUTPUT_DIR defaults to /tmp.
    """
    run_build_metrics(input_arg, output_dir)


@main.command("download-logs")
@click.option(
    "-i",
    "--input",
    "input_arg",
    required=True,
    help="A workflow-job URL, a JSON file of URL strings, or a Parquet file with a job_url "
    "column (e.g. from 'cimon query -c job_url').",
)
@click.option(
    "-p",
    "--pattern",
    help="Regular expression to search for in each job's downloaded log. If given, only jobs "
    "whose log matches are written to log-match.parquet in OUTPUT_DIR. If omitted, logs are "
    "simply downloaded and no output file is written.",
)
@click.option(
    "-o",
    "--output-dir",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./out/download-logs"),
    help="Directory to download logs into (and write log-match.parquet into, if --pattern "
    "matched any job).",
)
@click.option(
    "--clear-logs",
    is_flag=True,
    help="Only valid together with --pattern: once every job's log has been checked, delete "
    "the logs of jobs whose log did NOT match --pattern, keeping only the matching ones on "
    "disk. Without this flag (the default), every downloaded log is always kept, matched or "
    "not.",
)
def download_logs(
    input_arg: str, pattern: str | None, output_dir: Path, *, clear_logs: bool
) -> None:
    """Download each job's log from INPUT, optionally keeping only the ones matching PATTERN.

    INPUT is a workflow-job URL, a JSON file listing job URLs, or a Parquet
    file with a job_url column. Downloads every job's log into
    OUTPUT_DIR/logs/<run_id>/<job_id>.log -- always kept, so a later call
    over the same/overlapping input can skip logs it already downloaded. If
    --pattern is given, each log is also searched for it (a regular
    expression) and every matching job is written to log-match.parquet in
    OUTPUT_DIR (keeping all original columns for Parquet input); if nothing
    matched, no output file is written and a warning is logged instead.
    --clear-logs (only meaningful together with --pattern) deletes every
    downloaded log that did NOT match again once the analysis is done.
    """
    try:
        run_download_logs(input_arg, output_dir, pattern, clear_logs=clear_logs)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None


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


def _date_boundary_option(value: str) -> str:
    """Adapt `synch.parse_date_boundary` (an argparse `type=`) for use as a click option type."""
    try:
        return synch.parse_date_boundary(value)
    except argparse.ArgumentTypeError as exc:
        raise click.BadParameter(str(exc)) from exc


@main.command("prune-runs")
@click.argument(
    "parquet_file",
    type=click.Path(exists=True, dir_okay=False, writable=True, path_type=Path),
)
@click.option(
    "--from-date",
    default="0001-01-01",
    type=_date_boundary_option,
    metavar="DATE",
    help="Start of the created-at range to remove: YYYY-MM-DD, or an "
    "ISO-8601 timestamp (e.g. 2026-08-25T08:00:00Z) for hour/minute "
    "precision. Inclusive, same semantics as `cimon sync`'s --from-date. "
    "Defaults to 0001-01-01 (all earlier runs).",
)
@click.option(
    "--to-date",
    required=True,
    type=_date_boundary_option,
    metavar="DATE",
    help="End of the created-at range to remove: YYYY-MM-DD, or an "
    "ISO-8601 timestamp. Inclusive, same semantics as `cimon sync`'s --to-date.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show how many rows would be removed without modifying the Parquet file.",
)
def prune_runs(parquet_file: Path, from_date: str, to_date: str, dry_run: bool) -> None:
    """Remove every run created within [--from-date, --to-date] from a workflows Parquet cache, in place.

    PARQUET_FILE is the workflows Parquet cache to modify (e.g.
    ~/.cache/cimon/workflows.parquet). Matching runs (all of their job rows)
    are removed; everything else is kept unchanged. Use --dry-run to preview
    the number of rows that would be removed without changing the file.
    """
    before = ds.dataset(parquet_file, format="parquet").count_rows()

    table = WorkflowQuery(parquet_file).created_outside(from_date, to_date).to_table()
    removed = before - table.num_rows

    if dry_run:
        logger.info(
            f"Dry run: would remove {removed} row(s) created between {from_date} "
            f"and {to_date} from {parquet_file} ({table.num_rows} row(s) would remain); "
            "the file was not modified.",
        )
        return

    write_table_atomic(table, parquet_file)
    logger.info(
        f"Removed {removed} row(s) created between {from_date} and {to_date} "
        f"from {parquet_file} ({table.num_rows} row(s) remain).",
    )


def _query_sql(sql_path: Path, input_path: Path) -> "Table":
    try:
        import duckdb  # noqa: PLC0415
    except ImportError as exc:
        msg = "SQL queries require DuckDB. Use `uv run --extra viz cimon query ...`."
        raise click.ClickException(msg) from exc

    try:
        sql = sql_path.read_text(encoding="utf-8")
        with duckdb.connect(
            config={"enable_external_access": "false", "python_enable_replacements": "false"},
        ) as connection:
            statements = connection.extract_statements(sql)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                msg = "--sql-file must contain exactly one SELECT query."
                raise click.UsageError(msg)

            connection.register("source", ds.dataset(input_path, format="parquet"))
            return connection.execute(statements[0]).to_arrow_table()
    except (duckdb.Error, OSError, ValueError) as exc:
        raise click.ClickException(f"SQL query failed: {exc}") from exc


@main.command("query")
@click.argument(
    "spec_paths",
    metavar="SPECS...",
    nargs=-1,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
)
@click.option(
    "--sql-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="Run one DuckDB SELECT query against the input as `source`, instead of using SPECS.",
)
@click.option(
    "-i",
    "--input",
    "input_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="Path to the workflows Parquet cache to filter.",
)
@click.option(
    "-o",
    "--output",
    "output_path",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where to write the query result as a new Parquet file.",
)
@click.option(
    "-c",
    "--column",
    "output_columns",
    multiple=True,
    help="Restrict the output to this column. Can be given multiple times.",
)
def query(
    spec_paths: tuple[Path, ...],
    sql_file: Path | None,
    input_path: Path,
    output_path: Path,
    output_columns: tuple[str, ...],
) -> None:
    """Query the workflows Parquet cache and write the result as a new Parquet file.

    SPECS is one or more YAML/JSON files describing a filter (see
    cimon.workflows.query_spec). Given several, all of them must match
    (AND-combined) for a row to be kept. Use --sql-file instead for SQL.
    """
    if sql_file is not None:
        if spec_paths or output_columns:
            msg = "--sql-file cannot be combined with SPECS or --column."
            raise click.UsageError(msg)
        if output_path.resolve() == input_path.resolve():
            msg = "--output must be different from --input in SQL mode."
            raise click.UsageError(msg)
        table = _query_sql(sql_file, input_path)
    else:
        if not spec_paths:
            msg = "Provide at least one spec file or use --sql-file."
            raise click.UsageError(msg)
        workflow_query = WorkflowQuery(input_path)
        for spec_path in spec_paths:
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
            workflow_query = workflow_query.filter_spec(spec)
        if output_columns:
            workflow_query = workflow_query.columns(*output_columns)
        table = workflow_query.to_table()

    write_table_atomic(table, output_path)

    row_kind = "row(s)" if sql_file is not None else "filtered row(s)"
    logger.info(f"Wrote {table.num_rows} {row_kind} to {output_path}")


def _parse_params(raw_params: tuple[str, ...]) -> dict[str, str]:
    """Turn repeated `KEY=VALUE` options into a dict."""
    params: dict[str, str] = {}
    for raw in raw_params:
        key, separator, value = raw.partition("=")
        if not separator or not key:
            msg = f"Invalid parameter {raw!r}, expected KEY=VALUE."
            raise click.BadParameter(msg, param_hint="-p / --param")
        params[key] = value
    return params


@metrics.command("workflow-job-durations")
@click.option(
    "-i",
    "--input",
    "input_path",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="Path to the workflows Parquet cache to filter. Defaults to ~/.cache/cimon. "
    "Not used with -p run-urls-file (that file supplies the job data).",
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
    "-p",
    "--param",
    "raw_params",
    multiple=True,
    metavar="KEY=VALUE",
    help="Parameter, e.g. -p run-url=<URL>. Can be given multiple times. Accepted: run-url, "
    "run-urls-file, individual-html, tz, gap.",
)
def workflow_job_durations(
    input_path: Path | None,
    output_dir: Path,
    raw_params: tuple[str, ...],
) -> None:
    """Filter the workflows Parquet cache for one workflow run and render its job-duration timeline.

    Pass the run via -p run-url=https://<host>/<owner>/<repo>/actions/runs/<run_id>, or -p
    run-urls-file=<parquet> to average job phases across several selected runs instead. See
    cimon.visualization.registry's workflow-job-durations entry for all accepted -p parameters.
    """
    params = _parse_params(raw_params)
    try:
        pipeline.run("workflow-job-durations", input_path, output_dir, params)
    except KeyError as exc:
        raise click.UsageError(str(exc)) from None
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None


if __name__ == "__main__":
    main()
