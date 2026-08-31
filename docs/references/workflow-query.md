# Querying the workflows Parquet cache

`cimon` caches GitHub Actions workflow-run and job data as a single denormalized
Parquet file (`workflows.parquet`, one row per run **and job**). `WorkflowQuery`
and its declarative spec format let you filter that cache quickly and reuse
the same filter definitions from Python code, a CLI call, or a config file.

## Why this design

- **Fast**: filters compile down to `pyarrow.dataset` expressions, so
  predicate evaluation and column selection are pushed down into the Parquet
  reader (row groups that can't match are skipped, unused columns are never
  read).
- **Convenient**: a fluent Python builder (`WorkflowQuery`) for ad-hoc use,
  plus a declarative spec (dict / YAML / JSON) for anything that needs to be
  supplied dynamically -- e.g. a CLI flag or a saved file -- without writing
  Python code and without `eval()`/`exec()` (only a fixed set of known
  operators is recognized, so specs are safe to load from a file).
- **Extensible**: new filters are either one-line wrappers around a handful of
  generic building blocks (`filter`, `isin`, `between`, ...), or -- for
  anything not covered -- an escape hatch that accepts an arbitrary
  `pyarrow.dataset` expression.

## Row granularity

Each row is one **job** belonging to one **run**. Run-level columns
(`run_id`, `workflow_status`, `workflow_conclusion`, `created_at`, ...) repeat
across every job row of the same run; job-level columns (`job_id`,
`job_name`, `job_status`, `job_conclusion`, `job_duration_sec`,
`job_active_duration_sec`, `job_runner_name`, `job_runner_labels`, ...)
describe that specific job.

`job_duration_sec` is only set once a job has `completed_at` (its final
duration). `job_active_duration_sec` is set as soon as a job has started: it
equals `job_duration_sec` once completed, and for a still-running job it's
the time elapsed between `job_started_at` and that sync's own
`cache_updated_at` -- not "now", so it stays accurate even if read long after
a stale cache's last sync.

A filter on `job_status`/`job_runner_labels` therefore matches individual job
rows, not automatically every other job of the same run -- a run can well be
`in_progress` overall while the specific job row you're looking at already
`completed`.

## Python API

```python
from cimon.workflows.query import WorkflowQuery

rows = (
    WorkflowQuery("workflows.parquet")
    .job_status("in_progress")
    .runner_label("app-adas-src")
    .columns("run_id", "job_name", "job_runner_labels")
    .to_pylist()
)
```

Every filter method returns `self`, so calls chain, and each one is
AND-combined with any previous filter. `columns()` restricts which columns
are read from disk. Nothing touches disk until `to_table()`/`to_pylist()` is
called.

Built-in shortcuts: `run_id`, `workflow_conclusion`, `job_conclusion`,
`job_name`, `job_status`, `runner_label`, `created_between`. Generic building
blocks usable for anything else: `where(**equals)`, `isin(column, values)`,
`between(column, low, high)`, `not_null(column)`, and the escape hatch
`filter(expr)` for an arbitrary `pyarrow.dataset` expression.

## Declarative spec (YAML/JSON)

A filter can also be described as data instead of Python code -- see
`cimon.workflows.query_spec` -- and applied via `WorkflowQuery.filter_spec(spec)`
or the `cimon query` CLI command.

### Shape

```yaml
{"all": [<condition-or-group>, ...]}          # AND
{"any": [<condition-or-group>, ...]}          # OR
{"not": <condition-or-group>}                 # NOT
{"column": "...", "op": "eq", "value": ...}   # leaf condition
```

`all`/`any`/`not` can be nested arbitrarily deep.

### Supported operators

| op             | meaning                                              |
|-----------------|-------------------------------------------------------|
| `eq` / `ne`     | equal / not equal                                     |
| `gt` / `ge` / `lt` / `le` | greater/less than (or equal)                 |
| `in`            | value is one of a given list                          |
| `between`       | value is within `[low, high]` (inclusive)              |
| `is_null` / `is_not_null` | column is / isn't null                    |
| `starts_with`   | string column starts with a given prefix               |
| `contains`      | string column contains a given substring anywhere      |
| `list_contains` | list column (e.g. `job_runner_labels`) contains a value exactly (not just as a substring) |

All operators compile to `pyarrow.dataset` expressions, so they stay
pushdown-fast even for `list_contains` (implemented via a join + regex
match under the hood, but still a single expression evaluated by the
Parquet/Arrow engine, not a Python-level post-filter).

## Example specs

Runs that failed in a given month:

```yaml
all:
  - column: workflow_conclusion
    op: eq
    value: failure
  - column: created_at
    op: between
    value: ["2026-08-01T00:00:00Z", "2026-08-31T23:59:59Z"]
```

Successful jobs whose name only partially matches, in a date range
(see [filter_by_job_name.yml](../../tests/example_specs/filter_by_job_name.yml)):

```yaml
all:
  - column: created_at
    op: between
    value: ["2026-08-01T00:00:00Z", "2026-08-31T23:59:59Z"]
  - column: job_conclusion
    op: eq
    value: success
  - column: job_name
    op: contains
    value: "Build"
```

Several alternative job-name prefixes (OR):

```yaml
any:
  - column: job_name
    op: starts_with
    value: "Build Some"
  - column: job_name
    op: starts_with
    value: "Build Other"
```

Still-running jobs on a specific runner label
(see [filter_active_runners_by_label_.yml](../../tests/example_specs/filter_active_runners_by_label_.yml)):

```yaml
all:
  - column: job_status
    op: eq
    value: in_progress
  - column: job_runner_labels
    op: list_contains
    value: "app-adas-src"
```

Failed run OR (cancelled job OR very long job), demonstrating nesting:

```yaml
all:
  - column: workflow_status
    op: eq
    value: completed
  - any:
      - column: job_conclusion
        op: eq
        value: cancelled
      - column: job_duration_sec
        op: gt
        value: 600
```

## CLI usage

```bash
cimon query \
  --input workflows.parquet \
  --spec spec.yaml \
  --output filtered.parquet \
  --column run_id --column job_name --column job_runner_labels
```

Reads the spec file (YAML or JSON), filters `--input`, optionally restricts
the output to the given `--column` values (repeatable), and atomically writes
the result to `--output` as a new Parquet file -- ready to be picked up by a
visualization or statistics step (e.g. via `table.to_pandas()`).
