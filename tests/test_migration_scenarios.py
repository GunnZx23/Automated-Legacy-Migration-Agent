from __future__ import annotations

import inspect
from datetime import date

import pytest
from pydantic import ValidationError

from legacy_migration_agent.application.agent_run import (
    prepare_agent_run_request,
    start_agent_run,
)
from legacy_migration_agent.application.migration_scenarios import (
    MigrationLaunchContract,
    build_unit_registry,
    migration_launch_contract,
    migration_recipe,
    migration_recipes,
    migration_scenario,
    migration_scenario_by_id,
    migration_scenarios,
    migration_units_for_platform,
    require_canonical_launch_contract,
)
from legacy_migration_agent.contracts import Platform
from legacy_migration_agent.core.integrity import artifact_digest


def _recipe_map() -> dict[str, object]:
    return {recipe.recipe_id: recipe for recipe in migration_recipes()}


@pytest.mark.parametrize("scenario", migration_scenarios())
def test_scenario_derives_one_exact_immutable_launch_contract(scenario) -> None:
    contract = scenario.launch_contract

    assert contract == migration_launch_contract(scenario.scenario_id)
    assert contract.scenario_id == scenario.scenario_id
    assert contract.platform is scenario.platform
    assert contract.canonical_description == scenario.canonical_description
    assert contract.source_root == scenario.source_root
    assert contract.wiki_as_of == scenario.wiki_as_of
    assert contract.entry_path == scenario.entry_path
    assert contract.target_runtime == scenario.target_runtime
    assert contract.source_version == scenario.source_version
    assert contract.target_version == scenario.target_version
    assert contract.analyzer_version == scenario.analyzer_version
    assert contract.adapter_id == scenario.adapter_id
    assert contract.wiki_query == scenario.wiki_query
    assert contract.wiki_max_primary_hits == scenario.wiki_max_primary_hits
    assert contract.required_source_input_paths == scenario.required_source_input_paths
    assert contract.approved_output_paths == scenario.approved_output_paths
    assert contract.scope_policy_digest == scenario.scope_policy_digest
    assert contract.target_summary == scenario.target_summary
    assert "display_source_artifacts" not in type(contract).model_fields
    assert scenario.display_source_artifacts
    assert require_canonical_launch_contract(contract) == contract
    assert artifact_digest(contract).startswith("sha256:")


def test_scenario_inventory_has_only_the_three_supported_migrations() -> None:
    assert tuple(scenario.scenario_id for scenario in migration_scenarios()) == (
        "salesforce-vf-to-lwc",
        "case-management-console",
        "mulesoft-mule3-to-mule4",
    )
    assert migration_scenario(Platform.SALESFORCE).source_root == (
        "fixtures/salesforce/account-contact-explorer/input"
    )
    assert migration_scenario(Platform.MULESOFT).source_root == (
        "fixtures/mulesoft/customer-status-api/input"
    )


def test_launch_contract_cannot_be_mutated_or_extended() -> None:
    contract = migration_scenario(Platform.SALESFORCE).launch_contract

    with pytest.raises(ValidationError):
        contract.platform = Platform.MULESOFT  # type: ignore[misc]
    with pytest.raises(ValidationError):
        MigrationLaunchContract.model_validate(
            {**contract.model_dump(mode="python"), "description": "free-form authority"}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("platform", Platform.MULESOFT),
        ("canonical_description", "Target a Rust service instead."),
        ("source_root", "unapproved/source"),
        ("wiki_as_of", date(2026, 8, 25)),
        ("entry_path", "unapproved/source.file"),
        ("target_runtime", "Rust 1.89"),
        ("source_version", "Salesforce API 66.0"),
        ("target_version", "Salesforce API 68.0"),
        ("analyzer_version", "unapproved-analyzer-v1"),
        ("adapter_id", "unapproved-adapter-v1"),
        ("wiki_query", "unapproved retrieval authority"),
        ("wiki_max_primary_hits", 2),
        ("required_source_input_paths", ("EvilOpportunity.trigger",)),
        ("approved_output_paths", ("generated/evil.rs",)),
        ("scope_policy_digest", "sha256:" + "0" * 64),
        ("target_summary", "Generate Go code instead of the fixed target."),
    ),
)
def test_caller_constructed_launch_contract_drift_is_rejected(field: str, value: object) -> None:
    canonical = migration_scenario(Platform.SALESFORCE).launch_contract
    tampered = canonical.model_copy(update={field: value})

    with pytest.raises(ValueError, match="differs from the fixed scenario"):
        require_canonical_launch_contract(tampered)


def test_launch_entrypoints_expose_no_free_form_migration_authority() -> None:
    prepare_parameters = inspect.signature(prepare_agent_run_request).parameters
    start_parameters = inspect.signature(start_agent_run).parameters

    assert "launch_contract" in prepare_parameters
    assert "launch_contract" in start_parameters
    for forbidden in ("platform", "source_root", "description", "scenario_id", "wiki_as_of"):
        assert forbidden not in prepare_parameters
        assert forbidden not in start_parameters


def test_unknown_scenario_identity_is_not_inferred() -> None:
    with pytest.raises(KeyError):
        migration_scenario_by_id("salesforce-aura-to-lwc")
    with pytest.raises(KeyError):
        migration_launch_contract("mulesoft-to-spring")


def test_every_unit_references_a_known_recipe_with_shared_platform_values() -> None:
    recipes = _recipe_map()
    for scenario in migration_scenarios():
        assert scenario.recipe_id in recipes
        recipe = recipes[scenario.recipe_id]
        assert scenario.platform is recipe.platform
        assert scenario.analyzer_version == recipe.analyzer_version
        assert scenario.target_runtime == recipe.target_runtime
        assert scenario.source_version == recipe.source_version
        assert scenario.target_version == recipe.target_version
        assert scenario.behavior_contract_id
        assert scenario.definition_digest.startswith("sha256:")


def test_unknown_recipe_identity_is_not_inferred() -> None:
    with pytest.raises(KeyError):
        migration_recipe("salesforce-aura-to-lwc")


def test_units_group_by_platform_without_collapsing_to_one_per_platform() -> None:
    salesforce_units = migration_units_for_platform(Platform.SALESFORCE)
    assert migration_scenario(Platform.SALESFORCE) in salesforce_units
    assert all(unit.platform is Platform.SALESFORCE for unit in salesforce_units)


def test_registry_builder_accepts_two_units_sharing_one_platform_and_recipe() -> None:
    recipes = _recipe_map()
    primary = migration_scenario(Platform.SALESFORCE)
    sibling = primary.model_copy(
        update={
            "scenario_id": "salesforce-second-unit",
            "source_root": "fixtures/salesforce/second-unit/input",
            "entry_path": "fixtures/salesforce/second-unit/input/pages/Second.page",
        }
    )
    registry = build_unit_registry((primary, sibling), recipes)
    assert tuple(registry) == ("salesforce-vf-to-lwc", "salesforce-second-unit")
    assert registry["salesforce-second-unit"].recipe_id == primary.recipe_id


def test_registry_builder_rejects_duplicate_unit_id() -> None:
    recipes = _recipe_map()
    primary = migration_scenario(Platform.SALESFORCE)
    with pytest.raises(ValueError, match="duplicate migration unit id"):
        build_unit_registry((primary, primary), recipes)


def test_registry_builder_rejects_unknown_recipe() -> None:
    recipes = _recipe_map()
    orphan = migration_scenario(Platform.SALESFORCE).model_copy(
        update={"scenario_id": "salesforce-orphan", "recipe_id": "salesforce-unknown-recipe"}
    )
    with pytest.raises(ValueError, match="unknown recipe"):
        build_unit_registry((orphan,), recipes)


def test_registry_builder_rejects_duplicate_source_identity() -> None:
    recipes = _recipe_map()
    primary = migration_scenario(Platform.SALESFORCE)
    clone = primary.model_copy(update={"scenario_id": "salesforce-clone"})
    with pytest.raises(ValueError, match="reuses the source identity"):
        build_unit_registry((primary, clone), recipes)


def test_registry_builder_rejects_recipe_platform_mismatch() -> None:
    recipes = _recipe_map()
    mule_primary = migration_scenario(Platform.MULESOFT)
    mismatched = mule_primary.model_copy(
        update={"scenario_id": "mule-wrong-recipe", "recipe_id": "salesforce-visualforce-to-lwc"}
    )
    with pytest.raises(ValueError, match="platform does not match recipe"):
        build_unit_registry((mismatched,), recipes)
