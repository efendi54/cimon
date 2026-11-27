## v0.2.0 (2026-11-27)

### Feat

- introduce pypi to be default index for dependency resolution (give hints how to switch to alternatives)

## v0.1.0 (2026-11-27)

### Feat

- fix sync script call + adapt README.md content to current functionality
- introduce further ability to pass in (text) sql query (besides the ability to pass in spec file)
- rework metrics sub-command + remove elder visualizations
- extend printing runner information with showing assigned group-labels + remove explicit authentification in sync scripts
- support multiple workflows in cimon sync
- introduce plot analysis towards workflow runs as new visualization (reworked visualization)
- rework authentication towards taking .netrc into account (primarily)
- rename to cyclic-sync.sh and introduce env variable for sync-script to be called
- prune-jobs works with dry-run and from-date can be omitted (defaults to 01-01-01)
- introduce / improve backfill_static_runner_workflows.sh
- introduce build-metrics rework (listing build-buddy links in report) + rework sync script not to close terminal in case of failures
- introduce download-log sub-command (former file-log sub-command)
- introduce log-filtering (download and filter logs of job runs and create a resulting parquet with jobs that have logs matching a pattern)
- build-metridcs sub-command introduction
- introduce runner availability as subcommand and cache-hit-rate plots
- remove callgraph feature + adapt today-filtering

### Fix

- no gh api quota consumption in case logs are available locally when approaching download
- use correct workflow-run-url for different attempts
- do not interrupt if log file could not be downloaded but proceed with next ones
- handle the breaching of the quota limit in between
- fix build-metrics

## v0.0.3 (2026-09-04)

### Feat

- introduce quota limit detection
- adapt plotting of merge-group failures for better visibility
- introduce head_branch column
- introduce better update of pr related data in the cache
- update also canceled workflow runs that were not within request window
- print active and completed job durations side-by-side
- introduce visualizations for runner occupation and job durations for all active pr and merge-group related workflow-runs
- introduce first visualization draft (via html generation) for active runner-labels over time
- introduce bash script for updating all pull-request and merge-group related workflows
- introduce new subcommand for cache-overview
- introduce querying with spec files (and direct python api) / created docs for specs
- introduce querying for certain workflow runs from current cache
- introduce etag_cache mechanism

### Fix

- set correct output directory for visualizations
- introduce new sub-command for corner cases where elder job runs
- handle workflow runs that were marked as completed in cache to fix wrong in-progress duration calculation
- adapt outputs towards cache modifications
- optimize api rate consumption + fix handling of runs with no jobs

## v0.0.2 (2026-08-26)

### Feat

- introduce runner-label/name detection and started_at and completed_at for jobs too
- introduce parquet / json synching + minor cleanups / fixes

## v0.0.1 (2026-08-25)

### Feat

- introduce parallel execution of fetching with waiting-spinner
- change directory where cache is saved
- allow "any" as workflow-status and workflow-conclusion values
- reformat
- only handle specific workflow runs with the provided status by passing status requirement through the endpoint
- extend build-metrics to support providing a json that contains all workflow-job-urls (still working with only a workflow-run-url be passed over)
- add script for collecting workflow-job data (as pre-step for further analysis)
- extend build-metrics data + make GH_HOST mandatory for find_workflows
- extend json with workflow job run related data
- extended find_workflows to support parallel execution
- adding metrics related scripts (wip)
- add activejobs command
- rename cli options + adapt README.md
- first shot

### Fix

- better print outputs
- handle wrong host and token better
- indent correctly
- handling of cache keys was wrong it did not recognize cache-hits
- adapt coding style
- adapt tooling name
- use pyrefly for type-checking in pre-commit context
- fix test calling
