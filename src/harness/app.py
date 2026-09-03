"""FastAPI-Oberfläche des Golden-Path-Harness (Kern-API-Subset)."""
from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .contracts import (
    ApprovalCardV1,
    FailureReportV1,
    ResultReceiptV1,
    TaskCardV1,
    TaskStatus,
)
from .cockpit import CockpitSnapshot
from .metrics import QualityMetricsSnapshot
from .auth import (
    AuthenticationError,
    AuthorizationError,
    Principal,
    TokenAuthorizer,
    required_scope,
)
from .policy import (
    PolicyError,
    assert_task_allowed,
    load_agent_policy,
    load_catalog,
    load_data_policy,
    load_routing_policy,
    load_task_authority_policy,
)
from .projection import PandaProjectionBatch, ProjectionCursorError
from .resilience import ResilienceDecision, ResilienceSnapshot
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


def create_app(
    store: Store | None = None,
    authorizer: TokenAuthorizer | None = None,
) -> FastAPI:
    app = FastAPI(title="control-harness-golden-path", version="0.1.0")
    st = store or Store()
    st.init_db()
    auth = authorizer or TokenAuthorizer.from_environment()
    catalog = load_catalog()
    routing_policy = load_routing_policy()
    agent_policy = load_agent_policy()
    data_policy = load_data_policy()
    task_authority_policy = load_task_authority_policy()

    @app.middleware("http")
    async def authorize(request: Request, call_next):
        scope = required_scope(request.method, request.url.path)
        if scope is None:
            return await call_next(request)
        try:
            request.state.harness_principal = auth.authenticate(
                request.headers.get("Authorization"), scope
            )
        except AuthenticationError as exc:
            return JSONResponse(status_code=401, content={"detail": str(exc)})
        except AuthorizationError as exc:
            return JSONResponse(status_code=403, content={"detail": str(exc)})
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/operations/cockpit", response_model=CockpitSnapshot)
    def cockpit() -> CockpitSnapshot:
        return st.cockpit_snapshot(catalog, routing_policy)

    @app.get("/v1/operations/metrics", response_model=QualityMetricsSnapshot)
    def quality_metrics() -> QualityMetricsSnapshot:
        return st.quality_metrics_snapshot()

    @app.get(
        "/v1/projections/pandaos",
        response_model=PandaProjectionBatch,
    )
    def pandaos_projection(after_event_id: int = 0, full: bool = False):
        if after_event_id < 0:
            raise HTTPException(
                status_code=422, detail="after_event_id must be non-negative"
            )
        try:
            return st.pandaos_projection_snapshot(after_event_id, full)
        except ProjectionCursorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/tasks", status_code=201)
    def create_task(card: TaskCardV1) -> dict:
        try:
            assert_task_allowed(
                card,
                catalog,
                routing_policy,
                agent_policy,
                data_policy,
                task_authority_policy,
            )
        except PolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        task, created = st.create_task(card)
        return {"task": task, "created": created}

    @app.get("/v1/tasks/{task_id}")
    def get_task(task_id: UUID, request: Request) -> dict:
        try:
            task = st.get_task(task_id)
        except StoreError as exc:
            raise _to_http(exc) from exc
        _enforce_nas_task_access(request, task)
        return task

    @app.post("/v1/tasks/{task_id}/claim")
    def claim(task_id: UUID, body: ClaimRequest, request: Request) -> dict:
        _enforce_nas_worker(request, body.owner_role, body.worker_id)
        row = st.claim(task_id, body.owner_role, body.worker_id, body.lease_minutes)
        if row is None:
            raise HTTPException(
                status_code=409,
                detail="task not claimable (status, lease, or circuit gate)",
            )
        return row

    @app.post("/v1/tasks/{task_id}/heartbeat")
    def heartbeat(task_id: UUID, body: HeartbeatRequest, request: Request) -> dict:
        _enforce_nas_worker(request, "hermes_nas", body.worker_id)
        try:
            return st.heartbeat(task_id, body.worker_id, body.lease_minutes)
        except StoreError as exc:
            raise _to_http(exc) from exc

    @app.post("/v1/tasks/{task_id}/receipts")
    def receipts(task_id: UUID, receipt: ResultReceiptV1, request: Request) -> dict:
        _enforce_nas_worker(request, "hermes_nas", receipt.worker_instance)
        if receipt.task_id != task_id:
            raise HTTPException(status_code=422, detail="task_id mismatch")
        try:
            return st.submit_receipt(receipt)
        except StoreError as exc:
            raise _to_http(exc) from exc

    @app.post(
        "/v1/tasks/{task_id}/failures",
        response_model=ResilienceDecision,
    )
    def report_failure(
        task_id: UUID, report: FailureReportV1, request: Request
    ) -> ResilienceDecision:
        _enforce_nas_worker(request, "hermes_nas", report.worker_instance)
        if report.task_id != task_id:
            raise HTTPException(status_code=422, detail="task_id mismatch")
        try:
            return st.report_failure(report)
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

    @app.post("/v1/maintenance/release-retries")
    def release_retries() -> dict:
        return {"released": st.release_due_retries()}

    @app.get(
        "/v1/operations/resilience",
        response_model=ResilienceSnapshot,
    )
    def resilience() -> ResilienceSnapshot:
        return st.resilience_snapshot()

    return app


def _principal(request: Request) -> Principal:
    return getattr(
        request.state,
        "harness_principal",
        Principal("development", "development", frozenset({"*"})),
    )


def _enforce_nas_worker(request: Request, owner_role: str, worker_id: str) -> None:
    principal = _principal(request)
    if principal.role != "hermes_nas":
        return
    if owner_role != "hermes_nas" or not worker_id.startswith("svc-hermes-nas:"):
        raise HTTPException(status_code=403, detail="NAS identity is read-only worker scoped")


def _enforce_nas_task_access(request: Request, task: dict) -> None:
    principal = _principal(request)
    if principal.role == "hermes_nas" and task["owner_role"] != "hermes_nas":
        raise HTTPException(status_code=403, detail="NAS identity cannot read other roles")


app = create_app()
