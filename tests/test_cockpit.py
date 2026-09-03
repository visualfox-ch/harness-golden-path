from fastapi.testclient import TestClient

from harness.app import create_app
from harness.contracts import (
    Artifacts,
    CostReceipt,
    ResultReceiptV1,
    ValidationResult,
)
from harness.policy import load_catalog, load_routing_policy


def test_empty_cockpit_marks_missing_evidence_unavailable(store):
    snapshot = store.cockpit_snapshot(load_catalog(), load_routing_policy())
    panels = snapshot.panels

    assert panels.workers.status == "unavailable"
    assert panels.quota_cost.status == "unavailable"
    assert panels.quality.status == "unavailable"
    assert panels.knowledge.status == "unavailable"
    assert panels.flow.data["task_count"] == 0
    assert panels.routing.data["api_metered"] == "disabled"
    assert panels.routing.data["api_metered_allowed"] is False
    assert all(panel["sources"] for panel in panels.model_dump().values())


def test_cockpit_aggregates_persisted_worker_receipt_and_artifact_data(
    store, card_factory
):
    card = card_factory()
    store.create_task(card)
    store.claim(card.task_id, "hermes_local", "worker-cockpit")
    store.submit_receipt(
        ResultReceiptV1(
            task_id=card.task_id,
            correlation_id=card.correlation_id,
            worker_instance="worker-cockpit",
            outcome="completed",
            summary="Cockpit evidence receipt with passing validation.",
            artifacts=Artifacts(
                pull_request="PR-TEST-1",
                test_report="tests/test_cockpit.py",
            ),
            validation=ValidationResult(lint="passed", tests="passed"),
            cost_receipt=CostReceipt(
                provider_class="subscription_oauth",
                model_ref="anthropic_oauth_reasoner",
                quota_status="partial",
            ),
        )
    )

    panels = store.cockpit_snapshot(
        load_catalog(), load_routing_policy()
    ).panels

    assert panels.workers.status == "available"
    assert panels.workers.data["workers"][0]["owner_instance"] == "worker-cockpit"
    assert panels.model_oauth.data["observed_models"] == [
        "anthropic_oauth_reasoner"
    ]
    assert panels.model_oauth.data["live_oauth_probe"] == "unavailable"
    assert panels.quota_cost.status == "partial"
    assert panels.quota_cost.data["incremental_cost_chf"] == 0.0
    assert panels.quota_cost.data["quota_statuses"] == ["partial"]
    assert panels.quality.data["checks"]["tests"]["passed"] == 1
    assert panels.knowledge.status == "available"
    assert panels.knowledge.data["artifact_count"] == 2
    assert panels.flow.data["status_counts"]["review"] == 1
    assert panels.routing.data["observed_provider_classes"] == [
        "subscription_oauth"
    ]


def test_cockpit_reports_expired_active_lease_as_risk(store, card_factory):
    card = card_factory()
    store.create_task(card)
    store.claim(card.task_id, "hermes_local", "expired-worker", lease_minutes=0)

    risks = store.cockpit_snapshot(
        load_catalog(), load_routing_policy()
    ).panels.risks

    assert risks.status == "partial"
    assert risks.data["expired_active_lease_count"] == 1


def test_cockpit_api_returns_all_defined_panels(store):
    response = TestClient(create_app(store)).get("/v1/operations/cockpit")

    assert response.status_code == 200
    assert set(response.json()["panels"]) == {
        "workers",
        "model_oauth",
        "quota_cost",
        "quality",
        "flow",
        "risks",
        "knowledge",
        "routing",
    }
