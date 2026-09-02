# ruff: noqa: CPY001
"""Pydantic models describing the persisted workflow-run cache (workflows.json)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class JobInfo(BaseModel):
    """A single workflow job, as stored in the cache."""

    id: int
    name: str
    html_url: str
    run_attempt: int | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_sec: int | None = None
    status: str
    conclusion: str | None = None
    runner_name: str | None = None
    runner_labels: list[str] | None = None


class JobEntry(BaseModel):
    """Wrapper matching the on-disk `{"job": {...}}` cache shape."""

    job: JobInfo


class RunEntry(BaseModel):
    """A single workflow run, including its cached jobs."""

    run_id: int
    run_number: int
    run_attempt: int | None = None
    workflow_run_url: str
    event: str | None = None
    workflow_status: str
    workflow_conclusion: str | None = None
    created_at: str
    cache_updated_at: str
    jobs: list[JobEntry] = Field(default_factory=list)


class WorkflowInfo(BaseModel):
    """Metadata identifying the monitored workflow."""

    owner: str
    repo: str
    host: str
    id: int
    name: str
    file: str


class WorkflowCache(BaseModel):
    """Top-level structure persisted to workflows.json."""

    workflow: WorkflowInfo | None = None
    runs: list[RunEntry] = Field(default_factory=list)
    updated_at: str | None = None
