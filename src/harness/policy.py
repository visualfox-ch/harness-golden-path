"""Deterministische, fail-closed Prüfung des vollständigen Policy-Bundles."""
from __future__ import annotations

from pathlib import Path

import yaml

from .contracts import (
    DataClassification,
    ModelRouting,
    ProviderClass,
    TaskCardV1,
)

POLICY_DIR = Path(__file__).resolve().parent.parent.parent / "policies"


class PolicyError(Exception):
    pass


def load_catalog(path: Path | None = None) -> dict:
    catalog_path = path or POLICY_DIR / "model-catalog.yaml"
    with open(catalog_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_routing_policy(path: Path | None = None) -> dict:
    policy_path = path or POLICY_DIR / "routing-policy.yaml"
    with open(policy_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_data_policy(path: Path | None = None) -> dict:
    policy_path = path or POLICY_DIR / "data-classification.yaml"
    with open(policy_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_agent_policy(path: Path | None = None) -> dict:
    policy_path = path or POLICY_DIR / "agent-policy.yaml"
    with open(policy_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_task_authority_policy(path: Path | None = None) -> dict:
    policy_path = path or POLICY_DIR / "task-authority.yaml"
    with open(policy_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def assert_route_allowed(
    routing: ModelRouting,
    catalog: dict,
    data_classification: DataClassification = DataClassification.INTERNAL,
) -> None:
    """Reject routes that are unknown, unverified, metered, or data-incompatible."""
    models = catalog.get("models", {})
    if not routing.default_route:
        raise PolicyError("default_route is empty")
    for ref in routing.default_route:
        model = models.get(ref)
        if model is None:
            raise PolicyError(f"model_ref '{ref}' is not in the model catalog")
        if model.get("provider_class") == ProviderClass.API_METERED.value:
            raise PolicyError(f"model_ref '{ref}' is api_metered and forbidden")
        if model.get("connection_status") != "verified":
            raise PolicyError(f"model_ref '{ref}' is not a verified route")
        if ProviderClass(model["provider_class"]) not in routing.permitted_provider_classes:
            raise PolicyError(
                f"model_ref '{ref}' has provider_class outside permitted_provider_classes"
            )
        allowed_data = model.get("allowed_data_classes")
        if not isinstance(allowed_data, list):
            raise PolicyError(f"model_ref '{ref}' has no allowed_data_classes policy")
        if data_classification.value not in allowed_data:
            raise PolicyError(
                f"model_ref '{ref}' is forbidden for data class "
                f"'{data_classification.value}'"
            )


def assert_task_allowed(
    card: TaskCardV1,
    catalog: dict,
    routing_policy: dict,
    agent_policy: dict,
    data_policy: dict,
    task_authority_policy: dict,
) -> None:
    """Enforce executor, task-class, approval, route, and data boundaries."""
    if task_authority_policy.get("authoritative_system") != "control_harness":
        raise PolicyError("control_harness must remain the runtime authority")
    panda_projection = task_authority_policy.get("projections", {}).get(
        "pandaos_session_tasks", {}
    )
    if panda_projection.get("runtime_status_writeback") != "forbidden":
        raise PolicyError("PandaOS task projection must not write runtime status")

    classifications = data_policy.get("classifications", {})
    expected = {item.value for item in DataClassification}
    if set(classifications) != expected:
        raise PolicyError("data classification policy is incomplete")
    provider_rules = data_policy.get("provider_classes", {})
    expected_providers = {item.value for item in ProviderClass}
    if set(provider_rules) != expected_providers:
        raise PolicyError("data provider-class policy is incomplete")
    for model_ref, model in catalog.get("models", {}).items():
        provider_class = model.get("provider_class")
        if provider_class not in provider_rules:
            raise PolicyError(
                f"model_ref '{model_ref}' has an unknown provider_class policy"
            )
        if set(model.get("allowed_data_classes", [])) != set(
            provider_rules[provider_class]
        ):
            raise PolicyError(
                f"model_ref '{model_ref}' data policy conflicts with provider_class"
            )
    if card.data_classification == DataClassification.SECRET:
        raise PolicyError("secret data must be referenced, never embedded in a task")

    executors = agent_policy.get("executors", {})
    executor = executors.get(card.owner_role.value)
    if not isinstance(executor, dict):
        raise PolicyError(f"owner_role '{card.owner_role.value}' has no agent policy")
    if card.task_class not in executor.get("allowed_task_classes", []):
        raise PolicyError(
            f"owner_role '{card.owner_role.value}' cannot execute "
            f"task_class '{card.task_class}'"
        )
    if card.data_classification.value not in executor.get("allowed_data_classes", []):
        raise PolicyError(
            f"owner_role '{card.owner_role.value}' cannot access data class "
            f"'{card.data_classification.value}'"
        )

    class_policy = routing_policy.get("classes", {}).get(card.task_class)
    if not isinstance(class_policy, dict):
        raise PolicyError(f"task_class '{card.task_class}' has no routing policy")
    if class_policy.get("executor") not in executor.get("routing_executor_aliases", []):
        raise PolicyError("routing executor does not match owner_role policy")
    if card.model_routing.default_route != class_policy.get("route"):
        raise PolicyError("task route does not match the central routing policy")

    class_requirements = agent_policy.get("task_classes", {}).get(card.task_class)
    if not isinstance(class_requirements, dict):
        raise PolicyError(f"task_class '{card.task_class}' has no agent requirements")
    required_approvals = set(class_requirements.get("required_approvals", []))
    if not required_approvals.issubset(set(card.approval_required)):
        raise PolicyError("task is missing a required approval action")

    assert_route_allowed(card.model_routing, catalog, card.data_classification)
