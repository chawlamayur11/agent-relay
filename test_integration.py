"""Integration test for Agent Relay task exchange flow.

Tests the full lifecycle:
1. Register sender and recipient agents.
2. Sender submits a task (verifies status is 'queued').
3. Recipient claims the task (verifies claim_token and status is 'processing').
4. Recipient submits result (verifies status changes to 'completed').
5. Sender retrieves the task and verifies final 'completed' status and output.

Supports testing against an active HTTP server via RELAY_API_URL or in-memory
via FastAPI TestClient.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
import pytest
import httpx

_test_db_path = (Path(tempfile.gettempdir()) / "agent-relay-test.db").as_posix()
os.environ.setdefault("RELAY_DATABASE_URL", f"sqlite:///{_test_db_path}")

import main
from database import Base, engine


class ApiClient:
    """Wrapper that talks either to a live HTTP URL or FastAPI TestClient."""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or "").rstrip("/")
        if self.base_url:
            self._client = httpx.Client(base_url=self.base_url, timeout=30.0)
            self._is_testclient = False
        else:
            from fastapi.testclient import TestClient
            self._client = TestClient(main.app)
            self._is_testclient = True

    def post(self, path: str, **kwargs):
        return self._client.post(path, **kwargs)

    def get(self, path: str, **kwargs):
        return self._client.get(path, **kwargs)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


@pytest.fixture(autouse=True)
def setup_db():
    base_url = os.getenv("RELAY_API_URL")
    if not base_url:
        # Only reset DB if running locally in-process without an external server
        Base.metadata.create_all(engine)
    yield


def test_agent_task_exchange_lifecycle():
    base_url = os.getenv("RELAY_API_URL")
    with ApiClient(base_url) as client:
        # 1. Register sender agent
        sender_res = client.post("/api/v1/agents", json={"name": "sender-agent", "description": "Sends review tasks"})
        assert sender_res.status_code == 201, sender_res.text
        sender_data = sender_res.json()
        sender_token = sender_data["token"]
        sender_id = sender_data["agent_id"]
        sender_headers = {"Authorization": f"Bearer {sender_token}"}

        # 2. Register recipient agent
        recipient_res = client.post("/api/v1/agents", json={"name": "recipient-worker", "description": "Processes tasks"})
        assert recipient_res.status_code == 201, recipient_res.text
        recipient_data = recipient_res.json()
        recipient_token = recipient_data["token"]
        recipient_id = recipient_data["agent_id"]
        recipient_headers = {"Authorization": f"Bearer {recipient_token}"}

        # 3. Sender sends a task to the recipient
        task_input = "Hello Agent Relay: verify integration flow"
        send_task_res = client.post(
            "/api/v1/tasks",
            headers=sender_headers,
            json={"to": recipient_id, "input": task_input},
        )
        assert send_task_res.status_code == 201, send_task_res.text
        task_info = send_task_res.json()
        task_id = task_info["task_id"]
        assert task_info["status"] == "queued"

        # Sender sees status is 'queued' initially
        sender_task_check = client.get(f"/api/v1/tasks/{task_id}", headers=sender_headers)
        assert sender_task_check.status_code == 200
        assert sender_task_check.json()["status"] == "queued"

        # 4. Recipient claims the task
        claim_res = client.post(
            "/api/v1/tasks/claim",
            headers=recipient_headers,
            json={"worker_id": "integration-worker-1", "wait_seconds": 5},
        )
        assert claim_res.status_code == 200, claim_res.text
        claim_data = claim_res.json()
        assert claim_data["task_id"] == task_id
        assert claim_data["from"] == sender_id
        assert claim_data["input"] == task_input
        claim_token = claim_data["claim_token"]
        assert claim_token

        # Sender sees status is now 'processing'
        sender_task_check = client.get(f"/api/v1/tasks/{task_id}", headers=sender_headers)
        assert sender_task_check.status_code == 200
        assert sender_task_check.json()["status"] == "processing"

        # 5. Recipient completes the task with a result
        expected_output = "Task successfully completed by recipient-worker"
        complete_res = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": claim_token, "output": expected_output},
        )
        assert complete_res.status_code == 200, complete_res.text
        assert complete_res.json()["status"] == "completed"

        # 6. Sender retrieves the task result and confirms 'completed' status
        final_task_res = client.get(f"/api/v1/tasks/{task_id}", headers=sender_headers)
        assert final_task_res.status_code == 200, final_task_res.text
        final_data = final_task_res.json()
        assert final_data["status"] == "completed"
        assert final_data["output"] == expected_output
        assert final_data["error"] is None
        assert final_data["finished_at"] is not None

        # Verify delivery attempt outcome is completed
        attempts_res = client.get(f"/api/v1/tasks/{task_id}/attempts", headers=sender_headers)
        assert attempts_res.status_code == 200
        attempts_data = attempts_res.json()
        assert len(attempts_data["items"]) >= 1
        assert attempts_data["items"][0]["outcome"] == "completed"
