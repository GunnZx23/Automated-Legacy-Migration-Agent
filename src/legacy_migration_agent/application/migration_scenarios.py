"""Controller-owned scenarios for the interactive migration agent.

The browser may select a platform and edit the human request, but it never
supplies filesystem routes, writable paths, validation commands, or knowledge
cutoff dates.  Those authority-bearing values remain fixed here.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from pydantic import Field, field_validator

from legacy_migration_agent.contracts import Platform, StrictModel, validate_relative_path

_KNOWLEDGE_AS_OF: Final = date(2026, 8, 26)


class MigrationScenario(StrictModel):
    """One bounded fixture-backed migration supported by the Agent UI."""

    platform: Platform
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=10, max_length=1000)
    source_root: str
    wiki_as_of: date

    @field_validator("source_root")
    @classmethod
    def validate_source_root(cls, value: str) -> str:
        return validate_relative_path(value)


_SCENARIOS: Final[dict[Platform, MigrationScenario]] = {
    Platform.SALESFORCE: MigrationScenario(
        platform=Platform.SALESFORCE,
        title="Visualforce to Lightning Web Component",
        prompt=(
            "Migrate the bounded Visualforce account/contact explorer to an additive "
            "Lightning Web Component and Apex implementation."
        ),
        source_root="fixtures/salesforce/account-contact-explorer/input",
        wiki_as_of=_KNOWLEDGE_AS_OF,
    ),
    Platform.MULESOFT: MigrationScenario(
        platform=Platform.MULESOFT,
        title="Mule 3 to Mule 4",
        prompt=(
            "Migrate the bounded Mule 3 customer-status API slice to an additive Mule 4 "
            "application with a controller-owned validation boundary."
        ),
        source_root="fixtures/mulesoft/customer-status-api/input",
        wiki_as_of=_KNOWLEDGE_AS_OF,
    ),
}


def migration_scenarios() -> tuple[MigrationScenario, MigrationScenario]:
    """Return the fixed Salesforce and MuleSoft scenarios in display order."""

    return (_SCENARIOS[Platform.SALESFORCE], _SCENARIOS[Platform.MULESOFT])


def migration_scenario(platform: Platform) -> MigrationScenario:
    """Return the controller-owned scenario for ``platform``."""

    return _SCENARIOS[platform]


__all__ = ["MigrationScenario", "migration_scenario", "migration_scenarios"]
