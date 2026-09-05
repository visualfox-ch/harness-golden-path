"""Read-only System-Cockpit aus Store- und Policy-Evidenz."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .contracts import TaskStatus


class CockpitPanel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "partial", "unavailable", "disabled"]
    sources: list[str]
    data: dict[str, Any]


class CockpitPanels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workers: CockpitPanel
    model_oauth: CockpitPanel
    quota_cost: CockpitPanel
    quality: CockpitPanel
    flow: CockpitPanel
    risks: CockpitPanel
    knowledge: CockpitPanel
    routing: CockpitPanel


class CockpitSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    panels: CockpitPanels


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def build_cockpit_snapshot(
    conn, catalog: dict, routing_policy: dict
) -> CockpitSnapshot:
    """Aggregate only persisted state; missing evidence remains unavailable."""
    conn.execute("SET TRANSACTION READ ONLY")
    generated_at = conn.execute("SELECT now() AS value").fetchone()["value"]

    worker_rows = conn.execute(
        """
        SELECT owner_role, owner_instance, count(*) AS task_count,
               count(*) FILTER (WHERE status IN ('claimed', 'in_progress'))
                   AS active_task_count,
               max(heartbeat_at) AS last_heartbeat_at,
               max(lease_expires_at) AS latest_lease_expires_at
        FROM agent_tasks
        WHERE owner_instance IS NOT NULL
        GROUP BY owner_role, owner_instance
        ORDER BY owner_role, owner_instance
        """
    ).fetchall()
    workers = [{key: _iso(value) for key, value in row.items()} for row in worker_rows]

    receipt_rows = conn.execute(
        """
        SELECT outcome, receipt, created_at
        FROM routing_receipts
        ORDER BY receipt_id
        """
    ).fetchall()
    receipts = [row["receipt"] for row in receipt_rows]
    cost_receipts = [
        receipt.get("cost_receipt", {})
        for receipt in receipts
        if receipt.get("cost_receipt")
    ]
    observed_models = sorted(
        {item["model_ref"] for item in cost_receipts if item.get("model_ref")}
    )
    observed_provider_classes = sorted(
        {item["provider_class"] for item in cost_receipts if item.get("provider_class")}
    )

    configured_models = catalog.get("models", {})
    verified_oauth_routes = sorted(
        name
        for name, model in configured_models.items()
        if model.get("provider_class") == "subscription_oauth"
        and model.get("connection_status") == "verified"
    )

    quota_statuses = sorted(
        {item["quota_status"] for item in cost_receipts if item.get("quota_status")}
    )
    quota_visibility = sorted(
        {
            model.get("quota_visibility", "unavailable")
            for model in configured_models.values()
            if model.get("provider_class") == "subscription_oauth"
        }
    )
    incremental_cost = sum(
        float(item.get("incremental_cost_chf", 0.0)) for item in cost_receipts
    )
    quota_panel_status = "unavailable"
    if cost_receipts:
        quota_panel_status = (
            "available"
            if quota_statuses
            and set(quota_statuses) == {"available"}
            and set(quota_visibility) == {"full"}
            else "partial"
        )

    validations = [receipt.get("validation", {}) for receipt in receipts]
    validation_counts = {
        check: {
            state: sum(1 for item in validations if item.get(check) == state)
            for state in ("passed", "failed", "not_run")
        }
        for check in ("lint", "tests")
    }

    status_rows = conn.execute(
        "SELECT status, count(*) AS count FROM agent_tasks GROUP BY status"
    ).fetchall()
    status_counts = {status.value: 0 for status in TaskStatus}
    status_counts.update({row["status"]: row["count"] for row in status_rows})
    event_summary = conn.execute(
        """
        SELECT count(*) AS event_count, max(created_at) AS last_event_at
        FROM agent_events
        """
    ).fetchone()

    risk_summary = conn.execute(
        """
        SELECT
          count(*) FILTER (WHERE status = 'blocked') AS blocked_count,
          count(*) FILTER (WHERE status = 'failed') AS failed_count,
          count(*) FILTER (WHERE status = 'recovery_required') AS recovery_count,
          count(*) FILTER (
            WHERE status IN ('claimed', 'in_progress')
              AND lease_expires_at < now()
          ) AS expired_active_lease_count,
          (SELECT count(*) FROM dead_letters WHERE resolved_at IS NULL)
              AS open_dead_letter_count,
          (SELECT count(*) FROM recovery_cards WHERE status = 'open')
              AS open_recovery_card_count,
          (SELECT count(*) FROM circuit_breakers
           WHERE state = 'open' AND open_until > now())
              AS open_circuit_count
        FROM agent_tasks
        """
    ).fetchone()
    has_risk = any(value > 0 for value in risk_summary.values())

    artifacts = [
        value
        for receipt in receipts
        for value in (receipt.get("artifacts") or {}).values()
        if value
    ]
    knowledge_event_count = conn.execute(
        """
        SELECT count(*) AS count FROM agent_events
        WHERE event_type IN ('knowledge_captured', 'handover_recorded')
        """
    ).fetchone()["count"]
    knowledge_available = bool(artifacts or knowledge_event_count)

    routing_defaults = routing_policy.get("defaults", {})
    routing_classes = routing_policy.get("classes", {})

    return CockpitSnapshot(
        generated_at=generated_at,
        panels=CockpitPanels(
            workers=CockpitPanel(
                status="available" if workers else "unavailable",
                sources=["agent_tasks.owner_instance", "agent_tasks.heartbeat_at"],
                data={"workers": workers},
            ),
            model_oauth=CockpitPanel(
                status="available" if verified_oauth_routes else "unavailable",
                sources=[
                    "policies/model-catalog.yaml",
                    "routing_receipts.receipt.cost_receipt",
                ],
                data={
                    "verified_oauth_routes": verified_oauth_routes,
                    "observed_models": observed_models,
                    "live_oauth_probe": "unavailable",
                },
            ),
            quota_cost=CockpitPanel(
                status=quota_panel_status,
                sources=[
                    "routing_receipts.receipt.cost_receipt",
                    "policies/model-catalog.yaml",
                ],
                data={
                    "receipt_count": len(cost_receipts),
                    "incremental_cost_chf": incremental_cost,
                    "quota_statuses": quota_statuses,
                    "quota_visibility": quota_visibility or ["unavailable"],
                },
            ),
            quality=CockpitPanel(
                status="available" if validations else "unavailable",
                sources=["routing_receipts.receipt.validation"],
                data={"receipt_count": len(validations), "checks": validation_counts},
            ),
            flow=CockpitPanel(
                status="available",
                sources=["agent_tasks.status", "agent_events.created_at"],
                data={
                    "task_count": sum(status_counts.values()),
                    "status_counts": status_counts,
                    "event_count": event_summary["event_count"],
                    "last_event_at": _iso(event_summary["last_event_at"]),
                },
            ),
            risks=CockpitPanel(
                status="partial" if has_risk else "available",
                sources=[
                    "agent_tasks.status",
                    "agent_tasks.lease_expires_at",
                    "dead_letters.resolved_at",
                    "recovery_cards.status",
                    "circuit_breakers.state",
                ],
                data=dict(risk_summary),
            ),
            knowledge=CockpitPanel(
                status="available" if knowledge_available else "unavailable",
                sources=[
                    "routing_receipts.receipt.artifacts",
                    "agent_events.event_type",
                ],
                data={
                    "artifact_count": len(artifacts),
                    "knowledge_event_count": knowledge_event_count,
                },
            ),
            routing=CockpitPanel(
                status="available",
                sources=[
                    "policies/routing-policy.yaml",
                    "policies/model-catalog.yaml",
                    "routing_receipts.receipt.cost_receipt",
                ],
                data={
                    "api_metered": "disabled",
                    "api_metered_allowed": bool(
                        routing_defaults.get("api_metered_allowed", False)
                    ),
                    "configured_classes": sorted(routing_classes),
                    "verified_routes": sorted(configured_models),
                    "observed_provider_classes": observed_provider_classes,
                },
            ),
        ),
    )
