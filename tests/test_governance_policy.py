from pathlib import Path

import pytest
import yaml

from harness.contracts import (
    DataClassification,
    ExecutorRole,
    ModelRouting,
    ProviderClass,
)
from harness.policy import (
    PolicyError,
    assert_route_allowed,
    assert_task_allowed,
    load_agent_policy,
    load_catalog,
    load_data_policy,
    load_routing_policy,
    load_task_authority_policy,
)


def _assert_card(card):
    assert_task_allowed(
        card,
        load_catalog(),
        load_routing_policy(),
        load_agent_policy(),
        load_data_policy(),
        load_task_authority_policy(),
    )


def test_policy_bundle_accepts_internal_docs_task(card_factory):
    _assert_card(card_factory())


def test_confidential_local_data_cannot_use_cloud_oauth():
    routing = ModelRouting(
        permitted_provider_classes=[ProviderClass.SUBSCRIPTION_OAUTH],
        default_route=["anthropic_oauth_reasoner"],
    )
    with pytest.raises(PolicyError, match="forbidden for data class"):
        assert_route_allowed(
            routing, load_catalog(), DataClassification.CONFIDENTIAL_LOCAL
        )


def test_secret_task_content_is_rejected(card_factory):
    with pytest.raises(PolicyError, match="must be referenced"):
        _assert_card(card_factory(data_classification=DataClassification.SECRET))


def test_nas_worker_cannot_receive_docs_change(card_factory):
    with pytest.raises(PolicyError, match="cannot execute"):
        _assert_card(card_factory(owner_role=ExecutorRole.HERMES_NAS))


def test_docs_task_requires_merge_approval(card_factory):
    with pytest.raises(PolicyError, match="required approval"):
        _assert_card(card_factory(approval_required=[]))


def test_task_route_cannot_drift_from_central_policy(card_factory):
    routing = ModelRouting(
        permitted_provider_classes=[ProviderClass.LOCAL_MODEL],
        default_route=["deterministic_monitor"],
    )
    with pytest.raises(PolicyError, match="central routing policy"):
        _assert_card(card_factory(model_routing=routing))


def test_task_authority_rejects_panda_status_writeback(card_factory, tmp_path):
    authority = load_task_authority_policy()
    authority["projections"]["pandaos_session_tasks"][
        "runtime_status_writeback"
    ] = "allowed"
    path = tmp_path / "task-authority.yaml"
    path.write_text(yaml.safe_dump(authority), encoding="utf-8")
    with pytest.raises(PolicyError, match="must not write runtime status"):
        assert_task_allowed(
            card_factory(),
            load_catalog(),
            load_routing_policy(),
            load_agent_policy(),
            load_data_policy(),
            load_task_authority_policy(path),
        )


def test_model_catalog_cannot_drift_from_data_policy(card_factory):
    catalog = load_catalog()
    catalog["models"]["anthropic_oauth_reasoner"]["allowed_data_classes"].append(
        "confidential_local"
    )
    with pytest.raises(PolicyError, match="conflicts with provider_class"):
        assert_task_allowed(
            card_factory(),
            catalog,
            load_routing_policy(),
            load_agent_policy(),
            load_data_policy(),
            load_task_authority_policy(),
        )


def test_all_policy_files_are_yaml_mappings():
    policy_dir = Path(__file__).parents[1] / "policies"
    for path in policy_dir.glob("*.yaml"):
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)
