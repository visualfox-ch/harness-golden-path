import pytest
from fastapi.testclient import TestClient

from harness.app import create_app
from harness.contracts import (
    CostReceipt,
    ModelRouting,
    ProviderClass,
    ResultReceiptV1,
    ValidationResult,
)


@pytest.fixture()
def client(store):
    return TestClient(create_app(store))


def test_full_golden_path_flow(client, card_factory):
    card = card_factory()
    payload = card.model_dump(mode="json")

    created = client.post("/v1/tasks", json=payload)
    assert created.status_code == 201
    assert created.json()["created"] is True
    task_id = created.json()["task"]["task_id"]

    # Idempotenter zweiter Aufruf liefert dieselbe Task
    duplicate = client.post("/v1/tasks", json=payload)
    assert duplicate.json()["created"] is False
    assert duplicate.json()["task"]["task_id"] == task_id

    claimed = client.post(
        f"/v1/tasks/{task_id}/claim",
        json={"owner_role": "hermes_local", "worker_id": "meister-splinter"},
    )
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "claimed"

    # Konkurrierender Claim wird abgewiesen
    second_claim = client.post(
        f"/v1/tasks/{task_id}/claim",
        json={"owner_role": "hermes_local", "worker_id": "worker-2"},
    )
    assert second_claim.status_code == 409

    beat = client.post(
        f"/v1/tasks/{task_id}/heartbeat", json={"worker_id": "meister-splinter"}
    )
    assert beat.status_code == 200

    receipt = ResultReceiptV1(
        task_id=card.task_id,
        correlation_id=card.correlation_id,
        worker_instance="meister-splinter",
        outcome="completed",
        summary="Docs-Abschnitt ergänzt, gezielte Tests grün.",
        validation=ValidationResult(tests="passed"),
        cost_receipt=CostReceipt(
            provider_class=ProviderClass.SUBSCRIPTION_OAUTH,
            model_ref="anthropic_oauth_reasoner",
            quota_status="partial",
        ),
    )
    accepted = client.post(
        f"/v1/tasks/{task_id}/receipts", json=receipt.model_dump(mode="json")
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "review"

    approval = client.post(
        f"/v1/tasks/{task_id}/approval-requests",
        json={"action": "merge_main", "requested_by": "panda_os"},
    )
    assert approval.status_code == 201
    approval_id = approval.json()["approval_id"]
    assert client.get(f"/v1/tasks/{task_id}").json()["status"] == "awaiting_approval"

    decided = client.post(
        f"/v1/approvals/{approval_id}/decisions",
        json={"decision": "approved", "decided_by": "micha"},
    )
    assert decided.status_code == 200
    assert decided.json()["task_status"] == "done"

    trace = client.get(f"/v1/traces/{card.correlation_id}").json()["events"]
    event_types = [e["event_type"] for e in trace]
    for expected in (
        "task_created",
        "task_claimed",
        "receipt_accepted",
        "approval_requested",
        "approval_decided",
    ):
        assert expected in event_types


def test_receipt_from_non_owner_is_rejected(client, card_factory):
    card = card_factory()
    client.post("/v1/tasks", json=card.model_dump(mode="json"))
    client.post(
        f"/v1/tasks/{card.task_id}/claim",
        json={"owner_role": "hermes_local", "worker_id": "worker-a"},
    )
    receipt = ResultReceiptV1(
        task_id=card.task_id,
        correlation_id=card.correlation_id,
        worker_instance="worker-imposter",
        outcome="completed",
        summary="Behaupteter Abschluss ohne Lease-Ownership.",
        cost_receipt=CostReceipt(
            provider_class=ProviderClass.SUBSCRIPTION_OAUTH,
            model_ref="anthropic_oauth_reasoner",
        ),
    )
    response = client.post(
        f"/v1/tasks/{card.task_id}/receipts", json=receipt.model_dump(mode="json")
    )
    assert response.status_code == 409


def test_failed_outcome_creates_failure_state(client, card_factory):
    card = card_factory()
    client.post("/v1/tasks", json=card.model_dump(mode="json"))
    client.post(
        f"/v1/tasks/{card.task_id}/claim",
        json={"owner_role": "hermes_local", "worker_id": "worker-a"},
    )
    receipt = ResultReceiptV1(
        task_id=card.task_id,
        correlation_id=card.correlation_id,
        worker_instance="worker-a",
        outcome="failed",
        summary="Gezielte Tests rot: absichtlicher Failure-Path-Test.",
        validation=ValidationResult(tests="failed"),
        cost_receipt=CostReceipt(
            provider_class=ProviderClass.SUBSCRIPTION_OAUTH,
            model_ref="anthropic_oauth_reasoner",
        ),
    )
    response = client.post(
        f"/v1/tasks/{card.task_id}/receipts", json=receipt.model_dump(mode="json")
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_unknown_model_route_is_rejected_by_policy(client, card_factory):
    card = card_factory(
        model_routing=ModelRouting(
            permitted_provider_classes=[ProviderClass.SUBSCRIPTION_OAUTH],
            default_route=["model_not_in_catalog"],
        )
    )
    response = client.post("/v1/tasks", json=card.model_dump(mode="json"))
    assert response.status_code == 403
