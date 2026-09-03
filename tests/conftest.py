import os
from uuid import uuid4

import psycopg
import pytest

# Tests laufen NIE gegen die Betriebs-DB des Harness: lokal wird eine
# dedizierte Test-DB verwendet; nur explizit gesetztes HARNESS_DATABASE_URL
# (z. B. der CI-Service-Container) hat Vorrang.
os.environ.setdefault(
    "HARNESS_DATABASE_URL",
    "postgresql://harness:harness_dev@localhost:5433/harness_test",
)

from harness.contracts import (  # noqa: E402
    Budget,
    ExecutorRole,
    ModelRouting,
    ProviderClass,
    Scope,
    TaskCardV1,
)
from harness.store import Store  # noqa: E402


def make_card(**overrides) -> TaskCardV1:
    base = dict(
        title="Golden path docs task",
        project="harness-golden-path",
        owner_role=ExecutorRole.HERMES_LOCAL,
        objective="Ergänze den README-Abschnitt Golden Path um eine Statusnotiz.",
        scope=Scope(include=["README.md"]),
        acceptance=["README enthält den Abschnitt Golden Path"],
        budget=Budget(max_runtime_minutes=90),
        model_routing=ModelRouting(
            permitted_provider_classes=[ProviderClass.SUBSCRIPTION_OAUTH],
            default_route=["anthropic_oauth_reasoner"],
        ),
        idempotency_key="sha256:test:" + uuid4().hex,
        approval_required=["merge_main"],
    )
    base.update(overrides)
    return TaskCardV1(**base)


@pytest.fixture()
def card_factory():
    return make_card


@pytest.fixture()
def store():
    s = Store()
    s.init_db()
    with psycopg.connect(s.url) as conn:
        conn.execute(
            "TRUNCATE recovery_cards, dead_letters, circuit_breakers, "
            "agent_tasks, agent_events, approvals, routing_receipts"
        )
    return s
