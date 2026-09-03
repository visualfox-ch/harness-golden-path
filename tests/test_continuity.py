from harness.continuity import (
    EXPIRED_TASK_ID,
    claim_crash_fixture,
    recover_expired_fixture,
    seed_fixture,
    state_manifest,
)


def test_state_manifest_is_deterministic_and_covers_all_tables(store):
    seed_fixture(store)
    claim_crash_fixture(store)
    first = state_manifest(store)
    second = state_manifest(store)

    assert first == second
    assert first["counts"] == {
        "agent_tasks": 3,
        "agent_events": 6,
        "approvals": 0,
        "routing_receipts": 1,
        "circuit_breakers": 0,
        "dead_letters": 0,
        "recovery_cards": 0,
    }
    assert len(first["sha256"]) == 64


def test_restored_expired_lease_requires_recovery_without_duplicate(store):
    seed_fixture(store)
    claim_crash_fixture(store)

    result = recover_expired_fixture(store)

    assert result["status"] == "recovery_required"
    assert result["attempt_count"] == 1
    assert result["recovery_card_count"] == 1
    assert result["blind_retry_claimed"] is False
    assert store.get_task(EXPIRED_TASK_ID)["attempt_count"] == 1


def test_recovery_changes_manifest_once(store):
    seed_fixture(store)
    claim_crash_fixture(store)
    before = state_manifest(store)
    recover_expired_fixture(store)
    after = state_manifest(store)

    assert before["sha256"] != after["sha256"]
    assert after["counts"]["agent_events"] == 8
    assert after["counts"]["recovery_cards"] == 1
