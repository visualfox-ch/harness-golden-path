"""One-way, deterministic Harness to PandaOS task projection."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .contracts import ProjectionKind, TaskStatus


class ProjectionCursorError(ValueError):
    """Raised when a consumer cursor is ahead of authoritative Harness state."""


class PandaTaskProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projection_key: str
    harness_task_id: str
    correlation_id: str
    source_event_id: int
    subject: str
    project: str
    status: Literal["pending", "in_progress", "completed"]
    active_form: str
    owner_instance: str | None
    attempt_count: int
    approval_required: list[str]
    action_required: bool
    is_proof: bool
    fingerprint: str
    trace_ref: str


class PandaProjectionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    source_authority: Literal["control_harness"] = "control_harness"
    direction: Literal["harness_to_pandaos"] = "harness_to_pandaos"
    mode: Literal["incremental", "full"]
    from_event_id: int
    through_event_id: int
    tasks: list[PandaTaskProjection]


class ProjectionCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    panda_task_id: str
    source_event_id: int
    fingerprint: str


class ObservedPandaTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    status: Literal["pending", "in_progress", "completed"]
    active_form: str


class ProjectionOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "create", "update", "repair", "advance_cursor", "noop", "ignore_old"
    ]
    reason: str
    projection: PandaTaskProjection
    panda_task_id: str | None = None


_PANDA_STATUS = {
    TaskStatus.READY: "pending",
    TaskStatus.CLAIMED: "in_progress",
    TaskStatus.IN_PROGRESS: "in_progress",
    TaskStatus.REVIEW: "in_progress",
    TaskStatus.AWAITING_APPROVAL: "pending",
    TaskStatus.BLOCKED: "pending",
    TaskStatus.FAILED: "pending",
    TaskStatus.DONE: "completed",
    TaskStatus.CANCELLED: "completed",
    TaskStatus.RECOVERY_REQUIRED: "pending",
    TaskStatus.RETRY_WAIT: "pending",
    TaskStatus.AWAITING_DECISION: "pending",
}

_ACTION_STATES = {
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.AWAITING_DECISION,
    TaskStatus.BLOCKED,
    TaskStatus.FAILED,
    TaskStatus.RECOVERY_REQUIRED,
}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _active_form(row: dict, status: TaskStatus, is_proof: bool) -> str:
    if is_proof:
        return f"Evidence fixture — Harness status: {status.value}"
    if status == TaskStatus.READY:
        return "Ready in Harness"
    if status in {TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS}:
        owner = row.get("owner_instance") or "assigned worker"
        return f"Running in Harness via {owner}"
    if status == TaskStatus.REVIEW:
        return "Review in Harness"
    if status == TaskStatus.AWAITING_APPROVAL:
        actions = ", ".join(row.get("approval_required") or [])
        return f"Awaiting Harness approval: {actions or 'decision'}"
    if status == TaskStatus.RETRY_WAIT:
        release = _iso(row.get("next_attempt_at")) or "scheduled release"
        return f"Retry waiting until {release}"
    if status == TaskStatus.RECOVERY_REQUIRED:
        return "Harness recovery decision required"
    if status == TaskStatus.AWAITING_DECISION:
        return "Human decision required in Harness"
    if status == TaskStatus.BLOCKED:
        return "Blocked in Harness"
    if status == TaskStatus.FAILED:
        return "Failed in Harness"
    if status == TaskStatus.CANCELLED:
        return "Cancelled in Harness"
    return "Completed in Harness"


def _target_fingerprint(subject: str, status: str, active_form: str) -> str:
    encoded = json.dumps(
        {"activeForm": active_form, "status": status, "subject": subject},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def project_task(row: dict) -> PandaTaskProjection:
    """Map an authoritative task row to the safe PandaOS display contract."""
    status = TaskStatus(row["status"])
    card = row.get("card") or {}
    kind = ProjectionKind(
        card.get("projection_kind", ProjectionKind.OPERATIONAL.value)
    )
    is_proof = kind == ProjectionKind.EVIDENCE
    panda_status = _PANDA_STATUS[status]
    active_form = _active_form(row, status, is_proof)
    subject = row["title"]
    fingerprint = _target_fingerprint(subject, panda_status, active_form)
    task_id = str(row["task_id"])
    correlation_id = str(row["correlation_id"])
    return PandaTaskProjection(
        projection_key=f"harness:{task_id}",
        harness_task_id=task_id,
        correlation_id=correlation_id,
        source_event_id=int(row["source_event_id"]),
        subject=subject,
        project=row["project"],
        status=panda_status,
        active_form=active_form,
        owner_instance=row.get("owner_instance"),
        attempt_count=int(row.get("attempt_count") or 0),
        approval_required=list(row.get("approval_required") or []),
        action_required=not is_proof and status in _ACTION_STATES,
        is_proof=is_proof,
        fingerprint=fingerprint,
        trace_ref=f"/v1/traces/{correlation_id}",
    )


def build_projection_batch(
    rows: list[dict], from_event_id: int, through_event_id: int, full: bool
) -> PandaProjectionBatch:
    if from_event_id > through_event_id:
        raise ProjectionCursorError(
            "consumer cursor is ahead of authoritative Harness event state"
        )
    return PandaProjectionBatch(
        mode="full" if full else "incremental",
        from_event_id=from_event_id,
        through_event_id=through_event_id,
        tasks=[project_task(row) for row in rows],
    )


def plan_projection(
    projection: PandaTaskProjection,
    checkpoint: ProjectionCheckpoint | None,
    observed: ObservedPandaTask | None = None,
) -> ProjectionOperation:
    """Plan a non-regressing, idempotent PandaOS session-task mutation."""
    if checkpoint is None:
        return ProjectionOperation(
            action="create", reason="projection_missing", projection=projection
        )
    if projection.source_event_id < checkpoint.source_event_id:
        return ProjectionOperation(
            action="ignore_old",
            reason="older_harness_event",
            projection=projection,
            panda_task_id=checkpoint.panda_task_id,
        )
    if observed is not None:
        observed_fingerprint = _target_fingerprint(
            observed.subject, observed.status, observed.active_form
        )
        if observed_fingerprint != checkpoint.fingerprint:
            return ProjectionOperation(
                action="repair",
                reason="panda_projection_drift",
                projection=projection,
                panda_task_id=checkpoint.panda_task_id,
            )
    if projection.source_event_id == checkpoint.source_event_id:
        action = (
            "noop" if projection.fingerprint == checkpoint.fingerprint else "repair"
        )
        reason = "already_projected" if action == "noop" else "same_event_content_drift"
        return ProjectionOperation(
            action=action,
            reason=reason,
            projection=projection,
            panda_task_id=checkpoint.panda_task_id,
        )
    action = (
        "advance_cursor"
        if projection.fingerprint == checkpoint.fingerprint
        else "update"
    )
    reason = "display_unchanged" if action == "advance_cursor" else "new_harness_state"
    return ProjectionOperation(
        action=action,
        reason=reason,
        projection=projection,
        panda_task_id=checkpoint.panda_task_id,
    )
