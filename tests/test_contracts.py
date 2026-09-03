import pytest
from pydantic import ValidationError

from harness.contracts import CostReceipt, ModelRouting, ProviderClass, Scope
from harness.policy import PolicyError, assert_route_allowed, load_catalog


def test_api_metered_provider_class_is_forbidden():
    with pytest.raises(ValidationError):
        ModelRouting(
            permitted_provider_classes=[
                ProviderClass.SUBSCRIPTION_OAUTH,
                ProviderClass.API_METERED,
            ],
            default_route=["anthropic_oauth_reasoner"],
        )


def test_unsafe_scope_paths_are_rejected():
    with pytest.raises(ValidationError):
        Scope(include=["../etc/passwd"])
    with pytest.raises(ValidationError):
        Scope(include=["src/../../secrets"])


def test_cost_receipt_forbids_api_metered():
    with pytest.raises(ValidationError):
        CostReceipt(
            provider_class=ProviderClass.API_METERED,
            model_ref="some_metered_model",
        )


def test_policy_rejects_unknown_route():
    catalog = load_catalog()
    routing = ModelRouting(
        permitted_provider_classes=[ProviderClass.SUBSCRIPTION_OAUTH],
        default_route=["model_not_in_catalog"],
    )
    with pytest.raises(PolicyError):
        assert_route_allowed(routing, catalog)


def test_policy_rejects_unverified_route():
    catalog = {
        "models": {
            "assumed_model": {
                "provider_class": "subscription_oauth",
                "connection_status": "assumed",
            }
        }
    }
    routing = ModelRouting(
        permitted_provider_classes=[ProviderClass.SUBSCRIPTION_OAUTH],
        default_route=["assumed_model"],
    )
    with pytest.raises(PolicyError):
        assert_route_allowed(routing, catalog)


def test_policy_accepts_verified_catalog_route():
    catalog = load_catalog()
    routing = ModelRouting(
        permitted_provider_classes=[ProviderClass.SUBSCRIPTION_OAUTH],
        default_route=["anthropic_oauth_reasoner"],
    )
    assert_route_allowed(routing, catalog)
