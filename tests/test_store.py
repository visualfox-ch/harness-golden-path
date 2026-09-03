from concurrent.futures import ThreadPoolExecutor

import pytest

from harness.contracts import TaskStatus
from harness.store import OwnershipError, Store, TransitionError


def test_task_creation_is_idempotent(store, card_factory):
    card = card_factory()
    first, created_first = store.create_task(card)
    second, created_second = store.create_task(card)
    assert created_first is True
    assert created_second is False
    assert first["task_id"] == second["task_id"]


def test_claim_has_exactly_one_winner(store, card_factory):
    card = card_factory()
    store.create_task(card)

    def claim_with_own_connection(index: int):
        return Store().claim(card.task_id, "hermes_local", f"worker-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim_with_own_connection, range(8)))

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0]["status"] == "claimed"
    assert winners[0]["attempt_count"] == 1


def test_second_sequential_claim_fails(store, card_factory):
    card = card_factory()
    store.create_task(card)
    assert store.claim(card.task_id, "hermes_local", "worker-a") is not None
    assert store.claim(card.task_id, "hermes_local", "worker-b") is None


def test_heartbeat_requires_ownership(store, card_factory):
    card = card_factory()
    store.create_task(card)
    assert store.claim(card.task_id, "hermes_local", "worker-a") is not None

    with pytest.raises(OwnershipError):
        store.heartbeat(card.task_id, "worker-b")

    events = store.trace(card.correlation_id)
    assert any(e["event_type"] == "heartbeat_rejected" for e in events)
    # Der rechtmässige Owner darf weiter verlängern
    row = store.heartbeat(card.task_id, "worker-a")
    assert row["status"] == "claimed"


def test_expired_lease_becomes_recovery_not_rerun(store, card_factory):
    card = card_factory()
    store.create_task(card)
    store.claim(card.task_id, "hermes_local", "worker-a", lease_minutes=0)

    expired = store.expire_leases()
    assert [e["task_id"] for e in expired] == [str(card.task_id)]
    assert store.get_task(card.task_id)["status"] == "recovery_required"
    # Kein Blind-Retry: die Task ist nicht erneut claimbar
    assert store.claim(card.task_id, "hermes_local", "worker-b") is None


def test_invalid_transition_is_rejected(store, card_factory):
    card = card_factory()
    store.create_task(card)
    with pytest.raises(TransitionError):
        store.transition(card.task_id, TaskStatus.DONE)
