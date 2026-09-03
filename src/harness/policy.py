"""Deterministische Policy-Prüfung: nur verifizierte, nicht-metered Routen."""
from __future__ import annotations

from pathlib import Path

import yaml

from .contracts import ModelRouting, ProviderClass

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


def assert_route_allowed(routing: ModelRouting, catalog: dict) -> None:
    """Jede Route muss im Katalog stehen, verifiziert und nicht api_metered sein."""
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
