"""Durable, strictly deserialized LangGraph checkpoints for local runs."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from legacy_migration_agent.contracts import (
    ApprovalAction,
    ArtifactDigest,
    ChangeSet,
    CheckResult,
    CheckStatus,
    DecisionRequest,
    DependencyEvidence,
    EnvironmentKind,
    ImplementationIntervention,
    ImplementationInterventionEvidence,
    ManifestStatus,
    MigrationManifest,
    MigrationRequest,
    MigrationTarget,
    PlanningIntervention,
    PlanningInterventionEvidence,
    PlanningInterventionOption,
    Platform,
    RiskCategory,
    RiskFinding,
    ToolReceipt,
    TransformationStep,
    ValidationCommand,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.workflow import (
    Architect,
    Engineer,
    ManifestApproval,
    MigrationWorkflow,
    Validator,
    build_workflow,
)

CHECKPOINT_SUFFIX = ".sqlite3"


def strict_checkpoint_serializer() -> JsonPlusSerializer:
    """Allow only the project's inert typed state plus LangGraph safe types."""

    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_msgpack_modules=(
            ApprovalAction,
            ArtifactDigest,
            ChangeSet,
            CheckResult,
            CheckStatus,
            DecisionRequest,
            DependencyEvidence,
            EnvironmentKind,
            ImplementationIntervention,
            ImplementationInterventionEvidence,
            ManifestApproval,
            ManifestStatus,
            MigrationManifest,
            MigrationRequest,
            MigrationTarget,
            PlanningIntervention,
            PlanningInterventionEvidence,
            PlanningInterventionOption,
            Platform,
            RiskCategory,
            RiskFinding,
            ToolReceipt,
            TransformationStep,
            ValidationCommand,
            ValidationDisposition,
            ValidationReport,
        ),
    )


@contextmanager
def durable_migration_workflow(
    database_path: Path,
    architect: Architect,
    engineer: Engineer,
    validator: Validator,
    *,
    maximum_execution_attempts: int = 2,
) -> Iterator[MigrationWorkflow]:
    """Open a lightweight local workflow that survives process restarts.

    The database is trusted local state but is still deserialized through an
    explicit type allowlist. This is the SQLite checkpoint boundary used by the
    session-bound agent workflow; it is intentionally single-process and is not
    presented as a distributed persistence service.
    """

    path = _prepare_database_path(database_path)
    connection = sqlite3.connect(path, check_same_thread=False)
    try:
        if not path.exists() or path.is_symlink():  # defensive after connection open
            raise PolicyViolation("checkpoint database could not be created safely")
        os.chmod(path, 0o600)
        checkpointer = SqliteSaver(
            connection,
            serde=strict_checkpoint_serializer(),
        )
        yield build_workflow(
            architect,
            engineer,
            validator,
            checkpointer=checkpointer,
            maximum_execution_attempts=maximum_execution_attempts,
        )
    finally:
        connection.close()


def _prepare_database_path(database_path: Path) -> Path:
    if database_path.suffix != CHECKPOINT_SUFFIX:
        raise ValueError(f"checkpoint database must use {CHECKPOINT_SUFFIX}")
    parent = database_path.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ValueError("checkpoint database parent must be a directory")
    candidate = parent / database_path.name
    if candidate.exists():
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PolicyViolation("checkpoint database must be a regular non-symlink file")
    return candidate
