"""Versioned handoff contracts for the migration workflow.

The contracts intentionally encode safety invariants that must not depend on
prompt compliance. Cross-artifact checks live in :mod:`policies` because they
require both the manifest and the candidate output.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Literal["1.0"] = "1.0"
SchemaVersion = Literal["1.0"]
Identifier = Annotated[
    str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
Revision = Annotated[str, Field(min_length=7, max_length=160)]
Sha256Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
GitRevision = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
ActorIdentifier = Annotated[
    str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._@+-]*$")
]


class StrictModel(BaseModel):
    """Base class that rejects undeclared fields and supports immutable data."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Platform(StrEnum):
    SALESFORCE = "salesforce"
    MULESOFT = "mulesoft"


class EnvironmentKind(StrEnum):
    LOCAL = "local"
    SALESFORCE_SANDBOX = "salesforce_sandbox"
    MULE_RUNTIME = "mule_runtime"
    PRODUCTION = "production"


class ManifestStatus(StrEnum):
    PLANNED = "planned"
    DECISION_REQUIRED = "decision_required"


class TransformationStepKind(StrEnum):
    ARTIFACT_TRANSFORMATION = "artifact_transformation"
    SEMANTIC_DECISION = "semantic_decision"


class ValidationDisposition(StrEnum):
    READY_FOR_HUMAN_REVIEW = "ready_for_human_review"
    RECOVERABLE_FAILURE = "recoverable_failure"
    PLAN_INVALID = "plan_invalid"
    ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
    DECISION_REQUIRED = "decision_required"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    NONTERMINAL = "nonterminal"


class RiskCategory(StrEnum):
    SECURITY = "security"
    PUBLIC_CONTRACT = "public_contract"
    DESTRUCTIVE_CHANGE = "destructive_change"
    CROSS_APPLICATION = "cross_application"
    INCOMPLETE_EVIDENCE = "incomplete_evidence"
    DYNAMIC_DEPENDENCY = "dynamic_dependency"


class PlanningInterventionOption(StrEnum):
    """Non-authorizing terminal choices for an incomplete planning outcome."""

    RESOLVE_EVIDENCE_AND_REPLAN = "resolve_evidence_and_replan"
    EXPAND_SCOPE_AND_REPLAN = "expand_scope_and_replan"
    STOP_REQUEST = "stop_request"


class ApprovalAction(StrEnum):
    FREEZE_SCOPE = "freeze_scope"
    APPROVE_MANIFEST = "approve_manifest"
    EXPAND_SCOPE = "expand_scope"
    ACCEPT_HIGH_IMPACT_CHANGE = "accept_high_impact_change"
    COMMIT = "commit"
    PUSH = "push"
    OPEN_PULL_REQUEST = "open_pull_request"
    SANDBOX_VALIDATION = "sandbox_validation"
    PRODUCTION_VALIDATION = "production_validation"
    DEPLOY = "deploy"
    QUICK_DEPLOY = "quick_deploy"
    DESTRUCTIVE_CHANGE = "destructive_change"
    PUBLISH = "publish"


ImplementationInterventionAction = Literal[
    ApprovalAction.EXPAND_SCOPE,
    ApprovalAction.ACCEPT_HIGH_IMPACT_CHANGE,
]


class MigrationTarget(StrictModel):
    entry_path: str
    target_runtime: str
    source_version: str = Field(min_length=1, max_length=160)
    target_version: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1000)

    @field_validator("entry_path")
    @classmethod
    def validate_entry_path(cls, value: str) -> str:
        return validate_relative_path(value)


class MigrationRequest(StrictModel):
    schema_version: SchemaVersion = SCHEMA_VERSION
    request_id: Identifier
    platform: Platform
    repository: str = Field(min_length=1, max_length=2048)
    base_revision: Revision
    target: MigrationTarget
    allowed_environment: EnvironmentKind = EnvironmentKind.LOCAL
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def prohibit_production_as_default_target(self) -> MigrationRequest:
        if self.allowed_environment is EnvironmentKind.PRODUCTION:
            raise ValueError("production cannot be selected as the automatic execution environment")
        return self


class DependencyEvidence(StrictModel):
    path: str
    relation: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1, max_length=2048)
    resolved: bool = True

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class PlanningInterventionEvidence(StrictModel):
    """One immutable evidence source explaining why planning stopped."""

    category: RiskCategory
    source: str = Field(min_length=1, max_length=160)
    source_digest: Sha256Digest
    summary: str = Field(min_length=1, max_length=2000)
    affected_paths: tuple[str, ...] = Field(min_length=1, max_length=64)
    unresolved_items: tuple[str, ...] = Field(min_length=1, max_length=64)

    @field_validator("affected_paths")
    @classmethod
    def validate_affected_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(validate_relative_path(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("planning evidence affected paths must be unique")
        return normalized

    @field_validator("unresolved_items")
    @classmethod
    def validate_unresolved_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("planning evidence unresolved items cannot be blank")
        if len(values) != len(set(values)):
            raise ValueError("planning evidence unresolved items must be unique")
        return values


class PlanningIntervention(StrictModel):
    """Digest-bound preflight stop that grants no implementation authority."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    intervention_id: Identifier
    request_id: Identifier
    request_digest: Sha256Digest
    platform: Platform
    base_revision: Revision
    reason: str = Field(min_length=1, max_length=3000)
    requested_action: ApprovalAction
    affected_paths: tuple[str, ...] = Field(min_length=1, max_length=64)
    evidence: tuple[PlanningInterventionEvidence, ...] = Field(min_length=1, max_length=32)
    options: tuple[PlanningInterventionOption, ...] = Field(min_length=2, max_length=3)
    recommendation: str = Field(min_length=1, max_length=2000)

    @field_validator("requested_action")
    @classmethod
    def prohibit_implementation_authority(cls, value: ApprovalAction) -> ApprovalAction:
        if value not in {
            ApprovalAction.EXPAND_SCOPE,
            ApprovalAction.ACCEPT_HIGH_IMPACT_CHANGE,
        }:
            raise ValueError("planning interventions can request only scope or evidence decisions")
        return value

    @field_validator("affected_paths")
    @classmethod
    def validate_affected_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(validate_relative_path(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("planning intervention affected paths must be unique")
        return normalized

    @field_validator("options")
    @classmethod
    def validate_options(
        cls,
        values: tuple[PlanningInterventionOption, ...],
    ) -> tuple[PlanningInterventionOption, ...]:
        if len(values) != len(set(values)):
            raise ValueError("planning intervention options must be unique")
        return values

    @model_validator(mode="after")
    def validate_evidence_scope(self) -> PlanningIntervention:
        affected = set(self.affected_paths)
        outside = sorted(
            {path for item in self.evidence for path in item.affected_paths if path not in affected}
        )
        if outside:
            raise ValueError(
                "planning evidence paths must be included in intervention affected paths: "
                + ", ".join(outside)
            )
        evidence_keys = tuple(
            (item.category, item.source, item.source_digest) for item in self.evidence
        )
        if len(evidence_keys) != len(set(evidence_keys)):
            raise ValueError("planning intervention evidence bindings must be unique")
        return self


class ImplementationInterventionEvidence(StrictModel):
    """One digest-bound public fact supporting an Engineer implementation stop."""

    source: str = Field(min_length=1, max_length=160)
    source_digest: Sha256Digest
    summary: str = Field(min_length=1, max_length=2000)
    affected_paths: tuple[str, ...] = Field(min_length=1, max_length=64)

    @field_validator("affected_paths")
    @classmethod
    def validate_affected_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(validate_relative_path(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("implementation evidence affected paths must be unique")
        return normalized


class ImplementationIntervention(StrictModel):
    """Non-authorizing Engineer stop bound to its exact approved implementation input."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    status: Literal["decision_required"] = "decision_required"
    intervention_id: Identifier
    request_id: Identifier
    request_digest: Sha256Digest
    manifest_id: Identifier
    manifest_digest: Sha256Digest
    base_revision: Revision
    agent_version: str = Field(pattern=r"^engineer/v[1-9][0-9]*$", max_length=80)
    agent_definition_digest: Sha256Digest
    input_evidence_digest: Sha256Digest
    reason: str = Field(min_length=1, max_length=3000)
    requested_action: ImplementationInterventionAction
    affected_paths: tuple[str, ...] = Field(min_length=1, max_length=64)
    public_concerns: tuple[str, ...] = Field(min_length=1, max_length=32)
    evidence: tuple[ImplementationInterventionEvidence, ...] = Field(
        min_length=1,
        max_length=32,
    )
    options: tuple[PlanningInterventionOption, ...] = Field(min_length=2, max_length=3)
    recommendation: str = Field(min_length=1, max_length=2000)

    @field_validator("affected_paths")
    @classmethod
    def validate_affected_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(validate_relative_path(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("implementation intervention affected paths must be unique")
        return normalized

    @field_validator("public_concerns")
    @classmethod
    def validate_public_concerns(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("implementation intervention concerns cannot be blank")
        if len(values) != len(set(values)):
            raise ValueError("implementation intervention concerns must be unique")
        return values

    @field_validator("options")
    @classmethod
    def validate_options(
        cls,
        values: tuple[PlanningInterventionOption, ...],
    ) -> tuple[PlanningInterventionOption, ...]:
        if len(values) != len(set(values)):
            raise ValueError("implementation intervention options must be unique")
        return values

    @model_validator(mode="after")
    def validate_evidence_scope(self) -> ImplementationIntervention:
        affected = set(self.affected_paths)
        outside = sorted(
            {path for item in self.evidence for path in item.affected_paths if path not in affected}
        )
        if outside:
            raise ValueError(
                "implementation evidence paths must be included in intervention affected paths: "
                + ", ".join(outside)
            )
        evidence_keys = tuple((item.source, item.source_digest) for item in self.evidence)
        if len(evidence_keys) != len(set(evidence_keys)):
            raise ValueError("implementation intervention evidence bindings must be unique")
        return self


class TransformationStep(StrictModel):
    step_id: Identifier
    kind: TransformationStepKind = TransformationStepKind.ARTIFACT_TRANSFORMATION
    description: str = Field(min_length=1, max_length=2000)
    input_paths: tuple[str, ...]
    output_paths: tuple[str, ...]
    decision_id: Identifier | None = None
    evidence_ids: tuple[Identifier, ...] = ()

    @field_validator("input_paths", "output_paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_relative_path(value) for value in values)

    @model_validator(mode="after")
    def validate_step_kind(self) -> TransformationStep:
        if self.kind is TransformationStepKind.SEMANTIC_DECISION:
            if self.decision_id is None or not self.evidence_ids:
                raise ValueError("semantic decision steps require a decision ID and evidence IDs")
            if self.input_paths or self.output_paths:
                raise ValueError("semantic decision steps cannot own source or output paths")
            if len(self.evidence_ids) != len(set(self.evidence_ids)):
                raise ValueError("semantic decision evidence IDs must be unique")
        elif self.decision_id is not None or self.evidence_ids:
            raise ValueError("artifact transformation steps cannot carry decision evidence")
        return self


class ValidationCommand(StrictModel):
    check_id: Identifier
    command_id: Identifier
    purpose: str = Field(min_length=1, max_length=1000)
    environment: EnvironmentKind = EnvironmentKind.LOCAL
    required: bool = True

    @model_validator(mode="after")
    def prohibit_automatic_production_checks(self) -> ValidationCommand:
        if self.environment is EnvironmentKind.PRODUCTION:
            raise ValueError(
                "production validation must be represented by a human decision, not an automatic check"
            )
        return self


class RiskFinding(StrictModel):
    category: RiskCategory
    summary: str = Field(min_length=1, max_length=2000)
    evidence: tuple[str, ...] = ()
    requires_human_decision: bool = True


class MigrationManifest(StrictModel):
    schema_version: SchemaVersion = SCHEMA_VERSION
    manifest_id: Identifier
    request_id: Identifier
    platform: Platform
    base_revision: Revision
    approved_paths: tuple[str, ...] = Field(min_length=1)
    dependencies: tuple[DependencyEvidence, ...]
    transformations: tuple[TransformationStep, ...]
    validation_plan: tuple[ValidationCommand, ...]
    implementation_contract: tuple[str, ...] = Field(default=(), max_length=64)
    risks: tuple[RiskFinding, ...] = ()
    required_approvals: tuple[ApprovalAction, ...] = ()
    status: ManifestStatus = ManifestStatus.PLANNED

    @field_validator("approved_paths")
    @classmethod
    def validate_approved_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(validate_relative_path(value) for value in values))
        if not normalized:
            raise ValueError("approved_paths must contain at least one path")
        return normalized

    @field_validator("implementation_contract")
    @classmethod
    def validate_implementation_contract(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("implementation_contract entries must contain 1 to 2000 characters")
        if len(values) != len(set(values)):
            raise ValueError("implementation_contract entries must be unique")
        return values

    @model_validator(mode="after")
    def validate_decision_state(self) -> MigrationManifest:
        check_ids = tuple(check.check_id for check in self.validation_plan)
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("validation_plan check_ids must be unique")
        unresolved = any(not dependency.resolved for dependency in self.dependencies)
        mandatory_risk = any(risk.requires_human_decision for risk in self.risks)
        if (unresolved or mandatory_risk) and self.status is not ManifestStatus.DECISION_REQUIRED:
            raise ValueError(
                "unresolved dependencies or mandatory risks require decision_required status"
            )
        if self.status is ManifestStatus.DECISION_REQUIRED and not (unresolved or mandatory_risk):
            raise ValueError(
                "decision_required status needs unresolved evidence or a mandatory risk"
            )
        if mandatory_risk and not self.required_approvals:
            raise ValueError("mandatory risks require at least one explicit approval action")
        return self


class ArtifactDigest(StrictModel):
    """Immutable evidence for one regular artifact produced by a tool."""

    path: str
    sha256: Sha256Digest
    size_bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class ToolReceipt(StrictModel):
    receipt_id: Identifier
    tool_id: Identifier
    request_id: Identifier
    run_id: Identifier
    attempt: int = Field(ge=1)
    base_revision: Revision
    environment: EnvironmentKind
    input_artifact_digest: Sha256Digest
    operation: str = Field(min_length=1, max_length=2000)
    working_directory: str = Field(min_length=1, max_length=2048)
    started_at: datetime
    ended_at: datetime
    exit_code: int | None = None
    terminal: bool
    stdout_digest: Sha256Digest | None = None
    stderr_digest: Sha256Digest | None = None
    artifacts: tuple[ArtifactDigest, ...] = ()
    redacted: bool = False

    @model_validator(mode="after")
    def validate_timing_and_terminal_state(self) -> ToolReceipt:
        if self.ended_at < self.started_at:
            raise ValueError("ended_at cannot precede started_at")
        if self.terminal and self.exit_code is None:
            raise ValueError("terminal receipts require an exit_code")
        if not self.terminal and self.exit_code is not None:
            raise ValueError("nonterminal receipts cannot claim an exit_code")
        artifact_paths = tuple(artifact.path for artifact in self.artifacts)
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError("receipt artifact paths must be unique")
        return self


class ChangeSet(StrictModel):
    schema_version: SchemaVersion = SCHEMA_VERSION
    change_set_id: Identifier
    request_id: Identifier
    manifest_id: Identifier
    base_revision: Revision
    changed_paths: tuple[str, ...]
    unified_diff: str = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    receipts: tuple[ToolReceipt, ...] = ()

    @field_validator("changed_paths")
    @classmethod
    def validate_changed_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(validate_relative_path(value) for value in values))
        if not normalized:
            raise ValueError("changed_paths must contain at least one path")
        return normalized


class CheckResult(StrictModel):
    check_id: Identifier
    command_id: Identifier
    required: bool
    status: CheckStatus
    receipt: ToolReceipt | None = None
    summary: str = Field(min_length=1, max_length=2000)
    diagnostic_ids: tuple[Identifier, ...] = Field(default=(), max_length=64)

    @field_validator("diagnostic_ids")
    @classmethod
    def validate_diagnostic_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("diagnostic_ids must be unique")
        return values

    @model_validator(mode="after")
    def validate_receipt(self) -> CheckResult:
        if self.status in {CheckStatus.PASSED, CheckStatus.FAILED}:
            if self.receipt is None or not self.receipt.terminal:
                raise ValueError("passed and failed results require a terminal receipt")
        if self.status is CheckStatus.PASSED and self.receipt and self.receipt.exit_code != 0:
            raise ValueError("a passed result requires exit_code 0")
        if self.status is CheckStatus.FAILED and self.receipt and self.receipt.exit_code == 0:
            raise ValueError("a failed result cannot use exit_code 0")
        return self


class ValidationReport(StrictModel):
    schema_version: SchemaVersion = SCHEMA_VERSION
    report_id: Identifier
    request_id: Identifier
    manifest_id: Identifier
    change_set_id: Identifier
    base_revision: Revision
    results: tuple[CheckResult, ...]
    disposition: ValidationDisposition
    attempt: int = Field(ge=1)
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_disposition(self) -> ValidationReport:
        required = tuple(result for result in self.results if result.required)
        if not required:
            raise ValueError("at least one required validation result is required")
        all_required_passed = all(result.status is CheckStatus.PASSED for result in required)
        if (
            self.disposition is ValidationDisposition.READY_FOR_HUMAN_REVIEW
            and not all_required_passed
        ):
            raise ValueError(
                "ready_for_human_review requires every required check to pass terminally"
            )
        if (
            self.disposition is not ValidationDisposition.READY_FOR_HUMAN_REVIEW
            and all_required_passed
        ):
            raise ValueError("all required checks passed; use ready_for_human_review")
        return self


class DecisionRequest(StrictModel):
    schema_version: SchemaVersion = SCHEMA_VERSION
    decision_id: Identifier
    request_id: Identifier
    reason: str = Field(min_length=1, max_length=3000)
    requested_action: ApprovalAction
    affected_paths: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    options: tuple[str, ...] = Field(min_length=2)
    recommendation: str = Field(min_length=1, max_length=2000)

    @field_validator("affected_paths")
    @classmethod
    def validate_affected_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_relative_path(value) for value in values)


def validate_relative_path(value: str) -> str:
    """Return a canonical POSIX-style relative path or reject it.

    This is intentionally lexical. Filesystem resolution and symlink checks are
    performed by the isolated-workspace layer before any write.
    """

    candidate = value.strip().replace("\\", "/")
    if not candidate or candidate.startswith("/"):
        raise ValueError("paths must be non-empty and repository-relative")
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        raise ValueError("paths cannot contain control characters")
    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(
            "paths cannot contain empty, current-directory, or parent-directory segments"
        )
    if ":" in parts[0]:
        raise ValueError("drive-qualified paths are not allowed")
    return "/".join(parts)
