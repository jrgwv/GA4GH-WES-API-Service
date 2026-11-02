"""Schemas for the run failure diagnosis extension."""

from pydantic import BaseModel, Field


class FailedTask(BaseModel):
    """A task the execution engine reports as failed or cancelled."""

    name: str | None = None
    status: str | None = None
    message: str = ""


class RunDiagnosis(BaseModel):
    """Why a workflow run failed, as reported by the execution engine (AWS HealthOmics)."""

    run_id: str = Field(..., description="Workflow run ID")
    state: str | None = Field(None, description="Current WES state")
    available: bool = Field(
        ..., description="True when engine details could be read for this run"
    )
    engine_run_id: str | None = Field(None, description="Run ID in the execution engine")
    engine_status: str | None = None
    failure_reason: str | None = None
    message: str = ""
    failed_tasks: list[FailedTask] = Field(default_factory=list)
    log_tail: list[str] = Field(default_factory=list)
    note: str | None = Field(None, description="Why details are missing, when available is false")
    hint: str | None = Field(None, description="A likely next step for this failure reason, when one is known")
