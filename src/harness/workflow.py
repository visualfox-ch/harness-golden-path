"""Maschinenlesbare Workflow-Definitionen (P1-4).

Bildet Canvas-Entwürfe (z. B. Feature Delivery) als versionierte, schema-
validierte YAML ab. Diese Definitionen sind Harness-intern: sie hängen an
keiner Panda-Canvas-Fähigkeit (P0-6 hat bestätigt, dass Panda-Canvas keine
Live-Event-Bindung bietet) — die Darstellung erfolgt separat (P2-1, v0-Pfad).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, model_validator

from .contracts import StrictModel
from .policy import PolicyError

WORKFLOW_DIR = Path(__file__).resolve().parent.parent.parent / "workflows"

# Terminal-Sentinels ausserhalb des Node-Graphen — entsprechen TaskStatus-Werten,
# die keine eigenen Workflow-Knoten sind (der Harness-Store haelt den echten Status).
TERMINAL_SENTINELS = {"done", "blocked", "failed", "cancelled", "recovery_required"}

AutonomyLevel = Literal["A0", "A1", "A2", "A3", "A4", "A5"]


class WorkflowLimits(StrictModel):
    # Bis zu 7 Tage zugelassen: Approval-Knoten (z. B. merge_approval) warten auf
    # menschliche Entscheidung, nicht auf Modell-/Tool-Laufzeit — anders als das
    # engere Budget-Limit (480 min) fuer tatsaechliche Ausfuehrungsknoten.
    max_runtime_minutes: Annotated[int, Field(ge=1, le=10080)]
    max_attempts: Annotated[int, Field(ge=1, le=5)] = 1


class WorkflowTransitions(StrictModel):
    success: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    failure: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    approval: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    retry: Annotated[str, Field(min_length=1, max_length=80)] | None = None

    def targets(self) -> list[str]:
        return [t for t in (self.success, self.failure, self.approval, self.retry) if t]


class WorkflowNode(StrictModel):
    node_id: Annotated[str, Field(pattern=r"^[a-z0-9_]{3,80}$")]
    display_name: Annotated[str, Field(min_length=3, max_length=120)]
    owner_role: Annotated[str, Field(min_length=2, max_length=80)]
    autonomy_level: AutonomyLevel
    protected_side_effect: bool = False
    allowed_tools: list[Annotated[str, Field(min_length=1, max_length=80)]]
    allowed_model_roles: list[Annotated[str, Field(min_length=1, max_length=80)]] = (
        Field(default_factory=list)
    )
    routing_policy_class: Annotated[str, Field(min_length=1, max_length=80)] | None = (
        None
    )
    input_schema: Annotated[str, Field(min_length=1, max_length=80)]
    output_schema: Annotated[str, Field(min_length=1, max_length=80)]
    required_artifacts: list[Annotated[str, Field(min_length=1, max_length=80)]] = (
        Field(default_factory=list)
    )
    limits: WorkflowLimits
    transitions: WorkflowTransitions

    @model_validator(mode="after")
    def approval_nodes_are_a4_and_protected(self) -> WorkflowNode:
        if self.autonomy_level == "A4" and not self.protected_side_effect:
            raise ValueError(
                f"node '{self.node_id}' is A4 (approval-gated) but not marked "
                "protected_side_effect"
            )
        return self


class WorkflowDefinition(StrictModel):
    workflow_id: Annotated[str, Field(pattern=r"^[a-z0-9_]{3,80}$")]
    workflow_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    status: Literal["design_only", "active", "deprecated"] = "design_only"
    api_metered_fallback: Literal["forbidden"] = "forbidden"
    entry_node: Annotated[str, Field(min_length=1, max_length=80)]
    nodes: list[WorkflowNode]

    @model_validator(mode="after")
    def validate_graph(self) -> WorkflowDefinition:
        ids = [n.node_id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate node_id in workflow definition")
        id_set = set(ids)
        if self.entry_node not in id_set:
            raise ValueError(f"entry_node '{self.entry_node}' is not a defined node")
        for node in self.nodes:
            for target in node.transitions.targets():
                if target not in id_set and target not in TERMINAL_SENTINELS:
                    raise ValueError(
                        f"node '{node.node_id}' transitions to unknown node '{target}'"
                    )
        return self

    def node(self, node_id: str) -> WorkflowNode:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        raise KeyError(node_id)


class WorkflowError(Exception):
    pass


def load_workflow(
    path: Path | None = None, *, name: str | None = None
) -> WorkflowDefinition:
    if path is None:
        if name is None:
            raise WorkflowError("either path or name is required")
        path = WORKFLOW_DIR / f"{name}.yaml"
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return WorkflowDefinition.model_validate(raw)


def validate_against_routing_policy(
    workflow: WorkflowDefinition, routing_policy: dict
) -> None:
    """Jede referenzierte routing_policy_class muss in der Policy existieren."""
    classes = routing_policy.get("classes", {})
    for node in workflow.nodes:
        if node.routing_policy_class is None:
            continue
        if node.routing_policy_class not in classes:
            raise PolicyError(
                f"node '{node.node_id}' references unknown routing class "
                f"'{node.routing_policy_class}'"
            )
