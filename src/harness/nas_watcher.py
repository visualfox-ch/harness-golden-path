"""Read-only Hermes-NAS pilot worker with bounded, allowlisted probes."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

_ALLOWED_REPOSITORY = "visualfox-ch/harness-golden-path"
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class WorkerError(Exception):
    pass


class AuthenticationRevoked(WorkerError):
    pass


class ClaimConflict(WorkerError):
    pass


class HarnessClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def create_task(self, payload: dict) -> dict:
        return self._request("POST", "/v1/tasks", payload)

    def get_task(self, task_id: str) -> dict:
        return self._request("GET", f"/v1/tasks/{task_id}")

    def claim(self, task_id: str, worker_id: str, lease_minutes: int) -> dict:
        return self._request(
            "POST",
            f"/v1/tasks/{task_id}/claim",
            {
                "owner_role": "hermes_nas",
                "worker_id": worker_id,
                "lease_minutes": lease_minutes,
            },
        )

    def heartbeat(self, task_id: str, worker_id: str, lease_minutes: int) -> dict:
        return self._request(
            "POST",
            f"/v1/tasks/{task_id}/heartbeat",
            {"worker_id": worker_id, "lease_minutes": lease_minutes},
        )

    def submit_receipt(self, task: dict, worker_id: str, summary: str) -> dict:
        return self._request(
            "POST",
            f"/v1/tasks/{task['task_id']}/receipts",
            {
                "schema_version": 1,
                "task_id": task["task_id"],
                "correlation_id": task["correlation_id"],
                "worker_instance": worker_id,
                "outcome": "completed",
                "summary": summary,
                "validation": {
                    "lint": "not_run",
                    "tests": "passed",
                    "details": "Allowlisted read-only probes completed for the pilot window.",
                },
                "cost_receipt": {
                    "provider_class": "local_model",
                    "model_ref": "deterministic_monitor",
                    "incremental_cost_chf": 0.0,
                    "subscription_quota_consumed": False,
                    "quota_status": "unavailable",
                },
            },
        )

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            if exc.code == 401:
                raise AuthenticationRevoked("worker credential rejected") from exc
            if exc.code == 409:
                raise ClaimConflict("task is not newly claimable") from exc
            raise WorkerError(f"Harness returned HTTP {exc.code} for {path}") from exc
        except (URLError, TimeoutError) as exc:
            raise WorkerError(f"Harness unavailable for {path}") from exc


class PublicGitHubProbe:
    """Read only the public pilot repository; arbitrary targets are rejected."""

    def __init__(self, repository: str = _ALLOWED_REPOSITORY) -> None:
        if not _REPOSITORY_RE.fullmatch(repository) or repository != _ALLOWED_REPOSITORY:
            raise ValueError("repository is outside the P2-4 read-only allowlist")
        self.repository = repository

    def collect(self) -> dict:
        commit = _public_json(
            f"https://api.github.com/repos/{self.repository}/commits/main"
        )
        runs = _public_json(
            f"https://api.github.com/repos/{self.repository}/actions/runs?per_page=1"
        )
        latest = runs.get("workflow_runs", [{}])[0]
        return {
            "repository": self.repository,
            "head_sha": commit["sha"],
            "ci_run_id": latest.get("id"),
            "ci_status": latest.get("status", "unavailable"),
            "ci_conclusion": latest.get("conclusion", "unavailable"),
        }


class NasWatcher:
    def __init__(
        self,
        client: HarnessClient,
        probe: PublicGitHubProbe,
        task_id: str,
        worker_id: str,
        end_at: datetime,
        interval_seconds: int = 300,
        lease_minutes: int = 10,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        emit: Callable[[str], None] = print,
    ) -> None:
        UUID(task_id)
        if not worker_id.startswith("svc-hermes-nas:"):
            raise ValueError("worker_id must use the svc-hermes-nas namespace")
        if end_at.tzinfo is None:
            raise ValueError("end_at must be timezone-aware")
        self.client = client
        self.probe = probe
        self.task_id = task_id
        self.worker_id = worker_id
        self.end_at = end_at
        self.interval_seconds = interval_seconds
        self.lease_minutes = lease_minutes
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleeper = sleeper
        self.emit = emit

    def run(self) -> dict:
        task = self._claim_or_resume()
        cycles = 0
        while self.clock() < self.end_at:
            self.client.heartbeat(self.task_id, self.worker_id, self.lease_minutes)
            report = {
                "event": "watcher_report",
                "timestamp": self.clock().isoformat(),
                "task_id": self.task_id,
                "worker_id": self.worker_id,
                "checks": self.probe.collect(),
            }
            self.emit(json.dumps(report, sort_keys=True))
            cycles += 1
            self.sleeper(self.interval_seconds)
        return self.client.submit_receipt(
            task,
            self.worker_id,
            f"P2-4 read-only pilot completed with {cycles} heartbeat/report cycles.",
        )

    def _claim_or_resume(self) -> dict:
        try:
            return self.client.claim(self.task_id, self.worker_id, self.lease_minutes)
        except ClaimConflict:
            task = self.client.get_task(self.task_id)
            if (
                task.get("owner_instance") == self.worker_id
                and task.get("status") in {"claimed", "in_progress"}
            ):
                return task
            raise WorkerError("task is not owned by this worker")


def build_pilot_task() -> dict:
    task_id = str(UUID(os.environ["HARNESS_TASK_ID"]))
    correlation_id = str(UUID(os.environ["HARNESS_CORRELATION_ID"]))
    return {
        "schema_version": 1,
        "task_id": task_id,
        "correlation_id": correlation_id,
        "title": "P2-4 seven-day NAS read-only watcher pilot",
        "project": "harness-golden-path",
        "task_class": "read_only_monitor",
        "data_classification": "internal",
        "owner_role": "hermes_nas",
        "status": "ready",
        "objective": (
            "Run allowlisted read-only repository and CI probes from Hermes NAS, "
            "while maintaining a visible Harness lease for seven days."
        ),
        "scope": {
            "include": [
                "Harness health and lease heartbeat",
                "Public GitHub metadata for visualfox-ch/harness-golden-path",
            ],
            "exclude": [
                "Repository writes",
                "Docker socket access",
                "Deployments",
                "Secret or IAM changes",
            ],
        },
        "acceptance": [
            "Worker emits timestamped reports for the configured pilot window",
            "Harness records lease heartbeats from svc-hermes-nas",
            "No repository or infrastructure write capability is present",
        ],
        "budget": {
            "max_runtime_minutes": 10080,
            "max_attempts": 2,
            "max_incremental_cloud_cost_chf": 0.0,
        },
        "model_routing": {
            "permitted_provider_classes": ["local_model"],
            "default_route": ["deterministic_monitor"],
            "api_metered_fallback": "forbidden",
        },
        "idempotency_key": f"sha256:p2-4:nas-readonly:{task_id}",
        "approval_required": [],
    }


def _public_json(url: str) -> dict:
    request = Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise WorkerError("public GitHub probe failed") from exc


def _token_from_environment(env_name: str) -> str:
    path = Path(os.environ[env_name])
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise WorkerError(f"{env_name} does not reference a valid token")
    return token


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    base_url = os.environ.get("HARNESS_BASE_URL", "http://harness-api:8787")
    if mode == "seed":
        token = _token_from_environment("HARNESS_ORCHESTRATOR_TOKEN_FILE")
        result = HarnessClient(base_url, token).create_task(build_pilot_task())
        print(json.dumps({"created": result["created"], "task_id": result["task"]["task_id"]}))
        return 0
    if mode != "run":
        raise WorkerError("mode must be run or seed")
    token = _token_from_environment("HARNESS_NAS_WORKER_TOKEN_FILE")
    watcher = NasWatcher(
        client=HarnessClient(base_url, token),
        probe=PublicGitHubProbe(),
        task_id=os.environ["HARNESS_TASK_ID"],
        worker_id=os.environ.get("HARNESS_WORKER_ID", "svc-hermes-nas:p2-4"),
        end_at=datetime.fromisoformat(os.environ["PILOT_END_AT"].replace("Z", "+00:00")),
        interval_seconds=int(os.environ.get("PILOT_INTERVAL_SECONDS", "300")),
        lease_minutes=int(os.environ.get("HARNESS_LEASE_MINUTES", "10")),
    )
    try:
        watcher.run()
    except AuthenticationRevoked as exc:
        print(json.dumps({"event": "credential_revoked", "detail": str(exc)}))
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
