"""Deterministic state manifests and the isolated P2-6 recovery exercise."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, LiteralString, cast
from urllib.parse import urlparse
from uuid import UUID

from .contracts import (
    Budget,
    CostReceipt,
    ExecutorRole,
    ModelRouting,
    ProviderClass,
    ResultReceiptV1,
    Scope,
    TaskCardV1,
)
from .store import Store

TABLE_KEYS = {
    "agent_tasks": "task_id",
    "agent_events": "event_id",
    "approvals": "approval_id",
    "routing_receipts": "receipt_id",
    "circuit_breakers": "circuit_key",
    "dead_letters": "dead_letter_id",
    "recovery_cards": "recovery_id",
}

READY_TASK_ID = UUID("26000000-0000-4000-8000-000000000001")
EXPIRED_TASK_ID = UUID("26000000-0000-4000-8000-000000000002")
DONE_TASK_ID = UUID("26000000-0000-4000-8000-000000000003")


def _normalise(value: Any) -> Any:
    if isinstance(value, (datetime, UUID)):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value


def state_manifest(store: Store) -> dict:
    """Hash every authoritative Harness table in deterministic row order."""
    payload: dict[str, list[dict]] = {}
    with store._connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        agg = conn.execute("SELECT current_database() AS value").fetchone()
        assert agg is not None
        database = agg["value"]
        for table, key in TABLE_KEYS.items():
            query = cast(LiteralString, f"SELECT * FROM {table} ORDER BY {key}")
            rows = conn.execute(query, ()).fetchall()
            payload[table] = [_normalise(dict(row)) for row in rows]

    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "database": database,
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "counts": {table: len(rows) for table, rows in payload.items()},
        "max_event_id": max(
            (row["event_id"] for row in payload["agent_events"]), default=0
        ),
    }


def _fixture_card(task_id: UUID, suffix: str) -> TaskCardV1:
    return TaskCardV1(
        task_id=task_id,
        correlation_id=UUID(f"26000000-0000-4000-8000-0000000001{suffix}"),
        title=f"P2-6 continuity fixture {suffix}",
        project="harness-golden-path",
        task_class="read_only_monitor",
        owner_role=ExecutorRole.HERMES_NAS,
        objective="Prove deterministic backup, restore, and lease recovery behavior.",
        scope=Scope(include=["isolated-p2-6-test-stack"]),
        acceptance=["Recovered state matches the source manifest exactly"],
        budget=Budget(max_runtime_minutes=30, max_attempts=2),
        model_routing=ModelRouting(
            permitted_provider_classes=[ProviderClass.LOCAL_MODEL],
            default_route=["deterministic_monitor"],
        ),
        idempotency_key=f"sha256:p2-6-continuity-fixture-{suffix}",
    )


def seed_fixture(store: Store) -> dict:
    """Create ready, expired-lease, and completed records for restore coverage."""
    store.init_db()
    ready = _fixture_card(READY_TASK_ID, "01")
    expired = _fixture_card(EXPIRED_TASK_ID, "02")
    done = _fixture_card(DONE_TASK_ID, "03")
    for card in (ready, expired, done):
        store.create_task(card)

    store.claim(done.task_id, "hermes_nas", "p2-6-completed-worker", 10)
    store.submit_receipt(
        ResultReceiptV1(
            task_id=done.task_id,
            correlation_id=done.correlation_id,
            worker_instance="p2-6-completed-worker",
            outcome="completed",
            summary="Controlled continuity fixture completed without side effects.",
            cost_receipt=CostReceipt(
                provider_class=ProviderClass.LOCAL_MODEL,
                model_ref="deterministic_monitor",
                subscription_quota_consumed=False,
                quota_status="unavailable",
            ),
        )
    )
    return state_manifest(store)


def claim_crash_fixture(store: Store) -> dict:
    """Claim the fixture before the outer exercise kills this worker process."""
    claimed = store.claim(EXPIRED_TASK_ID, "hermes_nas", "p2-6-crashed-worker", 0)
    if claimed is None:
        raise RuntimeError("P2-6 crash fixture was not claimable")
    return {
        "task_id": str(EXPIRED_TASK_ID),
        "status": claimed["status"],
        "attempt_count": claimed["attempt_count"],
        "lease_expires_at": claimed["lease_expires_at"],
    }


def recover_expired_fixture(store: Store) -> dict:
    """Expire the restored lease and prove that a blind retry is impossible."""
    expired = store.expire_leases()
    second_expiry = store.expire_leases()
    blind_retry = store.claim(EXPIRED_TASK_ID, "hermes_nas", "p2-6-blind-retry", 10)
    task = store.get_task(EXPIRED_TASK_ID)
    snapshot = store.resilience_snapshot()
    matching_cards = [
        card
        for card in snapshot.recovery_cards
        if card["task_id"] == str(EXPIRED_TASK_ID)
    ]
    result = {
        "expired_count": len(expired),
        "second_expiry_count": len(second_expiry),
        "status": task["status"],
        "attempt_count": task["attempt_count"],
        "recovery_card_count": len(matching_cards),
        "recovery_trigger": matching_cards[0]["trigger"] if matching_cards else None,
        "blind_retry_claimed": blind_retry is not None,
    }
    expected = {
        "expired_count": 1,
        "second_expiry_count": 0,
        "status": "recovery_required",
        "attempt_count": 1,
        "recovery_card_count": 1,
        "recovery_trigger": "lease_expired",
        "blind_retry_claimed": False,
    }
    if result != expected:
        raise RuntimeError(f"P2-6 recovery invariant failed: {result!r}")
    return result


def _require_fixture_host(command: str, store: Store) -> None:
    if os.environ.get("P2_6_FIXTURE_MODE") != "1":
        raise SystemExit("P2_6_FIXTURE_MODE=1 is required")
    host = urlparse(store.url).hostname
    allowed_hosts = {
        "seed": {"source"},
        "crash-worker": {"source"},
        "manifest": {"source", "restore"},
        "recover": {"restore"},
    }[command]
    if host not in allowed_hosts:
        raise SystemExit(f"{command} is restricted to hosts {sorted(allowed_hosts)!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("seed", "crash-worker", "manifest", "recover")
    )
    args = parser.parse_args()
    store = Store()
    _require_fixture_host(args.command, store)
    if args.command == "seed":
        result = seed_fixture(store)
    elif args.command == "crash-worker":
        result = claim_crash_fixture(store)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)
        time.sleep(3600)
        return
    elif args.command == "recover":
        result = recover_expired_fixture(store)
    else:
        result = state_manifest(store)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
