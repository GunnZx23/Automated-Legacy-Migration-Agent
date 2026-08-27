"""Controller-owned launch contracts for the two supported migrations.

Selecting a scenario is the only launch-time choice. Human or model prose is
not parsed to decide the platform, source tree, migration direction, runtime,
versions, paths, or knowledge cutoff. Those authority-bearing values come
only from the immutable :class:`MigrationLaunchContract` derived here.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from pydantic import Field, field_validator

from legacy_migration_agent.contracts import (
    Identifier,
    Platform,
    Sha256Digest,
    StrictModel,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.platforms.mulesoft_local_checks import MULE3_APP
from legacy_migration_agent.platforms.mulesoft_runtime import (
    MULESOFT_PLATFORM_ADAPTER,
    MULESOFT_RUNTIME_CONFIG,
    MULESOFT_SOURCE_VERSION,
    MULESOFT_TARGET_RUNTIME,
    MULESOFT_TARGET_VERSION,
)
from legacy_migration_agent.platforms.salesforce_runtime import (
    SALESFORCE_API_RUNTIME,
    SALESFORCE_PLATFORM_ADAPTER,
    SALESFORCE_RUNTIME_CONFIG,
    SALESFORCE_SOURCE_ENTRY,
    SALESFORCE_TARGET_RUNTIME,
)

_KNOWLEDGE_AS_OF: Final = date(2026, 8, 27)
SALESFORCE_INITIAL_WIKI_EXACT_IDS: Final = (
    "controller_jest_account_options",
    "controller_jest_account_error",
    "controller_jest_selection_gate",
    "controller_jest_explicit_load",
    "controller_jest_loading_state",
    "controller_jest_stale_response",
    "controller_jest_blank_selection",
    "controller_jest_empty_state",
    "controller_jest_contacts_error",
    "salesforce_lwc_javascript_contract",
    "apex_public_interface_annotation_mismatch",
)
SALESFORCE_WIKI_QUERY: Final = "Visualforce LWC Apex security Jest migration " + " ".join(
    SALESFORCE_INITIAL_WIKI_EXACT_IDS
)
MULESOFT_WIKI_QUERY: Final = "Mule 3 Mule 4 DataWeave HTTP MUnit migration"


class MigrationLaunchContract(StrictModel):
    """Exact controller-owned authority for one supported migration launch."""

    scenario_id: Identifier
    platform: Platform
    canonical_description: str = Field(min_length=10, max_length=1000)
    source_root: str
    wiki_as_of: date
    entry_path: str
    target_runtime: str = Field(min_length=1, max_length=160)
    source_version: str = Field(min_length=1, max_length=160)
    target_version: str = Field(min_length=1, max_length=160)
    analyzer_version: str = Field(min_length=1, max_length=160)
    adapter_id: Identifier
    wiki_query: str = Field(min_length=1, max_length=500)
    wiki_max_primary_hits: int = Field(ge=1, le=32)
    required_source_input_paths: tuple[str, ...] = Field(min_length=1, max_length=256)
    approved_output_paths: tuple[str, ...] = Field(min_length=1, max_length=256)
    scope_policy_digest: Sha256Digest
    target_summary: str = Field(min_length=10, max_length=500)

    @field_validator("source_root", "entry_path")
    @classmethod
    def validate_single_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("required_source_input_paths", "approved_output_paths")
    @classmethod
    def validate_path_inventory(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(validate_relative_path(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("launch-contract path inventories must be unique")
        return normalized


class MigrationScenario(StrictModel):
    """Display metadata and fixed authority for one supported migration."""

    scenario_id: Identifier
    platform: Platform
    title: str = Field(min_length=1, max_length=200)
    canonical_description: str = Field(min_length=10, max_length=1000)
    display_source_artifacts: tuple[str, ...] = Field(min_length=1, max_length=8)
    target_summary: str = Field(min_length=10, max_length=500)
    source_root: str
    wiki_as_of: date
    entry_path: str
    target_runtime: str = Field(min_length=1, max_length=160)
    source_version: str = Field(min_length=1, max_length=160)
    target_version: str = Field(min_length=1, max_length=160)
    analyzer_version: str = Field(min_length=1, max_length=160)
    adapter_id: Identifier
    wiki_query: str = Field(min_length=1, max_length=500)
    wiki_max_primary_hits: int = Field(ge=1, le=32)
    required_source_input_paths: tuple[str, ...] = Field(min_length=1, max_length=256)
    approved_output_paths: tuple[str, ...] = Field(min_length=1, max_length=256)
    scope_policy_digest: Sha256Digest

    @field_validator("source_root", "entry_path")
    @classmethod
    def validate_single_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("display_source_artifacts")
    @classmethod
    def validate_display_source_artifacts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value.strip() or len(value) > 200 or "/" in value or "\\" in value
            for value in values
        ):
            raise ValueError("display source artifacts must be unique bounded file names")
        return values

    @field_validator("required_source_input_paths", "approved_output_paths")
    @classmethod
    def validate_path_inventory(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(validate_relative_path(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("scenario path inventories must be unique")
        return normalized

    @property
    def launch_contract(self) -> MigrationLaunchContract:
        """Return the exact launch authority derived from this fixed scenario."""

        return MigrationLaunchContract(
            scenario_id=self.scenario_id,
            platform=self.platform,
            canonical_description=self.canonical_description,
            source_root=self.source_root,
            wiki_as_of=self.wiki_as_of,
            entry_path=self.entry_path,
            target_runtime=self.target_runtime,
            source_version=self.source_version,
            target_version=self.target_version,
            analyzer_version=self.analyzer_version,
            adapter_id=self.adapter_id,
            wiki_query=self.wiki_query,
            wiki_max_primary_hits=self.wiki_max_primary_hits,
            required_source_input_paths=self.required_source_input_paths,
            approved_output_paths=self.approved_output_paths,
            scope_policy_digest=self.scope_policy_digest,
            target_summary=self.target_summary,
        )


_SCENARIOS: Final[dict[Platform, MigrationScenario]] = {
    Platform.SALESFORCE: MigrationScenario(
        scenario_id="salesforce-vf-to-lwc",
        platform=Platform.SALESFORCE,
        title="Visualforce to Lightning Web Component",
        canonical_description=(
            "Migrate the bounded Visualforce account/contact explorer "
            "(LegacyAccountContactExplorer.page and "
            "LegacyAccountContactExplorerController.cls) to an additive Lightning Web "
            "Component and Apex implementation. Preserve account selection, an explicit "
            "contact-loading action, visible loading, empty, and safe-error states, "
            "stale-response protection, sharing "
            "and field-security controls, and include Apex and LWC Jest tests."
        ),
        display_source_artifacts=(
            "LegacyAccountContactExplorer.page",
            "LegacyAccountContactExplorerController.cls",
        ),
        target_summary=(
            "Additive accountContactExplorer LWC, sharing-aware Apex service, metadata, and "
            "Apex/LWC tests while preserving the legacy Visualforce entry point."
        ),
        source_root="fixtures/salesforce/account-contact-explorer/input",
        wiki_as_of=_KNOWLEDGE_AS_OF,
        entry_path=SALESFORCE_SOURCE_ENTRY,
        target_runtime=SALESFORCE_TARGET_RUNTIME,
        source_version=SALESFORCE_API_RUNTIME,
        target_version=SALESFORCE_API_RUNTIME,
        analyzer_version=SALESFORCE_RUNTIME_CONFIG.analyzer_version,
        adapter_id=SALESFORCE_PLATFORM_ADAPTER.adapter_id,
        wiki_query=SALESFORCE_WIKI_QUERY,
        wiki_max_primary_hits=1,
        required_source_input_paths=(
            SALESFORCE_PLATFORM_ADAPTER.scope_policy.required_source_input_paths
        ),
        approved_output_paths=SALESFORCE_PLATFORM_ADAPTER.scope_policy.approved_output_paths,
        scope_policy_digest=SALESFORCE_PLATFORM_ADAPTER.scope_policy_digest,
    ),
    Platform.MULESOFT: MigrationScenario(
        scenario_id="mulesoft-mule3-to-mule4",
        platform=Platform.MULESOFT,
        title="Mule 3 to Mule 4",
        canonical_description=(
            "Migrate the bounded Mule 3 customer-status API slice to an additive Mule 4 "
            "application with a controller-owned validation boundary."
        ),
        display_source_artifacts=("customer-status-api.xml", "customer-status-api-test.xml"),
        target_summary=(
            "Additive Mule 4 customer-status application with DataWeave 2, configuration, "
            "Maven metadata, and target MUnit artifacts while preserving the Mule 3 source."
        ),
        source_root="fixtures/mulesoft/customer-status-api/input",
        wiki_as_of=_KNOWLEDGE_AS_OF,
        entry_path=MULE3_APP,
        target_runtime=MULESOFT_TARGET_RUNTIME,
        source_version=MULESOFT_SOURCE_VERSION,
        target_version=MULESOFT_TARGET_VERSION,
        analyzer_version=MULESOFT_RUNTIME_CONFIG.analyzer_version,
        adapter_id=MULESOFT_PLATFORM_ADAPTER.adapter_id,
        wiki_query=MULESOFT_WIKI_QUERY,
        wiki_max_primary_hits=1,
        required_source_input_paths=(
            MULESOFT_PLATFORM_ADAPTER.scope_policy.required_source_input_paths
        ),
        approved_output_paths=MULESOFT_PLATFORM_ADAPTER.scope_policy.approved_output_paths,
        scope_policy_digest=MULESOFT_PLATFORM_ADAPTER.scope_policy_digest,
    ),
}


def migration_scenarios() -> tuple[MigrationScenario, MigrationScenario]:
    """Return the fixed Salesforce and MuleSoft scenarios in display order."""

    return (_SCENARIOS[Platform.SALESFORCE], _SCENARIOS[Platform.MULESOFT])


def migration_scenario(platform: Platform) -> MigrationScenario:
    """Return the fixed scenario for ``platform`` for display and inspection."""

    return _SCENARIOS[platform]


def migration_scenario_by_id(scenario_id: str) -> MigrationScenario:
    """Return one exact scenario identity without inferring from prose."""

    for scenario in _SCENARIOS.values():
        if scenario.scenario_id == scenario_id:
            return scenario
    raise KeyError(scenario_id)


def migration_launch_contract(scenario_id: str) -> MigrationLaunchContract:
    """Derive the immutable launch contract for one exact scenario identity."""

    return migration_scenario_by_id(scenario_id).launch_contract


def require_canonical_launch_contract(
    contract: MigrationLaunchContract,
) -> MigrationLaunchContract:
    """Return the canonical contract or reject any caller-constructed drift.

    The digest is an equality binding over canonical typed values, not a
    signature or proof of caller identity.
    """

    canonical = migration_launch_contract(contract.scenario_id)
    if artifact_digest(contract) != artifact_digest(canonical):
        raise ValueError("migration launch contract differs from the fixed scenario")
    return canonical


__all__ = [
    "MULESOFT_WIKI_QUERY",
    "MigrationLaunchContract",
    "MigrationScenario",
    "SALESFORCE_INITIAL_WIKI_EXACT_IDS",
    "SALESFORCE_WIKI_QUERY",
    "migration_launch_contract",
    "migration_scenario",
    "migration_scenario_by_id",
    "migration_scenarios",
    "require_canonical_launch_contract",
]
