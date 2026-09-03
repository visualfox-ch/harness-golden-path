from fastapi.testclient import TestClient

from harness.app import create_app
from harness.contracts import (
    CostReceipt,
    ResultReceiptV1,
    ValidationResult,
)


def _submit_receipt(store, card, worker_id: str, outcome: str = "completed"):
    store.create_task(card)
    store.claim(card.task_id, "hermes_local", worker_id)
    return store.submit_receipt(
        ResultReceiptV1(
            task_id=card.task_id,
            correlation_id=card.correlation_id,
            worker_instance=worker_id,
            outcome=outcome,
            summary="Persisted evidence for routing and quality metric aggregation.",
            validation=ValidationResult(lint="passed", tests="passed"),
            cost_receipt=CostReceipt(
                provider_class="subscription_oauth",
                model_ref="anthropic_oauth_reasoner",
                quota_status="partial",
            ),
        )
    )


def test_metrics_keep_unknown_operational_values_unavailable(store):
    snapshot = store.quality_metrics_snapshot()

    assert snapshot.receipt_count == 0
    assert snapshot.first_pass_rate.status == "unavailable"
    assert snapshot.retry_rate.status == "unavailable"
    assert snapshot.escalation_rate.status == "unavailable"
    assert snapshot.rework_minutes.status == "unavailable"
    assert snapshot.routes == []


def test_metrics_aggregate_first_pass_retry_and_route_evidence(store, card_factory):
    _submit_receipt(store, card_factory(), "first-pass-worker")
    retried_card = card_factory()
    store.create_task(retried_card)
    store.claim(retried_card.task_id, "hermes_local", "retry-worker")
    store.heartbeat(retried_card.task_id, "retry-worker", lease_minutes=0)
    store.expire_leases()
    store.claim(retried_card.task_id, "hermes_local", "retry-worker")
    store.submit_receipt(
        ResultReceiptV1(
            task_id=retried_card.task_id,
            correlation_id=retried_card.correlation_id,
            worker_instance="retry-worker",
            outcome="completed",
            summary="Completed after the persisted lease recovery path.",
            validation=ValidationResult(lint="passed", tests="failed"),
            cost_receipt=CostReceipt(
                provider_class="subscription_oauth",
                model_ref="anthropic_oauth_reasoner",
                quota_status="partial",
            ),
        )
    )

    snapshot = store.quality_metrics_snapshot()

    assert snapshot.receipt_count == 2
    assert snapshot.first_pass_rate.model_dump() == {
        "status": "available",
        "numerator": 1,
        "denominator": 2,
        "rate": 0.5,
        "reason": None,
    }
    assert snapshot.retry_rate.rate == 0.5
    assert snapshot.validation_checks["tests"] == {
        "passed": 1,
        "failed": 1,
        "not_run": 0,
    }
    assert snapshot.routes[0].model_ref == "anthropic_oauth_reasoner"
    assert snapshot.routes[0].receipt_count == 2
    assert snapshot.routes[0].incremental_cost_chf == 0.0


def test_metrics_api_is_read_only(store):
    response = TestClient(create_app(store)).get("/v1/operations/metrics")

    assert response.status_code == 200
    assert response.json()["receipt_count"] == 0
