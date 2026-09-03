"""FastAPI-Oberfläche des Golden-Path-Harness (Kern-API-Subset)."""
from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from .contracts import ApprovalCardV1, ResultReceiptV1, TaskCardV1, TaskStatus
from .cockpit import CockpitSnapshot
from .policy import (
    PolicyError,
    assert_route_allowed,
    load_catalog,
    load_routing_policy,
)
from .store import NotFoundError, OwnershipError, Store, StoreError, TransitionError


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimRequest(StrictBody):
    owner_role: str
    worker_id: str
    lease_minutes: int = 10


class HeartbeatRequest(StrictBody):
    worker_id: str
    lease_minutes: int = 10


class ApprovalRequest(StrictBody):
    action: str
    requested_by: str


class DecisionRequest(StrictBody):
    decision: str
    decided_by: str
    reason: str = ""


def _to_http(exc: StoreError) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


def create_app(store: Store | None = None) -> FastAPI:
    app = FastAPI(title="control-harness-golden-path", version="0.1.0")
    st = store or Store()
    st.init_db()
    catalog = load_catalog()
    routing_policy = load_routing_policy()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/operations/cockpit", response_model=CockpitSnapshot)
    def cockpit() -> CockpitSnapshot:
        return st.cockpit_snapshot(catalog, routing_policy)

    @app.post("/v1/tasks", status_code=201)
    def create_task(card: TaskCardV1) -> dict:
        try:
            assert_route_allowed(card.model_routing, catalog)
        except PolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        task, created = st.create_task(card)
        return {"task": task, "created": created}

    @app.get("/v1/tasks/{task_id}")
    def get_task(task_id: UUID) -> dict:
        try:
            return st.get_task(task_id)
        except StoreError as exc:
            raise _to_http(exc) from exc

    @app.post("/v1/tasks/{task_id}/claim")
    def claim(task_id: UUID, body: ClaimRequest) -> dict:
        row = st.claim(task_id, body.owner_role, body.worker_id, body.lease_minutes)
        if row is None:
            raise HTTPException(
                status_code=409,
                detail="task not claimable (not ready or lease still active)",
            )
        return row

    @app.post("/v1/tasks/{task_id}/heartbeat")
    def heartbeat(task_id: UUID, body: HeartbeatRequest) -> dict:
        try:
            return st.heartbeat(task_id, body.worker_id, body.lease_minutes)
        except StoreError as exc:
            raise _to_http(exc) from exc

    @app.post("/v1/tasks/{task_id}/receipts")
    def receipts(task_id: UUID, receipt: ResultReceiptV1) -> dict:
        if receipt.task_id != task_id:
            raise HTTPException(status_code=422, detail="task_id mismatch")
        try:
            return st.submit_receipt(receipt)
        except StoreError as exc:
            raise _to_http(exc) from exc

    @app.post("/v1/tasks/{task_id}/approval-requests", status_code=201)
    def approval_request(task_id: UUID, body: ApprovalRequest) -> dict:
        card = ApprovalCardV1(
            task_id=task_id, action=body.action, requested_by=body.requested_by
        )
        try:
            return st.request_approval(card)
        except StoreError as exc:
            raise _to_http(exc) from exc

    @app.post("/v1/approvals/{approval_id}/decisions")
    def decide(approval_id: UUID, body: DecisionRequest) -> dict:
        try:
            return st.decide_approval(
                approval_id, body.decision, body.decided_by, body.reason
            )
        except StoreError as exc:
            raise _to_http(exc) from exc

    @app.get("/v1/traces/{correlation_id}")
    def trace(correlation_id: UUID) -> dict:
        return {"events": st.trace(correlation_id)}

    @app.post("/v1/maintenance/expire-leases")
    def expire_leases() -> dict:
        return {"expired": st.expire_leases()}

    return app


app = create_app()
