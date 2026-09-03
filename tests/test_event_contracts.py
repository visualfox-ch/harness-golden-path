from uuid import uuid4

import pytest
from pydantic import ValidationError

from harness.contracts import (
    EVENT_PAYLOAD_FIELDS,
    EventEnvelopeV1,
    HarnessEventType,
    utcnow,
)


def _event(event_type: HarnessEventType, payload: dict[str, object]) -> EventEnvelopeV1:
    return EventEnvelopeV1(
        event_id=1,
        task_id=uuid4(),
        correlation_id=uuid4(),
        event_type=event_type,
        payload=payload,
        created_at=utcnow(),
    )


@pytest.mark.parametrize("event_type", list(HarnessEventType))
def test_every_registered_event_type_has_an_exact_payload_contract(event_type):
    payload = {field: f"evidence-{field}" for field in EVENT_PAYLOAD_FIELDS[event_type]}

    assert _event(event_type, payload).event_type == event_type


def test_event_contract_rejects_unknown_event_types_and_payload_drift():
    with pytest.raises(ValidationError, match="event_type"):
        EventEnvelopeV1(
            event_id=1,
            task_id=uuid4(),
            correlation_id=uuid4(),
            event_type="unregistered_event",
            payload={},
            created_at=utcnow(),
        )

    with pytest.raises(ValidationError, match="payload drift"):
        _event(HarnessEventType.TASK_CREATED, {"title": "only one field"})
    with pytest.raises(ValidationError, match="payload drift"):
        _event(
            HarnessEventType.TASK_CREATED,
            {"title": "safe", "project": "harness", "objective": "secret"},
        )


def test_persisted_trace_and_panda_projection_fulfil_v1_contracts(
    store, card_factory
):
    card = card_factory()
    store.create_task(card)
    store.claim(card.task_id, "hermes_local", "event-contract-worker")

    trace = store.trace(card.correlation_id)
    envelopes = [EventEnvelopeV1(**event) for event in trace]
    projection = store.pandaos_projection_snapshot().tasks[0]

    assert [event.event_type for event in envelopes] == [
        HarnessEventType.TASK_CREATED,
        HarnessEventType.TASK_CLAIMED,
    ]
    assert projection.source_event_id == envelopes[-1].event_id
    assert set(projection.model_dump()) == {
        "projection_key",
        "harness_task_id",
        "correlation_id",
        "source_event_id",
        "subject",
        "project",
        "status",
        "active_form",
        "owner_instance",
        "attempt_count",
        "approval_required",
        "action_required",
        "is_proof",
        "fingerprint",
        "trace_ref",
    }
