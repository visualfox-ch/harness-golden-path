import pytest
from pydantic import ValidationError

from harness.policy import PolicyError, load_routing_policy
from harness.workflow import (
    WorkflowDefinition,
    WorkflowError,
    load_workflow,
    validate_against_routing_policy,
)


def test_feature_delivery_loads_and_is_1_0_0():
    workflow = load_workflow(name="feature-delivery-1.0.0")
    assert workflow.workflow_id == "feature_delivery"
    assert workflow.workflow_version == "1.0.0"
    assert workflow.status == "design_only"
    assert workflow.api_metered_fallback == "forbidden"


def test_every_node_has_input_output_schema_and_limits():
    workflow = load_workflow(name="feature-delivery-1.0.0")
    assert len(workflow.nodes) == 9
    for node in workflow.nodes:
        assert node.input_schema
        assert node.output_schema
        assert node.limits.max_runtime_minutes > 0


def test_model_consuming_nodes_reference_a_routing_class():
    workflow = load_workflow(name="feature-delivery-1.0.0")
    deterministic_or_human = {"harness_gate", "github_pr_ci", "merge_approval", "protected_merge"}
    for node in workflow.nodes:
        if node.node_id in deterministic_or_human:
            continue
        assert node.routing_policy_class is not None, (
            f"node '{node.node_id}' consumes model roles but has no routing_policy_class"
        )


def test_routing_classes_exist_in_routing_policy():
    workflow = load_workflow(name="feature-delivery-1.0.0")
    policy = load_routing_policy()
    validate_against_routing_policy(workflow, policy)  # no raise


def test_unknown_routing_class_is_rejected():
    workflow = load_workflow(name="feature-delivery-1.0.0")
    broken = workflow.model_copy(deep=True)
    broken.nodes[0].routing_policy_class = "not_a_real_class"
    with pytest.raises(PolicyError):
        validate_against_routing_policy(broken, load_routing_policy())


def test_approval_gated_nodes_are_marked_protected():
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {
                "workflow_id": "broken_workflow",
                "workflow_version": "1.0.0",
                "entry_node": "gate",
                "nodes": [
                    {
                        "node_id": "gate",
                        "display_name": "Ungeschuetztes Approval-Gate",
                        "owner_role": "human",
                        "autonomy_level": "A4",
                        "protected_side_effect": False,  # muss ablehnen
                        "allowed_tools": ["approval_ui"],
                        "input_schema": "x",
                        "output_schema": "y",
                        "limits": {"max_runtime_minutes": 5},
                        "transitions": {"success": "done"},
                    }
                ],
            }
        )


def test_transition_to_unknown_node_is_rejected():
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {
                "workflow_id": "broken_workflow",
                "workflow_version": "1.0.0",
                "entry_node": "a",
                "nodes": [
                    {
                        "node_id": "a",
                        "display_name": "A",
                        "owner_role": "x",
                        "autonomy_level": "A1",
                        "allowed_tools": [],
                        "input_schema": "x",
                        "output_schema": "y",
                        "limits": {"max_runtime_minutes": 5},
                        "transitions": {"success": "nonexistent_node"},
                    }
                ],
            }
        )


def test_entry_node_must_exist():
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {
                "workflow_id": "broken_workflow",
                "workflow_version": "1.0.0",
                "entry_node": "does_not_exist",
                "nodes": [
                    {
                        "node_id": "a",
                        "display_name": "A",
                        "owner_role": "x",
                        "autonomy_level": "A1",
                        "allowed_tools": [],
                        "input_schema": "x",
                        "output_schema": "y",
                        "limits": {"max_runtime_minutes": 5},
                        "transitions": {},
                    }
                ],
            }
        )


def test_duplicate_node_id_is_rejected():
    node = {
        "node_id": "dup",
        "display_name": "Dup",
        "owner_role": "x",
        "autonomy_level": "A1",
        "allowed_tools": [],
        "input_schema": "x",
        "output_schema": "y",
        "limits": {"max_runtime_minutes": 5},
        "transitions": {},
    }
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {
                "workflow_id": "broken_workflow",
                "workflow_version": "1.0.0",
                "entry_node": "dup",
                "nodes": [node, dict(node)],
            }
        )


def test_load_workflow_requires_path_or_name():
    with pytest.raises(WorkflowError):
        load_workflow()
