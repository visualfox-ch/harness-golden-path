"""Contract-Modelle des Control Harness (Golden Path v0).

Pydantic v2 mit extra="forbid": kein unbekanntes Feld hat versteckten Einfluss.
Statusmenge und Transitionstabelle folgen der Zielarchitektur (2026-09-03);
Statuswechsel erfolgen ausschliesslich über den Store, nie aus freiem Text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TaskStatus(str, Enum):
    READY = "ready"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"
    FAILED = "failed"
    DONE = "done"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery_required"
    RETRY_WAIT = "retry_wait"
    AWAITING_DECISION = "awaiting_decision"


TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.READY: {TaskStatus.CLAIMED, TaskStatus.CANCELLED},
    TaskStatus.CLAIMED: {
        TaskStatus.IN_PROGRESS,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.RECOVERY_REQUIRED,
        TaskStatus.RETRY_WAIT,
        TaskStatus.AWAITING_DECISION,
    },
    TaskStatus.IN_PROGRESS: {
        TaskStatus.REVIEW,
        TaskStatus.DONE,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.RECOVERY_REQUIRED,
        TaskStatus.RETRY_WAIT,
        TaskStatus.AWAITING_DECISION,
    },
    TaskStatus.REVIEW: {
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.BLOCKED,
        TaskStatus.DONE,
    },
    TaskStatus.AWAITING_APPROVAL: {TaskStatus.DONE, TaskStatus.BLOCKED},
    TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.FAILED: {TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.RECOVERY_REQUIRED: {
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.DONE,
        TaskStatus.CANCELLED,
    },
    TaskStatus.RETRY_WAIT: {
        TaskStatus.READY,
        TaskStatus.RECOVERY_REQUIRED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.AWAITING_DECISION: {
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.DONE: set(),
    TaskStatus.CANCELLED: set(),
}


class ExecutorRole(str, Enum):
    HERMES_LOCAL = "hermes_local"
    HERMES_NAS = "hermes_nas"


class ProviderClass(str, Enum):
    LOCAL_MODEL = "local_model"
    SUBSCRIPTION_OAUTH = "subscription_oauth"
    API_METERED = "api_metered"


class DataClassification(str, Enum):
    INTERNAL = "internal"
    CONFIDENTIAL_LOCAL = "confidential_local"
    CONFIDENTIAL_CLOUD_APPROVED = "confidential_cloud_approved"
    SECRET = "secret"


class ProjectionKind(str, Enum):
    OPERATIONAL = "operational"
    EVIDENCE = "evidence"


class FailureClass(str, Enum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    QUALITY_FAILURE = "quality_failure"
    QUOTA_EXHAUSTED = "quota_exhausted"
    POLICY_VIOLATION = "policy_violation"
    PERMANENT = "permanent"
    UNCERTAIN_SIDE_EFFECT = "uncertain_side_effect"


class SideEffectState(str, Enum):
    NONE = "none"
    CONFIRMED_NONE = "confirmed_none"
    UNKNOWN = "unknown"


class HarnessEventType(str, Enum):
    TASK_CREATED = "task_created"
    TASK_CLAIMED = "task_claimed"
    STATUS_CHANGED = "status_changed"
    HEARTBEAT_REJECTED = "heartbeat_rejected"
    LEASE_EXPIRED = "lease_expired"
    FAILURE_CLASSIFIED = "failure_classified"
    RECOVERY_CARD_CREATED = "recovery_card_created"
    DEAD_LETTER_CREATED = "dead_letter_created"
    RETRY_SCHEDULED = "retry_scheduled"
    RETRY_RELEASED = "retry_released"
    RECEIPT_REJECTED = "receipt_rejected"
    RECEIPT_ACCEPTED = "receipt_accepted"
    CIRCUIT_CLOSED = "circuit_closed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"


EVENT_PAYLOAD_FIELDS: dict[HarnessEventType, frozenset[str]] = {
    HarnessEventType.TASK_CREATED: frozenset({"title", "project"}),
    HarnessEventType.TASK_CLAIMED: frozenset({"worker", "attempt"}),
    HarnessEventType.STATUS_CHANGED: frozenset({"from", "to", "reason"}),
    HarnessEventType.HEARTBEAT_REJECTED: frozenset({"worker"}),
    HarnessEventType.LEASE_EXPIRED: frozenset({"action"}),
    HarnessEventType.FAILURE_CLASSIFIED: frozenset(
        {"failure_class", "side_effect_state", "circuit_key"}
    ),
    HarnessEventType.RECOVERY_CARD_CREATED: frozenset(
        {"recovery_id", "trigger", "allowed_actions"}
    ),
    HarnessEventType.DEAD_LETTER_CREATED: frozenset(
        {"dead_letter_id", "failure_class"}
    ),
    HarnessEventType.RETRY_SCHEDULED: frozenset(
        {"retry_at", "delay_seconds", "attempt"}
    ),
    HarnessEventType.RETRY_RELEASED: frozenset({"next_attempt"}),
    HarnessEventType.RECEIPT_REJECTED: frozenset({"reason", "worker"}),
    HarnessEventType.RECEIPT_ACCEPTED: frozenset(
        {"outcome", "status", "model_ref", "provider_class"}
    ),
    HarnessEventType.CIRCUIT_CLOSED: frozenset({"circuit_key"}),
    HarnessEventType.APPROVAL_REQUESTED: frozenset({"approval_id", "action"}),
    HarnessEventType.APPROVAL_DECIDED: frozenset(
        {"approval_id", "decision", "decided_by"}
    ),
}


class Budget(StrictModel):
    max_runtime_minutes: Annotated[int, Field(ge=1, le=10080)]
    max_attempts: Annotated[int, Field(ge=1, le=5)] = 2
    max_incremental_cloud_cost_chf: Annotated[float, Field(ge=0, le=0)] = 0.0


class Scope(StrictModel):
    include: list[Annotated[str, Field(min_length=1, max_length=300)]]
    exclude: list[Annotated[str, Field(min_length=1, max_length=300)]] = []

    @field_validator("include", "exclude")
    @classmethod
    def reject_unsafe_paths(cls, paths: list[str]) -> list[str]:
        forbidden_prefixes = ("../", "~", "/etc/", "/var/run/")
        for path in paths:
            if path.startswith(forbidden_prefixes) or ".." in path:
                raise ValueError(f"unsafe path reference: {path}")
        return paths


class ModelRouting(StrictModel):
    permitted_provider_classes: list[ProviderClass]
    default_route: list[Annotated[str, Field(pattern=r"^[a-z0-9_/-]{3,100}$")]]
    api_metered_fallback: Literal["forbidden"] = "forbidden"

    @model_validator(mode="after")
    def enforce_oauth_only(self) -> ModelRouting:
        if ProviderClass.API_METERED in self.permitted_provider_classes:
            raise ValueError(
                "metered API provider class is forbidden by current policy"
            )
        return self


class TaskCardV1(StrictModel):
    schema_version: Literal[1] = 1
    task_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID = Field(default_factory=uuid4)
    title: Annotated[str, Field(min_length=8, max_length=160)]
    project: Annotated[str, Field(pattern=r"^[a-zA-Z0-9._-]{2,80}$")]
    task_class: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")] = "docs_change"
    data_classification: DataClassification = DataClassification.INTERNAL
    projection_kind: ProjectionKind = ProjectionKind.OPERATIONAL
    owner_role: ExecutorRole
    status: Literal[TaskStatus.READY] = TaskStatus.READY
    objective: Annotated[str, Field(min_length=20, max_length=4000)]
    scope: Scope
    acceptance: list[Annotated[str, Field(min_length=8, max_length=500)]]
    budget: Budget
    model_routing: ModelRouting
    idempotency_key: Annotated[str, Field(min_length=16, max_length=128)]
    approval_required: list[Annotated[str, Field(min_length=3, max_length=80)]] = []
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def restrict_long_running_tasks_to_nas(self) -> TaskCardV1:
        if (
            self.budget.max_runtime_minutes > 480
            and self.owner_role != ExecutorRole.HERMES_NAS
        ):
            raise ValueError("only hermes_nas tasks may run longer than 480 minutes")
        return self


class Artifacts(StrictModel):
    branch: Annotated[str, Field(max_length=200)] | None = None
    worktree_ref: Annotated[str, Field(max_length=200)] | None = None
    commit_sha: Annotated[str, Field(max_length=64)] | None = None
    pull_request: Annotated[str, Field(max_length=300)] | None = None
    test_report: Annotated[str, Field(max_length=300)] | None = None


class ValidationResult(StrictModel):
    lint: Literal["passed", "failed", "not_run"] = "not_run"
    tests: Literal["passed", "failed", "not_run"] = "not_run"
    details: Annotated[str, Field(max_length=2000)] = ""


class CostReceipt(StrictModel):
    provider_class: ProviderClass
    model_ref: Annotated[str, Field(pattern=r"^[a-z0-9_/-]{3,100}$")]
    incremental_cost_chf: Annotated[float, Field(ge=0, le=0)] = 0.0
    subscription_quota_consumed: bool = True
    quota_status: Literal["available", "partial", "unavailable", "unknown"] = "unknown"

    @model_validator(mode="after")
    def forbid_metered(self) -> CostReceipt:
        if self.provider_class == ProviderClass.API_METERED:
            raise ValueError("api_metered receipts are forbidden by current policy")
        return self


class ResultReceiptV1(StrictModel):
    schema_version: Literal[1] = 1
    task_id: UUID
    correlation_id: UUID
    worker_instance: Annotated[str, Field(min_length=3, max_length=120)]
    outcome: Literal["completed", "failed", "blocked"]
    summary: Annotated[str, Field(min_length=10, max_length=4000)]
    artifacts: Artifacts = Artifacts()
    validation: ValidationResult = ValidationResult()
    cost_receipt: CostReceipt
    created_at: datetime = Field(default_factory=utcnow)


class FailureReportV1(StrictModel):
    schema_version: Literal[1] = 1
    task_id: UUID
    correlation_id: UUID
    worker_instance: Annotated[str, Field(min_length=3, max_length=120)]
    failure_class: FailureClass
    reason: Annotated[str, Field(min_length=10, max_length=2000)]
    side_effect_state: SideEffectState = SideEffectState.NONE
    circuit_key: Annotated[str, Field(pattern=r"^[a-zA-Z0-9._:/-]{3,120}$")] | None = (
        None
    )
    retry_after_seconds: Annotated[int, Field(ge=0, le=86400)] | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def require_unknown_side_effect(self) -> FailureReportV1:
        if (
            self.failure_class == FailureClass.UNCERTAIN_SIDE_EFFECT
            and self.side_effect_state != SideEffectState.UNKNOWN
        ):
            raise ValueError("uncertain_side_effect requires side_effect_state=unknown")
        return self


class ApprovalCardV1(StrictModel):
    schema_version: Literal[1] = 1
    approval_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    action: Annotated[str, Field(min_length=3, max_length=80)]
    requested_by: Annotated[str, Field(min_length=3, max_length=120)]
    status: Literal["requested", "approved", "rejected"] = "requested"
    decided_by: Annotated[str, Field(max_length=120)] | None = None
    reason: Annotated[str, Field(max_length=2000)] = ""
    created_at: datetime = Field(default_factory=utcnow)


class EventEnvelopeV1(StrictModel):
    schema_version: Literal[1] = 1
    event_id: int
    task_id: UUID
    correlation_id: UUID
    event_type: HarnessEventType
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime

    @model_validator(mode="after")
    def enforce_registered_payload_shape(self) -> EventEnvelopeV1:
        expected = EVENT_PAYLOAD_FIELDS[self.event_type]
        actual = set(self.payload)
        missing = expected - actual
        unexpected = actual - expected
        if missing or unexpected:
            raise ValueError(
                f"event '{self.event_type.value}' payload drift: "
                f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
            )
        return self
