"""Compact, provider-free evaluation registry and result verification.

The course benchmark is a fixed 6 x 4 x 3 design.  This module deliberately
stores the design and the absence of formal results without retaining copied
workspaces or pretending that local fixtures are live-agent measurements.
Loading and verification are local, deterministic, and make no provider or
platform calls.
"""

from __future__ import annotations

import hashlib
import json
import stat
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from legacy_migration_agent.contracts import Sha256Digest, validate_relative_path
from legacy_migration_agent.core.integrity import ArtifactStore, artifact_digest
from legacy_migration_agent.core.workspace import content_revision

SCHEMA_VERSION: Literal["1.0"] = "1.0"
EXPECTED_CELL_COUNT = 72
MAX_DOCUMENT_BYTES = 512_000

Identifier = Annotated[
    str,
    Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$"),
]


class EvaluationVerificationError(ValueError):
    """Raised when the compact evaluation design or results are inconsistent."""


class StrictModel(BaseModel):
    """Immutable JSON contract that rejects unknown fields and non-finite values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class Platform(StrEnum):
    SALESFORCE = "salesforce"
    MULESOFT = "mulesoft"


class Complexity(StrEnum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


class ExpectedOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    DECISION_REQUIRED = "decision_required"


class ResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DECISION_REQUIRED = "decision_required"
    UNAVAILABLE = "unavailable"
    NOT_PERFORMED = "not_performed"


class MetricDirection(StrEnum):
    MINIMUM = "minimum"
    MAXIMUM = "maximum"
    REPORT_ONLY = "report_only"


class MetricUnit(StrEnum):
    PROPORTION = "proportion"
    COUNT = "count"
    MILLISECONDS = "milliseconds"
    TOKENS = "tokens"
    MICRO_USD = "micro_usd"
    MICRO_USD_PER_UNIT = "micro_usd_per_unit"


class MetricId(StrEnum):
    OPERATIONAL_SUCCESS_RATE = "operational_success_rate"
    SEMANTIC_CONFORMANCE_RATE = "semantic_conformance_rate"
    AUTHORIZATION_VIOLATIONS = "authorization_violations"
    READY_CLAIM_PRECISION = "ready_claim_precision"
    DEPENDENCY_RECALL = "dependency_recall"
    INTERVENTION_PRECISION = "intervention_precision"
    INTERVENTION_RECALL = "intervention_recall"
    FIRST_PASS_VALIDATION_RATE = "first_pass_validation_rate"
    BOUNDED_REPAIR_SUCCESS_RATE = "bounded_repair_success_rate"
    WIKI_SUPPORT_ACCURACY = "wiki_support_accuracy"
    ESCAPED_DEFECTS = "escaped_defects"
    ESCAPED_HIGH_IMPACT_DEFECTS = "escaped_high_impact_defects"
    ACCEPTED_MIGRATION_UNITS = "accepted_migration_units"
    STAGE_LATENCY_MS = "stage_latency_ms"
    ACCEPTED_UNIT_LATENCY_MS = "accepted_unit_latency_ms"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    COST_MICRO_USD = "cost_micro_usd"
    COST_PER_ACCEPTED_UNIT_MICRO_USD = "cost_per_accepted_unit_micro_usd"


class EvaluationCase(StrictModel):
    case_id: Identifier
    platform: Platform
    complexity: Complexity
    title: str = Field(min_length=1, max_length=160)
    fixture_path: str = Field(min_length=1, max_length=240)
    expected_outcome: ExpectedOutcome


class EvaluationConfiguration(StrictModel):
    config_id: Identifier
    uses_dependency_graph: bool
    uses_wiki: bool
    uses_agents: bool
    uses_bounded_correction: bool


class MetricDefinition(StrictModel):
    metric_id: MetricId
    unit: MetricUnit
    direction: MetricDirection
    threshold: float | None = None

    @model_validator(mode="after")
    def validate_threshold(self) -> MetricDefinition:
        if self.direction is MetricDirection.REPORT_ONLY and self.threshold is not None:
            raise ValueError("report-only metrics cannot declare a threshold")
        if self.direction is not MetricDirection.REPORT_ONLY and self.threshold is None:
            raise ValueError("gated metrics require a threshold")
        return self


_EXPECTED_CASES: tuple[tuple[str, Platform, Complexity, str, ExpectedOutcome], ...] = (
    (
        "salesforce-simple-page-controller",
        Platform.SALESFORCE,
        Complexity.SIMPLE,
        "fixtures/salesforce/account-contact-explorer/input",
        ExpectedOutcome.SUCCEEDED,
    ),
    (
        "salesforce-medium-account-contact",
        Platform.SALESFORCE,
        Complexity.MEDIUM,
        "fixtures/salesforce/account-contact-explorer/input",
        ExpectedOutcome.SUCCEEDED,
    ),
    (
        "salesforce-complex-dynamic-closure",
        Platform.SALESFORCE,
        Complexity.COMPLEX,
        "fixtures/salesforce/account-contact-explorer/input",
        ExpectedOutcome.DECISION_REQUIRED,
    ),
    (
        "mule-simple-response-subflow",
        Platform.MULESOFT,
        Complexity.SIMPLE,
        "fixtures/mulesoft/customer-status-api/input",
        ExpectedOutcome.SUCCEEDED,
    ),
    (
        "mule-medium-http-flow",
        Platform.MULESOFT,
        Complexity.MEDIUM,
        "fixtures/mulesoft/customer-status-api/input",
        ExpectedOutcome.SUCCEEDED,
    ),
    (
        "mule-complex-error-contract",
        Platform.MULESOFT,
        Complexity.COMPLEX,
        "fixtures/mulesoft/customer-status-api/input",
        ExpectedOutcome.DECISION_REQUIRED,
    ),
)

_EXPECTED_CONFIGURATIONS: tuple[tuple[str, bool, bool, bool, bool], ...] = (
    ("full-agent", True, True, True, True),
    ("ablation-no-wiki", True, False, True, True),
    ("ablation-no-correction", True, True, True, False),
    ("static-only", True, False, False, False),
)

_METRIC_SPECS: tuple[tuple[MetricId, MetricUnit, MetricDirection, float | None], ...] = (
    (
        MetricId.OPERATIONAL_SUCCESS_RATE,
        MetricUnit.PROPORTION,
        MetricDirection.REPORT_ONLY,
        None,
    ),
    (
        MetricId.SEMANTIC_CONFORMANCE_RATE,
        MetricUnit.PROPORTION,
        MetricDirection.REPORT_ONLY,
        None,
    ),
    (
        MetricId.AUTHORIZATION_VIOLATIONS,
        MetricUnit.COUNT,
        MetricDirection.MAXIMUM,
        0.0,
    ),
    (
        MetricId.READY_CLAIM_PRECISION,
        MetricUnit.PROPORTION,
        MetricDirection.MINIMUM,
        1.0,
    ),
    (
        MetricId.DEPENDENCY_RECALL,
        MetricUnit.PROPORTION,
        MetricDirection.MINIMUM,
        0.95,
    ),
    (
        MetricId.INTERVENTION_PRECISION,
        MetricUnit.PROPORTION,
        MetricDirection.REPORT_ONLY,
        None,
    ),
    (
        MetricId.INTERVENTION_RECALL,
        MetricUnit.PROPORTION,
        MetricDirection.MINIMUM,
        1.0,
    ),
    (
        MetricId.FIRST_PASS_VALIDATION_RATE,
        MetricUnit.PROPORTION,
        MetricDirection.REPORT_ONLY,
        None,
    ),
    (
        MetricId.BOUNDED_REPAIR_SUCCESS_RATE,
        MetricUnit.PROPORTION,
        MetricDirection.REPORT_ONLY,
        None,
    ),
    (
        MetricId.WIKI_SUPPORT_ACCURACY,
        MetricUnit.PROPORTION,
        MetricDirection.REPORT_ONLY,
        None,
    ),
    (MetricId.ESCAPED_DEFECTS, MetricUnit.COUNT, MetricDirection.REPORT_ONLY, None),
    (
        MetricId.ESCAPED_HIGH_IMPACT_DEFECTS,
        MetricUnit.COUNT,
        MetricDirection.MAXIMUM,
        0.0,
    ),
    (
        MetricId.ACCEPTED_MIGRATION_UNITS,
        MetricUnit.COUNT,
        MetricDirection.REPORT_ONLY,
        None,
    ),
    (
        MetricId.STAGE_LATENCY_MS,
        MetricUnit.MILLISECONDS,
        MetricDirection.REPORT_ONLY,
        None,
    ),
    (
        MetricId.ACCEPTED_UNIT_LATENCY_MS,
        MetricUnit.MILLISECONDS,
        MetricDirection.REPORT_ONLY,
        None,
    ),
    (MetricId.INPUT_TOKENS, MetricUnit.TOKENS, MetricDirection.REPORT_ONLY, None),
    (MetricId.OUTPUT_TOKENS, MetricUnit.TOKENS, MetricDirection.REPORT_ONLY, None),
    (MetricId.COST_MICRO_USD, MetricUnit.MICRO_USD, MetricDirection.REPORT_ONLY, None),
    (
        MetricId.COST_PER_ACCEPTED_UNIT_MICRO_USD,
        MetricUnit.MICRO_USD_PER_UNIT,
        MetricDirection.REPORT_ONLY,
        None,
    ),
)


class EvaluationRegistry(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    registry_id: Literal["legacy-migration-benchmark-v1"]
    declaration_status: Literal["predeclared_unmeasured"]
    repetitions: Literal[3]
    require_full_cross_product: Literal[True]
    cases: tuple[EvaluationCase, ...] = Field(min_length=6, max_length=6)
    configurations: tuple[EvaluationConfiguration, ...] = Field(min_length=4, max_length=4)
    metrics: tuple[MetricDefinition, ...] = Field(
        min_length=len(_METRIC_SPECS), max_length=len(_METRIC_SPECS)
    )

    @model_validator(mode="after")
    def validate_fixed_design(self) -> EvaluationRegistry:
        observed_cases = tuple(
            (
                case.case_id,
                case.platform,
                case.complexity,
                case.fixture_path,
                case.expected_outcome,
            )
            for case in self.cases
        )
        if observed_cases != _EXPECTED_CASES:
            raise ValueError("registry must contain the canonical platform/complexity cases")

        observed_configs = tuple(
            (
                config.config_id,
                config.uses_dependency_graph,
                config.uses_wiki,
                config.uses_agents,
                config.uses_bounded_correction,
            )
            for config in self.configurations
        )
        if observed_configs != _EXPECTED_CONFIGURATIONS:
            raise ValueError("registry must contain the four canonical execution configurations")

        observed_metrics = tuple(
            (metric.metric_id, metric.unit, metric.direction, metric.threshold)
            for metric in self.metrics
        )
        if observed_metrics != _METRIC_SPECS:
            raise ValueError("registry metric definitions or thresholds changed")
        return self


class EvaluationCell(StrictModel):
    cell_id: Identifier
    case_id: Identifier
    config_id: Identifier
    repetition: int = Field(ge=1, le=3)
    status: ResultStatus
    reason: Literal["formal_run_not_performed"]


class ExecutionBoundary(StrictModel):
    provider_invoked: Literal[False]
    external_platform_invoked: Literal[False]
    salesforce_org_invoked: Literal[False]
    mulesoft_runtime_invoked: Literal[False]
    authentication_invoked: Literal[False]
    human_gate_performed: Literal[False]
    external_authority_granted: Literal[False]


class StatusCounts(StrictModel):
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    decision_required: int = Field(ge=0)
    unavailable: int = Field(ge=0)
    not_performed: int = Field(ge=0)


class MetricAggregate(StrictModel):
    metric_id: MetricId
    value: int | float | None
    measured_cells: int = Field(ge=0)
    threshold_met: bool | None


class EvaluationSummary(StrictModel):
    planned_cells: Literal[72]
    recorded_cells: Literal[72]
    status_counts: StatusCounts
    metrics: tuple[MetricAggregate, ...] = Field(
        min_length=len(_METRIC_SPECS), max_length=len(_METRIC_SPECS)
    )
    complete: Literal[False]
    full_agent_gate_passed: Literal[False]
    passed: Literal[False]


class EvaluationResults(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    results_id: Literal["legacy-migration-benchmark-v1-current"]
    registry_id: Literal["legacy-migration-benchmark-v1"]
    snapshot_status: Literal["not_performed"]
    execution_boundary: ExecutionBoundary
    cells: tuple[EvaluationCell, ...] = Field(
        min_length=EXPECTED_CELL_COUNT, max_length=EXPECTED_CELL_COUNT
    )
    summary: EvaluationSummary
    limitations: tuple[str, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_snapshot_consistency(self) -> EvaluationResults:
        cell_ids = tuple(cell.cell_id for cell in self.cells)
        if len(set(cell_ids)) != EXPECTED_CELL_COUNT:
            raise ValueError("evaluation cell IDs must be unique")
        if any(cell.status is not ResultStatus.NOT_PERFORMED for cell in self.cells):
            raise ValueError("the compact current snapshot must record every cell not_performed")

        expected_counts = StatusCounts(
            succeeded=0,
            failed=0,
            decision_required=0,
            unavailable=0,
            not_performed=EXPECTED_CELL_COUNT,
        )
        if self.summary.status_counts != expected_counts:
            raise ValueError("summary status counts do not match the recorded cells")

        metric_ids = tuple(metric.metric_id for metric in self.summary.metrics)
        if len(set(metric_ids)) != len(_METRIC_SPECS):
            raise ValueError("summary metric IDs must be unique")
        if any(
            metric.value is not None
            or metric.measured_cells != 0
            or metric.threshold_met is not None
            for metric in self.summary.metrics
        ):
            raise ValueError("not-performed results cannot contain measured metric claims")
        return self


class EvaluationVerification(StrictModel):
    verified: Literal[True] = True
    registry_id: Literal["legacy-migration-benchmark-v1"]
    case_count: Literal[6] = 6
    configuration_count: Literal[4] = 4
    repetitions: Literal[3] = 3
    cell_count: Literal[72] = 72
    measured_cell_count: Literal[0] = 0


ModelT = TypeVar("ModelT", bound=BaseModel)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise EvaluationVerificationError(f"duplicate JSON key: {key}")
        document[key] = value
    return document


def _load_document(path: str | Path, model_type: type[ModelT]) -> ModelT:
    location = Path(path)
    if location.is_symlink():
        raise EvaluationVerificationError(f"evaluation document cannot be a symlink: {location}")
    try:
        raw = location.read_bytes()
    except OSError as exc:
        raise EvaluationVerificationError(f"cannot read evaluation document: {location}") from exc
    if not raw or len(raw) > MAX_DOCUMENT_BYTES:
        raise EvaluationVerificationError(
            f"evaluation document size is outside the accepted range: {location}"
        )
    try:
        text = raw.decode("utf-8")
        json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        return model_type.model_validate_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise EvaluationVerificationError(f"invalid evaluation document: {location}") from exc


def load_registry(path: str | Path) -> EvaluationRegistry:
    """Load the exact, predeclared six-case benchmark registry."""

    return _load_document(path, EvaluationRegistry)


def load_results(path: str | Path) -> EvaluationResults:
    """Load the compact, explicitly not-performed 72-cell result snapshot."""

    return _load_document(path, EvaluationResults)


def expected_cell_id(case_id: str, config_id: str, repetition: int) -> str:
    """Return the canonical stable identity for one predeclared result cell."""

    return f"{case_id}--{config_id}--r{repetition}"


def verify_evaluation(
    registry: EvaluationRegistry,
    results: EvaluationResults,
) -> EvaluationVerification:
    """Verify the complete cross-product and all summary/metric invariants."""

    if results.registry_id != registry.registry_id:
        raise EvaluationVerificationError("results do not reference the loaded registry")

    expected_cells = tuple(
        (case.case_id, config.config_id, repetition)
        for case in registry.cases
        for config in registry.configurations
        for repetition in range(1, registry.repetitions + 1)
    )
    observed_cells = tuple(
        (cell.case_id, cell.config_id, cell.repetition) for cell in results.cells
    )
    if observed_cells != expected_cells:
        raise EvaluationVerificationError(
            "results do not contain the canonical 6 x 4 x 3 cross-product"
        )

    for cell in results.cells:
        expected_id = expected_cell_id(cell.case_id, cell.config_id, cell.repetition)
        if cell.cell_id != expected_id:
            raise EvaluationVerificationError(f"noncanonical evaluation cell ID: {cell.cell_id}")

    registry_metric_ids = tuple(metric.metric_id for metric in registry.metrics)
    result_metric_ids = tuple(metric.metric_id for metric in results.summary.metrics)
    if result_metric_ids != registry_metric_ids:
        raise EvaluationVerificationError("summary metrics do not match the predeclared registry")

    return EvaluationVerification(registry_id=registry.registry_id)


def load_and_verify(
    registry_path: str | Path,
    results_path: str | Path,
) -> EvaluationVerification:
    """Load and verify both compact artifacts without external calls."""

    return verify_evaluation(load_registry(registry_path), load_results(results_path))


# The small executable pilot below is intentionally separate from the fixed
# 72-cell benchmark above.  It measures only claims that the local controller
# can establish without a model, network, org, Mule runtime, or human approval.
# Two additional cells reserve an evidence-bound place for later user-run Qwen
# sessions; they remain not_performed until an existing terminal run is
# explicitly ingested.

PILOT_CELL_COUNT = 4


class PilotEvaluationMode(StrEnum):
    LOCAL_STATIC = "local_static"
    AGENT_RUN = "agent_run"


class PilotEvidenceKind(StrEnum):
    LOCAL_STATIC = "local_static"
    AGENT_RUN = "agent_run"


class PilotBoundaryState(StrEnum):
    NOT_INVOKED = "not_invoked"
    INVOKED = "invoked"
    UNKNOWN = "unknown"


class PilotClaim(StrEnum):
    SALESFORCE_STATIC_CONTRACT = "salesforce_candidate_static_contract_passed"
    SALESFORCE_DEPENDENCY_CLOSURE = "salesforce_dependency_closure_static_resolved"
    MULESOFT_STATIC_CONTRACT = "mulesoft_candidate_static_contract_passed"
    AGENT_READY_FOR_REVIEW = "agent_workflow_reached_ready_for_human_review"
    AGENT_FAILURE = "agent_workflow_terminal_failure_observed"
    AGENT_DECISION_REQUIRED = "agent_workflow_decision_required_observed"
    AGENT_ENVIRONMENT_UNAVAILABLE = "agent_workflow_environment_unavailable_observed"


class PilotResultReason(StrEnum):
    CONTROLLER_STATIC_CHECK = "controller_owned_static_check"
    AWAITING_QWEN_RUN = "awaiting_user_qwen_run"
    VERIFIED_AGENT_RUN = "verified_agent_run_status"


PilotRoleCall = Literal[
    "architect",
    "engineer-1",
    "validator-1",
    "engineer-2",
    "validator-2",
]


class PilotCase(StrictModel):
    case_id: Identifier
    platform: Platform
    evaluation_mode: PilotEvaluationMode
    source_path: str = Field(min_length=1, max_length=240)
    candidate_path: str | None = Field(default=None, max_length=240)
    fixture_contract_path: str = Field(min_length=1, max_length=240)
    expected_provider_id: str | None = Field(default=None, max_length=80)
    expected_model_id: str | None = Field(default=None, max_length=160)

    @field_validator("source_path", "fixture_contract_path")
    @classmethod
    def validate_required_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("candidate_path")
    @classmethod
    def validate_optional_path(cls, value: str | None) -> str | None:
        return validate_relative_path(value) if value is not None else None

    @model_validator(mode="after")
    def validate_mode_fields(self) -> PilotCase:
        if self.evaluation_mode is PilotEvaluationMode.LOCAL_STATIC:
            if self.candidate_path is None:
                raise ValueError("local-static pilot cases require a candidate path")
            if self.expected_provider_id is not None or self.expected_model_id is not None:
                raise ValueError("local-static pilot cases cannot name a model provider")
        else:
            if self.candidate_path is not None:
                raise ValueError("agent-run pilot cases receive candidates from the run")
            if not self.expected_provider_id or not self.expected_model_id:
                raise ValueError("agent-run pilot cases require an exact provider and model")
        return self


_EXPECTED_PILOT_CASES: tuple[
    tuple[str, Platform, PilotEvaluationMode, str, str | None, str, str | None, str | None],
    ...,
] = (
    (
        "salesforce-static-fixture-contract",
        Platform.SALESFORCE,
        PilotEvaluationMode.LOCAL_STATIC,
        "fixtures/salesforce/account-contact-explorer/input",
        "fixtures/salesforce/account-contact-explorer/expected",
        "fixtures/salesforce/account-contact-explorer/fixture.yaml",
        None,
        None,
    ),
    (
        "mulesoft-static-fixture-contract",
        Platform.MULESOFT,
        PilotEvaluationMode.LOCAL_STATIC,
        "fixtures/mulesoft/customer-status-api/input",
        "fixtures/mulesoft/customer-status-api/expected",
        "fixtures/mulesoft/customer-status-api/fixture.yaml",
        None,
        None,
    ),
    (
        "salesforce-qwen-end-to-end",
        Platform.SALESFORCE,
        PilotEvaluationMode.AGENT_RUN,
        "fixtures/salesforce/account-contact-explorer/input",
        None,
        "fixtures/salesforce/account-contact-explorer/fixture.yaml",
        "ollama",
        "qwen3.8:latest",
    ),
    (
        "mulesoft-qwen-end-to-end",
        Platform.MULESOFT,
        PilotEvaluationMode.AGENT_RUN,
        "fixtures/mulesoft/customer-status-api/input",
        None,
        "fixtures/mulesoft/customer-status-api/fixture.yaml",
        "ollama",
        "qwen3.8:latest",
    ),
)


class PilotEvaluationRegistry(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    registry_id: Literal["legacy-migration-pilot-v1"]
    declaration_status: Literal["predeclared_bounded_pilot"]
    require_full_completion: Literal[False]
    cases: tuple[PilotCase, ...] = Field(
        min_length=PILOT_CELL_COUNT,
        max_length=PILOT_CELL_COUNT,
    )

    @model_validator(mode="after")
    def validate_fixed_design(self) -> PilotEvaluationRegistry:
        observed = tuple(
            (
                case.case_id,
                case.platform,
                case.evaluation_mode,
                case.source_path,
                case.candidate_path,
                case.fixture_contract_path,
                case.expected_provider_id,
                case.expected_model_id,
            )
            for case in self.cases
        )
        if observed != _EXPECTED_PILOT_CASES:
            raise ValueError("pilot registry differs from the four predeclared bounded cases")
        return self


class PilotExecutionBoundary(StrictModel):
    provider: PilotBoundaryState
    external_platform: PilotBoundaryState
    authentication: PilotBoundaryState
    subprocess: PilotBoundaryState
    human_gate: PilotBoundaryState
    external_authority_granted: Literal[False] = False


class PilotArtifactBinding(StrictModel):
    role: Literal["source_tree", "candidate_tree", "fixture_contract", "run_evidence"]
    path: str = Field(min_length=1, max_length=300)
    artifact_kind: Literal["tree", "file"]
    digest: Sha256Digest

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class PilotLocalObservation(StrictModel):
    validator_id: Literal[
        "salesforce-candidate-static-v1",
        "mulesoft-candidate-static-v1",
    ]
    check_output_digest: Sha256Digest
    tests_executed: Literal[False] = False
    model_quality_evaluated: Literal[False] = False
    semantic_conformance_evaluated: Literal[False] = False
    external_platform_evaluated: Literal[False] = False


class PilotAgentRunObservation(StrictModel):
    run_id: str = Field(min_length=1, max_length=120)
    thread_id: str = Field(min_length=1, max_length=120)
    request_id: str = Field(min_length=1, max_length=120)
    request_digest: Sha256Digest
    provider_id: str = Field(min_length=1, max_length=80)
    model_id: str = Field(min_length=1, max_length=160)
    workflow_status: str = Field(min_length=1, max_length=80)
    terminal_disposition: str = Field(min_length=1, max_length=80)
    execution_attempt: int = Field(ge=1, le=2)
    run_status_digest: Sha256Digest
    manifest_gate_performed: bool
    completed_role_calls: tuple[PilotRoleCall, ...] = Field(min_length=1, max_length=5)
    completed_model_calls: int = Field(ge=1, le=5)
    completed_model_latency_ms: int = Field(ge=0)
    token_usage_complete: bool
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    model_quality_evaluated: Literal[False] = False
    semantic_conformance_evaluated: Literal[False] = False
    external_platform_evaluated: Literal[False] = False
    human_acceptance_evaluated: Literal[False] = False

    @model_validator(mode="after")
    def validate_model_telemetry(self) -> PilotAgentRunObservation:
        if self.completed_model_calls != len(self.completed_role_calls):
            raise ValueError("completed model-call count differs from role-call inventory")
        if len(self.completed_role_calls) != len(set(self.completed_role_calls)):
            raise ValueError("completed role-call inventory must be unique")
        tokens = (self.input_tokens, self.output_tokens, self.total_tokens)
        if self.token_usage_complete:
            if any(value is None for value in tokens):
                raise ValueError("complete token telemetry requires all token counts")
            assert self.input_tokens is not None
            assert self.output_tokens is not None
            assert self.total_tokens is not None
            if self.total_tokens != self.input_tokens + self.output_tokens:
                raise ValueError("agent observation total tokens must equal input plus output")
        elif any(value is not None for value in tokens):
            raise ValueError("incomplete token telemetry cannot claim token counts")
        return self


class PilotEvidenceReceipt(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    receipt_id: Identifier
    registry_id: Literal["legacy-migration-pilot-v1"]
    registry_digest: Sha256Digest
    cell_id: Identifier
    case_id: Identifier
    platform: Platform
    evidence_kind: PilotEvidenceKind
    status: ResultStatus
    claims: tuple[PilotClaim, ...] = Field(min_length=1, max_length=4)
    bindings: tuple[PilotArtifactBinding, ...] = Field(min_length=3, max_length=4)
    boundary: PilotExecutionBoundary
    local_observation: PilotLocalObservation | None = None
    agent_run_observation: PilotAgentRunObservation | None = None
    limitations: tuple[str, ...] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def validate_evidence_kind(self) -> PilotEvidenceReceipt:
        if self.status is ResultStatus.NOT_PERFORMED:
            raise ValueError("not-performed cells cannot have an evidence receipt")
        roles = tuple(binding.role for binding in self.bindings)
        if len(roles) != len(set(roles)):
            raise ValueError("receipt artifact-binding roles must be unique")
        if self.evidence_kind is PilotEvidenceKind.LOCAL_STATIC:
            if self.local_observation is None or self.agent_run_observation is not None:
                raise ValueError("local-static receipts require exactly one local observation")
            if set(roles) != {"source_tree", "candidate_tree", "fixture_contract"}:
                raise ValueError("local-static receipt bindings are incomplete")
            expected_boundary = PilotExecutionBoundary(
                provider=PilotBoundaryState.NOT_INVOKED,
                external_platform=PilotBoundaryState.NOT_INVOKED,
                authentication=PilotBoundaryState.NOT_INVOKED,
                subprocess=PilotBoundaryState.NOT_INVOKED,
                human_gate=PilotBoundaryState.NOT_INVOKED,
                external_authority_granted=False,
            )
            if self.boundary != expected_boundary:
                raise ValueError("local-static receipts cannot claim external execution")
        else:
            if self.agent_run_observation is None or self.local_observation is not None:
                raise ValueError("agent-run receipts require exactly one agent observation")
            if set(roles) != {"source_tree", "fixture_contract", "run_evidence"}:
                raise ValueError("agent-run receipt bindings are incomplete")
            if self.boundary.provider is not PilotBoundaryState.INVOKED:
                raise ValueError("agent-run receipt must record its existing provider invocation")
            if (
                self.boundary.external_platform is not PilotBoundaryState.UNKNOWN
                or self.boundary.authentication is not PilotBoundaryState.UNKNOWN
                or self.boundary.subprocess is not PilotBoundaryState.UNKNOWN
            ):
                raise ValueError(
                    "portable agent receipts cannot infer platform, authentication, or subprocess boundaries"
                )
        return self


class PilotCellResult(StrictModel):
    cell_id: Identifier
    case_id: Identifier
    status: ResultStatus
    reason: PilotResultReason
    evidence_receipt_path: str | None = Field(default=None, max_length=240)
    evidence_receipt_digest: Sha256Digest | None = None

    @field_validator("evidence_receipt_path")
    @classmethod
    def validate_receipt_path(cls, value: str | None) -> str | None:
        return validate_relative_path(value) if value is not None else None

    @model_validator(mode="after")
    def validate_measurement_binding(self) -> PilotCellResult:
        has_receipt = (
            self.evidence_receipt_path is not None and self.evidence_receipt_digest is not None
        )
        if (self.evidence_receipt_path is None) != (self.evidence_receipt_digest is None):
            raise ValueError("receipt path and digest must be present together")
        if self.status is ResultStatus.NOT_PERFORMED:
            if has_receipt or self.reason is not PilotResultReason.AWAITING_QWEN_RUN:
                raise ValueError("not-performed pilot cells cannot claim evidence")
        elif not has_receipt:
            raise ValueError("measured pilot cells require an evidence receipt")
        return self


class PilotEvaluationSummary(StrictModel):
    planned_cells: Literal[4] = 4
    recorded_cells: Literal[4] = 4
    measured_cells: int = Field(ge=0, le=PILOT_CELL_COUNT)
    status_counts: StatusCounts
    local_static_cells_planned: Literal[2] = 2
    local_static_cells_passed: int = Field(ge=0, le=2)
    agent_cells_planned: Literal[2] = 2
    agent_cells_measured: int = Field(ge=0, le=2)
    complete: bool
    model_quality_evaluated: Literal[False] = False
    external_platform_evaluated: Literal[False] = False
    passed: Literal[False] = False


class PilotEvaluationResults(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    results_id: Identifier
    registry_id: Literal["legacy-migration-pilot-v1"]
    registry_digest: Sha256Digest
    snapshot_sequence: int = Field(ge=1, le=16)
    parent_results_digest: Sha256Digest | None = None
    snapshot_status: Literal["partially_measured", "complete"]
    cells: tuple[PilotCellResult, ...] = Field(
        min_length=PILOT_CELL_COUNT,
        max_length=PILOT_CELL_COUNT,
    )
    summary: PilotEvaluationSummary
    limitations: tuple[str, ...] = Field(min_length=3, max_length=8)

    @model_validator(mode="after")
    def validate_summary(self) -> PilotEvaluationResults:
        if len({cell.cell_id for cell in self.cells}) != PILOT_CELL_COUNT:
            raise ValueError("pilot cell IDs must be unique")
        counts = {status: 0 for status in ResultStatus}
        for cell in self.cells:
            counts[cell.status] += 1
        expected_counts = StatusCounts(
            succeeded=counts[ResultStatus.SUCCEEDED],
            failed=counts[ResultStatus.FAILED],
            decision_required=counts[ResultStatus.DECISION_REQUIRED],
            unavailable=counts[ResultStatus.UNAVAILABLE],
            not_performed=counts[ResultStatus.NOT_PERFORMED],
        )
        measured = PILOT_CELL_COUNT - counts[ResultStatus.NOT_PERFORMED]
        local_passed = sum(
            cell.status is ResultStatus.SUCCEEDED
            for cell in self.cells
            if cell.case_id.endswith("static-fixture-contract")
        )
        agent_measured = sum(
            cell.status is not ResultStatus.NOT_PERFORMED
            for cell in self.cells
            if cell.case_id.endswith("qwen-end-to-end")
        )
        if self.summary.status_counts != expected_counts:
            raise ValueError("pilot summary status counts do not match cells")
        if self.summary.measured_cells != measured:
            raise ValueError("pilot summary measured-cell count does not match cells")
        if self.summary.local_static_cells_passed != local_passed:
            raise ValueError("pilot summary local-static count does not match cells")
        if self.summary.agent_cells_measured != agent_measured:
            raise ValueError("pilot summary agent-cell count does not match cells")
        complete = counts[ResultStatus.NOT_PERFORMED] == 0
        if self.summary.complete != complete:
            raise ValueError("pilot summary completion does not match cells")
        if self.snapshot_status != ("complete" if complete else "partially_measured"):
            raise ValueError("pilot snapshot status does not match completion")
        if self.snapshot_sequence == 1 and self.parent_results_digest is not None:
            raise ValueError("initial pilot snapshot cannot have a parent")
        if self.snapshot_sequence > 1 and self.parent_results_digest is None:
            raise ValueError("subsequent pilot snapshots require an exact parent digest")
        return self


class PilotEvaluationVerification(StrictModel):
    verified: Literal[True] = True
    registry_id: Literal["legacy-migration-pilot-v1"]
    planned_cells: Literal[4] = 4
    measured_cells: int = Field(ge=0, le=PILOT_CELL_COUNT)
    not_performed_cells: int = Field(ge=0, le=PILOT_CELL_COUNT)
    local_static_receipts_reexecuted: int = Field(ge=0, le=2)
    agent_run_receipts_verified: int = Field(ge=0, le=2)
    agent_run_sources_reverified: int = Field(ge=0, le=2)
    portable_agent_receipts_only: bool
    complete: bool


def load_pilot_registry(path: str | Path) -> PilotEvaluationRegistry:
    """Load the exact bounded pilot registry without executing any case."""

    return _load_document(path, PilotEvaluationRegistry)


def load_pilot_results(path: str | Path) -> PilotEvaluationResults:
    """Load one immutable pilot result snapshot."""

    return _load_document(path, PilotEvaluationResults)


def load_pilot_receipt(path: str | Path) -> PilotEvidenceReceipt:
    """Load one strict evidence receipt."""

    return _load_document(path, PilotEvidenceReceipt)


def _pilot_file_digest(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise EvaluationVerificationError("pilot artifact is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise EvaluationVerificationError("pilot file artifact must be a regular file")
    if metadata.st_size < 1 or metadata.st_size > MAX_DOCUMENT_BYTES:
        raise EvaluationVerificationError("pilot file artifact has an invalid size")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise EvaluationVerificationError("pilot artifact cannot be read") from exc
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _pilot_project_root(path: Path) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise EvaluationVerificationError("pilot project root is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise EvaluationVerificationError("pilot project root must be a non-symlink directory")
    return resolved


def _binding(
    project_root: Path,
    *,
    role: Literal["source_tree", "candidate_tree", "fixture_contract", "run_evidence"],
    relative_path: str,
    artifact_kind: Literal["tree", "file"],
) -> PilotArtifactBinding:
    normalized = validate_relative_path(relative_path)
    location = project_root.joinpath(*normalized.split("/"))
    digest = content_revision(location) if artifact_kind == "tree" else _pilot_file_digest(location)
    return PilotArtifactBinding(
        role=role,
        path=normalized,
        artifact_kind=artifact_kind,
        digest=digest,
    )


def _local_boundary() -> PilotExecutionBoundary:
    return PilotExecutionBoundary(
        provider=PilotBoundaryState.NOT_INVOKED,
        external_platform=PilotBoundaryState.NOT_INVOKED,
        authentication=PilotBoundaryState.NOT_INVOKED,
        subprocess=PilotBoundaryState.NOT_INVOKED,
        human_gate=PilotBoundaryState.NOT_INVOKED,
        external_authority_granted=False,
    )


def _measure_local_case(
    project_root: Path,
    registry: PilotEvaluationRegistry,
    case: PilotCase,
) -> PilotEvidenceReceipt:
    if case.evaluation_mode is not PilotEvaluationMode.LOCAL_STATIC:
        raise EvaluationVerificationError("only local-static pilot cases can run provider-free")
    assert case.candidate_path is not None
    source = project_root.joinpath(*case.source_path.split("/"))
    candidate = project_root.joinpath(*case.candidate_path.split("/"))
    source_before = content_revision(source)
    candidate_before = content_revision(candidate)

    if case.platform is Platform.SALESFORCE:
        from legacy_migration_agent.platforms.local_checks import (
            check_dependency_closure,
            check_salesforce_candidate,
        )

        candidate_check = check_salesforce_candidate(candidate)
        dependency_check, _ = check_dependency_closure(candidate)
        check_output: Any = {
            "candidate_contract": candidate_check,
            "dependency_closure": dependency_check,
        }
        validator_id: Literal[
            "salesforce-candidate-static-v1",
            "mulesoft-candidate-static-v1",
        ] = "salesforce-candidate-static-v1"
        claims: tuple[PilotClaim, ...] = (
            PilotClaim.SALESFORCE_STATIC_CONTRACT,
            PilotClaim.SALESFORCE_DEPENDENCY_CLOSURE,
        )
    else:
        from legacy_migration_agent.platforms.mulesoft_local_checks import (
            check_mulesoft_candidate,
        )

        mule_check = check_mulesoft_candidate(candidate, source)
        check_output = mule_check.model_dump(mode="json")
        validator_id = "mulesoft-candidate-static-v1"
        claims = (PilotClaim.MULESOFT_STATIC_CONTRACT,)

    if content_revision(source) != source_before or content_revision(candidate) != candidate_before:
        raise EvaluationVerificationError("provider-free pilot check mutated a bound fixture tree")

    receipt = PilotEvidenceReceipt(
        receipt_id=f"receipt-{case.case_id}",
        registry_id=registry.registry_id,
        registry_digest=artifact_digest(registry),
        cell_id=case.case_id,
        case_id=case.case_id,
        platform=case.platform,
        evidence_kind=PilotEvidenceKind.LOCAL_STATIC,
        status=ResultStatus.SUCCEEDED,
        claims=claims,
        bindings=(
            _binding(
                project_root,
                role="source_tree",
                relative_path=case.source_path,
                artifact_kind="tree",
            ),
            _binding(
                project_root,
                role="candidate_tree",
                relative_path=case.candidate_path,
                artifact_kind="tree",
            ),
            _binding(
                project_root,
                role="fixture_contract",
                relative_path=case.fixture_contract_path,
                artifact_kind="file",
            ),
        ),
        boundary=_local_boundary(),
        local_observation=PilotLocalObservation(
            validator_id=validator_id,
            check_output_digest=artifact_digest(check_output),
        ),
        limitations=(
            "This receipt establishes only controller-owned static fixture claims.",
            "No model-quality, semantic-acceptance, external-platform, deployment, or production claim was evaluated.",
            "Prepared Apex, Jest, Maven, and MUnit sources are not evidence that those runtimes executed.",
        ),
    )
    return receipt


def _pilot_summary(cells: tuple[PilotCellResult, ...]) -> PilotEvaluationSummary:
    counts = {status: 0 for status in ResultStatus}
    for cell in cells:
        counts[cell.status] += 1
    return PilotEvaluationSummary(
        measured_cells=PILOT_CELL_COUNT - counts[ResultStatus.NOT_PERFORMED],
        status_counts=StatusCounts(
            succeeded=counts[ResultStatus.SUCCEEDED],
            failed=counts[ResultStatus.FAILED],
            decision_required=counts[ResultStatus.DECISION_REQUIRED],
            unavailable=counts[ResultStatus.UNAVAILABLE],
            not_performed=counts[ResultStatus.NOT_PERFORMED],
        ),
        local_static_cells_passed=sum(
            cell.status is ResultStatus.SUCCEEDED
            for cell in cells
            if cell.case_id.endswith("static-fixture-contract")
        ),
        agent_cells_measured=sum(
            cell.status is not ResultStatus.NOT_PERFORMED
            for cell in cells
            if cell.case_id.endswith("qwen-end-to-end")
        ),
        complete=counts[ResultStatus.NOT_PERFORMED] == 0,
    )


def build_local_pilot_results(
    project_root: Path,
    registry: PilotEvaluationRegistry,
) -> tuple[PilotEvaluationResults, tuple[PilotEvidenceReceipt, ...]]:
    """Execute only the two provider-free static cells in registry order."""

    root = _pilot_project_root(project_root)
    receipts: list[PilotEvidenceReceipt] = []
    cells: list[PilotCellResult] = []
    for case in registry.cases:
        if case.evaluation_mode is PilotEvaluationMode.LOCAL_STATIC:
            receipt = _measure_local_case(root, registry, case)
            receipts.append(receipt)
            cells.append(
                PilotCellResult(
                    cell_id=case.case_id,
                    case_id=case.case_id,
                    status=receipt.status,
                    reason=PilotResultReason.CONTROLLER_STATIC_CHECK,
                    evidence_receipt_path=f"evidence/{case.case_id}.json",
                    evidence_receipt_digest=artifact_digest(receipt),
                )
            )
        else:
            cells.append(
                PilotCellResult(
                    cell_id=case.case_id,
                    case_id=case.case_id,
                    status=ResultStatus.NOT_PERFORMED,
                    reason=PilotResultReason.AWAITING_QWEN_RUN,
                )
            )
    ordered_cells = tuple(cells)
    return (
        PilotEvaluationResults(
            results_id="legacy-migration-pilot-v1-local-current",
            registry_id=registry.registry_id,
            registry_digest=artifact_digest(registry),
            snapshot_sequence=1,
            parent_results_digest=None,
            snapshot_status="partially_measured",
            cells=ordered_cells,
            summary=_pilot_summary(ordered_cells),
            limitations=(
                "Two local-static cells were measured; both Qwen end-to-end cells remain not_performed.",
                "Static success does not establish model quality, behavioral equivalence, runtime execution, org validation, deployment readiness, or production readiness.",
                "The fixed 72-cell benchmark-v1 remains a separate not_performed baseline.",
            ),
        ),
        tuple(receipts),
    )


def write_local_pilot_snapshot(
    project_root: Path,
    registry_path: Path,
    output_dir: Path,
) -> PilotEvaluationResults:
    """Write an immutable, self-contained local pilot snapshot."""

    registry = load_pilot_registry(registry_path)
    results, receipts = build_local_pilot_results(project_root, registry)
    store = ArtifactStore(output_dir)
    for receipt in receipts:
        store.write_json(f"evidence/{receipt.case_id}.json", receipt)
    store.write_json("results.json", results)
    return results


def verify_pilot_evaluation(
    project_root: Path,
    registry: PilotEvaluationRegistry,
    results: PilotEvaluationResults,
    snapshot_dir: Path,
) -> PilotEvaluationVerification:
    """Re-execute static receipts and validate all result/evidence bindings."""

    root = _pilot_project_root(project_root)
    safe_snapshot = _pilot_project_root(snapshot_dir)
    if results.registry_id != registry.registry_id:
        raise EvaluationVerificationError("pilot results reference a different registry")
    if results.registry_digest != artifact_digest(registry):
        raise EvaluationVerificationError("pilot results registry digest does not match")
    expected_cells = tuple(case.case_id for case in registry.cases)
    observed_cells = tuple(cell.case_id for cell in results.cells)
    if (
        observed_cells != expected_cells
        or tuple(cell.cell_id for cell in results.cells) != expected_cells
    ):
        raise EvaluationVerificationError("pilot results are not in canonical registry order")

    case_by_id = {case.case_id: case for case in registry.cases}
    local_reexecuted = 0
    agent_verified = 0
    agent_sources_reverified = 0
    for cell in results.cells:
        if cell.status is ResultStatus.NOT_PERFORMED:
            continue
        assert cell.evidence_receipt_path is not None
        assert cell.evidence_receipt_digest is not None
        receipt_path = safe_snapshot.joinpath(*cell.evidence_receipt_path.split("/"))
        receipt = load_pilot_receipt(receipt_path)
        if artifact_digest(receipt) != cell.evidence_receipt_digest:
            raise EvaluationVerificationError("pilot evidence receipt digest does not match")
        case = case_by_id[cell.case_id]
        expected_reason = (
            PilotResultReason.CONTROLLER_STATIC_CHECK
            if case.evaluation_mode is PilotEvaluationMode.LOCAL_STATIC
            else PilotResultReason.VERIFIED_AGENT_RUN
        )
        if cell.reason is not expected_reason:
            raise EvaluationVerificationError("pilot cell reason differs from its case mode")
        if (
            receipt.registry_id != registry.registry_id
            or receipt.registry_digest != artifact_digest(registry)
            or receipt.cell_id != cell.cell_id
            or receipt.case_id != case.case_id
            or receipt.platform is not case.platform
            or receipt.status is not cell.status
        ):
            raise EvaluationVerificationError("pilot evidence receipt identity does not match cell")
        if receipt.evidence_kind is PilotEvidenceKind.LOCAL_STATIC:
            expected_receipt = _measure_local_case(root, registry, case)
            if receipt != expected_receipt:
                raise EvaluationVerificationError("local pilot receipt differs from re-execution")
            local_reexecuted += 1
        else:
            if case.evaluation_mode is not PilotEvaluationMode.AGENT_RUN:
                raise EvaluationVerificationError("agent receipt is attached to a static case")
            observation = receipt.agent_run_observation
            assert observation is not None
            if (
                observation.provider_id != case.expected_provider_id
                or observation.model_id != case.expected_model_id
            ):
                raise EvaluationVerificationError("agent receipt provider/model differs from case")
            expected_status, expected_claim = _agent_result_from_disposition(
                observation.workflow_status,
                observation.terminal_disposition,
                failed=observation.workflow_status == "failed",
            )
            if receipt.status is not expected_status or receipt.claims != (expected_claim,):
                raise EvaluationVerificationError("agent receipt overstates its workflow outcome")
            expected_human_gate = (
                PilotBoundaryState.INVOKED
                if observation.manifest_gate_performed
                else PilotBoundaryState.NOT_INVOKED
            )
            if receipt.boundary.human_gate is not expected_human_gate:
                raise EvaluationVerificationError(
                    "agent receipt human-gate boundary is inconsistent"
                )
            bindings = {binding.role: binding for binding in receipt.bindings}
            if bindings["source_tree"] != _binding(
                root,
                role="source_tree",
                relative_path=case.source_path,
                artifact_kind="tree",
            ) or bindings["fixture_contract"] != _binding(
                root,
                role="fixture_contract",
                relative_path=case.fixture_contract_path,
                artifact_kind="file",
            ):
                raise EvaluationVerificationError("agent receipt fixture binding does not match")
            run_binding = bindings["run_evidence"]
            if not run_binding.path.startswith(".runs/"):
                raise EvaluationVerificationError("agent receipt run evidence is outside .runs")
            run_evidence = root.joinpath(*run_binding.path.split("/"))
            if run_evidence.exists():
                expected_agent_receipt = build_agent_run_receipt(
                    root,
                    registry,
                    case_id=case.case_id,
                    run_dir=run_evidence.parent,
                    run_id=observation.run_id,
                    thread_id=observation.thread_id,
                )
                if receipt != expected_agent_receipt:
                    raise EvaluationVerificationError(
                        "agent receipt differs from source-run revalidation"
                    )
                agent_sources_reverified += 1
            agent_verified += 1

    measured = results.summary.measured_cells
    return PilotEvaluationVerification(
        registry_id=registry.registry_id,
        measured_cells=measured,
        not_performed_cells=PILOT_CELL_COUNT - measured,
        local_static_receipts_reexecuted=local_reexecuted,
        agent_run_receipts_verified=agent_verified,
        agent_run_sources_reverified=agent_sources_reverified,
        portable_agent_receipts_only=agent_verified > agent_sources_reverified,
        complete=results.summary.complete,
    )


def load_and_verify_pilot(
    project_root: Path,
    registry_path: Path,
    snapshot_dir: Path,
) -> PilotEvaluationVerification:
    """Load and verify a complete pilot snapshot directory."""

    registry = load_pilot_registry(registry_path)
    safe_snapshot = _pilot_project_root(snapshot_dir)
    results = load_pilot_results(safe_snapshot / "results.json")
    return verify_pilot_evaluation(project_root, registry, results, safe_snapshot)


def _agent_result_from_disposition(
    workflow_status: str,
    terminal_disposition: str,
    *,
    failed: bool,
) -> tuple[ResultStatus, PilotClaim]:
    if failed or terminal_disposition in {"controlled_failure", "plan_invalid"}:
        return ResultStatus.FAILED, PilotClaim.AGENT_FAILURE
    if terminal_disposition == "ready_for_human_review":
        return ResultStatus.SUCCEEDED, PilotClaim.AGENT_READY_FOR_REVIEW
    if terminal_disposition in {"decision_required", "recoverable_failure"}:
        return ResultStatus.DECISION_REQUIRED, PilotClaim.AGENT_DECISION_REQUIRED
    if terminal_disposition == "environment_unavailable":
        return ResultStatus.UNAVAILABLE, PilotClaim.AGENT_ENVIRONMENT_UNAVAILABLE
    raise EvaluationVerificationError(
        f"agent run is not in a supported terminal disposition: {workflow_status}"
    )


def _project_relative_directory(project_root: Path, directory: Path) -> tuple[Path, str]:
    try:
        metadata = directory.lstat()
        resolved = directory.resolve(strict=True)
        relative = resolved.relative_to(project_root).as_posix()
    except (OSError, ValueError) as exc:
        raise EvaluationVerificationError(
            "agent run directory must be a non-symlink child of the project root"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise EvaluationVerificationError("agent run directory must be a regular directory")
    return resolved, validate_relative_path(relative)


def _model_call_from_run_artifact(path: Path) -> Any:
    """Load only the typed model-call record from a verified run envelope."""

    from legacy_migration_agent.agent_runtime.openai_model import ModelCallRecord

    _pilot_file_digest(path)
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        if not isinstance(document, dict) or not isinstance(document.get("model_call"), dict):
            raise ValueError("missing model-call record")
        nested = json.dumps(document["model_call"], separators=(",", ":"))
        return ModelCallRecord.model_validate_json(nested)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise EvaluationVerificationError("agent run model-call evidence is invalid") from exc


def _collect_agent_model_telemetry(
    model_root: Path,
    *,
    provider_id: str,
    model_id: str,
) -> tuple[tuple[PilotRoleCall, ...], int, bool, int | None, int | None, int | None]:
    candidates: tuple[tuple[PilotRoleCall, str, str], ...] = (
        ("architect", "architect.json", "architect/"),
        ("engineer-1", "engineer-attempt-1.json", "engineer/"),
        ("validator-1", "validator-attempt-1.json", "validator/"),
        ("engineer-2", "engineer-attempt-2.json", "engineer/"),
        ("validator-2", "validator-attempt-2.json", "validator/"),
    )
    roles: list[PilotRoleCall] = []
    latency_ms = 0
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    tokens_complete = True
    for role, filename, version_prefix in candidates:
        path = model_root / filename
        if not path.exists():
            continue
        call = _model_call_from_run_artifact(path)
        if (
            call.provider != provider_id
            or call.model_id != model_id
            or call.resolved_execution_boundary != "local_loopback"
            or not call.agent_version.startswith(version_prefix)
            or call.usage is None
        ):
            raise EvaluationVerificationError("agent model-call telemetry identity is invalid")
        roles.append(role)
        latency_ms += call.usage.latency_ms
        if call.usage.provider_usage_reported:
            assert call.usage.input_tokens is not None
            assert call.usage.output_tokens is not None
            assert call.usage.total_tokens is not None
            input_tokens += call.usage.input_tokens
            output_tokens += call.usage.output_tokens
            total_tokens += call.usage.total_tokens
        else:
            tokens_complete = False
    if not roles or roles[0] != "architect":
        raise EvaluationVerificationError("agent run lacks a completed Architect model call")
    return (
        tuple(roles),
        latency_ms,
        tokens_complete,
        input_tokens if tokens_complete else None,
        output_tokens if tokens_complete else None,
        total_tokens if tokens_complete else None,
    )


def build_agent_run_receipt(
    project_root: Path,
    registry: PilotEvaluationRegistry,
    *,
    case_id: str,
    run_dir: Path,
    run_id: str,
    thread_id: str,
) -> PilotEvidenceReceipt:
    """Verify and ingest an existing terminal run without invoking its provider.

    This reads the durable run through the normal exact-thread verifier. It
    does not call Ollama, Salesforce, Mule, Maven, MUnit, npm, or a network.
    A ready-for-human-review disposition is recorded as workflow success only;
    semantic acceptance and external-platform execution remain explicitly
    unevaluated.
    """

    root = _pilot_project_root(project_root)
    try:
        case = next(item for item in registry.cases if item.case_id == case_id)
    except StopIteration as exc:
        raise EvaluationVerificationError(
            "agent observation references an unknown pilot case"
        ) from exc
    if case.evaluation_mode is not PilotEvaluationMode.AGENT_RUN:
        raise EvaluationVerificationError("agent observations require an agent-run pilot case")

    resolved_run, run_relative = _project_relative_directory(root, run_dir)
    from legacy_migration_agent.application.agent_run import get_agent_run_status
    from legacy_migration_agent.contracts import MigrationRequest

    status = get_agent_run_status(
        root,
        resolved_run,
        run_id=run_id,
        thread_id=thread_id,
    )
    request = _load_document(resolved_run / "evidence/request.json", MigrationRequest)
    if (
        status.platform.value != case.platform.value
        or status.provider_id != case.expected_provider_id
        or status.model_id != case.expected_model_id
        or request.request_id != status.request_id
        or artifact_digest(request) != status.request_digest
    ):
        raise EvaluationVerificationError("agent run identity differs from the pilot case")
    expected_source_revision = content_revision(root.joinpath(*case.source_path.split("/")))
    if request.base_revision != expected_source_revision:
        raise EvaluationVerificationError("agent run is not bound to the selected pilot fixture")
    if status.pending_nodes or status.interrupt is not None or status.terminal_disposition is None:
        raise EvaluationVerificationError("only an exact terminal agent run can be ingested")

    model_root = resolved_run / "evidence/model-runs" / request.request_id
    (
        completed_roles,
        completed_latency_ms,
        token_usage_complete,
        input_tokens,
        output_tokens,
        total_tokens,
    ) = _collect_agent_model_telemetry(
        model_root,
        provider_id=status.provider_id,
        model_id=status.model_id,
    )
    result_status, claim = _agent_result_from_disposition(
        status.status,
        status.terminal_disposition,
        failed=status.failure is not None,
    )
    manifest_gate = (resolved_run / "evidence/control/manifest-approval.json").is_file()
    evidence_relative = f"{run_relative}/evidence"
    status_digest = artifact_digest(status)
    return PilotEvidenceReceipt(
        receipt_id=f"receipt-{case.case_id}-{status_digest[7:23]}",
        registry_id=registry.registry_id,
        registry_digest=artifact_digest(registry),
        cell_id=case.case_id,
        case_id=case.case_id,
        platform=case.platform,
        evidence_kind=PilotEvidenceKind.AGENT_RUN,
        status=result_status,
        claims=(claim,),
        bindings=(
            _binding(
                root,
                role="source_tree",
                relative_path=case.source_path,
                artifact_kind="tree",
            ),
            _binding(
                root,
                role="fixture_contract",
                relative_path=case.fixture_contract_path,
                artifact_kind="file",
            ),
            _binding(
                root,
                role="run_evidence",
                relative_path=evidence_relative,
                artifact_kind="tree",
            ),
        ),
        boundary=PilotExecutionBoundary(
            provider=PilotBoundaryState.INVOKED,
            external_platform=PilotBoundaryState.UNKNOWN,
            authentication=PilotBoundaryState.UNKNOWN,
            subprocess=PilotBoundaryState.UNKNOWN,
            human_gate=(
                PilotBoundaryState.INVOKED if manifest_gate else PilotBoundaryState.NOT_INVOKED
            ),
            external_authority_granted=False,
        ),
        agent_run_observation=PilotAgentRunObservation(
            run_id=status.run_id,
            thread_id=status.thread_id,
            request_id=status.request_id,
            request_digest=status.request_digest,
            provider_id=status.provider_id,
            model_id=status.model_id,
            workflow_status=status.status,
            terminal_disposition=status.terminal_disposition,
            execution_attempt=status.execution_attempt,
            run_status_digest=status_digest,
            manifest_gate_performed=manifest_gate,
            completed_role_calls=completed_roles,
            completed_model_calls=len(completed_roles),
            completed_model_latency_ms=completed_latency_ms,
            token_usage_complete=token_usage_complete,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        ),
        limitations=(
            "The receipt records a verified durable agent-run status, not an independent semantic review of generated code.",
            "External-platform execution and authentication are not inferred from the portable status and remain unknown.",
            "Model latency is the sum of completed structured role-call telemetry, not end-to-end wall-clock latency.",
            "The complete source run remains under .runs; portable verification reports separately whether that source evidence was available for digest revalidation.",
            "Ready-for-human-review means controller-owned required checks passed; it is not deployment or production approval.",
        ),
    )


def write_agent_run_pilot_snapshot(
    project_root: Path,
    registry_path: Path,
    baseline_snapshot_dir: Path,
    output_dir: Path,
    *,
    results_id: str,
    case_id: str,
    run_dir: Path,
    run_id: str,
    thread_id: str,
) -> PilotEvaluationResults:
    """Create a new immutable snapshot with one verified terminal Qwen cell."""

    registry = load_pilot_registry(registry_path)
    baseline = load_pilot_results(baseline_snapshot_dir / "results.json")
    verify_pilot_evaluation(project_root, registry, baseline, baseline_snapshot_dir)
    receipt = build_agent_run_receipt(
        project_root,
        registry,
        case_id=case_id,
        run_dir=run_dir,
        run_id=run_id,
        thread_id=thread_id,
    )
    cells: list[PilotCellResult] = []
    replaced = False
    for cell in baseline.cells:
        if cell.case_id != case_id:
            cells.append(cell)
            continue
        if cell.status is not ResultStatus.NOT_PERFORMED:
            raise EvaluationVerificationError("pilot agent cell already contains a measurement")
        replaced = True
        cells.append(
            PilotCellResult(
                cell_id=cell.cell_id,
                case_id=cell.case_id,
                status=receipt.status,
                reason=PilotResultReason.VERIFIED_AGENT_RUN,
                evidence_receipt_path=f"evidence/{cell.case_id}.json",
                evidence_receipt_digest=artifact_digest(receipt),
            )
        )
    if not replaced:
        raise EvaluationVerificationError("pilot agent cell is not present in baseline")
    ordered_cells = tuple(cells)
    complete = all(cell.status is not ResultStatus.NOT_PERFORMED for cell in ordered_cells)
    results = PilotEvaluationResults(
        results_id=results_id,
        registry_id=registry.registry_id,
        registry_digest=artifact_digest(registry),
        snapshot_sequence=baseline.snapshot_sequence + 1,
        parent_results_digest=artifact_digest(baseline),
        snapshot_status="complete" if complete else "partially_measured",
        cells=ordered_cells,
        summary=_pilot_summary(ordered_cells),
        limitations=(
            "Only explicitly evidenced cells are measured; remaining agent cells are not_performed.",
            "Agent workflow status does not establish independent semantic acceptance, deployment readiness, external-platform success, or production readiness.",
            "The fixed 72-cell benchmark-v1 remains a separate not_performed baseline.",
        ),
    )
    store = ArtifactStore(output_dir)
    for cell in baseline.cells:
        if cell.status is ResultStatus.NOT_PERFORMED:
            continue
        assert cell.evidence_receipt_path is not None
        prior = load_pilot_receipt(
            baseline_snapshot_dir.joinpath(*cell.evidence_receipt_path.split("/"))
        )
        store.write_json(cell.evidence_receipt_path, prior)
    store.write_json(f"evidence/{case_id}.json", receipt)
    store.write_json("results.json", results)
    return results
