"""Immutable public contracts for the interactive Agent UI."""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal, TypeAlias

from pydantic import ConfigDict, Field

from legacy_migration_agent.agent_runtime.correction import CorrectionAction
from legacy_migration_agent.contracts import StrictModel

AgentStageState = Literal["pending", "active", "complete", "blocked"]
AgentFailureReasonCode = Literal[
    "configuration_invalid",
    "provider_refusal",
    "response_incomplete",
    "structured_output_invalid",
    "unauthorized_tool_call",
    "model_inventory_invalid",
    "provider_response_invalid",
    "required_approval_missing",
    "implementation_contract_invalid",
    "transformation_scope_invalid",
    "unresolved_question_risk_missing",
    "correction_delta_required",
    "correction_no_material_changes",
    "correction_signal_coverage_missing",
    "correction_scope_invalid",
    "correction_identical_candidate",
    "file_plan_scope_mismatch",
    "file_plan_delta_mismatch",
    "workspace_scope_mismatch",
    "workspace_not_clean",
    "attempt_two_scope_expansion_invalid",
    "policy_rejected",
    "provider_timeout",
    "provider_unavailable",
    "deterministic_validation_failed",
    "internal_failure",
]
AgentFailurePhase = Literal[
    "configuration",
    "model_inference",
    "structured_output",
    "provider_response",
    "policy_validation",
    "deterministic_validation",
    "internal",
]
AgentUiErrorCode = Literal[
    "invalid_platform",
    "invalid_decision",
    "invalid_reviewer",
    "invalid_correction",
    "run_capacity_reached",
    "unknown_run",
    "decision_already_recorded",
    "retry_already_recorded",
    "retry_unavailable",
    "candidate_unavailable",
    "candidate_export_unavailable",
    "run_unavailable",
    "invalid_message",
    "secret_material",
    "conversation_capacity_reached",
    "unknown_conversation",
    "conversation_closed",
    "conversation_not_ready",
    "conversation_unavailable",
    "stale_conversation",
    "conversation_launch_pending",
    "final_review_unavailable",
    "final_review_already_requested",
    "final_review_already_decided",
]
JsonScalar: TypeAlias = str | int | bool | None
_ERROR_MESSAGES: Final[dict[AgentUiErrorCode, str]] = {
    "invalid_platform": "Select one of the available migration scenarios.",
    "invalid_decision": "The manifest decision must be approve or reject.",
    "invalid_reviewer": "Enter a valid reviewer name.",
    "invalid_correction": "The correction approval does not match the offered retry.",
    "run_capacity_reached": "This Agent UI has reached its active-run limit.",
    "unknown_run": "The requested agent run is not available.",
    "decision_already_recorded": "This manifest decision has already been recorded.",
    "retry_already_recorded": "This correction decision has already been recorded.",
    "retry_unavailable": "A bounded implementation retry is not available for this run.",
    "candidate_unavailable": "A migration candidate is not available for this run.",
    "candidate_export_unavailable": "The candidate could not be saved to output/ safely.",
    "run_unavailable": "The agent run could not be verified safely.",
    "invalid_message": "Enter a message between 1 and 2000 characters.",
    "secret_material": "Remove credentials or secret-shaped values before continuing.",
    "conversation_capacity_reached": "This Agent UI has reached its conversation limit.",
    "unknown_conversation": "The requested conversation is not available.",
    "conversation_closed": "This conversation already launched a migration run.",
    "conversation_not_ready": (
        "Select a supported scenario and receive an Architect advisory before using the "
        "Controller launch gate."
    ),
    "conversation_unavailable": "The Architect conversation could not be completed safely.",
    "stale_conversation": "The conversation changed after this migration preview was shown. Review the latest Architect reply before launching.",
    "conversation_launch_pending": "This conversation already has a pending migration launch. Retry the exact launch instead of sending another message.",
    "final_review_unavailable": (
        "Final review requires a completed candidate whose deterministic disposition is "
        "ready for human review."
    ),
    "final_review_already_requested": "Final review has already been requested for this run.",
    "final_review_already_decided": "The final-review decision has already been recorded.",
}


class AgentUiError(RuntimeError):
    """A stable public error that never includes an internal exception or path."""

    def __init__(self, code: AgentUiErrorCode) -> None:
        self.code = code
        self.public_message = _ERROR_MESSAGES[code]
        super().__init__(self.public_message)


class AgentBoundariesView(StrictModel):
    provider_attempted: bool
    provider_invoked: bool | None
    model_call_record_persisted: bool
    structured_response_accepted: bool
    provider_id: Literal["ollama", "claude-cli"]
    model_id: str = Field(min_length=1, max_length=300)
    model_revision: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_identity_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    execution_boundary: Literal["local_loopback", "remote_provider_managed"]
    external_platform_invoked: Literal[False] = False
    source_mutated: Literal[False] = False
    deployment_performed: Literal[False] = False
    mode: Literal["local_ollama", "remote_claude_cli"]
    notice: str = Field(min_length=1, max_length=1000)


class AgentStageView(StrictModel):
    key: Literal["architect", "approval", "engineer", "validator"]
    label: str = Field(min_length=1, max_length=120)
    state: AgentStageState
    detail: str = Field(min_length=1, max_length=1000)


class AgentMetricsView(StrictModel):
    graph_nodes: int = Field(ge=0)
    graph_edges: int = Field(ge=0)
    wiki_hits: int = Field(ge=0)
    changed_files: int = Field(ge=0)


class AgentTransformationView(StrictModel):
    step_id: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=2000)
    input_paths: tuple[str, ...]
    output_paths: tuple[str, ...]


class AgentValidationCommandView(StrictModel):
    check_id: str = Field(min_length=1, max_length=160)
    command_id: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=1000)
    environment: str = Field(min_length=1, max_length=80)
    required: bool


class AgentRiskView(StrictModel):
    category: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=2000)
    evidence: tuple[str, ...]
    requires_human_decision: bool


class AgentManifestView(StrictModel):
    approval_required: bool
    manifest_id: str = Field(min_length=1, max_length=160)
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["planned", "decision_required"]
    approved_paths: tuple[str, ...]
    public_decisions: tuple[str, ...]
    implementation_contract: tuple[str, ...]
    cited_graph_nodes: tuple[str, ...]
    cited_wiki_pages: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    transformations: tuple[AgentTransformationView, ...]
    validation_commands: tuple[AgentValidationCommandView, ...]
    risks: tuple[AgentRiskView, ...]
    required_approvals: tuple[str, ...]


class AgentWikiHitView(StrictModel):
    page_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    path: str


class AgentEvidenceView(StrictModel):
    wiki_hits: tuple[AgentWikiHitView, ...]
    graph_summary: str = Field(min_length=1, max_length=1000)


class AgentCandidateFileView(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    path: str
    content: str


class AgentCandidateView(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    available: Literal[True] = True
    attempt: int = Field(ge=1, le=2)
    download_available: bool = True
    change_set_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    changed_paths: tuple[str, ...]
    unified_diff: str = Field(min_length=1)
    files: tuple[AgentCandidateFileView, ...]
    assumptions: tuple[str, ...]


class AgentValidationResultView(StrictModel):
    check_id: str = Field(min_length=1, max_length=160)
    status: Literal["passed", "failed", "unavailable", "nonterminal"]
    summary: str = Field(min_length=1, max_length=2000)
    diagnostic_ids: tuple[str, ...] = ()
    dependent_on: str | None = Field(default=None, min_length=1, max_length=160)


class AgentValidationView(StrictModel):
    attempt: int = Field(ge=1, le=2)
    disposition: Literal[
        "ready_for_human_review",
        "recoverable_failure",
        "plan_invalid",
        "environment_unavailable",
        "decision_required",
    ]
    results: tuple[AgentValidationResultView, ...]
    validator_summary: str = Field(min_length=1, max_length=3000)
    advisory_assessment: Literal["supports_report", "raises_concern", "escalate"] | None
    validator_completed: bool = True
    concerns: tuple[str, ...]
    deterministic_report_controls_disposition: Literal[True] = True
    final_review_enabled: bool


class AgentInterventionView(StrictModel):
    intervention_id: str = Field(min_length=1, max_length=160)
    requested_action: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=3000)
    affected_paths: tuple[str, ...]
    public_concerns: tuple[str, ...]
    recommendation: str = Field(min_length=1, max_length=2000)


class AgentFailureView(StrictModel):
    operation: str = Field(min_length=1, max_length=80)
    seam: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    reason_code: AgentFailureReasonCode
    phase: AgentFailurePhase
    response_received: bool | None
    schema_valid: bool | None
    policy_valid: bool | None
    attempt: int = Field(ge=1, le=2)
    terminal: Literal[True] = True
    retry_eligible: Literal[False] = False
    summary: str = Field(min_length=1, max_length=1000)
    guidance: str = Field(min_length=1, max_length=1000)


class AgentModelCallView(StrictModel):
    role: Literal["architect", "engineer", "validator"]
    attempt: int = Field(ge=1, le=2)
    agent_version: str = Field(min_length=1, max_length=80)
    latency_ms: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class AgentPlanningDecisionView(StrictModel):
    manifest_id: str = Field(min_length=1, max_length=160)
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    unresolved_questions: tuple[str, ...] = Field(max_length=16)
    required_approvals: tuple[str, ...]
    summary: str = Field(min_length=1, max_length=1000)


class AgentHumanDecisionView(StrictModel):
    selection: Literal["approve", "reject", "modify"]
    reviewer: str = Field(min_length=1, max_length=160)
    comment: str = Field(max_length=2000)
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class AgentCorrectionApprovalView(StrictModel):
    correction_id: str = Field(min_length=1, max_length=160)
    reviewer: str = Field(min_length=1, max_length=160)
    comment: str = Field(max_length=2000)
    authorized_attempt: Literal[2]


class AgentCorrectionView(StrictModel):
    correction_id: str = Field(min_length=1, max_length=160)
    completed_attempt: int = Field(ge=1, le=2)
    authorized_attempt: int | None = Field(default=None, ge=2, le=2)
    action: CorrectionAction
    failed_check_ids: tuple[str, ...]
    reason: str = Field(min_length=1, max_length=2000)
    retry_available: bool
    approval: AgentCorrectionApprovalView | None = None


class AgentAttemptView(StrictModel):
    attempt: int = Field(ge=1, le=2)
    disposition: Literal[
        "ready_for_human_review",
        "recoverable_failure",
        "plan_invalid",
        "environment_unavailable",
        "decision_required",
    ]
    failed_check_ids: tuple[str, ...]
    diagnostic_ids: tuple[str, ...]


class AgentFinalReviewView(StrictModel):
    """Public, non-authorizing projection of the named final-human-review gate."""

    status: Literal[
        "not_requested",
        "awaiting_final_review",
        "expired",
        "accepted",
        "rejected",
        "changes_requested",
    ]
    eligible: bool
    can_request: bool
    can_decide: bool
    review_id: str | None = Field(default=None, max_length=160)
    requester: str | None = Field(default=None, max_length=160)
    designated_reviewer: str | None = Field(default=None, max_length=160)
    requested_at: datetime | None = None
    expires_at: datetime | None = None
    selection: Literal["accept", "reject", "request_changes"] | None = None
    reviewer: str | None = Field(default=None, max_length=160)
    decided_at: datetime | None = None
    comment: str = Field(default="", max_length=2000)
    candidate_accepted: bool | None = None
    next_action: (
        Literal[
            "separate_external_action_required",
            "stop_request",
            "revise_and_start_new_review",
        ]
        | None
    ) = None
    authority_granted: Literal[False] = False
    external_actions_authorized: tuple[()] = ()


class AgentRunView(StrictModel):
    """Complete JSON-safe projection consumed by the local HTTP layer."""

    schema_version: Literal["1.0"] = "1.0"
    handle: str = Field(pattern=r"^[0-9a-f]{24}$")
    platform: Literal["salesforce", "mulesoft"]
    scenario_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
    scenario_title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=1000)
    status: str = Field(min_length=1, max_length=80)
    terminal_disposition: str | None = Field(default=None, max_length=80)
    execution_attempt: int = Field(ge=1, le=2)
    boundaries: AgentBoundariesView
    stages: tuple[AgentStageView, ...]
    model_calls: tuple[AgentModelCallView, ...] = ()
    metrics: AgentMetricsView
    manifest: AgentManifestView | None
    evidence: AgentEvidenceView
    candidate: AgentCandidateView | None
    validation: AgentValidationView | None
    intervention: AgentInterventionView | None = None
    planning_decision: AgentPlanningDecisionView | None = None
    human_decision: AgentHumanDecisionView | None = None
    correction: AgentCorrectionView | None = None
    attempt_history: tuple[AgentAttemptView, ...] = ()
    final_review: AgentFinalReviewView
    failure: AgentFailureView | None = None
