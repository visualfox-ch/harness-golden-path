"""Read-only routing and quality metrics derived from persisted receipts."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class MetricValue(BaseModel):
    """A metric is only available when its source data exists in the store."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "unavailable"]
    numerator: int | None = None
    denominator: int | None = None
    rate: float | None = None
    reason: str | None = None


class RouteMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_ref: str
    provider_class: str
    receipt_count: int
    completed_count: int
    non_completed_count: int
    incremental_cost_chf: float


class QualityMetricsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    receipt_count: int
    first_pass_rate: MetricValue
    retry_rate: MetricValue
    escalation_rate: MetricValue
    rework_minutes: MetricValue
    validation_checks: dict[str, dict[str, int]]
    routes: list[RouteMetrics]


def _rate_metric(numerator: int, denominator: int) -> MetricValue:
    if denominator == 0:
        return MetricValue(
            status="unavailable",
            reason="no terminal ResultReceiptV1 evidence is persisted",
        )
    return MetricValue(
        status="available",
        numerator=numerator,
        denominator=denominator,
        rate=numerator / denominator,
    )


def build_quality_metrics_snapshot(conn) -> QualityMetricsSnapshot:
    """Return only facts recoverable from routing_receipts and agent_tasks."""
    conn.execute("SET TRANSACTION READ ONLY")
    generated_at = conn.execute("SELECT now() AS value").fetchone()["value"]
    rows = conn.execute(
        """
        SELECT rr.outcome, rr.receipt, task.attempt_count
        FROM routing_receipts AS rr
        JOIN agent_tasks AS task ON task.task_id = rr.task_id
        WHERE task.card->>'projection_kind' = 'operational'
        ORDER BY rr.receipt_id
        """
    ).fetchall()

    receipt_count = len(rows)
    completed_count = sum(row["outcome"] == "completed" for row in rows)
    first_pass_count = sum(
        row["outcome"] == "completed" and row["attempt_count"] == 1
        for row in rows
    )
    retried_count = sum(row["attempt_count"] > 1 for row in rows)
    validations = [row["receipt"].get("validation", {}) for row in rows]
    validation_checks = {
        check: {
            state: sum(item.get(check) == state for item in validations)
            for state in ("passed", "failed", "not_run")
        }
        for check in ("lint", "tests")
    }

    route_totals: dict[tuple[str, str], RouteMetrics] = {}
    for row in rows:
        cost = row["receipt"].get("cost_receipt", {})
        model_ref = cost.get("model_ref")
        provider_class = cost.get("provider_class")
        if not model_ref or not provider_class:
            continue
        key = (model_ref, provider_class)
        metrics = route_totals.setdefault(
            key,
            RouteMetrics(
                model_ref=model_ref,
                provider_class=provider_class,
                receipt_count=0,
                completed_count=0,
                non_completed_count=0,
                incremental_cost_chf=0.0,
            ),
        )
        metrics.receipt_count += 1
        metrics.incremental_cost_chf += float(cost.get("incremental_cost_chf", 0.0))
        if row["outcome"] == "completed":
            metrics.completed_count += 1
        else:
            metrics.non_completed_count += 1

    return QualityMetricsSnapshot(
        generated_at=generated_at,
        receipt_count=receipt_count,
        first_pass_rate=_rate_metric(first_pass_count, receipt_count),
        retry_rate=_rate_metric(retried_count, receipt_count),
        escalation_rate=MetricValue(
            status="unavailable",
            reason="no normalized escalation event is persisted",
        ),
        rework_minutes=MetricValue(
            status="unavailable",
            reason="no rework duration is persisted",
        ),
        validation_checks=validation_checks,
        routes=sorted(
            route_totals.values(),
            key=lambda metric: (metric.model_ref, metric.provider_class),
        ),
    )
