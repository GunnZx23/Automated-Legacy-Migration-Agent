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
    migration_launch_contract,
    migration_scenario,
    migration_scenario_by_id,
    migration_scenarios,
    require_canonical_launch_contract,
)
from legacy_migration_agent.contracts import Platform
from legacy_migration_agent.core.integrity import artifact_digest


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


def test_scenario_inventory_has_only_the_two_supported_migrations() -> None:
    assert tuple(scenario.scenario_id for scenario in migration_scenarios()) == (
        "salesforce-vf-to-lwc",
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
