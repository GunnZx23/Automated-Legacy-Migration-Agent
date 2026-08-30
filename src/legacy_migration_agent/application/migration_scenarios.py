"""Controller-owned recipes, migration units, and launch contracts.

Selecting a migration unit is the only launch-time choice. Human or model prose
is not parsed to decide the platform, source tree, migration direction, runtime,
versions, paths, or knowledge cutoff. Those authority-bearing values come only
from the immutable :class:`MigrationLaunchContract` derived here.

Two concepts are separated so several bounded units can share one platform
behavior without duplicated orchestration:

* :class:`MigrationRecipe` is reusable platform behavior (the analyzer/graph
  builder version, target runtime and API versions, the shared retrieval query,
  and the allowed validation command catalog). Recipes are keyed by
  ``recipe_id``.
* :class:`MigrationScenario` is one controller-approved bounded migration unit
  (its scenario id, display metadata, source root, entry point, frozen input
  and output inventories, scope-policy digest, selected ``recipe_id``, and
  behavior-contract id). Units are keyed by ``scenario_id``, never by platform,
  so a platform may expose any number of units.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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
    CASE_SALESFORCE_PLATFORM_ADAPTER,
    CASE_SOURCE_ENTRY,
    SALESFORCE_API_RUNTIME,
    SALESFORCE_PLATFORM_ADAPTER,
    SALESFORCE_RUNTIME_CONFIG,
    SALESFORCE_SOURCE_ENTRY,
    SALESFORCE_TARGET_RUNTIME,
)

_KNOWLEDGE_AS_OF: Final = date(2026, 8, 29)
SALESFORCE_RECIPE_ID: Final = "salesforce-visualforce-to-lwc"
MULESOFT_RECIPE_ID: Final = "mulesoft-mule3-to-mule4"
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
# The Case unit pins four Case-specific controller-jest diagnostics plus the
# shared Account-error/reset, initial-guidance, blank-selection, and
# plain-JavaScript diagnostics. Requesting the full controller vocabulary would
# exceed the bounded per-page wiki excerpt limit; these ids resolve exactly the
# shared VF/LWC, validation, and Case pages. Security remains controller-owned in
# the fixed implementation contract rather than consuming another retrieval page.
CASE_INITIAL_WIKI_EXACT_IDS: Final = (
    "controller_jest_status_default",
    "controller_jest_case_results",
    "controller_jest_cases_error",
    "controller_jest_clear_selection",
    "salesforce_lwc_javascript_contract",
    "controller_jest_account_error",
    "controller_jest_initial_guidance",
    "controller_jest_account_error_reset",
    "controller_jest_account_error_stale_response",
    "controller_jest_blank_selection",
)
CASE_WIKI_QUERY: Final = "Visualforce LWC Apex security Jest migration " + " ".join(
    CASE_INITIAL_WIKI_EXACT_IDS
)
MULESOFT_WIKI_QUERY: Final = "Mule 3 Mule 4 DataWeave HTTP MUnit migration"


class MigrationRecipe(StrictModel):
    """Reusable, unit-independent platform behavior shared by many units."""

    recipe_id: Identifier
    platform: Platform
    title: str = Field(min_length=1, max_length=200)
    analyzer_version: str = Field(min_length=1, max_length=160)
    target_runtime: str = Field(min_length=1, max_length=160)
    source_version: str = Field(min_length=1, max_length=160)
    target_version: str = Field(min_length=1, max_length=160)
    wiki_query: str = Field(min_length=1, max_length=500)
    wiki_max_primary_hits: int = Field(ge=1, le=32)
    allowed_validation_command_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=64)

    @field_validator("allowed_validation_command_ids")
    @classmethod
    def validate_command_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("recipe validation command ids must be unique")
        return values


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
    """Display metadata and fixed authority for one bounded migration unit."""

    scenario_id: Identifier
    recipe_id: Identifier
    behavior_contract_id: Identifier
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
    def definition_digest(self) -> Sha256Digest:
        """Return the exact digest of this unit definition (identity binding)."""

        return artifact_digest(self)

    @property
    def launch_contract(self) -> MigrationLaunchContract:
        """Return the exact launch authority derived from this fixed unit."""

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


_RECIPES: Final[dict[str, MigrationRecipe]] = {
    SALESFORCE_RECIPE_ID: MigrationRecipe(
        recipe_id=SALESFORCE_RECIPE_ID,
        platform=Platform.SALESFORCE,
        title="Visualforce/Apex to Lightning Web Component/Apex",
        analyzer_version=SALESFORCE_RUNTIME_CONFIG.analyzer_version,
        target_runtime=SALESFORCE_TARGET_RUNTIME,
        source_version=SALESFORCE_API_RUNTIME,
        target_version=SALESFORCE_API_RUNTIME,
        wiki_query=SALESFORCE_WIKI_QUERY,
        wiki_max_primary_hits=1,
        allowed_validation_command_ids=(
            SALESFORCE_PLATFORM_ADAPTER.scope_policy.allowed_validation_command_ids
        ),
    ),
    MULESOFT_RECIPE_ID: MigrationRecipe(
        recipe_id=MULESOFT_RECIPE_ID,
        platform=Platform.MULESOFT,
        title="Mule 3 application to Mule 4 application",
        analyzer_version=MULESOFT_RUNTIME_CONFIG.analyzer_version,
        target_runtime=MULESOFT_TARGET_RUNTIME,
        source_version=MULESOFT_SOURCE_VERSION,
        target_version=MULESOFT_TARGET_VERSION,
        wiki_query=MULESOFT_WIKI_QUERY,
        wiki_max_primary_hits=1,
        allowed_validation_command_ids=(
            MULESOFT_PLATFORM_ADAPTER.scope_policy.allowed_validation_command_ids
        ),
    ),
}


def _validate_unit_against_recipe(unit: MigrationScenario, recipe: MigrationRecipe) -> None:
    """Reject a unit whose shared platform values disagree with its recipe."""

    if unit.platform is not recipe.platform:
        raise ValueError(
            f"unit {unit.scenario_id!r} platform does not match recipe {recipe.recipe_id!r}"
        )
    mismatched = tuple(
        name
        for name in ("analyzer_version", "target_runtime", "source_version", "target_version")
        if getattr(unit, name) != getattr(recipe, name)
    )
    if mismatched:
        raise ValueError(
            f"unit {unit.scenario_id!r} disagrees with recipe {recipe.recipe_id!r} "
            f"on {', '.join(mismatched)}"
        )


def build_unit_registry(
    units: Iterable[MigrationScenario],
    recipes: Mapping[str, MigrationRecipe],
) -> dict[str, MigrationScenario]:
    """Return an ordered unit registry, failing closed on invalid composition.

    This is the single fail-closed gate for the fixed registry and is reused by
    tests to prove that duplicate ids, unknown recipes, or duplicate source
    identity are rejected before any model call.
    """

    registry: dict[str, MigrationScenario] = {}
    seen_source_identity: dict[tuple[str, str], str] = {}
    for unit in units:
        if unit.scenario_id in registry:
            raise ValueError(f"duplicate migration unit id {unit.scenario_id!r}")
        recipe = recipes.get(unit.recipe_id)
        if recipe is None:
            raise ValueError(
                f"unit {unit.scenario_id!r} references unknown recipe {unit.recipe_id!r}"
            )
        _validate_unit_against_recipe(unit, recipe)
        identity = (unit.source_root, unit.entry_path)
        if identity in seen_source_identity:
            raise ValueError(
                f"unit {unit.scenario_id!r} reuses the source identity of "
                f"{seen_source_identity[identity]!r}"
            )
        seen_source_identity[identity] = unit.scenario_id
        registry[unit.scenario_id] = unit
    return registry


_UNITS: Final[tuple[MigrationScenario, ...]] = (
    MigrationScenario(
        scenario_id="salesforce-vf-to-lwc",
        recipe_id=SALESFORCE_RECIPE_ID,
        behavior_contract_id="salesforce-account-contact-explorer",
        platform=Platform.SALESFORCE,
        title="Visualforce to Lightning Web Component",
        canonical_description=(
            "Migrate the bounded Visualforce account/contact explorer "
            "(LegacyAccountContactExplorer.page and "
            "LegacyAccountContactExplorerController.cls) to an additive Lightning Web "
            "Component and Apex implementation. Preserve account selection, an explicit "
            "contact-loading action, visible loading, empty, and safe-error states, "
            "clear prior contact state and invalidate pending work whenever the account changes, "
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
    MigrationScenario(
        scenario_id="case-management-console",
        recipe_id=SALESFORCE_RECIPE_ID,
        behavior_contract_id="salesforce-case-management-console",
        platform=Platform.SALESFORCE,
        title="Case Management Console",
        canonical_description=(
            "Migrate the bounded Visualforce case management console "
            "(LegacyCaseManagementConsole.page, LegacyCaseManagementConsoleController.cls and "
            "LegacyCaseQueryService.cls) to an additive Lightning Web Component and Apex "
            "implementation. Preserve account selection, a status filter defaulting to Open, an "
            "explicit case-loading action with OPEN, CLOSED, and ALL behavior, keyed case results, "
            "initial selection guidance, visible loading, empty, and safe-error states, clear "
            "prior case state and invalidate pending work whenever the account or status changes, "
            "reset selection and Case state and invalidate pending work if the Account wire "
            "transitions to error, stale-response protection, an "
            "explicit clear action, sharing and "
            "field-security controls, and include Apex and LWC Jest tests."
        ),
        display_source_artifacts=(
            "LegacyCaseManagementConsole.page",
            "LegacyCaseManagementConsoleController.cls",
            "LegacyCaseQueryService.cls",
        ),
        target_summary=(
            "Additive caseManagementConsole LWC, sharing-aware Apex service, metadata, and "
            "Apex/LWC tests while preserving the legacy Visualforce entry point."
        ),
        source_root="fixtures/salesforce/case-management-console/input",
        wiki_as_of=_KNOWLEDGE_AS_OF,
        entry_path=CASE_SOURCE_ENTRY,
        target_runtime=SALESFORCE_TARGET_RUNTIME,
        source_version=SALESFORCE_API_RUNTIME,
        target_version=SALESFORCE_API_RUNTIME,
        analyzer_version=SALESFORCE_RUNTIME_CONFIG.analyzer_version,
        adapter_id=CASE_SALESFORCE_PLATFORM_ADAPTER.adapter_id,
        wiki_query=CASE_WIKI_QUERY,
        wiki_max_primary_hits=1,
        required_source_input_paths=(
            CASE_SALESFORCE_PLATFORM_ADAPTER.scope_policy.required_source_input_paths
        ),
        approved_output_paths=CASE_SALESFORCE_PLATFORM_ADAPTER.scope_policy.approved_output_paths,
        scope_policy_digest=CASE_SALESFORCE_PLATFORM_ADAPTER.scope_policy_digest,
    ),
    MigrationScenario(
        scenario_id="mulesoft-mule3-to-mule4",
        recipe_id=MULESOFT_RECIPE_ID,
        behavior_contract_id="mulesoft-customer-status-api",
        platform=Platform.MULESOFT,
        title="Mule 3 to Mule 4",
        canonical_description=(
            "Migrate the bounded Mule 3 customer-status API slice to an additive Mule 4 "
            "application. Preserve GET /api/customers/{customerId}/status behavior, use "
            "DataWeave 2, standalone Maven configuration, and target MUnit tests, retain the "
            "controller-owned validation boundary, and preserve the Mule 3 source."
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
)


_SCENARIOS: Final[dict[str, MigrationScenario]] = build_unit_registry(_UNITS, _RECIPES)


def _primary_by_platform() -> dict[Platform, MigrationScenario]:
    primary: dict[Platform, MigrationScenario] = {}
    for scenario in _SCENARIOS.values():
        primary.setdefault(scenario.platform, scenario)
    return primary


_PRIMARY_BY_PLATFORM: Final[dict[Platform, MigrationScenario]] = _primary_by_platform()


def migration_recipes() -> tuple[MigrationRecipe, ...]:
    """Return the fixed reusable platform recipes in registration order."""

    return tuple(_RECIPES.values())


def migration_recipe(recipe_id: str) -> MigrationRecipe:
    """Return one exact recipe identity without inferring from prose."""

    try:
        return _RECIPES[recipe_id]
    except KeyError as exc:
        raise KeyError(recipe_id) from exc


def migration_scenarios() -> tuple[MigrationScenario, ...]:
    """Return every registered migration unit in display order."""

    return tuple(_SCENARIOS.values())


def migration_units_for_platform(platform: Platform) -> tuple[MigrationScenario, ...]:
    """Return every registered unit for ``platform`` in display order."""

    return tuple(scenario for scenario in _SCENARIOS.values() if scenario.platform is platform)


def migration_scenario(platform: Platform) -> MigrationScenario:
    """Return the primary (first-registered) unit for ``platform``.

    A platform may now expose several units; this convenience resolves the
    primary unit for display and legacy callers. Identity-critical callers must
    resolve an exact unit through :func:`migration_scenario_by_id`.
    """

    try:
        return _PRIMARY_BY_PLATFORM[platform]
    except KeyError as exc:
        raise KeyError(platform) from exc


def migration_scenario_by_id(scenario_id: str) -> MigrationScenario:
    """Return one exact unit identity without inferring from prose."""

    try:
        return _SCENARIOS[scenario_id]
    except KeyError as exc:
        raise KeyError(scenario_id) from exc


def migration_launch_contract(scenario_id: str) -> MigrationLaunchContract:
    """Derive the immutable launch contract for one exact unit identity."""

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
    "CASE_INITIAL_WIKI_EXACT_IDS",
    "CASE_WIKI_QUERY",
    "MULESOFT_RECIPE_ID",
    "MULESOFT_WIKI_QUERY",
    "MigrationLaunchContract",
    "MigrationRecipe",
    "MigrationScenario",
    "SALESFORCE_INITIAL_WIKI_EXACT_IDS",
    "SALESFORCE_RECIPE_ID",
    "SALESFORCE_WIKI_QUERY",
    "build_unit_registry",
    "migration_launch_contract",
    "migration_recipe",
    "migration_recipes",
    "migration_scenario",
    "migration_scenario_by_id",
    "migration_scenarios",
    "migration_units_for_platform",
    "require_canonical_launch_contract",
]
