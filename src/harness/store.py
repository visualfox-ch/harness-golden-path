"""PostgreSQL State Store: atomarer Claim, Lease, Heartbeat, Idempotenz, Eventlog.

Der Store ist die einzige Komponente, die Statuswechsel schreibt (ADR-001:
Ein-Autoritäts-Regel). Jeder Wechsel läuft über die Transitionstabelle und
ein bedingtes UPDATE mit rowcount-Prüfung.
"""
from __future__ import annotations

import os
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from .contracts import (
    TRANSITIONS,
    ApprovalCardV1,
    ResultReceiptV1,
    TaskCardV1,
    TaskStatus,
)
from .cockpit import CockpitSnapshot, build_cockpit_snapshot

DEFAULT_URL = "postgresql://harness:harness_dev@localhost:5433/harness"

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id UUID PRIMARY KEY,
    correlation_id UUID NOT NULL,
    title TEXT NOT NULL,
    project TEXT NOT NULL,
    owner_role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    card JSONB NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    approval_required JSONB NOT NULL DEFAULT '[]'::jsonb,
    owner_instance TEXT,
    claimed_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_events (
    event_id BIGSERIAL PRIMARY KEY,
    task_id UUID NOT NULL,
    correlation_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_events_by_correlation
    ON agent_events (correlation_id, event_id);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES agent_tasks (task_id),
    action TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',
    decided_by TEXT,
    reason TEXT NOT NULL DEFAULT '',
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS routing_receipts (
    receipt_id BIGSERIAL PRIMARY KEY,
    task_id UUID NOT NULL,
    worker_instance TEXT NOT NULL,
    outcome TEXT NOT NULL,
    receipt JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_EXECUTION_STATES = (TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS)


class StoreError(Exception):
    pass


class NotFoundError(StoreError):
    pass


class OwnershipError(StoreError):
    pass


class TransitionError(StoreError):
    pass


def _jsonable(row: dict) -> dict:
    out: dict = {}
    for key, value in row.items():
        if isinstance(value, UUID):
            out[key] = str(value)
        elif isinstance(value, datetime):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


class Store:
    def __init__(self, url: str | None = None) -> None:
        self.url = url or os.environ.get("HARNESS_DATABASE_URL", DEFAULT_URL)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.url, row_factory=dict_row)

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA)

    def cockpit_snapshot(
        self, catalog: dict, routing_policy: dict
    ) -> CockpitSnapshot:
        with self._connect() as conn:
            return build_cockpit_snapshot(conn, catalog, routing_policy)

    # -- Events ---------------------------------------------------------------

    @staticmethod
    def _event(conn, task_id, correlation_id, event_type: str, payload: dict) -> None:
        conn.execute(
            "INSERT INTO agent_events (task_id, correlation_id, event_type, payload)"
            " VALUES (%s, %s, %s, %s)",
            (task_id, correlation_id, event_type, Json(payload)),
        )

    # -- Statuswechsel --------------------------------------------------------

    def _set_status(self, conn, task_id, expected: TaskStatus, target: TaskStatus) -> None:
        if target not in TRANSITIONS[expected]:
            raise TransitionError(
                f"transition {expected.value} -> {target.value} is not allowed"
            )
        if target in _EXECUTION_STATES:
            sql = "UPDATE agent_tasks SET status=%s WHERE task_id=%s AND status=%s"
        else:
            # Lease wird beim Verlassen der Ausführungszustände freigegeben
            sql = (
                "UPDATE agent_tasks SET status=%s, lease_expires_at=NULL"
                " WHERE task_id=%s AND status=%s"
            )
        cur = conn.execute(sql, (target.value, task_id, expected.value))
        if cur.rowcount != 1:
            raise TransitionError(f"concurrent status change on task {task_id}")

    def transition(self, task_id, target: TaskStatus, reason: str = "") -> dict:
        with self._connect() as conn:
            task = conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id=%s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task {task_id} not found")
            self._set_status(conn, task_id, TaskStatus(task["status"]), target)
            self._event(
                conn, task["task_id"], task["correlation_id"], "status_changed",
                {"from": task["status"], "to": target.value, "reason": reason},
            )
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id=%s", (task_id,)
            ).fetchone()
            return _jsonable(row)

    # -- Task-Lebenszyklus ----------------------------------------------------

    def create_task(self, card: TaskCardV1) -> tuple[dict, bool]:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO agent_tasks
                    (task_id, correlation_id, title, project, owner_role, status,
                     card, idempotency_key, approval_required)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *
                """,
                (
                    card.task_id, card.correlation_id, card.title, card.project,
                    card.owner_role.value, TaskStatus.READY.value,
                    Json(card.model_dump(mode="json")), card.idempotency_key,
                    Json(card.approval_required),
                ),
            ).fetchone()
            if row is None:
                existing = conn.execute(
                    "SELECT * FROM agent_tasks WHERE idempotency_key=%s",
                    (card.idempotency_key,),
                ).fetchone()
                return _jsonable(existing), False
            self._event(
                conn, row["task_id"], row["correlation_id"], "task_created",
                {"title": card.title, "project": card.project},
            )
            return _jsonable(row), True

    def get_task(self, task_id) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id=%s", (task_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"task {task_id} not found")
        return _jsonable(row)

    def claim(self, task_id, owner_role: str, worker_id: str,
              lease_minutes: int = 10) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE agent_tasks SET
                    status = 'claimed',
                    owner_role = %s,
                    owner_instance = %s,
                    claimed_at = now(),
                    heartbeat_at = now(),
                    lease_expires_at = now() + make_interval(mins => %s),
                    attempt_count = attempt_count + 1
                WHERE task_id = %s
                  AND status = 'ready'
                  AND (lease_expires_at IS NULL OR lease_expires_at < now())
                RETURNING *
                """,
                (owner_role, worker_id, lease_minutes, task_id),
            ).fetchone()
            if row is None:
                return None
            self._event(
                conn, row["task_id"], row["correlation_id"], "task_claimed",
                {"worker": worker_id, "attempt": row["attempt_count"]},
            )
            return _jsonable(row)

    def heartbeat(self, task_id, worker_id: str, lease_minutes: int = 10) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE agent_tasks SET
                    heartbeat_at = now(),
                    lease_expires_at = now() + make_interval(mins => %s)
                WHERE task_id = %s
                  AND owner_instance = %s
                  AND status IN ('claimed', 'in_progress')
                  AND lease_expires_at > now()
                RETURNING *
                """,
                (lease_minutes, task_id, worker_id),
            ).fetchone()
            if row is None:
                task = conn.execute(
                    "SELECT task_id, correlation_id FROM agent_tasks WHERE task_id=%s",
                    (task_id,),
                ).fetchone()
                if task is not None:
                    self._event(
                        conn, task["task_id"], task["correlation_id"],
                        "heartbeat_rejected", {"worker": worker_id},
                    )
                # Audit-Event festschreiben, bevor die Exception die Transaktion beendet
                conn.commit()
                raise OwnershipError("heartbeat without valid ownership rejected")
            return _jsonable(row)

    def expire_leases(self) -> list[dict]:
        """Lease-Ablauf ist kein Anlass für einen Re-Run: recovery_required."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                UPDATE agent_tasks SET status = 'recovery_required'
                WHERE status IN ('claimed', 'in_progress')
                  AND lease_expires_at < now()
                RETURNING task_id, correlation_id
                """
            ).fetchall()
            for row in rows:
                self._event(
                    conn, row["task_id"], row["correlation_id"], "lease_expired",
                    {"action": "recovery_required"},
                )
            return [_jsonable(r) for r in rows]

    # -- Receipts -------------------------------------------------------------

    def submit_receipt(self, receipt: ResultReceiptV1) -> dict:
        with self._connect() as conn:
            task = conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id=%s FOR UPDATE",
                (receipt.task_id,),
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task {receipt.task_id} not found")
            if task["owner_instance"] != receipt.worker_instance:
                self._event(
                    conn, task["task_id"], task["correlation_id"],
                    "receipt_rejected",
                    {"reason": "not lease owner", "worker": receipt.worker_instance},
                )
                # Audit-Event festschreiben, bevor die Exception die Transaktion beendet
                conn.commit()
                raise OwnershipError("receipt from non-owner worker rejected")
            current = TaskStatus(task["status"])
            if current not in _EXECUTION_STATES:
                raise TransitionError(
                    f"receipt not allowed in status {current.value}"
                )
            if current == TaskStatus.CLAIMED:
                self._set_status(conn, receipt.task_id, current, TaskStatus.IN_PROGRESS)
                current = TaskStatus.IN_PROGRESS
            target = {
                "completed": TaskStatus.REVIEW,
                "failed": TaskStatus.FAILED,
                "blocked": TaskStatus.BLOCKED,
            }[receipt.outcome]
            self._set_status(conn, receipt.task_id, current, target)
            conn.execute(
                "INSERT INTO routing_receipts (task_id, worker_instance, outcome, receipt)"
                " VALUES (%s, %s, %s, %s)",
                (receipt.task_id, receipt.worker_instance, receipt.outcome,
                 Json(receipt.model_dump(mode="json"))),
            )
            self._event(
                conn, task["task_id"], task["correlation_id"], "receipt_accepted",
                {"outcome": receipt.outcome, "status": target.value,
                 "model_ref": receipt.cost_receipt.model_ref,
                 "provider_class": receipt.cost_receipt.provider_class.value},
            )
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id=%s", (receipt.task_id,)
            ).fetchone()
            return _jsonable(row)

    # -- Approvals ------------------------------------------------------------

    def request_approval(self, card: ApprovalCardV1) -> dict:
        with self._connect() as conn:
            task = conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id=%s FOR UPDATE",
                (card.task_id,),
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task {card.task_id} not found")
            self._set_status(
                conn, card.task_id, TaskStatus(task["status"]),
                TaskStatus.AWAITING_APPROVAL,
            )
            conn.execute(
                "INSERT INTO approvals (approval_id, task_id, action, requested_by)"
                " VALUES (%s, %s, %s, %s)",
                (card.approval_id, card.task_id, card.action, card.requested_by),
            )
            self._event(
                conn, task["task_id"], task["correlation_id"], "approval_requested",
                {"approval_id": str(card.approval_id), "action": card.action},
            )
            return {"approval_id": str(card.approval_id), "status": "requested",
                    "task_status": TaskStatus.AWAITING_APPROVAL.value}

    def decide_approval(self, approval_id, decision: str, decided_by: str,
                        reason: str = "") -> dict:
        if decision not in ("approved", "rejected"):
            raise StoreError(f"invalid decision '{decision}'")
        with self._connect() as conn:
            approval = conn.execute(
                "SELECT * FROM approvals WHERE approval_id=%s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            if approval is None:
                raise NotFoundError(f"approval {approval_id} not found")
            if approval["status"] != "requested":
                raise StoreError("approval already decided")
            task = conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id=%s FOR UPDATE",
                (approval["task_id"],),
            ).fetchone()
            conn.execute(
                "UPDATE approvals SET status=%s, decided_by=%s, reason=%s,"
                " decided_at=now() WHERE approval_id=%s",
                (decision, decided_by, reason, approval_id),
            )
            target = TaskStatus.DONE if decision == "approved" else TaskStatus.BLOCKED
            self._set_status(conn, task["task_id"], TaskStatus(task["status"]), target)
            self._event(
                conn, task["task_id"], task["correlation_id"], "approval_decided",
                {"approval_id": str(approval_id), "decision": decision,
                 "decided_by": decided_by},
            )
            return {"approval_id": str(approval_id), "decision": decision,
                    "task_status": target.value}

    # -- Trace ----------------------------------------------------------------

    def trace(self, correlation_id) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_events WHERE correlation_id=%s ORDER BY event_id",
                (correlation_id,),
            ).fetchall()
        return [_jsonable(r) for r in rows]
