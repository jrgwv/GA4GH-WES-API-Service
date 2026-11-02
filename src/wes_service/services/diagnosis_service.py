"""Explain why a workflow run failed, using read-only calls to the execution engine (AWS HealthOmics).

The WES record of a failed run says only that it failed. The reason (for example CANNOT_START_CONTAINER_ERROR
and the message that goes with it) lives in the engine. This service reads it on the server, so clients such as
the MCP server never need AWS access: they call this API with the user's own token.

Authorization: the engine run ID is taken from the WES run record, never from the caller, so only runs that
WES lets the caller read can be explained. The task role needs omics:GetRun, omics:ListRunTasks and
logs:GetLogEvents on the HealthOmics run log group, all read-only.
"""

import asyncio
import logging
import re
from typing import Any
from urllib.parse import unquote

import boto3

from src.wes_service.config import get_settings
from src.wes_service.db.models import WorkflowRun
from src.wes_service.schemas.diagnosis import FailedTask, RunDiagnosis

logger = logging.getLogger(__name__)

LOG_GROUP = "/aws/omics/WorkflowLog"
RUN_ID_RE = re.compile(r"^\d{1,20}$")
_URL_RUN_RE = re.compile(r"/log-events/run(?:%2F|/)(\d+)")
MAX_LOG_LINES = 25
MAX_LINE_CHARS = 300
MAX_TOTAL_CHARS = 3500


def engine_run_id_from_record(run: WorkflowRun) -> str | None:
    """The engine run ID stored on the WES record, or parsed from the log links WES stored in its outputs."""
    if run.workflow_run_id and RUN_ID_RE.match(str(run.workflow_run_id)):
        return str(run.workflow_run_id)
    urls = (run.outputs or {}).get("log_urls") or {}
    candidates = [urls.get("run_log")]
    task_logs = urls.get("task_logs") or {}
    if isinstance(task_logs, dict):
        candidates.extend(task_logs.values())
    for url in candidates:
        if isinstance(url, str):
            match = _URL_RUN_RE.search(url)
            if match:
                return match.group(1)
    return None


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe(engine_run_id: str, region: str) -> dict[str, Any]:
    omics = boto3.client("omics", region_name=region)
    engine_run = omics.get_run(id=engine_run_id)
    info: dict[str, Any] = {
        "engine_status": engine_run.get("status"),
        "failure_reason": engine_run.get("failureReason"),
        "message": _clip(engine_run.get("statusMessage") or "", 600),
        "failed_tasks": [],
        "log_tail": [],
    }
    try:
        tasks = omics.list_run_tasks(id=engine_run_id).get("items", [])
        info["failed_tasks"] = [
            {
                "name": t.get("name"),
                "status": t.get("status"),
                "message": _clip(t.get("statusMessage") or "", 300),
            }
            for t in tasks
            if str(t.get("status", "")).upper() in ("FAILED", "CANCELLED")
        ][:5]
    except Exception:  # noqa: BLE001 - task detail is optional
        logger.info("Could not list tasks for engine run %s", engine_run_id)
    logs = boto3.client("logs", region_name=region)
    used = 0
    for stream in (f"run/{engine_run_id}/engine", f"run/{engine_run_id}"):
        try:
            events = logs.get_log_events(
                logGroupName=LOG_GROUP,
                logStreamName=stream,
                limit=MAX_LOG_LINES,
                startFromHead=False,
            ).get("events", [])
        except Exception:  # noqa: BLE001 - the stream may not exist or may be empty
            continue
        for event in events:
            line = _clip(unquote(event.get("message", "")), MAX_LINE_CHARS)
            if line and used + len(line) <= MAX_TOTAL_CHARS:
                info["log_tail"].append(line)
                used += len(line)
        if info["log_tail"]:
            break
    return info


async def diagnose_run(run: WorkflowRun) -> RunDiagnosis:
    """Build the diagnosis for an already-fetched WES run record."""
    state = run.state.value if run.state is not None else None
    engine_run_id = engine_run_id_from_record(run)
    if not engine_run_id:
        return RunDiagnosis(
            run_id=run.id,
            state=state,
            available=False,
            note="This run has no execution-engine run ID yet, so there is nothing more to read.",
        )
    try:
        info = await asyncio.to_thread(_describe, engine_run_id, get_settings().omics_region)
    except Exception as exc:  # noqa: BLE001 - report the class only, never AWS error bodies
        logger.warning("Engine lookup failed for run %s: %s", run.id, type(exc).__name__)
        return RunDiagnosis(
            run_id=run.id,
            state=state,
            available=False,
            engine_run_id=engine_run_id,
            note=f"Engine details could not be read ({type(exc).__name__}).",
        )
    return RunDiagnosis(
        run_id=run.id,
        state=state,
        available=True,
        engine_run_id=engine_run_id,
        engine_status=info["engine_status"],
        failure_reason=info["failure_reason"],
        message=info["message"],
        failed_tasks=[FailedTask(**t) for t in info["failed_tasks"]],
        log_tail=info["log_tail"],
    )
