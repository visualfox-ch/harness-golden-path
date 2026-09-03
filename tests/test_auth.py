from fastapi.testclient import TestClient

from harness.app import create_app
from harness.auth import TokenAuthorizer
from harness.contracts import ExecutorRole, ModelRouting, ProviderClass


def _secured_client(store, tmp_path, monkeypatch):
    orchestrator = tmp_path / "orchestrator-token"
    worker = tmp_path / "worker-token"
    orchestrator.write_text("o" * 48, encoding="utf-8")
    worker.write_text("w" * 48, encoding="utf-8")
    monkeypatch.setenv("HARNESS_AUTH_REQUIRED", "1")
    monkeypatch.setenv("HARNESS_ORCHESTRATOR_TOKEN_FILE", str(orchestrator))
    monkeypatch.setenv("HARNESS_NAS_WORKER_TOKEN_FILE", str(worker))
    return (
        TestClient(create_app(store, TokenAuthorizer.from_environment())),
        orchestrator,
        worker,
    )


def _bearer(value):
    return {"Authorization": f"Bearer {value}"}


def test_nas_token_is_limited_to_its_role(
    store, card_factory, tmp_path, monkeypatch
):
    client, _, _ = _secured_client(store, tmp_path, monkeypatch)
    assert client.get("/health").status_code == 200
    assert client.post("/v1/tasks", json={}).status_code == 401
    assert client.post(
        "/v1/tasks", json={}, headers=_bearer("w" * 48)
    ).status_code == 403

    card = card_factory(
        owner_role=ExecutorRole.HERMES_NAS,
        task_class="read_only_monitor",
        approval_required=[],
        model_routing=ModelRouting(
            permitted_provider_classes=[ProviderClass.LOCAL_MODEL],
            default_route=["deterministic_monitor"],
        ),
    )
    created = client.post(
        "/v1/tasks",
        json=card.model_dump(mode="json"),
        headers=_bearer("o" * 48),
    )
    assert created.status_code == 201
    wrong_role = client.post(
        f"/v1/tasks/{card.task_id}/claim",
        json={"owner_role": "hermes_local", "worker_id": "svc-hermes-nas:p2-4"},
        headers=_bearer("w" * 48),
    )
    assert wrong_role.status_code == 403
    claimed = client.post(
        f"/v1/tasks/{card.task_id}/claim",
        json={"owner_role": "hermes_nas", "worker_id": "svc-hermes-nas:p2-4"},
        headers=_bearer("w" * 48),
    )
    assert claimed.status_code == 200
    foreign_failure = client.post(
        f"/v1/tasks/{card.task_id}/failures",
        json={
            "schema_version": 1,
            "task_id": str(card.task_id),
            "correlation_id": str(card.correlation_id),
            "worker_instance": "another-worker",
            "failure_class": "permanent",
            "reason": "must be rejected before store mutation",
            "side_effect_state": "none",
        },
        headers=_bearer("w" * 48),
    )
    assert foreign_failure.status_code == 403


def test_token_rotation_revokes_old_worker_token_immediately(
    store, card_factory, tmp_path, monkeypatch
):
    client, _, worker = _secured_client(store, tmp_path, monkeypatch)
    card = card_factory(
        owner_role=ExecutorRole.HERMES_NAS,
        task_class="read_only_monitor",
        approval_required=[],
        model_routing=ModelRouting(
            permitted_provider_classes=[ProviderClass.LOCAL_MODEL],
            default_route=["deterministic_monitor"],
        ),
    )
    client.post(
        "/v1/tasks",
        json=card.model_dump(mode="json"),
        headers=_bearer("o" * 48),
    )
    assert client.get(
        f"/v1/tasks/{card.task_id}", headers=_bearer("w" * 48)
    ).status_code == 200
    worker.write_text("n" * 48, encoding="utf-8")
    assert client.get(
        f"/v1/tasks/{card.task_id}", headers=_bearer("w" * 48)
    ).status_code == 401
    assert client.get(
        f"/v1/tasks/{card.task_id}", headers=_bearer("n" * 48)
    ).status_code == 200
