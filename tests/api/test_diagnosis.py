"""Tests for the run diagnosis endpoint (GET /runs/{run_id}/diagnosis)."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.wes_service.db.models import WorkflowRun, WorkflowState

URL = "/ga4gh/wes/v1/runs/{}/diagnosis"
LOG_URL = "https://console.example/home#logsV2:log-groups/log-group/$252Faws$252Fomics$252FWorkflowLog/log-events/run$252F4242"


def _run(run_id: str, **kw) -> WorkflowRun:
    return WorkflowRun(
        id=run_id,
        state=kw.pop("state", WorkflowState.EXECUTOR_ERROR),
        workflow_type="CWL",
        workflow_type_version="v1.0",
        workflow_url="https://example.com/workflow.cwl",
        tags={},
        user_id="test_user",
        project="test-project",
        task_name="test-task",
        **kw,
    )


def _boto(omics: MagicMock, logs: MagicMock):
    def factory(service, **_):
        return omics if service == "omics" else logs

    return patch("src.wes_service.services.diagnosis_service.boto3.client", side_effect=factory)


class TestRunDiagnosis:
    def test_run_not_found(self, client: TestClient):
        assert client.get(URL.format("nope")).status_code == 404

    async def test_no_engine_run_id(self, client: TestClient, test_db):
        test_db.add(_run("r-none"))
        await test_db.commit()
        data = client.get(URL.format("r-none")).json()
        assert data["available"] is False
        assert data["engine_run_id"] is None
        assert "no execution-engine run ID" in data["note"]

    async def test_explains_failure(self, client: TestClient, test_db):
        test_db.add(_run("r-fail", workflow_run_id="4242"))
        await test_db.commit()
        omics, logs = MagicMock(), MagicMock()
        omics.get_run.return_value = {
            "status": "FAILED",
            "failureReason": "CANNOT_START_CONTAINER_ERROR",
            "statusMessage": "Container image   could not be pulled",
        }
        omics.list_run_tasks.return_value = {
            "items": [
                {"name": "ok", "status": "COMPLETED"},
                {"name": "main", "status": "FAILED", "statusMessage": "no image"},
            ]
        }
        logs.get_log_events.return_value = {"events": [{"message": "engine%20started"}, {"message": "boom"}]}
        with _boto(omics, logs):
            data = client.get(URL.format("r-fail")).json()
        assert data["available"] is True
        assert data["engine_run_id"] == "4242"
        assert data["failure_reason"] == "CANNOT_START_CONTAINER_ERROR"
        assert "bash" in data["hint"]
        assert data["message"] == "Container image could not be pulled"
        assert [t["name"] for t in data["failed_tasks"]] == ["main"]
        assert data["log_tail"] == ["engine started", "boom"]
        omics.get_run.assert_called_once_with(id="4242")

    async def test_engine_id_from_log_links(self, client: TestClient, test_db):
        test_db.add(_run("r-links", outputs={"log_urls": {"run_log": LOG_URL.replace("$252F", "/")}}))
        await test_db.commit()
        omics, logs = MagicMock(), MagicMock()
        omics.get_run.return_value = {"status": "FAILED"}
        omics.list_run_tasks.return_value = {"items": []}
        logs.get_log_events.return_value = {"events": []}
        with _boto(omics, logs):
            data = client.get(URL.format("r-links")).json()
        assert data["engine_run_id"] == "4242"
        assert data["hint"] is None  # no hint for a failure reason we know nothing about

    async def test_engine_error_is_reported_without_details(self, client: TestClient, test_db):
        test_db.add(_run("r-denied", workflow_run_id="4242"))
        await test_db.commit()
        omics = MagicMock()
        omics.get_run.side_effect = RuntimeError("secret internal detail")
        with _boto(omics, MagicMock()):
            data = client.get(URL.format("r-denied")).json()
        assert data["available"] is False
        assert "RuntimeError" in data["note"]
        assert "secret internal detail" not in str(data)

    async def test_non_numeric_engine_id_is_ignored(self, client: TestClient, test_db):
        test_db.add(_run("r-bad", workflow_run_id="abc; drop"))
        await test_db.commit()
        data = client.get(URL.format("r-bad")).json()
        assert data["available"] is False
        assert data["engine_run_id"] is None
