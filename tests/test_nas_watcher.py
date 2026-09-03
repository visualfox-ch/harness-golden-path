from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from harness.contracts import Budget, ExecutorRole, TaskCardV1
from harness.nas_watcher import ClaimConflict, NasWatcher, WorkerError


class FakeClient:
    def __init__(self):
        self.claimed = False
        self.heartbeats = 0
        self.receipts = 0

    def claim(self, task_id, worker_id, lease_minutes):
        if self.claimed:
            raise ClaimConflict()
        self.claimed = True
        return {
            "task_id": task_id,
            "correlation_id": "ccf28e33-6a16-46d6-b10c-51d75b946aa4",
            "owner_instance": worker_id,
            "status": "claimed",
        }

    def get_task(self, task_id):
        return {
            "task_id": task_id,
            "correlation_id": "ccf28e33-6a16-46d6-b10c-51d75b946aa4",
            "owner_instance": "svc-hermes-nas:p2-4",
            "status": "claimed",
        }

    def heartbeat(self, task_id, worker_id, lease_minutes):
        self.heartbeats += 1
        return {"status": "claimed"}

    def submit_receipt(self, task, worker_id, summary):
        self.receipts += 1
        return {"status": "done", "summary": summary}


class FakeProbe:
    def collect(self):
        return {"ci_status": "completed", "ci_conclusion": "success"}


def test_watcher_claims_heartbeats_reports_and_completes():
    client = FakeClient()
    now = datetime(2026, 9, 3, tzinfo=UTC)
    ticks = iter([now, now, now + timedelta(minutes=5)])
    output = []
    watcher = NasWatcher(
        client=client,
        probe=FakeProbe(),
        task_id="3bdb6591-3b72-4f79-a232-bd022f534d8c",
        worker_id="svc-hermes-nas:p2-4",
        end_at=now + timedelta(minutes=1),
        interval_seconds=0,
        clock=lambda: next(ticks),
        sleeper=lambda _: None,
        emit=output.append,
    )
    result = watcher.run()
    assert result["status"] == "done"
    assert client.heartbeats == 1
    assert client.receipts == 1
    assert len(output) == 1


def test_watcher_resumes_only_its_own_active_task():
    client = FakeClient()
    client.claimed = True
    now = datetime(2026, 9, 3, tzinfo=UTC)
    watcher = NasWatcher(
        client=client,
        probe=FakeProbe(),
        task_id="3bdb6591-3b72-4f79-a232-bd022f534d8c",
        worker_id="svc-hermes-nas:p2-4",
        end_at=now,
        clock=lambda: now,
    )
    assert watcher.run()["status"] == "done"


def test_local_tasks_cannot_request_seven_day_budget(card_factory):
    payload = card_factory().model_dump()
    payload["budget"] = Budget(max_runtime_minutes=10080)
    payload["owner_role"] = ExecutorRole.HERMES_LOCAL
    with pytest.raises(ValidationError):
        TaskCardV1(**payload)


def test_resume_rejects_task_owned_by_someone_else():
    client = FakeClient()
    client.claimed = True
    client.get_task = lambda _: {
        "owner_instance": "svc-hermes-nas:other",
        "status": "claimed",
    }
    now = datetime(2026, 9, 3, tzinfo=UTC)
    watcher = NasWatcher(
        client=client,
        probe=FakeProbe(),
        task_id="3bdb6591-3b72-4f79-a232-bd022f534d8c",
        worker_id="svc-hermes-nas:p2-4",
        end_at=now,
        clock=lambda: now,
    )
    with pytest.raises(WorkerError):
        watcher.run()
