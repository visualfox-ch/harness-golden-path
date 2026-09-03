from fastapi.testclient import TestClient

from harness.app import create_app
from harness.contracts import ProjectionKind
from harness.projection import (
    ObservedPandaTask,
    ProjectionCheckpoint,
    plan_projection,
)


def test_incremental_projection_uses_authoritative_event_cursor(store, card_factory):
    card = card_factory()
    store.create_task(card)

    first = store.pandaos_projection_snapshot()
    assert first.direction == "harness_to_pandaos"
    assert first.through_event_id > 0
    assert len(first.tasks) == 1
    assert first.tasks[0].status == "pending"

    repeated = store.pandaos_projection_snapshot(first.through_event_id)
    assert repeated.tasks == []
    assert repeated.through_event_id == first.through_event_id

    store.claim(card.task_id, "hermes_local", "svc-hermes-local:test")
    changed = store.pandaos_projection_snapshot(first.through_event_id)
    assert len(changed.tasks) == 1
    assert changed.tasks[0].status == "in_progress"
    assert changed.tasks[0].source_event_id > first.through_event_id


def test_full_projection_rebuild_returns_all_tasks(store, card_factory):
    first = card_factory()
    second = card_factory(title="Second projection task")
    store.create_task(first)
    store.create_task(second)
    cursor = store.pandaos_projection_snapshot().through_event_id

    rebuilt = store.pandaos_projection_snapshot(cursor, full=True)

    assert rebuilt.mode == "full"
    assert {item.harness_task_id for item in rebuilt.tasks} == {
        str(first.task_id),
        str(second.task_id),
    }


def test_projection_excludes_task_content_and_receipts(store, card_factory):
    card = card_factory(
        objective="SENSITIVE-CONTENT must never enter the PandaOS projection.",
    )
    store.create_task(card)

    payload = store.pandaos_projection_snapshot().model_dump_json()

    assert "SENSITIVE-CONTENT" not in payload
    assert "scope" not in payload
    assert "model_routing" not in payload


def test_evidence_projection_is_explicit_and_never_actionable(store, card_factory):
    card = card_factory(
        projection_kind=ProjectionKind.EVIDENCE,
        title="Ordinary title without proof heuristic",
    )
    store.create_task(card)

    item = store.pandaos_projection_snapshot().tasks[0]

    assert item.is_proof is True
    assert item.action_required is False
    assert item.active_form.startswith("Evidence fixture")


def test_projection_planner_is_idempotent_and_rejects_old_events(store, card_factory):
    card = card_factory()
    store.create_task(card)
    item = store.pandaos_projection_snapshot().tasks[0]
    checkpoint = ProjectionCheckpoint(
        panda_task_id="t-project-1",
        source_event_id=item.source_event_id,
        fingerprint=item.fingerprint,
    )

    assert plan_projection(item, None).action == "create"
    assert plan_projection(item, checkpoint).action == "noop"

    older = item.model_copy(update={"source_event_id": item.source_event_id - 1})
    assert plan_projection(older, checkpoint).action == "ignore_old"


def test_projection_planner_repairs_panda_drift(store, card_factory):
    card = card_factory()
    store.create_task(card)
    item = store.pandaos_projection_snapshot().tasks[0]
    checkpoint = ProjectionCheckpoint(
        panda_task_id="t-project-1",
        source_event_id=item.source_event_id,
        fingerprint=item.fingerprint,
    )
    observed = ObservedPandaTask(
        subject="Manually changed",
        status=item.status,
        active_form=item.active_form,
    )

    operation = plan_projection(item, checkpoint, observed)

    assert operation.action == "repair"
    assert operation.reason == "panda_projection_drift"


def test_projection_planner_advances_cursor_without_target_write(
    store, card_factory
):
    card = card_factory()
    store.create_task(card)
    item = store.pandaos_projection_snapshot().tasks[0]
    checkpoint = ProjectionCheckpoint(
        panda_task_id="t-project-1",
        source_event_id=item.source_event_id,
        fingerprint=item.fingerprint,
    )
    newer_same_display = item.model_copy(
        update={"source_event_id": item.source_event_id + 1}
    )

    assert plan_projection(newer_same_display, checkpoint).action == "advance_cursor"


def test_projection_api_is_read_only_and_cursor_ahead_fails_closed(
    store, card_factory
):
    store.create_task(card_factory())
    client = TestClient(create_app(store))

    response = client.get("/v1/projections/pandaos")
    assert response.status_code == 200
    assert response.json()["source_authority"] == "control_harness"

    ahead = client.get("/v1/projections/pandaos?after_event_id=999999")
    assert ahead.status_code == 409

    rebuilt = client.get(
        "/v1/projections/pandaos?after_event_id=999999&full=true"
    )
    assert rebuilt.status_code == 200
    assert rebuilt.json()["mode"] == "full"
    assert rebuilt.json()["from_event_id"] == 0

    assert client.post("/v1/projections/pandaos", json={}).status_code == 405
