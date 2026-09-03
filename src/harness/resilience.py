"""Fail-closed retry, recovery, dead-letter, and circuit-breaker policy."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .contracts import FailureClass, SideEffectState, TaskStatus


RETRYABLE_FAILURES = {
    FailureClass.TRANSIENT,
    FailureClass.RATE_LIMITED,
    FailureClass.QUALITY_FAILURE,
}
CIRCUIT_FAILURES = {
    FailureClass.TRANSIENT,
    FailureClass.RATE_LIMITED,
}
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 300
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 3600


class ResilienceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    failure_class: FailureClass
    action: Literal[
        "retry_scheduled",
        "awaiting_decision",
        "blocked",
        "dead_lettered",
        "recovery_required",
    ]
    task_status: TaskStatus
    retry_at: str | None = None
    recovery_id: str | None = None
    dead_letter_id: int | None = None
    circuit_state: Literal["closed", "open"] | None = None


class ResilienceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    circuits: list[dict]
    dead_letters: list[dict]
    recovery_cards: list[dict]


def retry_delay_seconds(
    failure_class: FailureClass,
    attempt_count: int,
    retry_after_seconds: int | None,
) -> int:
    """Return bounded exponential backoff, honoring explicit rate limits."""
    exponential = min(
        BASE_BACKOFF_SECONDS * (2 ** max(attempt_count - 1, 0)),
        MAX_BACKOFF_SECONDS,
    )
    if failure_class == FailureClass.RATE_LIMITED:
        return max(exponential, retry_after_seconds or 0)
    return exponential


def requires_recovery(
    failure_class: FailureClass, side_effect_state: SideEffectState
) -> bool:
    """Unknown side effects always override any caller-supplied retry class."""
    return (
        failure_class == FailureClass.UNCERTAIN_SIDE_EFFECT
        or side_effect_state == SideEffectState.UNKNOWN
    )
