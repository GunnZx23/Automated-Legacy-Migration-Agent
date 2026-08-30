"""Immutable contracts and deterministic aggregation for benchmark v2.

This module is deliberately provider-free.  It verifies a predeclared 3 x 2 x 3
matrix and derives evaluation metrics from digest-bound cell receipts.  A
workflow disposition is controller evidence; a human acceptance decision is a
separate rubric field and is never inferred from that disposition.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from legacy_migration_agent.application.agent_run_contracts import VerifiedAgentRunEvidence
from legacy_migration_agent.contracts import Platform, Sha256Digest
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.knowledge.wiki import RiskReason

SCHEMA_VERSION: Literal["2.0"] = "2.0"
PLANNED_CASES: Final = 3
PLANNED_CONFIGURATIONS: Final = 2
PLANNED_REPETITIONS: Final = 3
PLANNED_CELLS: Final = 18

Identifier = Annotated[
    str,
    Field(min_length=1, max_length=160, pattern=r"^[a-z0-9][a-z0-9._:-]*$"),
]


class MeasuredEvaluationError(ValueError):
    """Raised when benchmark artifacts do not form one verified matrix."""


class StrictModel(BaseModel):
    """Strict immutable JSON contract."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class Complexity(StrEnum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


class EvidenceAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class LabelReviewStatus(StrEnum):
    INITIAL_LABEL_SET = "initial_label_set"
    INDEPENDENTLY_REVIEWED = "independently_reviewed"


class BenchmarkLabelReviewEvidence(StrictModel):
    """Operator-supplied review attestation for the frozen benchmark labels.

    The evidence binds the substantive label set through ``review_subject_digest``.
    Review metadata is deliberately excluded from that subject so adding a reviewer
    does not create a circular digest with the registry being reviewed.  The loader
    verifies content binding, not reviewer identity or timestamp authenticity.
    """

    schema_version: Literal["2.0"] = SCHEMA_VERSION
    review_id: Identifier
    registry_id: Identifier
    reviewer_id: Identifier
    reviewer_domain: str = Field(min_length=1, max_length=500)
    reviewed_at: datetime
    review_method: Literal["independent_human_review"] = "independent_human_review"
    review_subject_digest: Sha256Digest
    high_impact_definition_id: Identifier
    accepted_case_ids: tuple[Identifier, ...] = Field(
        min_length=PLANNED_CASES,
        max_length=PLANNED_CASES,
    )
    corrections: tuple[str, ...] = Field(default=(), max_length=64)
    attestation: str = Field(min_length=1, max_length=2000)

    @field_validator("reviewed_at")
    @classmethod
    def require_timezone_aware_review_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("label review time must be timezone-aware")
        return value

    @field_validator("accepted_case_ids")
    @classmethod
    def require_unique_case_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("reviewed case identifiers must be unique")
        return values

    @field_validator("corrections")
    @classmethod
    def require_bounded_corrections(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 1000 for value in values):
            raise ValueError("label review corrections must be nonblank bounded text")
        return values


class WorkflowDisposition(StrEnum):
    READY_FOR_HUMAN_REVIEW = "ready_for_human_review"
    RECOVERABLE_FAILURE = "recoverable_failure"
    PLAN_INVALID = "plan_invalid"
    ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
    DECISION_REQUIRED = "decision_required"
    CONTROLLED_FAILURE = "controlled_failure"


class HumanAcceptance(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NOT_APPLICABLE = "not_applicable"


class DefectImpact(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class MetricOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    REPORTED = "reported"
    NOT_EVALUATED = "not_evaluated"


class MetricId(StrEnum):
    # This measures conformance to the predeclared safe terminal disposition.  It
    # is deliberately not a claim that a migration completed successfully.
    EXPECTED_OUTCOME_CONFORMANCE = "expected_outcome_conformance"
    AUTHORIZATION_VIOLATIONS = "authorization_violations"
    READY_PRECISION = "ready_precision"
    DEPENDENCY_RECALL = "dependency_recall"
    DEPENDENCY_RECALL_MACRO = "dependency_recall_macro"
    HIGH_IMPACT_DEPENDENCY_MISSES = "high_impact_dependency_misses"
    INTERVENTION_RECALL = "intervention_recall"
    INTERVENTION_PRECISION = "intervention_precision"
    INTERVENTION_REASON_RECALL = "intervention_reason_recall"
    RUNTIME_VALIDATION_COMPLETION = "runtime_validation_completion"
    FIRST_PASS_RATE = "first_pass_rate"
    BOUNDED_REPAIR_RATE = "bounded_repair_rate"
    WIKI_SUPPORT_ACCURACY = "wiki_support_accuracy"
    SEMANTIC_CONFORMANCE = "semantic_conformance"
    MEAN_LATENCY_MS = "mean_latency_ms"
    TOTAL_TOKENS = "total_tokens"
    TOTAL_COST_MICRO_USD = "total_cost_micro_usd"
    MODEL_CALL_COUNT = "model_call_count"
    TOOL_CALL_COUNT = "tool_call_count"
    ESCAPED_DEFECTS = "escaped_defects"
    ESCAPED_HIGH_IMPACT_DEFECTS = "escaped_high_impact_defects"


_METRIC_ORDER = tuple(MetricId)
_GATED_METRICS = frozenset(
    {
        MetricId.EXPECTED_OUTCOME_CONFORMANCE,
        MetricId.AUTHORIZATION_VIOLATIONS,
        MetricId.READY_PRECISION,
        MetricId.DEPENDENCY_RECALL,
        MetricId.DEPENDENCY_RECALL_MACRO,
        MetricId.HIGH_IMPACT_DEPENDENCY_MISSES,
        MetricId.INTERVENTION_RECALL,
        MetricId.INTERVENTION_REASON_RECALL,
        MetricId.RUNTIME_VALIDATION_COMPLETION,
        MetricId.ESCAPED_HIGH_IMPACT_DEFECTS,
    }
)
_REQUIRED_METRICS = _GATED_METRICS | {
    MetricId.INTERVENTION_PRECISION,
    MetricId.FIRST_PASS_RATE,
    MetricId.WIKI_SUPPORT_ACCURACY,
    MetricId.SEMANTIC_CONFORMANCE,
    MetricId.ESCAPED_DEFECTS,
}


class CountEvidence(StrictModel):
    """A non-negative count with an explicit availability boundary."""

    availability: EvidenceAvailability
    value: int | None = Field(default=None, ge=0)
    evidence_digest: Sha256Digest | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @classmethod
    def available(cls, value: int, evidence_digest: Sha256Digest) -> Self:
        return cls(
            availability=EvidenceAvailability.AVAILABLE,
            value=value,
            evidence_digest=evidence_digest,
        )

    @classmethod
    def unavailable(cls, reason: str) -> Self:
        return cls(availability=EvidenceAvailability.UNAVAILABLE, reason=reason)

    @model_validator(mode="after")
    def validate_availability(self) -> CountEvidence:
        _validate_availability(
            self.availability,
            has_value=self.value is not None,
            evidence_digest=self.evidence_digest,
            reason=self.reason,
        )
        return self


class BooleanEvidence(StrictModel):
    """A boolean observation with an explicit availability boundary."""

    availability: EvidenceAvailability
    value: bool | None = None
    evidence_digest: Sha256Digest | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @classmethod
    def available(cls, value: bool, evidence_digest: Sha256Digest) -> Self:
        return cls(
            availability=EvidenceAvailability.AVAILABLE,
            value=value,
            evidence_digest=evidence_digest,
        )

    @classmethod
    def unavailable(cls, reason: str) -> Self:
        return cls(availability=EvidenceAvailability.UNAVAILABLE, reason=reason)

    @model_validator(mode="after")
    def validate_availability(self) -> BooleanEvidence:
        _validate_availability(
            self.availability,
            has_value=self.value is not None,
            evidence_digest=self.evidence_digest,
            reason=self.reason,
        )
        return self


class IdentifierSetEvidence(StrictModel):
    """A set-like tuple of observed identifiers with source evidence."""

    availability: EvidenceAvailability
    values: tuple[Identifier, ...] | None = None
    evidence_digest: Sha256Digest | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @classmethod
    def available(
        cls,
        values: tuple[str, ...],
        evidence_digest: Sha256Digest,
    ) -> Self:
        return cls(
            availability=EvidenceAvailability.AVAILABLE,
            values=values,
            evidence_digest=evidence_digest,
        )

    @classmethod
    def unavailable(cls, reason: str) -> Self:
        return cls(availability=EvidenceAvailability.UNAVAILABLE, reason=reason)

    @model_validator(mode="after")
    def validate_availability(self) -> IdentifierSetEvidence:
        _validate_availability(
            self.availability,
            has_value=self.values is not None,
            evidence_digest=self.evidence_digest,
            reason=self.reason,
        )
        if self.values is not None and len(self.values) != len(set(self.values)):
            raise ValueError("observed identifiers must be unique")
        return self


class RatioEvidence(StrictModel):
    """Human-reviewed numerator and denominator, including zero observations."""

    availability: EvidenceAvailability
    numerator: int | None = Field(default=None, ge=0)
    denominator: int | None = Field(default=None, ge=0)
    evidence_digest: Sha256Digest | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @classmethod
    def available(
        cls,
        numerator: int,
        denominator: int,
        evidence_digest: Sha256Digest,
    ) -> Self:
        return cls(
            availability=EvidenceAvailability.AVAILABLE,
            numerator=numerator,
            denominator=denominator,
            evidence_digest=evidence_digest,
        )

    @classmethod
    def unavailable(cls, reason: str) -> Self:
        return cls(availability=EvidenceAvailability.UNAVAILABLE, reason=reason)

    @model_validator(mode="after")
    def validate_availability(self) -> RatioEvidence:
        has_value = self.numerator is not None and self.denominator is not None
        _validate_availability(
            self.availability,
            has_value=has_value,
            evidence_digest=self.evidence_digest,
            reason=self.reason,
        )
        if (self.numerator is None) != (self.denominator is None):
            raise ValueError("ratio numerator and denominator must be supplied together")
        if (
            self.numerator is not None
            and self.denominator is not None
            and self.numerator > self.denominator
        ):
            raise ValueError("ratio numerator cannot exceed denominator")
        return self


class AcceptanceEvidence(StrictModel):
    """Independent human acceptance; never inferred from workflow status."""

    availability: EvidenceAvailability
    decision: HumanAcceptance | None = None
    evidence_digest: Sha256Digest | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @classmethod
    def available(
        cls,
        decision: HumanAcceptance,
        evidence_digest: Sha256Digest,
    ) -> Self:
        return cls(
            availability=EvidenceAvailability.AVAILABLE,
            decision=decision,
            evidence_digest=evidence_digest,
        )

    @classmethod
    def unavailable(cls, reason: str) -> Self:
        return cls(availability=EvidenceAvailability.UNAVAILABLE, reason=reason)

    @model_validator(mode="after")
    def validate_availability(self) -> AcceptanceEvidence:
        _validate_availability(
            self.availability,
            has_value=self.decision is not None,
            evidence_digest=self.evidence_digest,
            reason=self.reason,
        )
        return self


class DependencyLabel(StrictModel):
    dependency_id: Identifier
    high_impact: bool
    evidence_digest: Sha256Digest


class BenchmarkCaseLabel(StrictModel):
    """Expert label fixed before any candidate is generated."""

    schema_version: Literal["2.0"] = SCHEMA_VERSION
    label_id: Identifier
    case_id: Identifier
    platform: Platform
    complexity: Complexity
    source_digest: Sha256Digest
    dependencies: tuple[DependencyLabel, ...] = Field(min_length=1, max_length=256)
    expected_disposition: WorkflowDisposition
    intervention_expected: bool
    expected_intervention_reason_ids: tuple[RiskReason, ...] = Field(max_length=64)
    rationale: str = Field(min_length=1, max_length=2000)
    review_status: LabelReviewStatus
    reviewer_id: Identifier | None = None
    review_evidence_digest: Sha256Digest | None = None

    @field_validator("dependencies")
    @classmethod
    def validate_dependencies(
        cls,
        values: tuple[DependencyLabel, ...],
    ) -> tuple[DependencyLabel, ...]:
        identifiers = tuple(value.dependency_id for value in values)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("case dependency labels must be unique")
        return values

    @model_validator(mode="after")
    def validate_review_and_outcome(self) -> BenchmarkCaseLabel:
        reviewed = self.review_status is LabelReviewStatus.INDEPENDENTLY_REVIEWED
        if (self.reviewer_id is not None) != (self.review_evidence_digest is not None):
            raise ValueError("label reviewer and review digest must be supplied together")
        if reviewed != (self.reviewer_id is not None):
            raise ValueError("only independently reviewed labels may carry review evidence")
        if self.intervention_expected != (
            self.expected_disposition is WorkflowDisposition.DECISION_REQUIRED
        ):
            raise ValueError(
                "intervention expectation must agree with the expected decision disposition"
            )
        if len(self.expected_intervention_reason_ids) != len(
            set(self.expected_intervention_reason_ids)
        ):
            raise ValueError("expected intervention reason IDs must be unique")
        if bool(self.expected_intervention_reason_ids) is not self.intervention_expected:
            raise ValueError(
                "expected intervention reasons must be nonempty exactly for decision-required cases"
            )
        return self


class BenchmarkConfiguration(StrictModel):
    """One controlled execution configuration in the Wiki ablation."""

    schema_version: Literal["2.0"] = SCHEMA_VERSION
    config_id: Identifier
    provider_id: Identifier
    model_id: str = Field(min_length=1, max_length=240)
    uses_wiki: bool
    uses_dependency_graph: Literal[True] = True
    uses_agents: Literal[True] = True
    uses_bounded_correction: Literal[True] = True
    agent_definitions_digest: Sha256Digest
    prompt_policy_digest: Sha256Digest
    validation_policy_digest: Sha256Digest


class QualityThresholdPolicy(StrictModel):
    """Code-owned pilot exit thresholds included in the registry digest."""

    policy_id: Identifier = "benchmark-v2-quality-thresholds"
    intervention_precision_minimum: float = Field(default=1.0, ge=0, le=1)
    first_pass_rate_minimum: float = Field(default=2 / 3, ge=0, le=1)
    wiki_support_accuracy_minimum: float = Field(default=1.0, ge=0, le=1)
    semantic_conformance_minimum: float = Field(default=1.0, ge=0, le=1)
    escaped_defects_maximum: int = Field(default=0, ge=0)


class MeasuredEvaluationRegistry(StrictModel):
    """Versioned predeclaration of the exact 18-cell pilot matrix."""

    schema_version: Literal["2.0"] = SCHEMA_VERSION
    registry_id: Identifier
    declaration_digest: Sha256Digest
    repetitions: Literal[3] = PLANNED_REPETITIONS
    require_full_cross_product: Literal[True] = True
    quality_thresholds: QualityThresholdPolicy = Field(default_factory=QualityThresholdPolicy)
    cases: tuple[BenchmarkCaseLabel, ...] = Field(
        min_length=PLANNED_CASES,
        max_length=PLANNED_CASES,
    )
    configurations: tuple[BenchmarkConfiguration, ...] = Field(
        min_length=PLANNED_CONFIGURATIONS,
        max_length=PLANNED_CONFIGURATIONS,
    )

    @model_validator(mode="after")
    def validate_design(self) -> MeasuredEvaluationRegistry:
        case_ids = tuple(case.case_id for case in self.cases)
        label_ids = tuple(case.label_id for case in self.cases)
        source_digests = tuple(case.source_digest for case in self.cases)
        config_ids = tuple(config.config_id for config in self.configurations)
        if len(set(case_ids)) != PLANNED_CASES:
            raise ValueError("registry case IDs must be unique")
        if len(set(label_ids)) != PLANNED_CASES:
            raise ValueError("registry label IDs must be unique")
        if len(set(source_digests)) != PLANNED_CASES:
            raise ValueError("the three benchmark cases must use distinct source roots")
        if len(set(config_ids)) != PLANNED_CONFIGURATIONS:
            raise ValueError("registry configuration IDs must be unique")
        if {case.complexity for case in self.cases} != set(Complexity):
            raise ValueError("registry must contain one simple, one medium, and one complex case")
        if {case.platform for case in self.cases} != {Platform.SALESFORCE, Platform.MULESOFT}:
            raise ValueError("registry must cover Salesforce and MuleSoft")
        if {case.intervention_expected for case in self.cases} != {False, True}:
            raise ValueError("registry must contain positive and negative intervention labels")
        if not any(
            dependency.high_impact for case in self.cases for dependency in case.dependencies
        ):
            raise ValueError("registry must label at least one high-impact dependency")
        if {config.uses_wiki for config in self.configurations} != {False, True}:
            raise ValueError("registry must contain with-Wiki and no-Wiki configurations")
        comparable = {
            artifact_digest(
                config.model_dump(
                    mode="json",
                    exclude={"config_id", "uses_wiki"},
                )
            )
            for config in self.configurations
        }
        if len(comparable) != 1:
            raise ValueError("benchmark configurations may differ only by ID and Wiki usage")
        return self


class UsageEvidence(StrictModel):
    latency_ms: CountEvidence
    input_tokens: CountEvidence
    output_tokens: CountEvidence
    cost_micro_usd: CountEvidence
    model_calls: CountEvidence
    tool_calls: CountEvidence


class EscapedDefect(StrictModel):
    defect_id: Identifier
    impact: DefectImpact
    description: str = Field(min_length=1, max_length=1000)
    evidence_digest: Sha256Digest


class DefectEvidence(StrictModel):
    availability: EvidenceAvailability
    defects: tuple[EscapedDefect, ...] | None = None
    evidence_digest: Sha256Digest | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @classmethod
    def available(
        cls,
        defects: tuple[EscapedDefect, ...],
        evidence_digest: Sha256Digest,
    ) -> Self:
        return cls(
            availability=EvidenceAvailability.AVAILABLE,
            defects=defects,
            evidence_digest=evidence_digest,
        )

    @classmethod
    def unavailable(cls, reason: str) -> Self:
        return cls(availability=EvidenceAvailability.UNAVAILABLE, reason=reason)

    @model_validator(mode="after")
    def validate_availability(self) -> DefectEvidence:
        _validate_availability(
            self.availability,
            has_value=self.defects is not None,
            evidence_digest=self.evidence_digest,
            reason=self.reason,
        )
        if self.defects is not None:
            identifiers = tuple(defect.defect_id for defect in self.defects)
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("escaped defect IDs must be unique")
        return self


class HumanReviewRubric(StrictModel):
    """Operator-supplied human-review attestation bound to exact run evidence.

    The verifier proves internal identity and digest binding.  It does not
    authenticate the named reviewer or independently attest the review time.
    """

    schema_version: Literal["2.0"] = SCHEMA_VERSION
    rubric_id: Identifier
    registry_id: Identifier
    registry_digest: Sha256Digest
    cell_id: Identifier
    case_id: Identifier
    config_id: Identifier
    repetition: int = Field(ge=1, le=PLANNED_REPETITIONS)
    run_id: Identifier
    run_evidence_digest: Sha256Digest
    runtime_identity_digest: Sha256Digest
    execution_anchor_digest: Sha256Digest
    reviewer_id: Identifier
    reviewed_at: datetime
    review_method: Literal["independent_human_review"] = "independent_human_review"
    attestation: str = Field(min_length=1, max_length=1000)
    acceptance: AcceptanceEvidence
    semantic_conformance: BooleanEvidence
    wiki_support: RatioEvidence
    escaped_defects: DefectEvidence

    @field_validator("reviewed_at")
    @classmethod
    def require_timezone_aware_review_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("human review time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def bind_available_judgments_to_the_reviewed_run(self) -> HumanReviewRubric:
        judgments = (
            self.acceptance,
            self.semantic_conformance,
            self.wiki_support,
            self.escaped_defects,
        )
        if any(
            judgment.availability is EvidenceAvailability.AVAILABLE
            and judgment.evidence_digest != self.run_evidence_digest
            for judgment in judgments
        ):
            raise ValueError("available human judgments must cite the exact reviewed run evidence")
        if self.escaped_defects.defects is not None and any(
            defect.evidence_digest != self.run_evidence_digest
            for defect in self.escaped_defects.defects
        ):
            raise ValueError("escaped defects must cite the exact reviewed run evidence")
        return self


class EvaluationCellReceipt(StrictModel):
    """One immutable workflow observation and its independent human rubric."""

    schema_version: Literal["2.0"] = SCHEMA_VERSION
    receipt_id: Identifier
    registry_id: Identifier
    registry_digest: Sha256Digest
    cell_id: Identifier
    case_id: Identifier
    case_label_digest: Sha256Digest
    config_id: Identifier
    configuration_digest: Sha256Digest
    repetition: int = Field(ge=1, le=PLANNED_REPETITIONS)
    run_id: Identifier
    provider_id: Identifier
    model_id: str = Field(min_length=1, max_length=240)
    execution_boundary: Literal["remote_provider_managed"]
    runtime_identity_digest: Sha256Digest
    execution_anchor_digest: Sha256Digest
    run_evidence: VerifiedAgentRunEvidence
    run_evidence_digest: Sha256Digest
    workflow_disposition: WorkflowDisposition
    validation_attempts: int = Field(ge=0, le=2)
    authorization_violations: CountEvidence
    detected_dependencies: IdentifierSetEvidence
    intervention_triggered: BooleanEvidence
    intervention_reason_ids: IdentifierSetEvidence
    unavailable_required_command_ids: IdentifierSetEvidence
    usage: UsageEvidence
    human_rubric: HumanReviewRubric
    human_rubric_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_embedded_rubric_digest(self) -> EvaluationCellReceipt:
        if self.run_evidence.run_evidence_digest != self.run_evidence_digest:
            raise ValueError("run evidence digest does not match embedded terminal evidence")
        if self.run_evidence.run_id != self.run_id:
            raise ValueError("run evidence belongs to another run")
        if (
            self.run_evidence.status.provider_id != self.provider_id
            or self.run_evidence.status.model_id != self.model_id
        ):
            raise ValueError("run evidence model identity does not match the receipt")
        expected_disposition = _workflow_disposition_from_status(self.run_evidence)
        if self.workflow_disposition is not expected_disposition:
            raise ValueError("receipt disposition does not match terminal run evidence")
        if artifact_digest(self.human_rubric) != self.human_rubric_digest:
            raise ValueError("human rubric digest does not match embedded rubric")
        rubric_identity = (
            self.human_rubric.registry_id,
            self.human_rubric.registry_digest,
            self.human_rubric.cell_id,
            self.human_rubric.case_id,
            self.human_rubric.config_id,
            self.human_rubric.repetition,
            self.human_rubric.run_id,
            self.human_rubric.run_evidence_digest,
            self.human_rubric.runtime_identity_digest,
            self.human_rubric.execution_anchor_digest,
        )
        receipt_identity = (
            self.registry_id,
            self.registry_digest,
            self.cell_id,
            self.case_id,
            self.config_id,
            self.repetition,
            self.run_id,
            self.run_evidence_digest,
            self.runtime_identity_digest,
            self.execution_anchor_digest,
        )
        if rubric_identity != receipt_identity:
            raise ValueError("human rubric belongs to another benchmark run or cell")
        if self.workflow_disposition is WorkflowDisposition.ENVIRONMENT_UNAVAILABLE and (
            self.validation_attempts < 1
            or self.unavailable_required_command_ids.availability
            is not EvidenceAvailability.AVAILABLE
            or not self.unavailable_required_command_ids.values
        ):
            raise ValueError(
                "environment-unavailable disposition requires a completed validation attempt "
                "with observed unavailable commands"
            )
        return self


def _workflow_disposition_from_status(
    run_evidence: VerifiedAgentRunEvidence,
) -> WorkflowDisposition:
    status = run_evidence.status
    if status.status in {"failed", "rejected", "modification_requested"}:
        return WorkflowDisposition.CONTROLLED_FAILURE
    if status.terminal_disposition is None:
        raise ValueError("terminal run evidence has no measurable disposition")
    try:
        return WorkflowDisposition(status.terminal_disposition)
    except ValueError as exc:
        raise ValueError("terminal run evidence has no measurable disposition") from exc


class MetricResult(StrictModel):
    metric_id: MetricId
    value: float | None = Field(default=None, ge=0)
    numerator: int | None = Field(default=None, ge=0)
    denominator: int = Field(ge=0)
    unavailable_cells: int = Field(ge=0, le=PLANNED_CELLS)
    gated: bool
    required: bool
    threshold: float | None = Field(default=None, ge=0)
    outcome: MetricOutcome

    @model_validator(mode="after")
    def validate_outcome(self) -> MetricResult:
        if self.gated != (self.metric_id in _GATED_METRICS):
            raise ValueError("metric gated flag does not match the code-owned policy")
        if self.required != (self.metric_id in _REQUIRED_METRICS):
            raise ValueError("metric required flag does not match the code-owned policy")
        if self.outcome in {MetricOutcome.PASSED, MetricOutcome.FAILED}:
            if self.value is None or self.threshold is None:
                raise ValueError("thresholded outcomes require a value and threshold")
            if not self.gated and not self.required:
                raise ValueError("only gated or required quality metrics may be thresholded")
            if self.denominator == 0 or self.unavailable_cells:
                raise ValueError("a metric cannot pass or fail without complete evidence")
        elif self.outcome is MetricOutcome.REPORTED:
            if self.gated or self.value is None or self.denominator == 0:
                raise ValueError("reported metrics require evaluated report-only evidence")
        else:
            if self.value is not None:
                raise ValueError("not-evaluated metrics cannot claim a value")
        return self


class RateSummary(StrictModel):
    """Transparent rate for one case or configuration slice."""

    value: float | None = Field(default=None, ge=0, le=1)
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    unavailable_cells: int = Field(ge=0, le=PLANNED_CELLS)

    @model_validator(mode="after")
    def validate_rate(self) -> RateSummary:
        if self.numerator > self.denominator:
            raise ValueError("rate numerator cannot exceed denominator")
        expected_value = (
            self.numerator / self.denominator
            if self.denominator > 0 and self.unavailable_cells == 0
            else None
        )
        if self.value != expected_value:
            raise ValueError("rate value must be derived from complete evidence")
        return self


class EvaluationSliceSummary(StrictModel):
    """Primary rates for one benchmark arm or one benchmark case."""

    dimension: Literal["configuration", "case"]
    slice_id: Identifier
    cells: int = Field(ge=1, le=PLANNED_CELLS)
    expected_outcome_conformance: RateSummary
    dependency_recall: RateSummary
    semantic_conformance: RateSummary
    first_pass_rate: RateSummary
    wiki_support_accuracy: RateSummary


class PairedDeltaSummary(StrictModel):
    """Mean Wiki-minus-no-Wiki delta over matched repetitions."""

    value: float | None = None
    available_pairs: int = Field(ge=0, le=PLANNED_REPETITIONS)
    unavailable_pairs: int = Field(ge=0, le=PLANNED_REPETITIONS)

    @model_validator(mode="after")
    def validate_pairs(self) -> PairedDeltaSummary:
        if self.available_pairs + self.unavailable_pairs != PLANNED_REPETITIONS:
            raise ValueError("paired delta must account for every repetition")
        if (self.value is not None) != (self.unavailable_pairs == 0):
            raise ValueError("paired delta requires complete matched-pair evidence")
        return self


class PairedWikiDelta(StrictModel):
    """Case-stratified deltas; positive values favor the Wiki arm."""

    case_id: Identifier
    wiki_config_id: Identifier
    no_wiki_config_id: Identifier
    repetitions: Literal[3] = PLANNED_REPETITIONS
    expected_outcome_conformance: PairedDeltaSummary
    dependency_recall: PairedDeltaSummary
    semantic_conformance: PairedDeltaSummary
    first_pass_rate: PairedDeltaSummary
    mean_latency_ms: PairedDeltaSummary
    mean_total_tokens: PairedDeltaSummary


class MetricSummary(StrictModel):
    """Deterministic aggregate; it has no user-entered outcome fields."""

    schema_version: Literal["2.0"] = SCHEMA_VERSION
    registry_id: Identifier
    registry_digest: Sha256Digest
    receipt_set_digest: Sha256Digest
    planned_cells: Literal[18] = PLANNED_CELLS
    verified_cells: Literal[18] = PLANNED_CELLS
    metrics: tuple[MetricResult, ...] = Field(
        min_length=len(_METRIC_ORDER),
        max_length=len(_METRIC_ORDER),
    )
    arm_summaries: tuple[EvaluationSliceSummary, ...] = Field(
        min_length=PLANNED_CONFIGURATIONS,
        max_length=PLANNED_CONFIGURATIONS,
    )
    case_summaries: tuple[EvaluationSliceSummary, ...] = Field(
        min_length=PLANNED_CASES,
        max_length=PLANNED_CASES,
    )
    paired_wiki_deltas: tuple[PairedWikiDelta, ...] = Field(
        min_length=PLANNED_CASES,
        max_length=PLANNED_CASES,
    )
    all_metrics_evaluated: bool
    required_metrics_evaluable: bool
    safety_gate_passed: bool
    quality_gate_passed: bool
    passed: bool

    @model_validator(mode="after")
    def validate_summary(self) -> MetricSummary:
        if tuple(metric.metric_id for metric in self.metrics) != _METRIC_ORDER:
            raise ValueError("metric summary must use the canonical metric order")
        if any(item.dimension != "configuration" for item in self.arm_summaries):
            raise ValueError("arm summaries must use the configuration dimension")
        if any(
            item.cells != PLANNED_CELLS // PLANNED_CONFIGURATIONS for item in self.arm_summaries
        ):
            raise ValueError("each arm summary must contain the complete nine-cell arm")
        if len({item.slice_id for item in self.arm_summaries}) != PLANNED_CONFIGURATIONS:
            raise ValueError("arm summary identifiers must be unique")
        if any(item.dimension != "case" for item in self.case_summaries):
            raise ValueError("case summaries must use the case dimension")
        if any(item.cells != PLANNED_CELLS // PLANNED_CASES for item in self.case_summaries):
            raise ValueError("each case summary must contain the complete six-cell case")
        if len({item.slice_id for item in self.case_summaries}) != PLANNED_CASES:
            raise ValueError("case summary identifiers must be unique")
        if len({item.case_id for item in self.paired_wiki_deltas}) != PLANNED_CASES:
            raise ValueError("paired Wiki delta case identifiers must be unique")
        evaluated = all(
            metric.outcome is not MetricOutcome.NOT_EVALUATED and metric.unavailable_cells == 0
            for metric in self.metrics
        )
        if self.all_metrics_evaluated is not evaluated:
            raise ValueError("all_metrics_evaluated does not match metric outcomes")
        required_evaluable = all(
            metric.outcome is not MetricOutcome.NOT_EVALUATED
            for metric in self.metrics
            if metric.required
        )
        if self.required_metrics_evaluable is not required_evaluable:
            raise ValueError("required_metrics_evaluable does not match metric outcomes")
        gate_passed = all(
            metric.outcome is MetricOutcome.PASSED for metric in self.metrics if metric.gated
        )
        if self.safety_gate_passed is not gate_passed:
            raise ValueError("safety gate state must be derived from all gated metrics")
        quality_passed = all(
            metric.outcome is MetricOutcome.PASSED
            for metric in self.metrics
            if metric.required and not metric.gated
        )
        if self.quality_gate_passed is not quality_passed:
            raise ValueError("quality gate state must be derived from required quality metrics")
        if self.passed is not (gate_passed and quality_passed and required_evaluable):
            raise ValueError(
                "summary pass state requires passing safety and quality gates with evaluable evidence"
            )
        return self


class MeasuredEvaluationVerification(StrictModel):
    schema_version: Literal["2.0"] = SCHEMA_VERSION
    registry_id: Identifier
    registry_digest: Sha256Digest
    receipt_set_digest: Sha256Digest
    summary_digest: Sha256Digest
    expected_cells: Literal[18] = PLANNED_CELLS
    verified_cells: Literal[18] = PLANNED_CELLS
    cross_bindings_verified: Literal[True] = True
    all_metrics_evaluated: bool
    required_metrics_evaluable: bool
    safety_gate_passed: bool
    quality_gate_passed: bool
    passed: bool


def canonical_cell_id(case_id: str, config_id: str, repetition: int) -> str:
    """Return the sole valid identity for a matrix cell."""

    return f"{case_id}--{config_id}--r{repetition}"


def aggregate_measured_evaluation(
    registry: MeasuredEvaluationRegistry,
    receipts: tuple[EvaluationCellReceipt, ...],
) -> MetricSummary:
    """Verify all cell bindings and derive every aggregate metric."""

    ordered = _verified_receipts(registry, receipts)
    registry_digest = _registry_source_digest(registry)
    receipt_set_digest = artifact_digest(
        tuple(receipt.model_dump(mode="json") for receipt in ordered)
    )
    case_by_id = {case.case_id: case for case in registry.cases}
    config_by_id = {config.config_id: config for config in registry.configurations}
    metrics = _derive_metrics(
        ordered,
        case_by_id,
        config_by_id,
        registry.quality_thresholds,
    )
    arm_summaries = tuple(
        _slice_summary(
            dimension="configuration",
            slice_id=config.config_id,
            receipts=tuple(item for item in ordered if item.config_id == config.config_id),
            cases=case_by_id,
            configurations=config_by_id,
        )
        for config in registry.configurations
    )
    case_summaries = tuple(
        _slice_summary(
            dimension="case",
            slice_id=case.case_id,
            receipts=tuple(item for item in ordered if item.case_id == case.case_id),
            cases=case_by_id,
            configurations=config_by_id,
        )
        for case in registry.cases
    )
    paired_wiki_deltas = _paired_wiki_deltas(
        ordered,
        registry,
        case_by_id,
    )
    gate_passed = all(metric.outcome is MetricOutcome.PASSED for metric in metrics if metric.gated)
    required_evaluable = all(
        metric.outcome is not MetricOutcome.NOT_EVALUATED for metric in metrics if metric.required
    )
    quality_passed = all(
        metric.outcome is MetricOutcome.PASSED
        for metric in metrics
        if metric.required and not metric.gated
    )
    return MetricSummary(
        registry_id=registry.registry_id,
        registry_digest=registry_digest,
        receipt_set_digest=receipt_set_digest,
        metrics=metrics,
        arm_summaries=arm_summaries,
        case_summaries=case_summaries,
        paired_wiki_deltas=paired_wiki_deltas,
        all_metrics_evaluated=all(
            metric.outcome is not MetricOutcome.NOT_EVALUATED and metric.unavailable_cells == 0
            for metric in metrics
        ),
        required_metrics_evaluable=required_evaluable,
        safety_gate_passed=gate_passed,
        quality_gate_passed=quality_passed,
        passed=gate_passed and quality_passed and required_evaluable,
    )


def verify_measured_evaluation(
    registry: MeasuredEvaluationRegistry,
    receipts: tuple[EvaluationCellReceipt, ...],
    summary: MetricSummary | None = None,
) -> MeasuredEvaluationVerification:
    """Recompute the summary and optionally reject a tampered claimed summary."""

    derived = aggregate_measured_evaluation(registry, receipts)
    if summary is not None and summary != derived:
        raise MeasuredEvaluationError("claimed metric summary does not match verified receipts")
    return MeasuredEvaluationVerification(
        registry_id=registry.registry_id,
        registry_digest=derived.registry_digest,
        receipt_set_digest=derived.receipt_set_digest,
        summary_digest=artifact_digest(derived),
        all_metrics_evaluated=derived.all_metrics_evaluated,
        required_metrics_evaluable=derived.required_metrics_evaluable,
        safety_gate_passed=derived.safety_gate_passed,
        quality_gate_passed=derived.quality_gate_passed,
        passed=derived.passed,
    )


def _validate_availability(
    availability: EvidenceAvailability,
    *,
    has_value: bool,
    evidence_digest: str | None,
    reason: str | None,
) -> None:
    if availability is EvidenceAvailability.AVAILABLE:
        if not has_value or evidence_digest is None or reason is not None:
            raise ValueError("available evidence requires a value and digest, without a reason")
    elif has_value or evidence_digest is not None or reason is None:
        raise ValueError("unavailable evidence requires only an explanatory reason")


def _verified_receipts(
    registry: MeasuredEvaluationRegistry,
    receipts: tuple[EvaluationCellReceipt, ...],
) -> tuple[EvaluationCellReceipt, ...]:
    try:
        receipts = tuple(
            EvaluationCellReceipt.model_validate(receipt.model_dump(mode="python"))
            for receipt in receipts
        )
    except (TypeError, ValueError) as exc:
        raise MeasuredEvaluationError("benchmark receipt contract is invalid") from exc
    registry_digest = _registry_source_digest(registry)
    case_by_id = {case.case_id: case for case in registry.cases}
    config_by_id = {config.config_id: config for config in registry.configurations}
    expected = tuple(
        (case.case_id, config.config_id, repetition)
        for case in registry.cases
        for config in registry.configurations
        for repetition in range(1, registry.repetitions + 1)
    )
    observed = tuple((item.case_id, item.config_id, item.repetition) for item in receipts)
    if len(observed) != len(set(observed)):
        raise MeasuredEvaluationError("duplicate benchmark cell receipt")
    missing = sorted(set(expected) - set(observed))
    unexpected = sorted(set(observed) - set(expected))
    if missing or unexpected or len(receipts) != PLANNED_CELLS:
        raise MeasuredEvaluationError(
            f"benchmark matrix mismatch; missing={missing}; unexpected={unexpected}"
        )
    by_key = {
        (receipt.case_id, receipt.config_id, receipt.repetition): receipt for receipt in receipts
    }
    ordered = tuple(by_key[key] for key in expected)
    run_ids = tuple(receipt.run_id for receipt in ordered)
    if len(run_ids) != len(set(run_ids)):
        raise MeasuredEvaluationError("benchmark cells must use unique run IDs")
    run_evidence_digests = tuple(receipt.run_evidence_digest for receipt in ordered)
    if len(run_evidence_digests) != len(set(run_evidence_digests)):
        raise MeasuredEvaluationError("benchmark cells must use unique run-evidence digests")
    for values, message in (
        (
            tuple(receipt.receipt_id for receipt in ordered),
            "benchmark cells must use unique receipt IDs",
        ),
        (
            tuple(receipt.human_rubric.rubric_id for receipt in ordered),
            "benchmark cells must use unique rubric IDs",
        ),
        (
            tuple(receipt.human_rubric_digest for receipt in ordered),
            "benchmark cells must use unique rubric digests",
        ),
    ):
        if len(values) != len(set(values)):
            raise MeasuredEvaluationError(message)
    if len({receipt.provider_id for receipt in ordered}) != 1:
        raise MeasuredEvaluationError("benchmark cells must use one provider identity")
    if len({receipt.model_id for receipt in ordered}) != 1:
        raise MeasuredEvaluationError("benchmark cells must use one model identity")
    if len({receipt.execution_boundary for receipt in ordered}) != 1:
        raise MeasuredEvaluationError("benchmark cells must use one execution boundary")
    if len({receipt.runtime_identity_digest for receipt in ordered}) != 1:
        raise MeasuredEvaluationError("benchmark cells must use one runtime identity")
    if len({receipt.execution_anchor_digest for receipt in ordered}) != 1:
        raise MeasuredEvaluationError("benchmark cells must use one execution anchor")
    for receipt in ordered:
        case = case_by_id[receipt.case_id]
        config = config_by_id[receipt.config_id]
        expected_cell_id = canonical_cell_id(
            receipt.case_id,
            receipt.config_id,
            receipt.repetition,
        )
        if (
            receipt.registry_id != registry.registry_id
            or receipt.registry_digest != registry_digest
        ):
            raise MeasuredEvaluationError("receipt registry binding is invalid")
        if receipt.cell_id != expected_cell_id:
            raise MeasuredEvaluationError("receipt cell identity is noncanonical")
        if receipt.case_label_digest != artifact_digest(case):
            raise MeasuredEvaluationError("receipt case-label binding is invalid")
        if receipt.configuration_digest != artifact_digest(config):
            raise MeasuredEvaluationError("receipt configuration binding is invalid")
        if receipt.provider_id != config.provider_id or receipt.model_id != config.model_id:
            raise MeasuredEvaluationError("receipt model identity differs from its configuration")
        if receipt.execution_boundary != "remote_provider_managed":
            raise MeasuredEvaluationError("benchmark receipt has an invalid execution boundary")
        rubric = receipt.human_rubric
        if receipt.human_rubric_digest != artifact_digest(rubric):
            raise MeasuredEvaluationError("human rubric digest binding is invalid")
        expected_rubric_values = (
            receipt.registry_id,
            receipt.registry_digest,
            receipt.cell_id,
            receipt.case_id,
            receipt.config_id,
            receipt.repetition,
            receipt.run_id,
            receipt.run_evidence_digest,
            receipt.runtime_identity_digest,
            receipt.execution_anchor_digest,
        )
        observed_rubric_values = (
            rubric.registry_id,
            rubric.registry_digest,
            rubric.cell_id,
            rubric.case_id,
            rubric.config_id,
            rubric.repetition,
            rubric.run_id,
            rubric.run_evidence_digest,
            rubric.runtime_identity_digest,
            rubric.execution_anchor_digest,
        )
        if observed_rubric_values != expected_rubric_values:
            raise MeasuredEvaluationError("human rubric cross-binding is invalid")
        if (
            receipt.workflow_disposition
            in {
                WorkflowDisposition.READY_FOR_HUMAN_REVIEW,
                WorkflowDisposition.RECOVERABLE_FAILURE,
                WorkflowDisposition.ENVIRONMENT_UNAVAILABLE,
            }
            and receipt.validation_attempts == 0
        ):
            raise MeasuredEvaluationError(
                "report-backed dispositions require at least one validation attempt"
            )
        if (
            receipt.workflow_disposition is WorkflowDisposition.DECISION_REQUIRED
            and receipt.validation_attempts != 0
        ):
            raise MeasuredEvaluationError(
                "planning decision receipts cannot claim validation attempts"
            )
        if (
            not config.uses_wiki
            and rubric.wiki_support.availability is not EvidenceAvailability.UNAVAILABLE
        ):
            raise MeasuredEvaluationError("no-Wiki cells cannot claim Wiki support evidence")
        if (
            config.uses_wiki
            and rubric.wiki_support.availability is EvidenceAvailability.AVAILABLE
            and rubric.wiki_support.denominator == 0
        ):
            raise MeasuredEvaluationError(
                "available Wiki evidence requires a positive reviewed denominator"
            )
    return ordered


def _registry_source_digest(registry: MeasuredEvaluationRegistry) -> Sha256Digest:
    """Hash the registry field-presence form verified from its source JSON."""

    return artifact_digest(registry.model_dump(mode="json", exclude_unset=True))


def _derive_metrics(
    receipts: tuple[EvaluationCellReceipt, ...],
    cases: dict[str, BenchmarkCaseLabel],
    configurations: dict[str, BenchmarkConfiguration],
    quality_thresholds: QualityThresholdPolicy,
) -> tuple[MetricResult, ...]:
    metrics: dict[MetricId, MetricResult] = {}

    expected_outcomes = sum(
        receipt.workflow_disposition is cases[receipt.case_id].expected_disposition
        for receipt in receipts
    )
    metrics[MetricId.EXPECTED_OUTCOME_CONFORMANCE] = _ratio_metric(
        MetricId.EXPECTED_OUTCOME_CONFORMANCE,
        expected_outcomes,
        len(receipts),
        threshold=1.0,
    )

    authorization = [receipt.authorization_violations for receipt in receipts]
    metrics[MetricId.AUTHORIZATION_VIOLATIONS] = _count_metric(
        MetricId.AUTHORIZATION_VIOLATIONS,
        authorization,
        threshold=0.0,
        maximum=True,
    )

    ready = [
        receipt
        for receipt in receipts
        if receipt.workflow_disposition is WorkflowDisposition.READY_FOR_HUMAN_REVIEW
    ]
    ready_unavailable = sum(
        receipt.human_rubric.acceptance.availability is EvidenceAvailability.UNAVAILABLE
        for receipt in ready
    )
    ready_accepted = sum(
        receipt.human_rubric.acceptance.decision is HumanAcceptance.ACCEPTED for receipt in ready
    )
    metrics[MetricId.READY_PRECISION] = _ratio_metric(
        MetricId.READY_PRECISION,
        ready_accepted,
        len(ready),
        unavailable_cells=ready_unavailable,
        threshold=1.0,
    )

    labels_reviewed = all(
        case.review_status is LabelReviewStatus.INDEPENDENTLY_REVIEWED for case in cases.values()
    )
    dependency_unavailable = 0
    dependency_expected = 0
    dependency_detected = 0
    high_expected = 0
    high_missed = 0
    for receipt in receipts:
        label = cases[receipt.case_id]
        evidence = receipt.detected_dependencies
        expected = {item.dependency_id for item in label.dependencies}
        high_impact = {item.dependency_id for item in label.dependencies if item.high_impact}
        dependency_expected += len(expected)
        high_expected += len(high_impact)
        if evidence.availability is EvidenceAvailability.UNAVAILABLE or not labels_reviewed:
            dependency_unavailable += 1
            continue
        detected = set(evidence.values or ())
        dependency_detected += len(expected & detected)
        high_missed += len(high_impact - detected)
    metrics[MetricId.DEPENDENCY_RECALL] = _ratio_metric(
        MetricId.DEPENDENCY_RECALL,
        dependency_detected,
        dependency_expected,
        unavailable_cells=dependency_unavailable,
        threshold=0.95,
    )
    case_dependency_recalls: list[float] = []
    for case_id, case in cases.items():
        case_receipts = tuple(item for item in receipts if item.case_id == case_id)
        case_expected = len(case.dependencies) * len(case_receipts)
        case_detected = 0
        case_unavailable = False
        expected_ids = {item.dependency_id for item in case.dependencies}
        for receipt in case_receipts:
            evidence = receipt.detected_dependencies
            if evidence.availability is EvidenceAvailability.UNAVAILABLE or not labels_reviewed:
                case_unavailable = True
                continue
            case_detected += len(expected_ids & set(evidence.values or ()))
        if not case_unavailable:
            case_dependency_recalls.append(case_detected / case_expected)
    macro_unavailable = dependency_unavailable if len(case_dependency_recalls) != len(cases) else 0
    metrics[MetricId.DEPENDENCY_RECALL_MACRO] = _value_metric(
        MetricId.DEPENDENCY_RECALL_MACRO,
        (
            sum(case_dependency_recalls) / len(case_dependency_recalls)
            if len(case_dependency_recalls) == len(cases)
            else None
        ),
        denominator=len(cases),
        unavailable_cells=macro_unavailable,
        threshold=0.95,
    )
    metrics[MetricId.HIGH_IMPACT_DEPENDENCY_MISSES] = _direct_metric(
        MetricId.HIGH_IMPACT_DEPENDENCY_MISSES,
        high_missed,
        high_expected,
        unavailable_cells=dependency_unavailable,
        threshold=0.0,
        maximum=True,
    )

    intervention_unavailable = sum(
        receipt.intervention_triggered.availability is EvidenceAvailability.UNAVAILABLE
        or not labels_reviewed
        for receipt in receipts
    )
    expected_interventions = sum(
        cases[receipt.case_id].intervention_expected for receipt in receipts
    )
    triggered = sum(receipt.intervention_triggered.value is True for receipt in receipts)
    true_positive = sum(
        cases[receipt.case_id].intervention_expected
        and receipt.intervention_triggered.value is True
        for receipt in receipts
    )
    metrics[MetricId.INTERVENTION_RECALL] = _ratio_metric(
        MetricId.INTERVENTION_RECALL,
        true_positive,
        expected_interventions,
        unavailable_cells=intervention_unavailable,
        threshold=1.0,
    )
    metrics[MetricId.INTERVENTION_PRECISION] = _ratio_metric(
        MetricId.INTERVENTION_PRECISION,
        true_positive,
        triggered,
        unavailable_cells=intervention_unavailable,
        threshold=quality_thresholds.intervention_precision_minimum,
    )

    intervention_reason_unavailable = 0
    expected_intervention_reasons = 0
    observed_intervention_reasons = 0
    for receipt in receipts:
        expected_reason_ids = {
            reason.value for reason in cases[receipt.case_id].expected_intervention_reason_ids
        }
        expected_intervention_reasons += len(expected_reason_ids)
        if not expected_reason_ids:
            continue
        evidence = receipt.intervention_reason_ids
        if evidence.availability is EvidenceAvailability.UNAVAILABLE or not labels_reviewed:
            intervention_reason_unavailable += 1
            continue
        observed_intervention_reasons += len(expected_reason_ids & set(evidence.values or ()))
    metrics[MetricId.INTERVENTION_REASON_RECALL] = _ratio_metric(
        MetricId.INTERVENTION_REASON_RECALL,
        observed_intervention_reasons,
        expected_intervention_reasons,
        unavailable_cells=intervention_reason_unavailable,
        threshold=1.0,
    )

    mule_receipts = [
        receipt for receipt in receipts if cases[receipt.case_id].platform is Platform.MULESOFT
    ]
    runtime_unavailable = sum(
        _is_runtime_unavailable_mule_cell(receipt, cases) for receipt in mule_receipts
    )
    runtime_completed = sum(
        receipt.workflow_disposition is WorkflowDisposition.READY_FOR_HUMAN_REVIEW
        and not _is_runtime_unavailable_mule_cell(receipt, cases)
        for receipt in mule_receipts
    )
    metrics[MetricId.RUNTIME_VALIDATION_COMPLETION] = _ratio_metric(
        MetricId.RUNTIME_VALIDATION_COMPLETION,
        runtime_completed,
        len(mule_receipts),
        unavailable_cells=runtime_unavailable,
        threshold=1.0,
    )

    # First-pass and repair rates measure successful candidate validation, not
    # conformance to a predeclared safe failure.  Runtime-unavailable Mule cells
    # therefore remain visible in the required runtime metric above but are not
    # credited as migration attempts here.
    attempted = [
        receipt
        for receipt in receipts
        if receipt.validation_attempts > 0 and not _is_runtime_unavailable_mule_cell(receipt, cases)
    ]
    first_pass = sum(
        receipt.validation_attempts == 1
        and receipt.workflow_disposition is WorkflowDisposition.READY_FOR_HUMAN_REVIEW
        for receipt in attempted
    )
    repairs = [receipt for receipt in attempted if receipt.validation_attempts == 2]
    repaired = sum(
        receipt.workflow_disposition is WorkflowDisposition.READY_FOR_HUMAN_REVIEW
        for receipt in repairs
    )
    metrics[MetricId.FIRST_PASS_RATE] = _ratio_metric(
        MetricId.FIRST_PASS_RATE,
        first_pass,
        len(attempted),
        threshold=quality_thresholds.first_pass_rate_minimum,
    )
    metrics[MetricId.BOUNDED_REPAIR_RATE] = _ratio_metric(
        MetricId.BOUNDED_REPAIR_RATE,
        repaired,
        len(repairs),
    )

    wiki_receipts = [receipt for receipt in receipts if configurations[receipt.config_id].uses_wiki]
    wiki_unavailable = sum(
        receipt.human_rubric.wiki_support.availability is EvidenceAvailability.UNAVAILABLE
        for receipt in wiki_receipts
    )
    wiki_supported = sum(
        receipt.human_rubric.wiki_support.numerator or 0 for receipt in wiki_receipts
    )
    wiki_reviewed = sum(
        receipt.human_rubric.wiki_support.denominator or 0 for receipt in wiki_receipts
    )
    metrics[MetricId.WIKI_SUPPORT_ACCURACY] = _ratio_metric(
        MetricId.WIKI_SUPPORT_ACCURACY,
        wiki_supported,
        wiki_reviewed,
        unavailable_cells=wiki_unavailable,
        threshold=quality_thresholds.wiki_support_accuracy_minimum,
    )

    semantic = [receipt.human_rubric.semantic_conformance for receipt in receipts]
    semantic_available = [
        item for item in semantic if item.availability is EvidenceAvailability.AVAILABLE
    ]
    metrics[MetricId.SEMANTIC_CONFORMANCE] = _ratio_metric(
        MetricId.SEMANTIC_CONFORMANCE,
        sum(item.value is True for item in semantic_available),
        len(semantic_available),
        unavailable_cells=len(semantic) - len(semantic_available),
        threshold=quality_thresholds.semantic_conformance_minimum,
    )

    latency = [receipt.usage.latency_ms for receipt in receipts]
    latency_values = [item.value for item in latency if item.value is not None]
    metrics[MetricId.MEAN_LATENCY_MS] = _direct_metric(
        MetricId.MEAN_LATENCY_MS,
        sum(latency_values),
        len(latency_values),
        unavailable_cells=len(latency) - len(latency_values),
        divide=True,
    )
    token_evidence = [
        (receipt.usage.input_tokens, receipt.usage.output_tokens) for receipt in receipts
    ]
    token_available = [
        (input_evidence.value, output_evidence.value)
        for input_evidence, output_evidence in token_evidence
        if input_evidence.value is not None and output_evidence.value is not None
    ]
    metrics[MetricId.TOTAL_TOKENS] = _direct_metric(
        MetricId.TOTAL_TOKENS,
        sum(
            (input_value or 0) + (output_value or 0)
            for input_value, output_value in token_available
        ),
        len(token_available),
        unavailable_cells=len(token_evidence) - len(token_available),
    )
    costs = [receipt.usage.cost_micro_usd for receipt in receipts]
    cost_values = [item.value for item in costs if item.value is not None]
    metrics[MetricId.TOTAL_COST_MICRO_USD] = _direct_metric(
        MetricId.TOTAL_COST_MICRO_USD,
        sum(cost_values),
        len(cost_values),
        unavailable_cells=len(costs) - len(cost_values),
    )
    model_calls = [receipt.usage.model_calls for receipt in receipts]
    model_call_values = [item.value for item in model_calls if item.value is not None]
    metrics[MetricId.MODEL_CALL_COUNT] = _direct_metric(
        MetricId.MODEL_CALL_COUNT,
        sum(model_call_values),
        len(model_call_values),
        unavailable_cells=len(model_calls) - len(model_call_values),
    )
    tool_calls = [receipt.usage.tool_calls for receipt in receipts]
    tool_call_values = [item.value for item in tool_calls if item.value is not None]
    metrics[MetricId.TOOL_CALL_COUNT] = _direct_metric(
        MetricId.TOOL_CALL_COUNT,
        sum(tool_call_values),
        len(tool_call_values),
        unavailable_cells=len(tool_calls) - len(tool_call_values),
    )

    defect_evidence = [receipt.human_rubric.escaped_defects for receipt in receipts]
    unavailable_defects = sum(
        evidence.availability is EvidenceAvailability.UNAVAILABLE for evidence in defect_evidence
    )
    defects = tuple(defect for evidence in defect_evidence for defect in (evidence.defects or ()))
    high_defects = sum(
        defect.impact in {DefectImpact.HIGH, DefectImpact.CRITICAL} for defect in defects
    )
    metrics[MetricId.ESCAPED_DEFECTS] = _direct_metric(
        MetricId.ESCAPED_DEFECTS,
        len(defects),
        len(defect_evidence),
        unavailable_cells=unavailable_defects,
        threshold=float(quality_thresholds.escaped_defects_maximum),
        maximum=True,
    )
    metrics[MetricId.ESCAPED_HIGH_IMPACT_DEFECTS] = _direct_metric(
        MetricId.ESCAPED_HIGH_IMPACT_DEFECTS,
        high_defects,
        len(defect_evidence),
        unavailable_cells=unavailable_defects,
        threshold=0.0,
        maximum=True,
    )
    return tuple(metrics[metric_id] for metric_id in _METRIC_ORDER)


def _slice_summary(
    *,
    dimension: Literal["configuration", "case"],
    slice_id: str,
    receipts: tuple[EvaluationCellReceipt, ...],
    cases: dict[str, BenchmarkCaseLabel],
    configurations: dict[str, BenchmarkConfiguration],
) -> EvaluationSliceSummary:
    expected_outcomes = sum(
        receipt.workflow_disposition is cases[receipt.case_id].expected_disposition
        for receipt in receipts
    )

    labels_reviewed = all(
        case.review_status is LabelReviewStatus.INDEPENDENTLY_REVIEWED for case in cases.values()
    )
    dependency_numerator = 0
    dependency_denominator = 0
    dependency_unavailable = 0
    for receipt in receipts:
        expected = {item.dependency_id for item in cases[receipt.case_id].dependencies}
        dependency_denominator += len(expected)
        if (
            not labels_reviewed
            or receipt.detected_dependencies.availability is EvidenceAvailability.UNAVAILABLE
        ):
            dependency_unavailable += 1
            continue
        dependency_numerator += len(expected & set(receipt.detected_dependencies.values or ()))

    semantic = tuple(receipt.human_rubric.semantic_conformance for receipt in receipts)
    semantic_available = tuple(
        item for item in semantic if item.availability is EvidenceAvailability.AVAILABLE
    )
    attempted = tuple(
        receipt
        for receipt in receipts
        if receipt.validation_attempts > 0 and not _is_runtime_unavailable_mule_cell(receipt, cases)
    )
    first_pass = sum(
        receipt.validation_attempts == 1
        and receipt.workflow_disposition is WorkflowDisposition.READY_FOR_HUMAN_REVIEW
        for receipt in attempted
    )
    wiki_receipts = tuple(
        receipt for receipt in receipts if configurations[receipt.config_id].uses_wiki
    )
    wiki_unavailable = sum(
        receipt.human_rubric.wiki_support.availability is EvidenceAvailability.UNAVAILABLE
        for receipt in wiki_receipts
    )
    wiki_numerator = sum(
        receipt.human_rubric.wiki_support.numerator or 0 for receipt in wiki_receipts
    )
    wiki_denominator = sum(
        receipt.human_rubric.wiki_support.denominator or 0 for receipt in wiki_receipts
    )
    return EvaluationSliceSummary(
        dimension=dimension,
        slice_id=slice_id,
        cells=len(receipts),
        expected_outcome_conformance=_rate_summary(
            expected_outcomes,
            len(receipts),
        ),
        dependency_recall=_rate_summary(
            dependency_numerator,
            dependency_denominator,
            unavailable_cells=dependency_unavailable,
        ),
        semantic_conformance=_rate_summary(
            sum(item.value is True for item in semantic_available),
            len(semantic_available),
            unavailable_cells=len(semantic) - len(semantic_available),
        ),
        first_pass_rate=_rate_summary(first_pass, len(attempted)),
        wiki_support_accuracy=_rate_summary(
            wiki_numerator,
            wiki_denominator,
            unavailable_cells=wiki_unavailable,
        ),
    )


def _rate_summary(
    numerator: int,
    denominator: int,
    *,
    unavailable_cells: int = 0,
) -> RateSummary:
    value = numerator / denominator if denominator > 0 and unavailable_cells == 0 else None
    return RateSummary(
        value=value,
        numerator=numerator,
        denominator=denominator,
        unavailable_cells=unavailable_cells,
    )


def _paired_wiki_deltas(
    receipts: tuple[EvaluationCellReceipt, ...],
    registry: MeasuredEvaluationRegistry,
    cases: dict[str, BenchmarkCaseLabel],
) -> tuple[PairedWikiDelta, ...]:
    wiki_config = next(item for item in registry.configurations if item.uses_wiki)
    no_wiki_config = next(item for item in registry.configurations if not item.uses_wiki)
    by_cell = {
        (receipt.case_id, receipt.config_id, receipt.repetition): receipt for receipt in receipts
    }
    labels_reviewed = all(
        case.review_status is LabelReviewStatus.INDEPENDENTLY_REVIEWED for case in cases.values()
    )
    deltas: list[PairedWikiDelta] = []
    for case in registry.cases:
        pairs = tuple(
            (
                by_cell[(case.case_id, wiki_config.config_id, repetition)],
                by_cell[(case.case_id, no_wiki_config.config_id, repetition)],
            )
            for repetition in range(1, registry.repetitions + 1)
        )
        deltas.append(
            PairedWikiDelta(
                case_id=case.case_id,
                wiki_config_id=wiki_config.config_id,
                no_wiki_config_id=no_wiki_config.config_id,
                expected_outcome_conformance=_paired_delta(
                    tuple(
                        (
                            float(wiki.workflow_disposition is case.expected_disposition),
                            float(no_wiki.workflow_disposition is case.expected_disposition),
                        )
                        for wiki, no_wiki in pairs
                    )
                ),
                dependency_recall=_paired_delta(
                    tuple(
                        (
                            _receipt_dependency_recall(wiki, case, labels_reviewed),
                            _receipt_dependency_recall(
                                no_wiki,
                                case,
                                labels_reviewed,
                            ),
                        )
                        for wiki, no_wiki in pairs
                    )
                ),
                semantic_conformance=_paired_delta(
                    tuple(
                        (
                            _boolean_evidence_value(wiki.human_rubric.semantic_conformance),
                            _boolean_evidence_value(no_wiki.human_rubric.semantic_conformance),
                        )
                        for wiki, no_wiki in pairs
                    )
                ),
                first_pass_rate=_paired_delta(
                    tuple(
                        (
                            _first_pass_value(wiki, case),
                            _first_pass_value(no_wiki, case),
                        )
                        for wiki, no_wiki in pairs
                    )
                ),
                mean_latency_ms=_paired_delta(
                    tuple(
                        (
                            _count_evidence_value(wiki.usage.latency_ms),
                            _count_evidence_value(no_wiki.usage.latency_ms),
                        )
                        for wiki, no_wiki in pairs
                    )
                ),
                mean_total_tokens=_paired_delta(
                    tuple(
                        (
                            _total_token_value(wiki),
                            _total_token_value(no_wiki),
                        )
                        for wiki, no_wiki in pairs
                    )
                ),
            )
        )
    return tuple(deltas)


def _paired_delta(
    values: tuple[tuple[float | None, float | None], ...],
) -> PairedDeltaSummary:
    available = tuple(
        wiki - no_wiki for wiki, no_wiki in values if wiki is not None and no_wiki is not None
    )
    unavailable = len(values) - len(available)
    return PairedDeltaSummary(
        value=sum(available) / len(available) if unavailable == 0 else None,
        available_pairs=len(available),
        unavailable_pairs=unavailable,
    )


def _receipt_dependency_recall(
    receipt: EvaluationCellReceipt,
    case: BenchmarkCaseLabel,
    labels_reviewed: bool,
) -> float | None:
    if (
        not labels_reviewed
        or receipt.detected_dependencies.availability is EvidenceAvailability.UNAVAILABLE
    ):
        return None
    expected = {item.dependency_id for item in case.dependencies}
    return len(expected & set(receipt.detected_dependencies.values or ())) / len(expected)


def _boolean_evidence_value(evidence: BooleanEvidence) -> float | None:
    return float(evidence.value) if evidence.value is not None else None


def _count_evidence_value(evidence: CountEvidence) -> float | None:
    return float(evidence.value) if evidence.value is not None else None


def _first_pass_value(
    receipt: EvaluationCellReceipt,
    case: BenchmarkCaseLabel,
) -> float | None:
    if receipt.validation_attempts == 0 or _is_runtime_unavailable_mule_receipt(
        receipt,
        case,
    ):
        return None
    return float(
        receipt.validation_attempts == 1
        and receipt.workflow_disposition is WorkflowDisposition.READY_FOR_HUMAN_REVIEW
    )


def _is_runtime_unavailable_mule_cell(
    receipt: EvaluationCellReceipt,
    cases: dict[str, BenchmarkCaseLabel],
) -> bool:
    return _is_runtime_unavailable_mule_receipt(receipt, cases[receipt.case_id])


def _is_runtime_unavailable_mule_receipt(
    receipt: EvaluationCellReceipt,
    case: BenchmarkCaseLabel,
) -> bool:
    if case.platform is not Platform.MULESOFT:
        return False
    evidence = receipt.unavailable_required_command_ids
    return receipt.workflow_disposition is WorkflowDisposition.ENVIRONMENT_UNAVAILABLE or (
        evidence.availability is EvidenceAvailability.AVAILABLE and bool(evidence.values)
    )


def _total_token_value(receipt: EvaluationCellReceipt) -> float | None:
    if receipt.usage.input_tokens.value is None or receipt.usage.output_tokens.value is None:
        return None
    return float(receipt.usage.input_tokens.value + receipt.usage.output_tokens.value)


def _count_metric(
    metric_id: MetricId,
    evidence: list[CountEvidence],
    *,
    threshold: float,
    maximum: bool,
) -> MetricResult:
    available = [item.value for item in evidence if item.value is not None]
    return _direct_metric(
        metric_id,
        sum(available),
        len(available),
        unavailable_cells=len(evidence) - len(available),
        threshold=threshold,
        maximum=maximum,
    )


def _value_metric(
    metric_id: MetricId,
    value: float | None,
    *,
    denominator: int,
    unavailable_cells: int = 0,
    threshold: float,
    maximum: bool = False,
) -> MetricResult:
    gated = metric_id in _GATED_METRICS
    required = metric_id in _REQUIRED_METRICS
    if value is None or denominator == 0 or (required and unavailable_cells):
        return MetricResult(
            metric_id=metric_id,
            denominator=denominator,
            unavailable_cells=unavailable_cells,
            gated=gated,
            required=required,
            threshold=threshold,
            outcome=MetricOutcome.NOT_EVALUATED,
        )
    outcome = (
        MetricOutcome.PASSED
        if ((value <= threshold) if maximum else (value >= threshold))
        else MetricOutcome.FAILED
    )
    return MetricResult(
        metric_id=metric_id,
        value=value,
        denominator=denominator,
        unavailable_cells=unavailable_cells,
        gated=gated,
        required=required,
        threshold=threshold,
        outcome=outcome,
    )


def _ratio_metric(
    metric_id: MetricId,
    numerator: int,
    denominator: int,
    *,
    unavailable_cells: int = 0,
    threshold: float | None = None,
) -> MetricResult:
    return _direct_metric(
        metric_id,
        numerator,
        denominator,
        unavailable_cells=unavailable_cells,
        threshold=threshold,
        divide=True,
    )


def _direct_metric(
    metric_id: MetricId,
    numerator: int,
    denominator: int,
    *,
    unavailable_cells: int = 0,
    threshold: float | None = None,
    maximum: bool = False,
    divide: bool = False,
) -> MetricResult:
    gated = metric_id in _GATED_METRICS
    required = metric_id in _REQUIRED_METRICS
    if denominator == 0 or (required and unavailable_cells):
        return MetricResult(
            metric_id=metric_id,
            numerator=numerator,
            denominator=denominator,
            unavailable_cells=unavailable_cells,
            gated=gated,
            required=required,
            threshold=threshold,
            outcome=MetricOutcome.NOT_EVALUATED,
        )
    value = numerator / denominator if divide else float(numerator)
    if threshold is not None:
        outcome = (
            MetricOutcome.PASSED
            if ((value <= threshold) if maximum else (value >= threshold))
            else MetricOutcome.FAILED
        )
    elif gated:
        raise AssertionError("gated metric is missing its threshold")
    else:
        outcome = MetricOutcome.REPORTED
    return MetricResult(
        metric_id=metric_id,
        value=value,
        numerator=numerator,
        denominator=denominator,
        unavailable_cells=unavailable_cells,
        gated=gated,
        required=required,
        threshold=threshold,
        outcome=outcome,
    )
