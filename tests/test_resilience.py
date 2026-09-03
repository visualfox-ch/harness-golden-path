from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from harness.app import create_app
from harness.contracts import (
    Budget,
    CostReceipt,
    FailureClass,
    FailureReportV1,
    ProviderClass,
    ResultReceiptV1,
    SideEffectState,
)


def _claimed_task(store, card_factory, *, worker="resilience-worker", **overrides):
    card = card_factory(**overrides)
    store.create_task(card)
    store.claim(card.task_id, "hermes_local", worker)
    return card


def _report(card, failure_class, *, worker="resilience-worker", **overrides):
    base = {
        "task_id": card.task_id,
        "correlation_id": card.correlation_id,
        "worker_instance": worker,
        "failure_class": failure_class,
        "reason": f"Controlled {failure_class.value} failure for resilience test.",
    }
    base.update(overrides)
    return FailureReportV1(**base)


@pytest.mark.parametrize(
    ("failure_class", "expected_action", "expected_status", "extra"),
    [
        (FailureClass.TRANSIENT, "retry_scheduled", "retry_wait", {}),
        (
            FailureClass.RATE_LIMITED,
            "retry_scheduled",
            "retry_wait",
            {"retry_after_seconds": 90},
        ),
        (FailureClass.QUALITY_FAILURE, "retry_scheduled", "retry_wait", {}),
        (
            FailureClass.QUOTA_EXHAUSTED,
            "awaiting_decision",
            "awaiting_decision",
            {},
        ),
        (FailureClass.POLICY_VIOLATION, "blocked", "blocked", {}),
        (FailureClass.PERMANENT, "dead_lettered", "failed", {}),
        (
            FailureClass.UNCERTAIN_SIDE_EFFECT,
            "recovery_required",
            "recovery_required",
            {"side_effect_state": SideEffectState.UNKNOWN},
        ),
    ],
)
def test_each_failure_class_has_a_fail_closed_decision(
    store,
    card_factory,
    failure_class,
    expected_action,
    expected_status,
    extra,
):
    card = _claimed_task(store, card_factory)

    decision = store.report_failure(_report(card, failure_class, **extra))

    assert decision.action == expected_action
    assert decision.task_status.value == expected_status
    assert store.get_task(card.task_id)["status"] == expected_status


def test_unknown_side_effect_overrides_retry_class_and_requires_recovery(
    store, card_factory
):
    card = _claimed_task(store, card_factory)

    decision = store.report_failure(
        _report(
            card,
            FailureClass.TRANSIENT,
            side_effect_state=SideEffectState.UNKNOWN,
        )
    )
    snapshot = store.resilience_snapshot()

    assert decision.action == "recovery_required"
    assert decision.retry_at is None
    assert snapshot.dead_letters == []
    assert snapshot.recovery_cards[0]["task_id"] == str(card.task_id)
    assert snapshot.recovery_cards[0]["allowed_actions"] == [
        "inspect_read_only",
        "confirm_side_effect",
        "request_human_decision",
    ]


def test_exhausted_retry_budget_creates_dead_letter(store, card_factory):
    card = _claimed_task(
        store,
        card_factory,
        budget=Budget(max_runtime_minutes=90, max_attempts=1),
    )

    decision = store.report_failure(_report(card, FailureClass.TRANSIENT))
    snapshot = store.resilience_snapshot()

    assert decision.action == "dead_lettered"
    assert decision.dead_letter_id is not None
    assert snapshot.dead_letters[0]["failure_class"] == "transient"
    assert "retry budget exhausted" in snapshot.dead_letters[0]["reason"]


def test_lease_expiry_creates_read_only_recovery_card(store, card_factory):
    card = card_factory()
    store.create_task(card)
    store.claim(card.task_id, "hermes_local", "expired-worker", lease_minutes=0)

    store.expire_leases()
    snapshot = store.resilience_snapshot()

    assert store.get_task(card.task_id)["status"] == "recovery_required"
    assert snapshot.recovery_cards[0]["trigger"] == "lease_expired"
    assert store.claim(card.task_id, "hermes_local", "blind-retry") is None


def test_circuit_opens_blocks_release_and_closes_after_probe_success(
    store, card_factory
):
    for index in range(3):
        worker = f"circuit-worker-{index}"
        card = _claimed_task(store, card_factory, worker=worker)
        decision = store.report_failure(
            _report(
                card,
                FailureClass.TRANSIENT,
                worker=worker,
                circuit_key="route:anthropic_oauth_reasoner",
            )
        )
    snapshot = store.resilience_snapshot()
    assert decision.circuit_state == "open"
    assert snapshot.circuits[0]["failure_count"] == 3
    assert snapshot.circuits[0]["state"] == "open"

    gated_card = card_factory()
    store.create_task(gated_card)
    assert store.claim(
        gated_card.task_id, "hermes_local", "circuit-gated-worker"
    ) is None

    with store._connect() as conn:
        conn.execute(
            "UPDATE agent_tasks SET next_attempt_at=now() - interval '1 second'"
        )
    assert store.release_due_retries() == []

    with store._connect() as conn:
        conn.execute(
            "UPDATE circuit_breakers SET open_until=now() - interval '1 second'"
        )
    released = store.release_due_retries()
    assert len(released) == 3

    probe = gated_card
    assert store.claim(
        probe.task_id, "hermes_local", "half-open-probe"
    ) is not None
    store.submit_receipt(
        ResultReceiptV1(
            task_id=probe.task_id,
            correlation_id=probe.correlation_id,
            worker_instance="half-open-probe",
            outcome="completed",
            summary="Controlled half-open probe completed successfully.",
            cost_receipt=CostReceipt(
                provider_class=ProviderClass.SUBSCRIPTION_OAUTH,
                model_ref="anthropic_oauth_reasoner",
                quota_status="partial",
            ),
        )
    )
    circuit = store.resilience_snapshot().circuits[0]
    assert circuit["state"] == "closed"
    assert circuit["failure_count"] == 0


def test_resilience_api_and_cockpit_expose_real_recovery_state(
    store, card_factory
):
    card = _claimed_task(store, card_factory)
    store.report_failure(_report(card, FailureClass.PERMANENT))
    client = TestClient(create_app(store))

    response = client.get("/v1/operations/resilience")
    cockpit = client.get("/v1/operations/cockpit")

    assert response.status_code == 200
    assert response.json()["dead_letters"][0]["task_id"] == str(card.task_id)
    risks = cockpit.json()["panels"]["risks"]
    assert risks["status"] == "partial"
    assert risks["data"]["open_dead_letter_count"] == 1


def test_failure_api_rejects_task_id_mismatch(store, card_factory):
    card = _claimed_task(store, card_factory)
    report = _report(card, FailureClass.TRANSIENT).model_dump(mode="json")
    report["task_id"] = "00000000-0000-0000-0000-000000000001"

    response = TestClient(create_app(store)).post(
        f"/v1/tasks/{card.task_id}/failures", json=report
    )

    assert response.status_code == 422


def test_worker_cannot_poison_an_unrelated_circuit(store, card_factory):
    card = _claimed_task(store, card_factory)

    response = TestClient(create_app(store)).post(
        f"/v1/tasks/{card.task_id}/failures",
        json=_report(
            card,
            FailureClass.TRANSIENT,
            circuit_key="route:unrelated_model",
        ).model_dump(mode="json"),
    )

    assert response.status_code == 409
    assert store.get_task(card.task_id)["status"] == "claimed"
    assert store.resilience_snapshot().circuits == []


def test_rate_limit_retry_honors_retry_after(store, card_factory):
    card = _claimed_task(store, card_factory)
    before = datetime.now(timezone.utc)

    decision = store.report_failure(
        _report(
            card,
            FailureClass.RATE_LIMITED,
            retry_after_seconds=120,
        )
    )

    retry_at = datetime.fromisoformat(decision.retry_at)
    assert (retry_at - before).total_seconds() >= 119
