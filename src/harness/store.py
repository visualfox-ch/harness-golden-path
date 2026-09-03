"""PostgreSQL State Store: atomarer Claim, Lease, Heartbeat, Idempotenz, Eventlog.

Der Store ist die einzige Komponente, die Statuswechsel schreibt (ADR-001:
Ein-Autoritäts-Regel). Jeder Wechsel läuft über die Transitionstabelle und
ein bedingtes UPDATE mit rowcount-Prüfung.
"""
from __future__ import annotations

import os
from datetime import datetime
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from .contracts import (
    TRANSITIONS,
    ApprovalCardV1,
    FailureClass,
    FailureReportV1,
    ResultReceiptV1,
    SideEffectState,
    TaskCardV1,
    TaskStatus,
)
from .cockpit import CockpitSnapshot, build_cockpit_snapshot
from .resilience import (
    CIRCUIT_COOLDOWN_SECONDS,
    CIRCUIT_FAILURE_THRESHOLD,
    CIRCUIT_FAILURES,
    RETRYABLE_FAILURES,
    ResilienceDecision,
    ResilienceSnapshot,
    requires_recovery,
    retry_delay_seconds,
)

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

ALTER TABLE agent_tasks
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;
ALTER TABLE agent_tasks
    ADD COLUMN IF NOT EXISTS last_failure_class TEXT;
ALTER TABLE agent_tasks
    ADD COLUMN IF NOT EXISTS circuit_key TEXT;

CREATE TABLE IF NOT EXISTS circuit_breakers (
    circuit_key TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'closed',
    failure_count INTEGER NOT NULL DEFAULT 0,
    failure_threshold INTEGER NOT NULL,
    opened_at TIMESTAMPTZ,
    open_until TIMESTAMPTZ,
    last_failure_class TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dead_letters (
    dead_letter_id BIGSERIAL PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES agent_tasks (task_id),
    correlation_id UUID NOT NULL,
    failure_class TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS recovery_cards (
    recovery_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES agent_tasks (task_id),
    correlation_id UUID NOT NULL,
    trigger TEXT NOT NULL,
    reason TEXT NOT NULL,
    allowed_actions JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
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

    def _create_recovery_card(
        self, conn, task: dict, trigger: str, reason: str
    ) -> str:
        recovery_id = uuid4()
        allowed_actions = [
            "inspect_read_only",
            "confirm_side_effect",
            "request_human_decision",
        ]
        conn.execute(
            """
            INSERT INTO recovery_cards
                (recovery_id, task_id, correlation_id, trigger, reason,
                 allowed_actions)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                recovery_id,
                task["task_id"],
                task["correlation_id"],
                trigger,
                reason,
                Json(allowed_actions),
            ),
        )
        self._event(
            conn,
            task["task_id"],
            task["correlation_id"],
            "recovery_card_created",
            {
                "recovery_id": str(recovery_id),
                "trigger": trigger,
                "allowed_actions": allowed_actions,
            },
        )
        return str(recovery_id)

    def _record_dead_letter(
        self, conn, task: dict, report: FailureReportV1, reason: str
    ) -> int:
        row = conn.execute(
            """
            INSERT INTO dead_letters
                (task_id, correlation_id, failure_class, reason, payload)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING dead_letter_id
            """,
            (
                task["task_id"],
                task["correlation_id"],
                report.failure_class.value,
                reason,
                Json(report.model_dump(mode="json")),
            ),
        ).fetchone()
        dead_letter_id = row["dead_letter_id"]
        self._event(
            conn,
            task["task_id"],
            task["correlation_id"],
            "dead_letter_created",
            {
                "dead_letter_id": dead_letter_id,
                "failure_class": report.failure_class.value,
            },
        )
        return dead_letter_id

    def _record_circuit_failure(
        self, conn, circuit_key: str, failure_class: FailureClass
    ) -> dict:
        row = conn.execute(
            """
            INSERT INTO circuit_breakers
                (circuit_key, state, failure_count, failure_threshold,
                 last_failure_class)
            VALUES (%s, 'closed', 1, %s, %s)
            ON CONFLICT (circuit_key) DO UPDATE SET
                failure_count = circuit_breakers.failure_count + 1,
                last_failure_class = EXCLUDED.last_failure_class,
                updated_at = now()
            RETURNING *
            """,
            (
                circuit_key,
                CIRCUIT_FAILURE_THRESHOLD,
                failure_class.value,
            ),
        ).fetchone()
        if row["failure_count"] < row["failure_threshold"]:
            return row
        return conn.execute(
            """
            UPDATE circuit_breakers SET
                state = 'open',
                opened_at = COALESCE(opened_at, now()),
                open_until = now() + make_interval(secs => %s),
                updated_at = now()
            WHERE circuit_key = %s
            RETURNING *
            """,
            (CIRCUIT_COOLDOWN_SECONDS, circuit_key),
        ).fetchone()

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
                     card, idempotency_key, approval_required, circuit_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *
                """,
                (
                    card.task_id, card.correlation_id, card.title, card.project,
                    card.owner_role.value, TaskStatus.READY.value,
                    Json(card.model_dump(mode="json")), card.idempotency_key,
                    Json(card.approval_required),
                    f"route:{card.model_routing.default_route[0]}",
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
                  AND NOT EXISTS (
                      SELECT 1 FROM circuit_breakers
                      WHERE circuit_breakers.circuit_key = agent_tasks.circuit_key
                        AND circuit_breakers.state = 'open'
                        AND circuit_breakers.open_until > now()
                  )
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
                RETURNING *
                """
            ).fetchall()
            for row in rows:
                self._event(
                    conn, row["task_id"], row["correlation_id"], "lease_expired",
                    {"action": "recovery_required"},
                )
                self._create_recovery_card(
                    conn,
                    row,
                    "lease_expired",
                    "Lease expired; execution outcome and side effects are unknown.",
                )
            return [_jsonable(r) for r in rows]

    # -- Resilienz -----------------------------------------------------------

    def report_failure(self, report: FailureReportV1) -> ResilienceDecision:
        with self._connect() as conn:
            task = conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id=%s FOR UPDATE",
                (report.task_id,),
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task {report.task_id} not found")
            if task["correlation_id"] != report.correlation_id:
                raise StoreError("failure report correlation_id mismatch")
            if task["owner_instance"] != report.worker_instance:
                raise OwnershipError("failure report from non-owner worker rejected")
            current = TaskStatus(task["status"])
            if current not in _EXECUTION_STATES:
                raise TransitionError(
                    f"failure report not allowed in status {current.value}"
                )

            self._event(
                conn,
                task["task_id"],
                task["correlation_id"],
                "failure_classified",
                {
                    "failure_class": report.failure_class.value,
                    "side_effect_state": report.side_effect_state.value,
                    "circuit_key": task["circuit_key"],
                },
            )

            if (
                report.circuit_key
                and report.circuit_key != task["circuit_key"]
            ):
                raise StoreError(
                    "failure report circuit_key does not match task route"
                )

            if requires_recovery(
                report.failure_class, report.side_effect_state
            ):
                self._set_status(
                    conn, task["task_id"], current, TaskStatus.RECOVERY_REQUIRED
                )
                recovery_id = self._create_recovery_card(
                    conn,
                    task,
                    report.failure_class.value,
                    report.reason,
                )
                return ResilienceDecision(
                    task_id=str(task["task_id"]),
                    failure_class=report.failure_class,
                    action="recovery_required",
                    task_status=TaskStatus.RECOVERY_REQUIRED,
                    recovery_id=recovery_id,
                )

            circuit = None
            if task["circuit_key"] and report.failure_class in CIRCUIT_FAILURES:
                circuit = self._record_circuit_failure(
                    conn, task["circuit_key"], report.failure_class
                )

            if report.failure_class in RETRYABLE_FAILURES:
                max_attempts = int(task["card"]["budget"]["max_attempts"])
                if task["attempt_count"] < max_attempts:
                    delay_seconds = retry_delay_seconds(
                        report.failure_class,
                        task["attempt_count"],
                        report.retry_after_seconds,
                    )
                    if circuit and circuit["state"] == "open":
                        delay_seconds = max(
                            delay_seconds, CIRCUIT_COOLDOWN_SECONDS
                        )
                    retry_at = conn.execute(
                        "SELECT now() + make_interval(secs => %s) AS value",
                        (delay_seconds,),
                    ).fetchone()["value"]
                    self._set_status(
                        conn, task["task_id"], current, TaskStatus.RETRY_WAIT
                    )
                    conn.execute(
                        """
                        UPDATE agent_tasks SET
                            next_attempt_at=%s,
                            last_failure_class=%s,
                            circuit_key=%s
                        WHERE task_id=%s
                        """,
                        (
                            retry_at,
                            report.failure_class.value,
                            task["circuit_key"],
                            task["task_id"],
                        ),
                    )
                    self._event(
                        conn,
                        task["task_id"],
                        task["correlation_id"],
                        "retry_scheduled",
                        {
                            "retry_at": retry_at.isoformat(),
                            "delay_seconds": delay_seconds,
                            "attempt": task["attempt_count"],
                        },
                    )
                    return ResilienceDecision(
                        task_id=str(task["task_id"]),
                        failure_class=report.failure_class,
                        action="retry_scheduled",
                        task_status=TaskStatus.RETRY_WAIT,
                        retry_at=retry_at.isoformat(),
                        circuit_state=circuit["state"] if circuit else None,
                    )

                self._set_status(
                    conn, task["task_id"], current, TaskStatus.FAILED
                )
                dead_letter_id = self._record_dead_letter(
                    conn,
                    task,
                    report,
                    f"retry budget exhausted: {report.reason}",
                )
                return ResilienceDecision(
                    task_id=str(task["task_id"]),
                    failure_class=report.failure_class,
                    action="dead_lettered",
                    task_status=TaskStatus.FAILED,
                    dead_letter_id=dead_letter_id,
                    circuit_state=circuit["state"] if circuit else None,
                )

            if report.failure_class == FailureClass.QUOTA_EXHAUSTED:
                self._set_status(
                    conn,
                    task["task_id"],
                    current,
                    TaskStatus.AWAITING_DECISION,
                )
                return ResilienceDecision(
                    task_id=str(task["task_id"]),
                    failure_class=report.failure_class,
                    action="awaiting_decision",
                    task_status=TaskStatus.AWAITING_DECISION,
                )

            if report.failure_class == FailureClass.POLICY_VIOLATION:
                self._set_status(
                    conn, task["task_id"], current, TaskStatus.BLOCKED
                )
                return ResilienceDecision(
                    task_id=str(task["task_id"]),
                    failure_class=report.failure_class,
                    action="blocked",
                    task_status=TaskStatus.BLOCKED,
                )

            self._set_status(conn, task["task_id"], current, TaskStatus.FAILED)
            dead_letter_id = self._record_dead_letter(
                conn, task, report, report.reason
            )
            return ResilienceDecision(
                task_id=str(task["task_id"]),
                failure_class=report.failure_class,
                action="dead_lettered",
                task_status=TaskStatus.FAILED,
                dead_letter_id=dead_letter_id,
            )

    def release_due_retries(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                UPDATE agent_tasks SET
                    status='ready',
                    next_attempt_at=NULL,
                    owner_instance=NULL,
                    claimed_at=NULL,
                    heartbeat_at=NULL
                WHERE status='retry_wait'
                  AND next_attempt_at <= now()
                  AND NOT EXISTS (
                      SELECT 1 FROM circuit_breakers
                      WHERE circuit_breakers.circuit_key = agent_tasks.circuit_key
                        AND circuit_breakers.state = 'open'
                        AND circuit_breakers.open_until > now()
                  )
                RETURNING *
                """
            ).fetchall()
            for row in rows:
                self._event(
                    conn,
                    row["task_id"],
                    row["correlation_id"],
                    "retry_released",
                    {"next_attempt": row["attempt_count"] + 1},
                )
            return [_jsonable(row) for row in rows]

    def resilience_snapshot(self) -> ResilienceSnapshot:
        with self._connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            circuits = conn.execute(
                "SELECT * FROM circuit_breakers ORDER BY circuit_key"
            ).fetchall()
            dead_letters = conn.execute(
                "SELECT * FROM dead_letters ORDER BY dead_letter_id"
            ).fetchall()
            recovery_cards = conn.execute(
                "SELECT * FROM recovery_cards ORDER BY created_at, recovery_id"
            ).fetchall()
        return ResilienceSnapshot(
            circuits=[_jsonable(row) for row in circuits],
            dead_letters=[_jsonable(row) for row in dead_letters],
            recovery_cards=[_jsonable(row) for row in recovery_cards],
        )

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
            if target == TaskStatus.REVIEW and task.get("circuit_key"):
                closed = conn.execute(
                    """
                    UPDATE circuit_breakers SET
                        state='closed', failure_count=0, opened_at=NULL,
                        open_until=NULL, updated_at=now()
                    WHERE circuit_key=%s
                    RETURNING circuit_key
                    """,
                    (task["circuit_key"],),
                ).fetchone()
                if closed:
                    self._event(
                        conn,
                        task["task_id"],
                        task["correlation_id"],
                        "circuit_closed",
                        {"circuit_key": task["circuit_key"]},
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
