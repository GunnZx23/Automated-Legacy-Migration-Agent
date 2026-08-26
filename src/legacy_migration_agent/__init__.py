"""Automated Legacy Migration Agent."""

from legacy_migration_agent.contracts import (
    ChangeSet,
    DecisionRequest,
    ImplementationIntervention,
    ImplementationInterventionEvidence,
    MigrationManifest,
    MigrationRequest,
    PlanningIntervention,
    PlanningInterventionEvidence,
    PlanningInterventionOption,
    ToolReceipt,
    ValidationReport,
)

__all__ = [
    "ChangeSet",
    "DecisionRequest",
    "ImplementationIntervention",
    "ImplementationInterventionEvidence",
    "MigrationManifest",
    "MigrationRequest",
    "PlanningIntervention",
    "PlanningInterventionEvidence",
    "PlanningInterventionOption",
    "ToolReceipt",
    "ValidationReport",
]

__version__ = "0.1.0"
