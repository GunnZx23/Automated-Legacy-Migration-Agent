"""Bounded Ollama-backed application service for the interactive Agent UI.

The browser selects one fixed scenario and supplies advisory conversation
messages; it cannot author the launch request. The server owns the canonical
launch contract, Ollama identity, filesystem routes, request identities,
approved paths, validation commands, and deployment boundaries. The service
executes the real :class:`AgentRun` lifecycle and projects its durable evidence
into a deliberately small UI contract.
"""

from __future__ import annotations

import logging
import math
import re
import secrets
import stat
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, TypeAlias, cast

from pydantic import ConfigDict, Field

from legacy_migration_agent.agent_runtime.agent_definitions import load_agent_registry
from legacy_migration_agent.agent_runtime.correction import (
    CorrectionAction,
    CorrectionApproval,
    CorrectionController,
    CorrectionRequest,
    correction_failure_ids,
    validation_failure_dependencies,
)
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectAgent,
    ArchitectConversationContext,
    ArchitectConversationMessage,
    ArchitectRun,
    EngineerRun,
    ValidatorAssessment,
)
from legacy_migration_agent.agent_runtime.ollama_model import (
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    MAX_OLLAMA_TIMEOUT_SECONDS,
    MIN_OLLAMA_TIMEOUT_SECONDS,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelCallRecord,
    ModelConfigurationError,
    ModelOutputError,
    ModelRuntimeError,
)
from legacy_migration_agent.application.agent_run import (
    AgentRunFailure,
    AgentRunModelClients,
    AgentRunStatus,
    assert_agent_request_secret_free,
    build_local_ollama_model_clients,
    get_agent_run_status,
    prepare_agent_run_request,
    recover_incomplete_agent_run_start,
    resume_agent_run,
    retry_agent_run,
    start_agent_run,
)
from legacy_migration_agent.application.architect_conversation import (
    MAX_CONVERSATION_EXCHANGES,
    MAX_CONVERSATIONS,
    ArchitectConversationExchange,
    ArchitectConversationSnapshot,
    ArchitectConversationStaleLaunch,
    ArchitectConversationStore,
    ArchitectConversationView,
    conversation_history,
    project_architect_conversation,
)
from legacy_migration_agent.application.candidate_export import (
    CandidateExportResult,
    build_candidate_archive,
)
from legacy_migration_agent.application.candidate_export import (
    export_candidate as persist_candidate_export,
)
from legacy_migration_agent.application.final_review import (
    FINAL_REVIEW_DECISION_PATH,
    FINAL_REVIEW_RECORD_PATH,
    FINAL_REVIEW_REQUEST_PATH,
    FinalReviewDecision,
    FinalReviewRecord,
    FinalReviewRequest,
    get_final_review_status_for_run,
)
from legacy_migration_agent.application.final_review import (
    decide_final_review_for_run as persist_final_review_decision,
)
from legacy_migration_agent.application.final_review import (
    request_final_review_for_run as persist_final_review_request,
)
from legacy_migration_agent.application.migration_scenarios import (
    MigrationLaunchContract,
    migration_launch_contract,
    migration_scenario,
    migration_scenario_by_id,
    migration_scenarios,
    require_canonical_launch_contract,
)
from legacy_migration_agent.contracts import (
    ImplementationIntervention,
    MigrationManifest,
    MigrationRequest,
    PlanningIntervention,
    StrictModel,
    ValidationDisposition,
    ValidationReport,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import ArtifactStore, artifact_digest
from legacy_migration_agent.core.observability import lifecycle_event
from legacy_migration_agent.core.policies import (
    PolicyViolation,
    validate_change_set,
    validate_report,
)
from legacy_migration_agent.core.redaction import SecretRedactor
from legacy_migration_agent.core.run_session import AgentRunContext
from legacy_migration_agent.core.workspace import snapshot_tree
from legacy_migration_agent.graphs.graph_contracts import DependencyGraph
from legacy_migration_agent.knowledge.wiki import RetrievalTrace
from legacy_migration_agent.workflow import ManifestApproval, WorkflowStatus

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

_HANDLE_PATTERN: Final = re.compile(r"^[0-9a-f]{24}$")
_SERVICE_LOCKS_GUARD = threading.Lock()
_SERVICE_LOCKS: dict[tuple[Path, str], threading.RLock] = {}
_REVIEWER_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_FINAL_REVIEW_ACTOR_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,159}$")
_MAX_FINAL_REVIEW_WINDOW: Final = timedelta(days=14)
_OLLAMA_RUNTIME_PROVIDER: Final[Literal["ollama"]] = "ollama"
_OLLAMA_READINESS_TIMEOUT_SECONDS: Final = 3.0
_LOCAL_MODEL_APPROVER: Final = "local-ui-operator"
_MANIFEST_APPROVAL_PATH: Final = "control/manifest-approval.json"
_CORRECTION_REQUEST_ATTEMPT_ONE_PATH: Final = "control/correction-request-attempt-1.json"
_CORRECTION_APPROVAL_ATTEMPT_TWO_PATH: Final = "control/correction-approval-attempt-2.json"
_TERMINAL_UI_RUN_STATUSES: Final[frozenset[str]] = frozenset(
    {
        WorkflowStatus.COMPLETED,
        WorkflowStatus.REJECTED,
        WorkflowStatus.MODIFICATION_REQUESTED,
        WorkflowStatus.DECISION_REQUIRED,
        "failed",
    }
)
_LOCAL_BOUNDARY_NOTICE: Final = (
    "Real structured-output inference ran through the server-owned loopback Ollama endpoint "
    "against a fixed synthetic source fixture. The agent creates an isolated migration "
    "candidate only; no Salesforce org, Mule runtime, source mutation, deployment, Git action, "
    "or publication is invoked."
)
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
_FAILURE_DETAILS: Final[
    dict[
        AgentFailureReasonCode,
        tuple[AgentFailurePhase, bool | None, bool | None, bool | None, str, str],
    ]
] = {
    "configuration_invalid": (
        "configuration",
        None,
        None,
        None,
        "The controller rejected the local model configuration before a usable response.",
        "Check that the configured Ollama model is installed, then start a fresh run.",
    ),
    "provider_refusal": (
        "model_inference",
        True,
        False,
        None,
        "The local model declined to produce the requested structured role output.",
        "Refine the bounded migration request and start a fresh run.",
    ),
    "response_incomplete": (
        "structured_output",
        True,
        False,
        None,
        "The local model response ended before the structured role output was complete.",
        "Start a fresh run; reduce model load or allow a longer server-owned timeout if needed.",
    ),
    "structured_output_invalid": (
        "structured_output",
        True,
        False,
        None,
        "The local model responded, but its role output did not satisfy the typed contract.",
        "Start a fresh run and use the harness trace to identify the rejected role boundary.",
    ),
    "unauthorized_tool_call": (
        "structured_output",
        True,
        False,
        None,
        (
            "The local model returned a native provider tool call instead of the required "
            "structured role response."
        ),
        (
            "Start a fresh run; the controller blocked the provider tool interface. Declared "
            "evidence selections are typed output fields, not native tool calls."
        ),
    ),
    "model_inventory_invalid": (
        "configuration",
        None,
        None,
        None,
        "The local Ollama model inventory could not prove the selected model identity.",
        "Confirm the exact model alias and digest with ollama list, then start a fresh run.",
    ),
    "provider_response_invalid": (
        "provider_response",
        None,
        None,
        None,
        "Ollama returned a response that failed the controller's provider-protocol checks.",
        "Confirm the local Ollama service and selected model, then start a fresh run.",
    ),
    "required_approval_missing": (
        "policy_validation",
        True,
        True,
        False,
        "The Controller-expanded manifest was missing its required human-approval binding.",
        (
            "Start a fresh run and inspect the Controller expansion and scope-policy binding; "
            "the Architect does not author approval actions."
        ),
    ),
    "implementation_contract_invalid": (
        "policy_validation",
        True,
        True,
        False,
        (
            "The Controller-expanded manifest failed its controller-owned implementation-contract "
            "check."
        ),
        (
            "Start a fresh run and inspect Controller expansion against the frozen scope policy; "
            "the Architect does not copy or author implementation-contract entries."
        ),
    ),
    "transformation_scope_invalid": (
        "policy_validation",
        True,
        True,
        False,
        "The Controller-expanded manifest violated the frozen transformation boundary.",
        (
            "Start a fresh run. Use only frozen legacy files as inputs, cover every required "
            "source input, and assign every approved output to exactly one transformation; "
            "generated target files cannot become inputs to later steps."
        ),
    ),
    "unresolved_question_risk_missing": (
        "policy_validation",
        True,
        True,
        False,
        (
            "The Architect returned unresolved planning questions without identifying a "
            "material risk that requires a human decision."
        ),
        (
            "Start a fresh run. The Architect must either resolve those questions from the "
            "frozen evidence or identify the material risk and mark it as requiring a human "
            "decision."
        ),
    ),
    "policy_rejected": (
        "policy_validation",
        True,
        True,
        False,
        "The role output passed its schema but failed a controller-owned policy check.",
        "Start a fresh run and inspect the public policy phase in the harness trace.",
    ),
    "provider_timeout": (
        "model_inference",
        None,
        None,
        None,
        "The local Ollama request exceeded the server-owned inference deadline.",
        "Confirm Ollama is responsive, then restart with a longer timeout or a smaller installed model.",
    ),
    "provider_unavailable": (
        "model_inference",
        None,
        None,
        None,
        "The controller could not complete the local Ollama request.",
        "Confirm Ollama is running and the configured model is installed, then start a fresh run.",
    ),
    "deterministic_validation_failed": (
        "deterministic_validation",
        None,
        None,
        None,
        "The controller-owned deterministic validation boundary could not produce a trusted report.",
        "Inspect the harness stage and local validation environment before starting a fresh run.",
    ),
    "internal_failure": (
        "internal",
        None,
        None,
        None,
        "The workflow ended at a sanitized internal boundary without authorizing external action.",
        "Start a fresh run; if the failure repeats, inspect the durable sanitized evidence.",
    ),
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
    provider_id: Literal["ollama"]
    model_id: str = Field(min_length=1, max_length=300)
    model_revision: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    execution_boundary: Literal["local_loopback"]
    external_platform_invoked: Literal[False] = False
    source_mutated: Literal[False] = False
    deployment_performed: Literal[False] = False
    mode: Literal["local_ollama"]
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


class AgentUiService:
    """Execute and inspect bounded migrations using one server-owned Ollama model."""

    def __init__(
        self,
        project_root: Path,
        *,
        run_root: str = ".runs/agent-ui",
        max_runs: int = 16,
        ollama_model_id: str,
        ollama_timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    ) -> None:
        if max_runs < 1 or max_runs > 128:
            raise ValueError("max_runs must be between 1 and 128")
        normalized_run_root = validate_relative_path(run_root)
        if normalized_run_root != ".runs/agent-ui":
            raise ValueError("the UI run root must be .runs/agent-ui")
        self._project_root = _safe_project_root(project_root)
        self._run_root_relative = normalized_run_root
        self._run_root = self._project_root.joinpath(*normalized_run_root.split("/"))
        self._max_runs = max_runs
        self._ollama_model_id = _normalize_ollama_model_id(ollama_model_id)
        self._ollama_timeout_seconds = _normalize_ollama_timeout_seconds(ollama_timeout_seconds)
        self._registry_lock = _shared_service_lock(self._run_root, "registry")

    def scenarios(self) -> tuple[dict[str, JsonScalar], ...]:
        """Return the exact public sample contract without filesystem routes or commands."""

        return tuple(
            {
                "scenario_id": scenario.scenario_id,
                "platform": scenario.platform.value,
                "title": scenario.title,
                "canonical_request": scenario.canonical_description,
                "source": " + ".join(scenario.display_source_artifacts),
                "target": scenario.target_summary,
            }
            for scenario in migration_scenarios()
        )

    def model_configuration(self) -> dict[str, JsonScalar]:
        """Return the immutable browser-safe model identity owned by the server."""

        return {
            "provider": _OLLAMA_RUNTIME_PROVIDER,
            "model_id": self._ollama_model_id,
            "execution_boundary": "local_loopback",
        }

    def runtime_readiness(self) -> dict[str, JsonScalar]:
        """Probe only the fixed Ollama inventory seam and return sanitized readiness facts.

        The same loopback-only adapter used for model calls performs this check.  The
        browser receives no endpoint, transport exception, inventory body, or alternate
        provider control.
        """

        lifecycle_event(
            "ui.provider.readiness.started",
            provider=_OLLAMA_RUNTIME_PROVIDER,
            model_id=self._ollama_model_id,
        )
        reachable: bool | None = None
        installed: bool | None = None
        status = "readiness_unavailable"
        try:
            models = self._local_models(self._ollama_model_id)
            raw_probe = getattr(models.architect, "_resolve_model_revision", None)
            if not callable(raw_probe):
                raise TypeError("local Ollama client does not expose its inventory probe")
            probe = cast(Callable[..., str], raw_probe)
            revision = probe(
                timeout_seconds=min(
                    self._ollama_timeout_seconds,
                    _OLLAMA_READINESS_TIMEOUT_SECONDS,
                )
            )
            if re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is None:
                raise ModelOutputError("local Ollama inventory returned an invalid revision")
        except ModelConfigurationError:
            reachable = True
            installed = False
            status = "model_unavailable"
        except ModelOutputError:
            reachable = True
            installed = None
            status = "inventory_unverified"
        except ModelRuntimeError:
            reachable = False
            installed = None
            status = "ollama_unreachable"
        except Exception as error:
            lifecycle_event(
                "ui.provider.readiness.failed",
                level=logging.ERROR,
                provider=_OLLAMA_RUNTIME_PROVIDER,
                public_status=status,
                error_type=type(error).__name__,
            )
        else:
            reachable = True
            installed = True
            status = "ready"
        lifecycle_event(
            "ui.provider.readiness.completed",
            provider=_OLLAMA_RUNTIME_PROVIDER,
            public_status=status,
            ollama_reachable=reachable,
            model_installed=installed,
        )
        return {
            "provider": _OLLAMA_RUNTIME_PROVIDER,
            "model_id": self._ollama_model_id,
            "configured": True,
            "ollama_reachable": reachable,
            "model_installed": installed,
            "status": status,
        }

    def create_conversation(self, *, scenario_id: str | None) -> ArchitectConversationView:
        """Create one append-only public intake conversation without starting a run."""

        launch_contract = self._conversation_launch_contract(scenario_id)
        selected_platform = None if launch_contract is None else launch_contract.platform
        try:
            with self._registry_lock:
                self._prepare_run_root()
                store = self._conversation_store()
                if store.conversation_count() >= MAX_CONVERSATIONS:
                    raise AgentUiError("conversation_capacity_reached")
                conversation_id = self._new_conversation_id(store)
                snapshot = store.create(
                    conversation_id,
                    initial_platform=selected_platform,
                    initial_scenario_id=(
                        None if launch_contract is None else launch_contract.scenario_id
                    ),
                )
                lifecycle_event(
                    "ui.conversation.created",
                    conversation_id=conversation_id,
                    platform=(None if selected_platform is None else selected_platform.value),
                )
                return project_architect_conversation(snapshot)
        except AgentUiError:
            raise
        except Exception as error:
            lifecycle_event(
                "ui.service.failed",
                level=logging.ERROR,
                action="conversation_create",
                public_code="conversation_unavailable",
                error_type=type(error).__name__,
            )
            raise AgentUiError("conversation_unavailable") from None

    def get_conversation(self, conversation_id: str) -> ArchitectConversationView:
        """Read one verified public conversation snapshot."""

        self._validate_conversation_id(conversation_id)
        try:
            with self._lock_for(f"conversation:{conversation_id}"):
                self._prepare_run_root()
                snapshot = self._conversation_store().load(conversation_id)
                self._verify_conversation_snapshot(snapshot)
                return project_architect_conversation(snapshot)
        except FileNotFoundError:
            raise AgentUiError("unknown_conversation") from None
        except AgentUiError:
            raise
        except Exception as error:
            lifecycle_event(
                "ui.service.failed",
                level=logging.ERROR,
                action="conversation_get",
                conversation_id=conversation_id,
                public_code="conversation_unavailable",
                error_type=type(error).__name__,
            )
            raise AgentUiError("conversation_unavailable") from None

    def send_conversation_message(
        self,
        conversation_id: str,
        *,
        message: str,
        scenario_id: str | None,
    ) -> ArchitectConversationView:
        """Ask the Architect for one advisory intake reply; never start migration work."""

        self._validate_conversation_id(conversation_id)
        launch_contract = self._conversation_launch_contract(scenario_id)
        scenario = (
            None
            if launch_contract is None
            else migration_scenario_by_id(launch_contract.scenario_id)
        )
        launch_contract_digest = (
            None if launch_contract is None else artifact_digest(launch_contract)
        )
        selected_platform = None if launch_contract is None else launch_contract.platform
        user_message = ArchitectConversationMessage(
            role="user",
            content=self._validate_conversation_message(message),
        )
        try:
            with self._lock_for(f"conversation:{conversation_id}"):
                self._prepare_run_root()
                store = self._conversation_store()
                try:
                    snapshot = store.load(conversation_id)
                except FileNotFoundError:
                    raise AgentUiError("unknown_conversation") from None
                self._verify_conversation_snapshot(snapshot)
                if snapshot.launch_intent is not None:
                    if snapshot.launch is None:
                        raise AgentUiError("conversation_launch_pending")
                    raise AgentUiError("conversation_closed")
                if len(snapshot.exchanges) >= MAX_CONVERSATION_EXCHANGES:
                    raise AgentUiError("conversation_closed")

                context = ArchitectConversationContext(
                    selected_platform=selected_platform,
                    scenario_id=(None if launch_contract is None else launch_contract.scenario_id),
                    source_artifacts=(
                        () if scenario is None else scenario.display_source_artifacts
                    ),
                    target_summary=(
                        None if launch_contract is None else launch_contract.target_summary
                    ),
                    canonical_request=(
                        None if launch_contract is None else launch_contract.canonical_description
                    ),
                    launch_contract_digest=launch_contract_digest,
                    history=conversation_history(snapshot, user_message),
                )
                models = self._local_models(self._ollama_model_id)
                if snapshot.exchanges:
                    expected_revision = snapshot.exchanges[
                        -1
                    ].architect_run.model_call.model_revision
                    bind_revision = getattr(models.architect, "bind_model_revision", None)
                    if expected_revision is None or not callable(bind_revision):
                        raise AgentUiError("conversation_unavailable")
                    bind_revision(expected_revision)
                architect = ArchitectAgent(
                    load_agent_registry(self._project_root / "agents"),
                    models.architect,
                )
                lifecycle_event(
                    "ui.conversation.model.started",
                    conversation_id=conversation_id,
                    exchange=len(snapshot.exchanges) + 1,
                    platform=(None if selected_platform is None else selected_platform.value),
                )
                run = architect.converse(context)
                updated = store.append_exchange(
                    conversation_id,
                    selected_platform=selected_platform,
                    scenario_id=(None if launch_contract is None else launch_contract.scenario_id),
                    launch_contract_digest=launch_contract_digest,
                    user_message=user_message,
                    architect_run=run,
                )
                self._verify_conversation_snapshot(updated)
                lifecycle_event(
                    "ui.conversation.model.completed",
                    conversation_id=conversation_id,
                    exchange=len(updated.exchanges),
                    readiness=run.reply.status,
                )
                return project_architect_conversation(updated)
        except AgentUiError:
            raise
        except Exception as error:
            lifecycle_event(
                "ui.service.failed",
                level=logging.ERROR,
                action="conversation_message",
                conversation_id=conversation_id,
                public_code="conversation_unavailable",
                error_type=type(error).__name__,
            )
            raise AgentUiError("conversation_unavailable") from None

    def launch_conversation(
        self,
        conversation_id: str,
        *,
        launch_token: str,
    ) -> tuple[ArchitectConversationView, AgentRunView]:
        """Explicitly start the existing workflow from the latest ready intake result."""

        self._validate_conversation_id(conversation_id)
        if (
            not isinstance(launch_token, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", launch_token) is None
        ):
            raise AgentUiError("stale_conversation")
        try:
            with self._lock_for(f"conversation:{conversation_id}"):
                self._prepare_run_root()
                store = self._conversation_store()
                try:
                    snapshot = store.load(conversation_id)
                except FileNotFoundError:
                    raise AgentUiError("unknown_conversation") from None
                self._verify_conversation_snapshot(snapshot)
                view = project_architect_conversation(snapshot)
                if view.readiness.launch_token != launch_token:
                    raise AgentUiError("stale_conversation")
                if snapshot.launch is not None:
                    run_view = self.get(snapshot.launch.handle)
                    self._verify_conversation_launch_binding(
                        view,
                        run_view,
                        expected_model_revision=snapshot.launch.model_revision,
                    )
                    return view, run_view
                if (
                    not view.readiness.ready
                    or view.readiness.platform is None
                    or view.readiness.scenario_id is None
                    or view.readiness.canonical_request is None
                    or view.readiness.launch_contract_digest is None
                ):
                    raise AgentUiError("conversation_not_ready")
                launch_contract = migration_launch_contract(view.readiness.scenario_id)
                if artifact_digest(launch_contract) != view.readiness.launch_contract_digest:
                    raise AgentUiError("conversation_unavailable")
                if snapshot.launch_intent is None:
                    reserved_handle = self._new_handle()
                    try:
                        snapshot = store.begin_launch(
                            conversation_id,
                            handle=reserved_handle,
                            expected_launch_token=launch_token,
                        )
                    except ArchitectConversationStaleLaunch:
                        raise AgentUiError("stale_conversation") from None
                    if snapshot.launch_intent is None:
                        raise AgentUiError("conversation_unavailable")
                    # A concurrent exact-token caller may have published the
                    # one immutable reservation first. Always follow the
                    # persisted handle instead of the caller's unused random
                    # proposal.
                    reserved_handle = snapshot.launch_intent.handle
                else:
                    reserved_handle = snapshot.launch_intent.handle
                if snapshot.launch_intent is None:
                    raise AgentUiError("conversation_unavailable")
                expected_model_revision = snapshot.launch_intent.model_revision
                requested_at = snapshot.launch_intent.requested_at
                lifecycle_event(
                    "ui.conversation.launch.started",
                    conversation_id=conversation_id,
                    platform=view.readiness.platform.value,
                    scenario_id=view.readiness.scenario_id,
                )
                if self._run_dir(reserved_handle).exists():
                    # A prior request may have completed the run but crashed
                    # before publishing the conversation receipt. Reconcile
                    # only that immutable reserved handle; never create a
                    # second run for the same intake decision.
                    try:
                        run_view = self.get(reserved_handle)
                    except AgentUiError:
                        run_view = self._recover_incomplete_reserved_start(
                            launch_contract,
                            handle=reserved_handle,
                            expected_model_revision=expected_model_revision,
                            requested_at=requested_at,
                        )
                else:
                    run_view = self.start(
                        launch_contract,
                        _reserved_handle=reserved_handle,
                        _expected_model_revision=expected_model_revision,
                        _requested_at=requested_at,
                    )
                if run_view.handle != reserved_handle:
                    raise AgentUiError("conversation_unavailable")
                self._verify_conversation_launch_binding(
                    view,
                    run_view,
                    expected_model_revision=expected_model_revision,
                )
                updated = store.record_launch(
                    conversation_id,
                    handle=reserved_handle,
                )
                lifecycle_event(
                    "ui.conversation.launch.completed",
                    conversation_id=conversation_id,
                    handle=run_view.handle,
                )
                return project_architect_conversation(updated), run_view
        except AgentUiError:
            raise
        except Exception as error:
            lifecycle_event(
                "ui.service.failed",
                level=logging.ERROR,
                action="conversation_launch",
                conversation_id=conversation_id,
                public_code="conversation_unavailable",
                error_type=type(error).__name__,
            )
            raise AgentUiError("conversation_unavailable") from None

    def start(
        self,
        launch_contract: MigrationLaunchContract,
        *,
        _reserved_handle: str | None = None,
        _expected_model_revision: str | None = None,
        _requested_at: datetime | None = None,
    ) -> AgentRunView:
        """Run the real Architect and stop at the real manifest approval gate."""

        if not isinstance(launch_contract, MigrationLaunchContract):
            raise AgentUiError("run_unavailable")
        try:
            contract = require_canonical_launch_contract(launch_contract)
        except (TypeError, ValueError, KeyError):
            raise AgentUiError("run_unavailable") from None
        if _reserved_handle is not None and (
            not isinstance(_reserved_handle, str)
            or _HANDLE_PATTERN.fullmatch(_reserved_handle) is None
        ):
            raise AgentUiError("run_unavailable")
        if _expected_model_revision is not None and (
            _reserved_handle is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", _expected_model_revision) is None
        ):
            raise AgentUiError("run_unavailable")
        if _requested_at is not None and (
            _reserved_handle is None or not _is_utc_timestamp(_requested_at)
        ):
            raise AgentUiError("run_unavailable")
        scenario = migration_scenario_by_id(contract.scenario_id)
        description = contract.canonical_description
        handle: str | None = None
        try:
            with self._registry_lock:
                self._prepare_run_root()
                if self._active_owned_run_count() >= self._max_runs:
                    raise AgentUiError("run_capacity_reached")
                handle = self._new_handle() if _reserved_handle is None else _reserved_handle
                run_dir = self._run_dir(handle)
                if _reserved_handle is not None and run_dir.exists():
                    raise AgentUiError("run_unavailable")
                lifecycle_event(
                    "ui.run.created",
                    handle=handle,
                    platform=contract.platform.value,
                    prompt_chars=len(description),
                )
                source = self._source_root(scenario.source_root)
                source_before = snapshot_tree(source)
                request_id, run_id, thread_id = self._identities(handle)
                request = prepare_agent_run_request(
                    self._project_root,
                    request_id=request_id,
                    launch_contract=contract,
                    requested_at=(
                        _agent_requested_at() if _requested_at is None else _requested_at
                    ),
                )
                models = self._local_models(self._ollama_model_id)
                if _expected_model_revision is not None:
                    models.bind_recorded_model_revision(_expected_model_revision)
                status = start_agent_run(
                    self._project_root,
                    run_dir,
                    run_id=run_id,
                    thread_id=thread_id,
                    launch_contract=contract,
                    request=request,
                    models=models,
                )
                if status.status not in {"awaiting_approval", "decision_required", "failed"}:
                    raise AgentUiError("run_unavailable")
                if status.status == "awaiting_approval" and status.interrupt is None:
                    raise AgentUiError("run_unavailable")
                if snapshot_tree(source) != source_before:
                    raise AgentUiError("run_unavailable")
                reloaded = self._load_status(handle)
                if reloaded != status:
                    raise AgentUiError("run_unavailable")
                view = self._project_view(handle, reloaded)
                _log_ui_run_status(handle, reloaded)
                return view
        except AgentUiError:
            raise
        except Exception as error:
            lifecycle_event(
                "ui.service.failed",
                level=logging.ERROR,
                action="start",
                handle=handle,
                public_code="run_unavailable",
                error_type=type(error).__name__,
            )
            raise AgentUiError("run_unavailable") from None

    def _recover_incomplete_reserved_start(
        self,
        launch_contract: MigrationLaunchContract,
        *,
        handle: str,
        expected_model_revision: str,
        requested_at: datetime,
    ) -> AgentRunView:
        """Recover only the exact unadvanced bootstrap bound by launch intent."""

        contract = require_canonical_launch_contract(launch_contract)
        scenario = migration_scenario_by_id(contract.scenario_id)
        with self._registry_lock, self._lock_for(handle):
            source = self._source_root(scenario.source_root)
            source_before = snapshot_tree(source)
            request_id, run_id, thread_id = self._identities(handle)
            request = prepare_agent_run_request(
                self._project_root,
                request_id=request_id,
                launch_contract=contract,
                requested_at=requested_at,
            )
            models = self._local_models(self._ollama_model_id)
            models.bind_recorded_model_revision(expected_model_revision)
            status = recover_incomplete_agent_run_start(
                self._project_root,
                self._run_dir(handle),
                run_id=run_id,
                thread_id=thread_id,
                launch_contract=contract,
                request=request,
                models=models,
            )
            if status.status not in {"awaiting_approval", "decision_required", "failed"}:
                raise AgentUiError("run_unavailable")
            if status.status == "awaiting_approval" and status.interrupt is None:
                raise AgentUiError("run_unavailable")
            if snapshot_tree(source) != source_before:
                raise AgentUiError("run_unavailable")
            reloaded = self._load_status(handle)
            if reloaded != status:
                raise AgentUiError("run_unavailable")
            view = self._project_view(handle, reloaded)
            _log_ui_run_status(handle, reloaded)
            return view

    def get(self, handle: str) -> AgentRunView:
        """Reload the canonical run and project only verified durable evidence."""

        try:
            return self._get_verified_view(handle)
        except AgentUiError as error:
            if error.code == "run_unavailable":
                lifecycle_event(
                    "ui.service.failed",
                    level=logging.ERROR,
                    action="get",
                    handle=handle,
                    public_code="run_unavailable",
                    error_type=type(error).__name__,
                )
            raise
        except Exception as error:
            lifecycle_event(
                "ui.service.failed",
                level=logging.ERROR,
                action="get",
                handle=handle,
                public_code="run_unavailable",
                error_type=type(error).__name__,
            )
            raise AgentUiError("run_unavailable") from None

    def request_final_review(
        self,
        handle: str,
        *,
        requester: str,
        designated_reviewer: str,
        requested_at: datetime,
        expires_at: datetime,
    ) -> AgentRunView:
        """Bind one completed candidate to two distinct declarative human identities."""

        normalized_requester = _validate_final_review_actor(requester)
        normalized_reviewer = _validate_final_review_actor(designated_reviewer)
        if normalized_requester == normalized_reviewer:
            raise AgentUiError("invalid_reviewer")
        if not _valid_final_review_window(requested_at, expires_at):
            raise AgentUiError("final_review_unavailable")
        self._require_known_handle(handle)
        try:
            with self._lock_for(handle):
                status = self._load_status(handle)
                current = self._project_view(handle, status)
                if current.final_review.status != "not_requested":
                    raise AgentUiError("final_review_already_requested")
                if not current.final_review.can_request:
                    raise AgentUiError("final_review_unavailable")
                persist_final_review_request(
                    self._project_root,
                    self._run_dir(handle),
                    run_id=status.run_id,
                    thread_id=status.thread_id,
                    requester=normalized_requester,
                    designated_reviewer=normalized_reviewer,
                    requested_at=requested_at,
                    expires_at=expires_at,
                )
                reloaded = self._load_status(handle)
                if reloaded != status:
                    raise AgentUiError("run_unavailable")
                view = self._project_view(handle, reloaded)
                if (
                    view.final_review.status != "awaiting_final_review"
                    or view.final_review.requester != normalized_requester
                    or view.final_review.designated_reviewer != normalized_reviewer
                    or view.final_review.authority_granted
                    or view.final_review.external_actions_authorized
                ):
                    raise AgentUiError("run_unavailable")
                lifecycle_event(
                    "ui.final_review.requested",
                    handle=handle,
                    authority_granted=False,
                )
                return view
        except AgentUiError:
            raise
        except PolicyViolation:
            raise AgentUiError("final_review_unavailable") from None
        except Exception as error:
            lifecycle_event(
                "ui.service.failed",
                level=logging.ERROR,
                action="final_review_request",
                handle=handle,
                public_code="run_unavailable",
                error_type=type(error).__name__,
            )
            raise AgentUiError("run_unavailable") from None

    def decide_final_review(
        self,
        handle: str,
        *,
        selection: str,
        reviewer: str,
        comment: str,
        decided_at: datetime,
    ) -> AgentRunView:
        """Consume one pending review without granting any external action authority."""

        if selection not in {"accept", "reject", "request_changes"}:
            raise AgentUiError("invalid_decision")
        normalized_reviewer = _validate_final_review_actor(reviewer)
        if not isinstance(comment, str) or len(comment) > 2000 or "\x00" in comment:
            raise AgentUiError("invalid_reviewer")
        if SecretRedactor().redact(comment).changed:
            raise AgentUiError("secret_material")
        if not _is_utc_timestamp(decided_at):
            raise AgentUiError("final_review_unavailable")
        self._require_known_handle(handle)
        try:
            with self._lock_for(handle):
                status = self._load_status(handle)
                current = self._project_view(handle, status)
                if current.final_review.status == "not_requested":
                    raise AgentUiError("final_review_unavailable")
                if current.final_review.status != "awaiting_final_review":
                    raise AgentUiError("final_review_already_decided")
                if not current.final_review.can_decide:
                    raise AgentUiError("final_review_unavailable")
                if current.final_review.designated_reviewer != normalized_reviewer:
                    raise AgentUiError("invalid_reviewer")
                persisted = persist_final_review_decision(
                    self._project_root,
                    self._run_dir(handle),
                    run_id=status.run_id,
                    thread_id=status.thread_id,
                    reviewer=normalized_reviewer,
                    selection=cast(
                        Literal["accept", "reject", "request_changes"],
                        selection,
                    ),
                    decided_at=decided_at,
                    comment=comment,
                )
                if (
                    persisted.external_actions_authorized
                    or persisted.source_mutated
                    or persisted.deployment_performed
                    or persisted.publication_performed
                ):
                    raise AgentUiError("run_unavailable")
                reloaded = self._load_status(handle)
                if reloaded != status:
                    raise AgentUiError("run_unavailable")
                view = self._project_view(handle, reloaded)
                if (
                    view.final_review.status != persisted.outcome
                    or view.final_review.reviewer != normalized_reviewer
                    or view.final_review.authority_granted
                    or view.final_review.external_actions_authorized
                ):
                    raise AgentUiError("run_unavailable")
                lifecycle_event(
                    "ui.final_review.decided",
                    handle=handle,
                    selection=selection,
                    authority_granted=False,
                )
                return view
        except AgentUiError:
            raise
        except PolicyViolation:
            raise AgentUiError("final_review_unavailable") from None
        except Exception as error:
            lifecycle_event(
                "ui.service.failed",
                level=logging.ERROR,
                action="final_review_decision",
                handle=handle,
                public_code="run_unavailable",
                error_type=type(error).__name__,
            )
            raise AgentUiError("run_unavailable") from None

    def latest(self) -> AgentRunView | None:
        """Recover the newest verifiable local UI run after a lost browser response."""

        try:
            root_metadata = self._run_root.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise AgentUiError("run_unavailable")
        try:
            self._run_root.resolve(strict=True).relative_to(self._project_root)
            children = tuple(self._run_root.iterdir())
        except (OSError, ValueError):
            raise AgentUiError("run_unavailable") from None

        candidates: list[tuple[int, str]] = []
        for child in children:
            if _HANDLE_PATTERN.fullmatch(child.name) is None:
                continue
            try:
                metadata = child.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                continue
            candidates.append((metadata.st_mtime_ns, child.name))
        recovered: AgentRunView | None = None
        incompatible_run_count = 0
        for _, handle in sorted(candidates, reverse=True):
            try:
                recovered = self._get_verified_view(handle)
            except (AgentUiError, FileNotFoundError, PolicyViolation, ValueError):
                incompatible_run_count += 1
                continue
            break
        if incompatible_run_count:
            lifecycle_event(
                "ui.latest.scan.completed",
                candidate_count=len(candidates),
                incompatible_run_count=incompatible_run_count,
                recovered=recovered is not None,
            )
        return recovered

    def decide(
        self,
        handle: str,
        *,
        selection: str,
        reviewer: str,
        comment: str = "",
    ) -> AgentRunView:
        """Record one exact approval or rejection against the current interrupt."""

        if not isinstance(selection, str) or selection not in {"approve", "reject"}:
            raise AgentUiError("invalid_decision")
        if (
            not isinstance(reviewer, str)
            or _REVIEWER_PATTERN.fullmatch(reviewer) is None
            or not isinstance(comment, str)
        ):
            raise AgentUiError("invalid_reviewer")
        self._require_known_handle(handle)
        try:
            with self._lock_for(handle):
                status = self._load_status(handle)
                if status.status != "awaiting_approval" or status.interrupt is None:
                    raise AgentUiError("decision_already_recorded")
                interrupt = status.interrupt
                try:
                    approval = ManifestApproval(
                        decision_id=interrupt.decision_id,
                        request_id=interrupt.request_id,
                        manifest_id=interrupt.manifest_id,
                        manifest_digest=interrupt.manifest_digest,
                        requested_action=interrupt.requested_action,
                        selection=selection,
                        reviewer=reviewer,
                        comment=comment,
                    )
                except ValueError:
                    raise AgentUiError("invalid_reviewer") from None
                lifecycle_event(
                    "ui.manifest.decision.received",
                    handle=handle,
                    selection=selection,
                )
                scenario = migration_scenario(status.platform)
                source = self._source_root(scenario.source_root)
                source_before = snapshot_tree(source)
                run_id, thread_id = self._run_thread_ids(handle)
                models = None
                if selection == "approve":
                    models = self._models_for_resume(status)
                resumed = resume_agent_run(
                    self._project_root,
                    self._run_dir(handle),
                    run_id=run_id,
                    thread_id=thread_id,
                    approval=approval,
                    models=models,
                )
                if selection == "approve":
                    if resumed.status not in {"completed", "decision_required", "failed"}:
                        raise AgentUiError("run_unavailable")
                    if resumed.status == "completed" and resumed.terminal_disposition not in {
                        "ready_for_human_review",
                        "recoverable_failure",
                        "plan_invalid",
                        "environment_unavailable",
                        "decision_required",
                    }:
                        raise AgentUiError("run_unavailable")
                    if (
                        resumed.status == "decision_required"
                        and resumed.terminal_disposition != "decision_required"
                    ):
                        raise AgentUiError("run_unavailable")
                    if resumed.status == "failed" and (
                        resumed.terminal_disposition != "controlled_failure"
                        or resumed.failure is None
                    ):
                        raise AgentUiError("run_unavailable")
                elif resumed.status != "rejected":
                    raise AgentUiError("run_unavailable")
                if snapshot_tree(source) != source_before:
                    raise AgentUiError("run_unavailable")
                reloaded = self._load_status(handle)
                if reloaded != resumed:
                    raise AgentUiError("run_unavailable")
                if selection == "reject" and self._engineer_artifact_exists(reloaded):
                    raise AgentUiError("run_unavailable")
                view = self._project_view(handle, reloaded)
                _log_ui_run_status(handle, reloaded)
                return view
        except AgentUiError:
            raise
        except Exception as error:
            lifecycle_event(
                "ui.service.failed",
                level=logging.ERROR,
                action="decide",
                handle=handle,
                public_code="run_unavailable",
                error_type=type(error).__name__,
            )
            raise AgentUiError("run_unavailable") from None

    def retry(
        self,
        handle: str,
        *,
        correction_id: str,
        reviewer: str,
        comment: str = "",
    ) -> AgentRunView:
        """Authorize the exact persisted attempt-one correction and run attempt two once."""

        if not isinstance(correction_id, str) or not correction_id:
            raise AgentUiError("invalid_correction")
        if not isinstance(reviewer, str) or not isinstance(comment, str):
            raise AgentUiError("invalid_reviewer")
        self._require_known_handle(handle)
        try:
            with self._lock_for(handle):
                status = self._load_status(handle)
                authorized_continuation = _is_authorized_retry_continuation_status(status)
                if status.execution_attempt != 1 and not authorized_continuation:
                    raise AgentUiError("retry_already_recorded")
                if not authorized_continuation and (
                    status.status != "completed"
                    or status.terminal_disposition != "recoverable_failure"
                ):
                    raise AgentUiError("retry_unavailable")

                store = ArtifactStore(self._run_dir(handle) / "evidence")
                correction = (
                    _verified_authorized_retry_continuation(store, status)[0]
                    if authorized_continuation
                    else _verified_retry_request(store, status)
                )
                if correction_id != correction.correction_id:
                    raise AgentUiError(
                        "retry_already_recorded"
                        if authorized_continuation
                        else "invalid_correction"
                    )
                try:
                    reconstructed_approval = CorrectionController.approve_retry(
                        correction,
                        presented_correction_id=correction_id,
                        reviewer=reviewer,
                        comment=comment,
                    )
                except ValueError:
                    raise AgentUiError("invalid_reviewer") from None
                try:
                    persisted_payload = store.read_json(_CORRECTION_APPROVAL_ATTEMPT_TWO_PATH)
                except FileNotFoundError:
                    if authorized_continuation:
                        raise AgentUiError("run_unavailable") from None
                    approval = reconstructed_approval
                else:
                    persisted_approval = CorrectionApproval.model_validate(persisted_payload)
                    if persisted_approval != reconstructed_approval:
                        raise AgentUiError("retry_already_recorded")
                    approval = persisted_approval

                lifecycle_event(
                    "ui.correction.retry.authorized",
                    handle=handle,
                    correction_id=correction.correction_id,
                    action=correction.action.value,
                    completed_attempt=correction.completed_attempt,
                    authorized_attempt=approval.authorized_attempt,
                    failed_signals=",".join(correction.failed_check_ids),
                )

                scenario = migration_scenario(status.platform)
                source = self._source_root(scenario.source_root)
                source_before = snapshot_tree(source)
                run_id, thread_id = self._run_thread_ids(handle)
                try:
                    retried = retry_agent_run(
                        self._project_root,
                        self._run_dir(handle),
                        run_id=run_id,
                        thread_id=thread_id,
                        approval=approval,
                        models=self._models_for_resume(status),
                    )
                finally:
                    # Keep this assertion on both success and failure paths.  A lower-layer
                    # regression must not be hidden merely because execution raised before
                    # returning a status.  TreeSnapshot equality includes file modes and
                    # directory shape as well as bytes.
                    if snapshot_tree(source) != source_before:
                        raise AgentUiError("run_unavailable")
                if retried.execution_attempt != 2:
                    raise AgentUiError("run_unavailable")
                if retried.status == "completed":
                    if retried.terminal_disposition not in {
                        "ready_for_human_review",
                        "recoverable_failure",
                        "plan_invalid",
                        "environment_unavailable",
                        "decision_required",
                    }:
                        raise AgentUiError("run_unavailable")
                elif retried.status == "decision_required":
                    if retried.terminal_disposition != "decision_required":
                        raise AgentUiError("run_unavailable")
                elif retried.status == "failed":
                    if (
                        retried.terminal_disposition != "controlled_failure"
                        or retried.failure is None
                        or retried.failure.attempt != 2
                    ):
                        raise AgentUiError("run_unavailable")
                else:
                    raise AgentUiError("run_unavailable")
                reloaded = self._load_status(handle)
                if reloaded != retried:
                    raise AgentUiError("run_unavailable")
                persisted_approval = CorrectionApproval.model_validate(
                    store.read_json(_CORRECTION_APPROVAL_ATTEMPT_TWO_PATH)
                )
                if persisted_approval != approval:
                    raise AgentUiError("run_unavailable")
                view = self._project_view(handle, reloaded)
                _log_ui_run_status(handle, reloaded)
                return view
        except AgentUiError:
            raise
        except Exception as error:
            lifecycle_event(
                "ui.service.failed",
                level=logging.ERROR,
                action="retry",
                handle=handle,
                public_code="run_unavailable",
                error_type=type(error).__name__,
            )
            raise AgentUiError("run_unavailable") from None

    def export_candidate(self, handle: str) -> CandidateExportResult:
        """Save one download-eligible candidate with its validation disposition."""

        self._require_known_handle(handle)
        try:
            with self._lock_for(handle):
                status = self._load_status(handle)
                view = self._project_view(handle, status)
                candidate = view.candidate
                validation = view.validation
                manifest = view.manifest
                if (
                    status.status != "completed"
                    or candidate is None
                    or not candidate.download_available
                    or validation is None
                    or manifest is None
                    or validation.attempt != candidate.attempt
                ):
                    raise AgentUiError("candidate_unavailable")
                result = persist_candidate_export(
                    project_root=self._project_root,
                    platform=view.platform,
                    handle=handle,
                    attempt=cast(Literal[1, 2], candidate.attempt),
                    terminal_validation_disposition=validation.disposition,
                    manifest_digest=manifest.manifest_digest,
                    change_set_digest=candidate.change_set_digest,
                    files=tuple((item.path, item.content) for item in candidate.files),
                )
                lifecycle_event(
                    "ui.candidate.exported",
                    handle=handle,
                    attempt=result.attempt,
                    disposition=result.validation_disposition,
                    file_count=result.file_count,
                )
                return result
        except AgentUiError:
            raise
        except Exception as error:
            lifecycle_event(
                "ui.service.failed",
                level=logging.ERROR,
                action="candidate_export",
                handle=handle,
                public_code="candidate_export_unavailable",
                error_type=type(error).__name__,
            )
            raise AgentUiError("candidate_export_unavailable") from None

    def candidate_zip(self, handle: str) -> bytes:
        """Return the same deterministic ZIP bytes used by the output exporter."""

        self._require_known_handle(handle)
        try:
            with self._lock_for(handle):
                view = self._project_view(handle, self._load_status(handle))
                if view.candidate is None or not view.candidate.download_available:
                    raise AgentUiError("candidate_unavailable")
                return build_candidate_archive(
                    tuple((item.path, item.content) for item in view.candidate.files)
                )
        except AgentUiError:
            raise
        except Exception as error:
            lifecycle_event(
                "ui.service.failed",
                level=logging.ERROR,
                action="candidate_zip",
                handle=handle,
                public_code="run_unavailable",
                error_type=type(error).__name__,
            )
            raise AgentUiError("run_unavailable") from None

    def _project_view(self, handle: str, status: AgentRunStatus) -> AgentRunView:
        scenario = migration_scenario(status.platform)
        store = ArtifactStore(self._run_dir(handle) / "evidence")
        run_context = AgentRunContext.model_validate(store.read_json("run-context.json"))
        if run_context.slice_id != scenario.scenario_id:
            raise AgentUiError("run_unavailable")
        request = MigrationRequest.model_validate(store.read_json("request.json"))
        if request.request_id != status.request_id or request.platform is not status.platform:
            raise AgentUiError("run_unavailable")
        model_root = f"model-runs/{status.request_id}"
        graph, wiki_trace = self._context_evidence(store, model_root)
        try:
            architect_payload = store.read_json(f"{model_root}/architect.json")
        except FileNotFoundError:
            return self._project_pre_manifest_terminal(
                handle,
                status,
                scenario_id=scenario.scenario_id,
                scenario_title=scenario.title,
                request=request,
                store=store,
                model_root=model_root,
                graph=graph,
                wiki_trace=wiki_trace,
            )
        architect = ArchitectRun.model_validate(architect_payload)
        manifest = architect.proposal.manifest
        if status.manifest_id != manifest.manifest_id or status.manifest_digest != artifact_digest(
            manifest
        ):
            raise AgentUiError("run_unavailable")
        if graph is None or wiki_trace is None:
            raise AgentUiError("run_unavailable")
        planning_decision = _architect_planning_decision(architect, status)
        candidate, validation, terminal_calls, intervention = self._terminal_evidence(
            store,
            model_root,
            manifest,
            status,
        )
        correction = _correction_view(store, status, model_root=model_root)
        attempt_history = _attempt_history(store, model_root, manifest, status)
        prior_calls = _prior_attempt_model_calls(store, model_root, manifest, status)
        model_calls = (
            architect.model_call,
            *tuple(call for call, _ in prior_calls),
            *terminal_calls,
        )
        boundaries = self._project_boundaries(status, model_calls)
        human_decision = _human_decision_view(store, status, manifest)
        return AgentRunView(
            handle=handle,
            platform=status.platform.value,
            scenario_id=scenario.scenario_id,
            scenario_title=scenario.title,
            prompt=request.target.description,
            status=status.status,
            terminal_disposition=status.terminal_disposition,
            execution_attempt=status.execution_attempt,
            boundaries=boundaries,
            stages=_stages(
                status,
                graph,
                wiki_trace,
                candidate,
                planning_decision,
            ),
            metrics=AgentMetricsView(
                graph_nodes=len(graph.nodes),
                graph_edges=len(graph.edges),
                wiki_hits=len(wiki_trace.hits),
                changed_files=0 if candidate is None else len(candidate.changed_paths),
            ),
            model_calls=(
                _model_call_view(architect.model_call, attempt=1),
                *tuple(_model_call_view(call, attempt=attempt) for call, attempt in prior_calls),
                *tuple(
                    _model_call_view(call, attempt=status.execution_attempt)
                    for call in terminal_calls
                ),
            ),
            manifest=_manifest_view(architect),
            evidence=AgentEvidenceView(
                wiki_hits=tuple(
                    AgentWikiHitView(page_id=hit.page_id, title=hit.title, path=hit.path)
                    for hit in wiki_trace.hits
                ),
                graph_summary=(
                    f"{len(graph.nodes)} dependency nodes and {len(graph.edges)} edges were "
                    f"derived from the fixed {status.platform.value} source fixture."
                ),
            ),
            candidate=candidate,
            validation=validation,
            intervention=intervention,
            planning_decision=planning_decision,
            human_decision=human_decision,
            correction=correction,
            attempt_history=attempt_history,
            final_review=_final_review_view(
                self._project_root,
                self._run_dir(handle),
                status,
                store,
            ),
            failure=_failure_view(status),
        )

    def _terminal_evidence(
        self,
        store: ArtifactStore,
        model_root: str,
        manifest: MigrationManifest,
        status: AgentRunStatus,
    ) -> tuple[
        AgentCandidateView | None,
        AgentValidationView | None,
        tuple[ModelCallRecord, ...],
        AgentInterventionView | None,
    ]:
        attempt = status.execution_attempt
        if _is_authorized_retry_continuation_status(status):
            engineer = EngineerRun.model_validate(
                store.read_json(f"{model_root}/engineer-attempt-1.json")
            )
            report = ValidationReport.model_validate(
                store.read_json(f"{model_root}/report-attempt-1.json")
            )
            assessment = ValidatorAssessment.model_validate(
                store.read_json(f"{model_root}/validator-attempt-1.json")
            )
            change_set = engineer.change_set
            if (
                change_set is None
                or report.attempt != 1
                or report.disposition is not ValidationDisposition.RECOVERABLE_FAILURE
                or assessment.authoritative_disposition is not report.disposition
                or assessment.advisory.manifest_digest != artifact_digest(manifest)
                or assessment.advisory.change_set_digest != artifact_digest(change_set)
                or assessment.advisory.report_digest != artifact_digest(report)
            ):
                raise AgentUiError("run_unavailable")
            validate_report(report, manifest, change_set)
            return (
                _candidate_view(
                    engineer,
                    manifest,
                    attempt=1,
                    download_available=False,
                ),
                _validation_view(report, engineer, manifest, assessment=assessment),
                (),
                None,
            )
        if status.status == "decision_required":
            if manifest.status.value == "decision_required":
                try:
                    store.read_json(f"{model_root}/engineer-attempt-{attempt}.json")
                except FileNotFoundError:
                    pass
                else:
                    raise AgentUiError("run_unavailable")
                return None, None, (), None
            engineer = EngineerRun.model_validate(
                store.read_json(f"{model_root}/engineer-attempt-{attempt}.json")
            )
            intervention = engineer.intervention
            if (
                intervention is None
                or engineer.change_set is not None
                or engineer.file_plan is not None
                or intervention.manifest_id != manifest.manifest_id
                or intervention.manifest_digest != artifact_digest(manifest)
                or status.terminal_disposition != "decision_required"
            ):
                raise AgentUiError("run_unavailable")
            return (
                (_prior_candidate_view(store, model_root, manifest) if attempt == 2 else None),
                None,
                (engineer.model_call,),
                _implementation_intervention_view(intervention),
            )
        if status.status == "failed":
            failure = status.failure
            if failure is None or status.terminal_disposition != "controlled_failure":
                raise AgentUiError("run_unavailable")
            engineer_path = f"{model_root}/engineer-attempt-{attempt}.json"
            try:
                engineer_payload = store.read_json(engineer_path)
            except FileNotFoundError:
                failed_engineer: EngineerRun | None = None
            else:
                failed_engineer = EngineerRun.model_validate(engineer_payload)
            if failure.seam in {"validator", "deterministic_validator"} and failed_engineer is None:
                raise AgentUiError("run_unavailable")
            if failure.seam == "engineer" and failed_engineer is not None:
                raise AgentUiError("run_unavailable")
            terminal_calls = () if failed_engineer is None else (failed_engineer.model_call,)
            if failed_engineer is None:
                return (
                    (_prior_candidate_view(store, model_root, manifest) if attempt == 2 else None),
                    None,
                    terminal_calls,
                    None,
                )
            candidate = _candidate_view(
                failed_engineer,
                manifest,
                attempt=attempt,
                download_available=False,
            )
            if failure.seam != "validator":
                return candidate, None, terminal_calls, None
            report = ValidationReport.model_validate(
                store.read_json(f"{model_root}/report-attempt-{attempt}.json")
            )
            validation = _validation_view(
                report,
                failed_engineer,
                manifest,
                assessment=None,
            )
            return candidate, validation, terminal_calls, None
        if status.status != "completed":
            return None, None, (), None
        engineer = EngineerRun.model_validate(
            store.read_json(f"{model_root}/engineer-attempt-{attempt}.json")
        )
        report = ValidationReport.model_validate(
            store.read_json(f"{model_root}/report-attempt-{attempt}.json")
        )
        assessment = ValidatorAssessment.model_validate(
            store.read_json(f"{model_root}/validator-attempt-{attempt}.json")
        )
        change_set = engineer.change_set
        if change_set is None:
            raise AgentUiError("run_unavailable")
        if (
            assessment.advisory.manifest_digest != artifact_digest(manifest)
            or assessment.advisory.change_set_digest != artifact_digest(change_set)
            or assessment.advisory.report_digest != artifact_digest(report)
            or assessment.authoritative_disposition is not report.disposition
            or status.terminal_disposition != report.disposition.value
        ):
            raise AgentUiError("run_unavailable")
        return (
            _candidate_view(
                engineer,
                manifest,
                attempt=attempt,
                download_available=True,
            ),
            _validation_view(report, engineer, manifest, assessment=assessment),
            (
                (engineer.model_call,)
                if assessment.model_call is None
                else (engineer.model_call, assessment.model_call)
            ),
            None,
        )

    def _context_evidence(
        self,
        store: ArtifactStore,
        model_root: str,
    ) -> tuple[DependencyGraph | None, RetrievalTrace | None]:
        try:
            graph_payload = store.read_json(f"{model_root}/dependency-graph.json")
        except FileNotFoundError:
            graph = None
        else:
            graph = DependencyGraph.model_validate(graph_payload)
        try:
            wiki_payload = store.read_json(f"{model_root}/wiki-trace.json")
        except FileNotFoundError:
            wiki_trace = None
        else:
            wiki_trace = RetrievalTrace.model_validate(wiki_payload)
        return graph, wiki_trace

    def _project_pre_manifest_terminal(
        self,
        handle: str,
        status: AgentRunStatus,
        *,
        scenario_id: str,
        scenario_title: str,
        request: MigrationRequest,
        store: ArtifactStore,
        model_root: str,
        graph: DependencyGraph | None,
        wiki_trace: RetrievalTrace | None,
    ) -> AgentRunView:
        intervention: AgentInterventionView | None = None
        if status.status == "decision_required" and status.failure is None:
            planning = PlanningIntervention.model_validate(
                store.read_json(f"{model_root}/architect-preflight.json")
            )
            if (
                planning.request_id != request.request_id
                or planning.request_digest != artifact_digest(request)
                or planning.platform is not request.platform
                or planning.base_revision != request.base_revision
                or status.terminal_disposition != "decision_required"
            ):
                raise AgentUiError("run_unavailable")
            intervention = _planning_intervention_view(planning)
        elif (
            status.status != "failed"
            or status.failure is None
            or status.failure.seam != "architect"
            or status.terminal_disposition != "controlled_failure"
        ):
            raise AgentUiError("run_unavailable")
        boundaries = self._project_boundaries_without_call(status)
        graph_nodes = 0 if graph is None else len(graph.nodes)
        graph_edges = 0 if graph is None else len(graph.edges)
        wiki_hits = () if wiki_trace is None else wiki_trace.hits
        graph_summary = (
            "The controller stopped before complete dependency-graph evidence was available."
            if graph is None
            else (
                f"{graph_nodes} dependency nodes and {graph_edges} edges were derived before "
                "the workflow stopped."
            )
        )
        return AgentRunView(
            handle=handle,
            platform=status.platform.value,
            scenario_id=scenario_id,
            scenario_title=scenario_title,
            prompt=request.target.description,
            status=status.status,
            terminal_disposition=status.terminal_disposition,
            execution_attempt=status.execution_attempt,
            boundaries=boundaries,
            stages=_pre_manifest_terminal_stages(status),
            metrics=AgentMetricsView(
                graph_nodes=graph_nodes,
                graph_edges=graph_edges,
                wiki_hits=len(wiki_hits),
                changed_files=0,
            ),
            manifest=None,
            evidence=AgentEvidenceView(
                wiki_hits=tuple(
                    AgentWikiHitView(page_id=hit.page_id, title=hit.title, path=hit.path)
                    for hit in wiki_hits
                ),
                graph_summary=graph_summary,
            ),
            candidate=None,
            validation=None,
            intervention=intervention,
            final_review=_final_review_view(
                self._project_root,
                self._run_dir(handle),
                status,
                store,
            ),
            failure=_failure_view(status),
        )

    def _project_boundaries(
        self,
        status: AgentRunStatus,
        calls: tuple[ModelCallRecord, ...],
    ) -> AgentBoundariesView:
        """Project runtime truth only after cross-checking persisted role calls."""

        if not calls or any(call.execution_boundary is None for call in calls):
            raise AgentUiError("run_unavailable")
        identities = {(call.provider, call.model_id) for call in calls}
        boundaries = {call.resolved_execution_boundary for call in calls}
        if identities != {(status.provider_id, status.model_id)} or len(boundaries) != 1:
            raise AgentUiError("run_unavailable")
        execution_boundary = next(iter(boundaries))
        revisions = {call.model_revision for call in calls}
        if len(revisions) != 1:
            raise AgentUiError("run_unavailable")
        model_revision = next(iter(revisions))
        if (
            execution_boundary == "local_loopback"
            and status.provider_id == _OLLAMA_RUNTIME_PROVIDER
            and model_revision is not None
        ):
            active_failure_seam = None if status.failure is None else status.failure.seam
            return AgentBoundariesView(
                provider_attempted=True,
                provider_invoked=True,
                model_call_record_persisted=True,
                structured_response_accepted=active_failure_seam
                not in {
                    "engineer",
                    "validator",
                },
                provider_id=status.provider_id,
                model_id=status.model_id,
                model_revision=model_revision,
                execution_boundary="local_loopback",
                mode="local_ollama",
                notice=_LOCAL_BOUNDARY_NOTICE,
            )
        raise AgentUiError("run_unavailable")

    def _project_boundaries_without_call(
        self,
        status: AgentRunStatus,
    ) -> AgentBoundariesView:
        if status.provider_id == _OLLAMA_RUNTIME_PROVIDER:
            attempted = status.failure is not None and status.failure.seam == "architect"
            response_accepted = bool(
                status.failure is not None
                and _resolved_failure_reason(status.failure)
                in {
                    "implementation_contract_invalid",
                    "required_approval_missing",
                    "transformation_scope_invalid",
                    "unresolved_question_risk_missing",
                    "policy_rejected",
                }
            )
            if response_accepted:
                notice = (
                    "A loopback Ollama response passed structured-output validation, then the "
                    "controller rejected it at the policy boundary. No model-call record was "
                    "claimed and no external action was invoked."
                )
            elif attempted:
                notice = (
                    "A loopback Ollama call was attempted, but no accepted structured response "
                    "or model-call record was persisted. No external action was invoked."
                )
            else:
                notice = (
                    "The controller stopped before invoking the configured loopback Ollama "
                    "model. No external action was invoked."
                )
            return AgentBoundariesView(
                provider_attempted=attempted,
                provider_invoked=True if response_accepted else None if attempted else False,
                model_call_record_persisted=False,
                structured_response_accepted=response_accepted,
                provider_id=status.provider_id,
                model_id=status.model_id,
                model_revision=None,
                execution_boundary="local_loopback",
                mode="local_ollama",
                notice=notice,
            )
        raise AgentUiError("run_unavailable")

    @staticmethod
    def _validate_request_secret_boundary(value: str) -> None:
        try:
            assert_agent_request_secret_free(value)
        except PolicyViolation:
            raise AgentUiError("secret_material") from None

    def _verify_conversation_snapshot(
        self,
        snapshot: ArchitectConversationSnapshot,
    ) -> None:
        """Replay-bind every public turn and enforce one local model revision."""

        models = self._local_models(self._ollama_model_id)
        architect = ArchitectAgent(
            load_agent_registry(self._project_root / "agents"),
            models.architect,
        )
        prior_exchanges: list[ArchitectConversationExchange] = []
        expected_revision: str | None = None
        for exchange in snapshot.exchanges:
            partial = ArchitectConversationSnapshot(
                header=snapshot.header,
                exchanges=tuple(prior_exchanges),
            )
            launch_contract = None
            if exchange.scenario_id is not None:
                try:
                    launch_contract = migration_launch_contract(exchange.scenario_id)
                except KeyError:
                    raise AgentUiError("conversation_unavailable") from None
                if launch_contract.platform is not exchange.selected_platform:
                    raise AgentUiError("conversation_unavailable")
            scenario = (
                None
                if launch_contract is None
                else migration_scenario_by_id(launch_contract.scenario_id)
            )
            context = ArchitectConversationContext(
                selected_platform=exchange.selected_platform,
                scenario_id=(None if launch_contract is None else launch_contract.scenario_id),
                source_artifacts=(() if scenario is None else scenario.display_source_artifacts),
                target_summary=(
                    None if launch_contract is None else launch_contract.target_summary
                ),
                canonical_request=(
                    None if launch_contract is None else launch_contract.canonical_description
                ),
                launch_contract_digest=exchange.launch_contract_digest,
                history=conversation_history(partial, exchange.user_message),
            )
            call = exchange.architect_run.model_call
            if (
                call.provider != _OLLAMA_RUNTIME_PROVIDER
                or call.model_id != self._ollama_model_id
                or call.resolved_execution_boundary != "local_loopback"
                or call.model_revision is None
            ):
                raise AgentUiError("conversation_unavailable")
            if expected_revision is None:
                expected_revision = call.model_revision
            elif call.model_revision != expected_revision:
                raise AgentUiError("conversation_unavailable")
            architect.verify_conversation_replay(exchange.architect_run, context)
            prior_exchanges.append(exchange)

    @staticmethod
    def _verify_conversation_launch_binding(
        conversation: ArchitectConversationView,
        run: AgentRunView,
        *,
        expected_model_revision: str,
    ) -> None:
        """Require exact scenario-contract and model-revision launch provenance."""

        revision_bound = run.boundaries.model_revision == expected_model_revision
        if (
            conversation.readiness.platform is None
            or conversation.readiness.scenario_id is None
            or conversation.readiness.canonical_request is None
            or conversation.readiness.launch_contract_digest is None
            or run.platform != conversation.readiness.platform.value
            or run.scenario_id != conversation.readiness.scenario_id
            or run.prompt != conversation.readiness.canonical_request
            or artifact_digest(migration_launch_contract(conversation.readiness.scenario_id))
            != conversation.readiness.launch_contract_digest
            or (
                not revision_bound
                and not _is_verified_pre_manifest_terminal_without_model_record(run)
            )
            or (conversation.launch_handle is not None and run.handle != conversation.launch_handle)
        ):
            raise AgentUiError("conversation_unavailable")

    @staticmethod
    def _conversation_launch_contract(
        scenario_id: str | None,
    ) -> MigrationLaunchContract | None:
        if scenario_id is None:
            return None
        if not isinstance(scenario_id, str):
            raise AgentUiError("invalid_platform")
        try:
            return migration_launch_contract(scenario_id)
        except KeyError:
            raise AgentUiError("invalid_platform") from None

    @staticmethod
    def _validate_conversation_id(value: str) -> str:
        if not isinstance(value, str) or _HANDLE_PATTERN.fullmatch(value) is None:
            raise AgentUiError("unknown_conversation")
        return value

    @staticmethod
    def _validate_conversation_message(value: str) -> str:
        if not isinstance(value, str):
            raise AgentUiError("invalid_message")
        normalized = value.strip()
        if (
            not 1 <= len(normalized) <= 2_000
            or "\x00" in normalized
            or any(
                ord(character) < 32 and character not in {"\n", "\t"} for character in normalized
            )
        ):
            raise AgentUiError("invalid_message")
        AgentUiService._validate_request_secret_boundary(normalized)
        return normalized

    def _models_for_resume(
        self,
        status: AgentRunStatus,
    ) -> AgentRunModelClients:
        if (
            status.provider_id == _OLLAMA_RUNTIME_PROVIDER
            and self._ollama_model_id == status.model_id
        ):
            return self._local_models(status.model_id)
        raise AgentUiError("run_unavailable")

    def _local_models(self, model_id: str) -> AgentRunModelClients:
        return build_local_ollama_model_clients(
            model_id=model_id,
            timeout_seconds=self._ollama_timeout_seconds,
            approval=LiveModelApproval(
                allow_live_api=True,
                allow_prompt_data_sharing=True,
                approved_by=_LOCAL_MODEL_APPROVER,
            ),
        )

    def _load_status(self, handle: str) -> AgentRunStatus:
        run_id, thread_id = self._run_thread_ids(handle)
        return get_agent_run_status(
            self._project_root,
            self._run_dir(handle),
            run_id=run_id,
            thread_id=thread_id,
        )

    def _get_verified_view(self, handle: str) -> AgentRunView:
        """Project one exact run without choosing a public logging policy."""

        self._require_known_handle(handle)
        with self._lock_for(handle):
            return self._project_view(handle, self._load_status(handle))

    def _engineer_artifact_exists(self, status: AgentRunStatus) -> bool:
        path = (
            self._run_dir(_handle_from_run_id(status.run_id))
            / "evidence"
            / "model-runs"
            / status.request_id
            / "engineer-attempt-1.json"
        )
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    def _prepare_run_root(self) -> None:
        parent = self._project_root / ".runs"
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        parent_metadata = parent.lstat()
        if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
            raise AgentUiError("run_unavailable")
        parent.resolve(strict=True).relative_to(self._project_root)

        self._run_root.mkdir(mode=0o700, exist_ok=True)
        metadata = self._run_root.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AgentUiError("run_unavailable")
        self._run_root.resolve(strict=True).relative_to(self._project_root)

    def _active_owned_run_count(self) -> int:
        """Count incomplete runs, treating unreadable owned entries as active.

        Terminal evidence remains available for readback but does not consume
        the concurrent-run allowance.  A valid-handle entry is never ignored
        merely because its type or durable evidence is corrupt.
        """

        count = 0
        for child in self._run_root.iterdir():
            if _HANDLE_PATTERN.fullmatch(child.name) is None:
                continue
            try:
                metadata = child.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    count += 1
                    continue
                child.resolve(strict=True).relative_to(self._run_root)
                status = self._load_status(child.name)
            except Exception:
                count += 1
                continue
            if status.status not in _TERMINAL_UI_RUN_STATUSES:
                count += 1
        return count

    def _new_handle(self) -> str:
        for _ in range(32):
            handle = secrets.token_hex(12)
            if not self._run_dir(handle).exists():
                return handle
        raise AgentUiError("run_unavailable")

    def _new_conversation_id(self, store: ArchitectConversationStore) -> str:
        for _ in range(32):
            conversation_id = secrets.token_hex(12)
            if not (store.root / conversation_id).exists():
                return conversation_id
        raise AgentUiError("conversation_unavailable")

    def _conversation_store(self) -> ArchitectConversationStore:
        return ArchitectConversationStore(self._run_root / "conversations")

    def _require_known_handle(self, handle: str) -> None:
        if not isinstance(handle, str) or _HANDLE_PATTERN.fullmatch(handle) is None:
            raise AgentUiError("unknown_run")
        path = self._run_dir(handle)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            raise AgentUiError("unknown_run") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AgentUiError("unknown_run")

    def _lock_for(self, handle: str) -> threading.RLock:
        return _shared_service_lock(self._run_root, handle)

    def _run_dir(self, handle: str) -> Path:
        return self._run_root / handle

    def _source_root(self, source_relative: str) -> Path:
        source = self._project_root.joinpath(*source_relative.split("/"))
        resolved = source.resolve(strict=True)
        resolved.relative_to(self._project_root)
        metadata = source.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AgentUiError("run_unavailable")
        return resolved

    @staticmethod
    def _identities(handle: str) -> tuple[str, str, str]:
        return f"ui-request-{handle}", f"ui-run-{handle}", f"ui-thread-{handle}"

    @staticmethod
    def _run_thread_ids(handle: str) -> tuple[str, str]:
        _, run_id, thread_id = AgentUiService._identities(handle)
        return run_id, thread_id


def _is_verified_pre_manifest_terminal_without_model_record(run: AgentRunView) -> bool:
    """Allow an exact reserved launch to publish its controlled terminal run.

    The UI binds the intake revision on the Ollama client before calling the
    run starter. A run directory can therefore exist at the reserved handle
    only after that revision check succeeded. When Architect generation or
    policy validation then fails before a ``ModelCallRecord`` is accepted,
    there is deliberately no model revision in the public run projection.
    The exact contract, scenario, prompt, and reserved handle remain checked
    separately by ``_verify_conversation_launch_binding``.
    """

    if (
        run.execution_attempt != 1
        or run.manifest is not None
        or run.candidate is not None
        or run.validation is not None
        or run.model_calls
        or run.boundaries.model_call_record_persisted
        or run.boundaries.model_revision is not None
        or run.boundaries.execution_boundary != "local_loopback"
        or run.boundaries.mode != "local_ollama"
    ):
        return False
    if run.status == "decision_required":
        return (
            run.terminal_disposition == "decision_required"
            and run.intervention is not None
            and run.failure is None
        )
    return (
        run.status == "failed"
        and run.terminal_disposition == "controlled_failure"
        and run.intervention is None
        and run.failure is not None
        and run.failure.seam == "architect"
        and run.failure.attempt == 1
    )


def _validate_final_review_actor(value: object) -> str:
    if not isinstance(value, str) or _FINAL_REVIEW_ACTOR_PATTERN.fullmatch(value) is None:
        raise AgentUiError("invalid_reviewer")
    if SecretRedactor().redact(value).changed:
        raise AgentUiError("secret_material")
    return value


def _is_utc_timestamp(value: object) -> bool:
    return bool(
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


def _valid_final_review_window(requested_at: object, expires_at: object) -> bool:
    if not _is_utc_timestamp(requested_at) or not _is_utc_timestamp(expires_at):
        return False
    assert isinstance(requested_at, datetime)
    assert isinstance(expires_at, datetime)
    return requested_at < expires_at <= requested_at + _MAX_FINAL_REVIEW_WINDOW


def _final_review_eligible(status: AgentRunStatus) -> bool:
    return bool(
        status.status == "completed"
        and status.terminal_disposition == ValidationDisposition.READY_FOR_HUMAN_REVIEW.value
        and status.failure is None
        and not status.pending_nodes
    )


def _final_review_view(
    project_root: Path,
    run_dir: Path,
    run_status: AgentRunStatus,
    store: ArtifactStore,
) -> AgentFinalReviewView:
    """Verify and project final-review evidence without treating it as authority."""

    eligible = _final_review_eligible(run_status)
    try:
        request_payload = store.read_json(FINAL_REVIEW_REQUEST_PATH)
    except FileNotFoundError:
        # A lifecycle index or later artifact without its request is corruption, not a
        # fresh review opportunity.
        for path in (
            "indexes/final-review-requested.json",
            "indexes/final-review-decided.json",
            FINAL_REVIEW_DECISION_PATH,
            FINAL_REVIEW_RECORD_PATH,
        ):
            try:
                store.read_json(path)
            except FileNotFoundError:
                continue
            raise AgentUiError("run_unavailable") from None
        return AgentFinalReviewView(
            status="not_requested",
            eligible=eligible,
            can_request=eligible,
            can_decide=False,
        )

    request = FinalReviewRequest.model_validate(request_payload)
    status = get_final_review_status_for_run(
        project_root,
        run_dir,
        run_id=run_status.run_id,
        thread_id=run_status.thread_id,
    )
    if (
        request.run_id != run_status.run_id
        or request.thread_id != run_status.thread_id
        or request.request_id != run_status.request_id
        or request.review_id != status.review_id
        or artifact_digest(request) != status.request_digest
        or request.authority_granted
        or status.external_actions_authorized
    ):
        raise AgentUiError("run_unavailable")

    if status.status == "awaiting_final_review":
        for path in (FINAL_REVIEW_DECISION_PATH, FINAL_REVIEW_RECORD_PATH):
            try:
                store.read_json(path)
            except FileNotFoundError:
                continue
            raise AgentUiError("run_unavailable") from None
        return AgentFinalReviewView(
            status=status.status,
            eligible=eligible,
            can_request=False,
            can_decide=eligible and datetime.now(UTC) <= request.expires_at,
            review_id=request.review_id,
            requester=request.requester,
            designated_reviewer=request.designated_reviewer,
            requested_at=request.requested_at,
            expires_at=request.expires_at,
        )

    decision = FinalReviewDecision.model_validate(store.read_json(FINAL_REVIEW_DECISION_PATH))
    record = FinalReviewRecord.model_validate(store.read_json(FINAL_REVIEW_RECORD_PATH))
    expected_outcome = {
        "accept": "accepted",
        "reject": "rejected",
        "request_changes": "changes_requested",
    }[decision.selection]
    if (
        decision.review_id != request.review_id
        or decision.review_request_digest != artifact_digest(request)
        or decision.reviewer != request.designated_reviewer
        or decision.authority_granted
        or artifact_digest(decision) != status.decision_digest
        or record.review_id != request.review_id
        or record.review_request_digest != artifact_digest(request)
        or record.decision_id != decision.decision_id
        or record.decision_digest != artifact_digest(decision)
        or record.outcome != expected_outcome
        or record.outcome != status.status
        or record.candidate_accepted != status.candidate_accepted
        or record.run_id != request.run_id
        or record.thread_id != request.thread_id
        or record.request_id != request.request_id
        or record.manifest_digest != request.manifest_digest
        or record.change_set_digest != request.change_set_digest
        or record.validation_report_digest != request.validation_report_digest
        or record.source_revision != request.source_revision
        or record.reviewer != decision.reviewer
        or record.decided_at != decision.decided_at
        or record.external_actions_authorized
        or record.source_mutated
        or record.deployment_performed
        or record.publication_performed
    ):
        raise AgentUiError("run_unavailable")
    return AgentFinalReviewView(
        status=status.status,
        eligible=eligible,
        can_request=False,
        can_decide=False,
        review_id=request.review_id,
        requester=request.requester,
        designated_reviewer=request.designated_reviewer,
        requested_at=request.requested_at,
        expires_at=request.expires_at,
        selection=decision.selection,
        reviewer=decision.reviewer,
        decided_at=decision.decided_at,
        comment=decision.comment,
        candidate_accepted=record.candidate_accepted,
        next_action=record.next_action,
    )


def _log_ui_run_status(handle: str, status: AgentRunStatus) -> None:
    if status.status == "awaiting_approval":
        event = "ui.manifest.awaiting_approval"
    elif status.status == "decision_required":
        event = "ui.run.decision_required"
    elif status.status in {"completed", "failed", "rejected", "modification_requested"}:
        event = "ui.run.terminal"
    else:
        event = "ui.run.status"
    lifecycle_event(
        event,
        handle=handle,
        workflow_status=status.status,
        disposition=status.terminal_disposition,
        attempt=status.execution_attempt,
    )


def _safe_project_root(path: Path) -> Path:
    root = path.resolve(strict=True)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("project_root must be a non-symlink directory")
    for required in ("agents", "knowledge/wiki", "fixtures"):
        candidate = root.joinpath(*required.split("/"))
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        child_metadata = candidate.lstat()
        if stat.S_ISLNK(child_metadata.st_mode) or not stat.S_ISDIR(child_metadata.st_mode):
            raise ValueError("project_root is missing required agent UI assets")
    return root


def _normalize_ollama_model_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("ollama_model_id must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > 300:
        raise ValueError("ollama_model_id must contain 1 to 300 characters")
    if any(character in normalized for character in ("\x00", "\r", "\n")):
        raise ValueError("ollama_model_id contains a forbidden control character")
    return normalized


def _normalize_ollama_timeout_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("ollama_timeout_seconds must be a number")
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or not MIN_OLLAMA_TIMEOUT_SECONDS <= normalized <= MAX_OLLAMA_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "ollama_timeout_seconds must be between "
            f"{MIN_OLLAMA_TIMEOUT_SECONDS:g} and {MAX_OLLAMA_TIMEOUT_SECONDS:g}"
        )
    return normalized


def _handle_from_run_id(run_id: str) -> str:
    prefix = "ui-run-"
    if not run_id.startswith(prefix):
        raise AgentUiError("run_unavailable")
    handle = run_id.removeprefix(prefix)
    if _HANDLE_PATTERN.fullmatch(handle) is None:
        raise AgentUiError("run_unavailable")
    return handle


def _agent_requested_at() -> datetime:
    return datetime.now(UTC)


def _shared_service_lock(run_root: Path, key: str) -> threading.RLock:
    """Serialize one run or conversation across service instances in this process."""

    with _SERVICE_LOCKS_GUARD:
        return _SERVICE_LOCKS.setdefault((run_root, key), threading.RLock())


def _manifest_view(architect: ArchitectRun) -> AgentManifestView:
    manifest = architect.proposal.manifest
    return AgentManifestView(
        approval_required=manifest.status.value == "planned",
        manifest_id=manifest.manifest_id,
        manifest_digest=artifact_digest(manifest),
        status=manifest.status.value,
        approved_paths=manifest.approved_paths,
        public_decisions=tuple(
            decision.summary for decision in architect.agent_output.semantic_decisions
        ),
        implementation_contract=manifest.implementation_contract,
        cited_graph_nodes=architect.agent_output.cited_graph_nodes,
        cited_wiki_pages=architect.agent_output.cited_wiki_pages,
        unresolved_questions=architect.agent_output.unresolved_questions,
        transformations=tuple(
            AgentTransformationView(
                step_id=item.step_id,
                description=item.description,
                input_paths=item.input_paths,
                output_paths=item.output_paths,
            )
            for item in manifest.transformations
        ),
        validation_commands=tuple(
            AgentValidationCommandView(
                check_id=item.check_id,
                command_id=item.command_id,
                purpose=item.purpose,
                environment=item.environment.value,
                required=item.required,
            )
            for item in manifest.validation_plan
        ),
        risks=tuple(
            AgentRiskView(
                category=item.category.value,
                summary=item.summary,
                evidence=item.evidence,
                requires_human_decision=item.requires_human_decision,
            )
            for item in manifest.risks
        ),
        required_approvals=tuple(item.value for item in manifest.required_approvals),
    )


def _prior_candidate_view(
    store: ArtifactStore,
    model_root: str,
    manifest: MigrationManifest,
) -> AgentCandidateView:
    """Return the verified attempt-one candidate as read-only retry evidence."""

    engineer = EngineerRun.model_validate(store.read_json(f"{model_root}/engineer-attempt-1.json"))
    report = ValidationReport.model_validate(store.read_json(f"{model_root}/report-attempt-1.json"))
    change_set = engineer.change_set
    if (
        change_set is None
        or report.attempt != 1
        or report.disposition is not ValidationDisposition.RECOVERABLE_FAILURE
    ):
        raise AgentUiError("run_unavailable")
    validate_report(report, manifest, change_set)
    return _candidate_view(
        engineer,
        manifest,
        attempt=1,
        download_available=False,
    )


def _candidate_view(
    engineer: EngineerRun,
    manifest: MigrationManifest,
    *,
    attempt: int,
    download_available: bool,
) -> AgentCandidateView:
    change_set = engineer.change_set
    file_plan = engineer.file_plan
    if change_set is None or file_plan is None or engineer.intervention is not None:
        raise AgentUiError("run_unavailable")
    validate_change_set(change_set, manifest)
    if tuple(sorted(change_set.changed_paths)) != tuple(
        sorted(update.path for update in file_plan.updates)
    ) or tuple(sorted(change_set.changed_paths)) != tuple(sorted(manifest.approved_paths)):
        raise AgentUiError("run_unavailable")
    return AgentCandidateView(
        attempt=attempt,
        download_available=download_available,
        change_set_digest=artifact_digest(change_set),
        changed_paths=change_set.changed_paths,
        unified_diff=change_set.unified_diff,
        files=tuple(
            AgentCandidateFileView(path=update.path, content=update.content)
            for update in sorted(file_plan.updates, key=lambda item: item.path)
        ),
        assumptions=change_set.assumptions,
    )


def _validation_view(
    report: ValidationReport,
    engineer: EngineerRun,
    manifest: MigrationManifest,
    *,
    assessment: ValidatorAssessment | None,
) -> AgentValidationView:
    change_set = engineer.change_set
    if change_set is None:
        raise AgentUiError("run_unavailable")
    validate_report(report, manifest, change_set)
    advisory_assessment: Literal["supports_report", "raises_concern", "escalate"] | None
    if assessment is None or assessment.model_call is None:
        summary = (
            assessment.advisory.summary
            if assessment is not None
            else (
                "Controller-owned deterministic checks completed and remain authoritative. "
                "The Validator model advisory did not complete."
            )
        )
        advisory_assessment = None
        concerns: tuple[str, ...] = ()
        validator_completed = False
    else:
        summary = assessment.advisory.summary
        raw_advisory_assessment = assessment.advisory.assessment
        if raw_advisory_assessment == "unavailable":
            raise AgentUiError("run_unavailable")
        advisory_assessment = raw_advisory_assessment
        concerns = assessment.advisory.concerns
        validator_completed = True
    dependencies = validation_failure_dependencies(report)
    return AgentValidationView(
        attempt=report.attempt,
        disposition=report.disposition.value,
        results=tuple(
            AgentValidationResultView(
                check_id=result.check_id,
                status=result.status.value,
                summary=result.summary,
                diagnostic_ids=result.diagnostic_ids,
                dependent_on=dependencies.get(result.check_id),
            )
            for result in report.results
        ),
        validator_summary=summary,
        advisory_assessment=advisory_assessment,
        validator_completed=validator_completed,
        concerns=concerns,
        final_review_enabled=(report.disposition is ValidationDisposition.READY_FOR_HUMAN_REVIEW),
    )


def _verified_retry_request(
    store: ArtifactStore,
    status: AgentRunStatus,
) -> CorrectionRequest:
    """Load the exact attempt-one correction without deriving authority from prose."""

    correction = CorrectionRequest.model_validate(
        store.read_json(_CORRECTION_REQUEST_ATTEMPT_ONE_PATH)
    )
    summary = status.correction
    if (
        status.execution_attempt != 1
        or status.status != "completed"
        or status.terminal_disposition != "recoverable_failure"
        or summary is None
        or correction.action is not CorrectionAction.RETRY_IMPLEMENTATION
        or correction.completed_attempt != 1
        or correction.next_attempt != 2
        or correction.maximum_attempts != 2
        or correction.requires_new_manifest_approval
        or correction.requires_new_manifest_digest
        or summary.correction_id != correction.correction_id
        or summary.request_id != correction.request_id
        or summary.manifest_id != correction.manifest_id
        or summary.manifest_digest != correction.manifest_digest
        or summary.report_id != correction.report_id
        or summary.report_digest != correction.report_digest
        or summary.change_set_digest != correction.change_set_digest
        or summary.base_revision != correction.base_revision
        or summary.completed_attempt != correction.completed_attempt
        or summary.authorized_attempt != correction.next_attempt
        or summary.action is not correction.action
    ):
        raise AgentUiError("retry_unavailable")
    return correction


def _is_authorized_retry_continuation_status(status: AgentRunStatus) -> bool:
    """Recognize only the verified attempt-two Engineer recovery checkpoint."""

    return (
        status.execution_attempt == 2
        and status.status == "implementing"
        and status.terminal_disposition == "recoverable_failure"
        and status.pending_nodes == ("engineer",)
        and status.task_failed
        and status.interrupt is None
        and status.failure is None
        and status.correction is not None
        and status.correction.completed_attempt == 1
        and status.correction.authorized_attempt == 2
        and status.correction.action is CorrectionAction.RETRY_IMPLEMENTATION
    )


def _verified_authorized_retry_continuation(
    store: ArtifactStore,
    status: AgentRunStatus,
) -> tuple[CorrectionRequest, CorrectionApproval]:
    """Load the immutable correction and approval for the one resumable checkpoint."""

    if not _is_authorized_retry_continuation_status(status):
        raise AgentUiError("retry_already_recorded")
    correction = CorrectionRequest.model_validate(
        store.read_json(_CORRECTION_REQUEST_ATTEMPT_ONE_PATH)
    )
    if (
        correction.completed_attempt != 1
        or correction.next_attempt != 2
        or correction.maximum_attempts != 2
        or correction.action is not CorrectionAction.RETRY_IMPLEMENTATION
        or correction.requires_new_manifest_approval
        or correction.requires_new_manifest_digest
        or not _status_correction_matches(status, correction)
    ):
        raise AgentUiError("run_unavailable")
    approval = CorrectionApproval.model_validate(
        store.read_json(_CORRECTION_APPROVAL_ATTEMPT_TWO_PATH)
    )
    expected = CorrectionController.approve_retry(
        correction,
        presented_correction_id=approval.correction_id,
        reviewer=approval.reviewer,
        comment=approval.comment,
    )
    if approval != expected:
        raise AgentUiError("run_unavailable")
    return correction, approval


def _correction_view(
    store: ArtifactStore,
    status: AgentRunStatus,
    *,
    model_root: str,
) -> AgentCorrectionView | None:
    """Project the latest typed correction while retaining attempt-two approval evidence."""

    try:
        attempt_one_payload = store.read_json(_CORRECTION_REQUEST_ATTEMPT_ONE_PATH)
    except FileNotFoundError:
        if status.execution_attempt == 2:
            raise AgentUiError("run_unavailable") from None
        return None
    attempt_one = CorrectionRequest.model_validate(attempt_one_payload)
    if attempt_one.completed_attempt != 1 or attempt_one.maximum_attempts != 2:
        raise AgentUiError("run_unavailable")

    correction = attempt_one
    if status.execution_attempt == 2:
        if (
            attempt_one.action is not CorrectionAction.RETRY_IMPLEMENTATION
            or attempt_one.next_attempt != 2
            or attempt_one.requires_new_manifest_approval
            or attempt_one.requires_new_manifest_digest
        ):
            raise AgentUiError("run_unavailable")
        try:
            attempt_two_payload = store.read_json("control/correction-request-attempt-2.json")
        except FileNotFoundError:
            # A controlled Engineer/Validator failure can stop before attempt two has
            # a ValidationReport and therefore before a terminal correction exists.
            # In that case the attempt-one authorization remains the latest typed
            # correction evidence, but it can never become available for a third run.
            if status.status not in {"failed", "decision_required"} and not (
                _is_authorized_retry_continuation_status(status)
            ):
                raise AgentUiError("run_unavailable") from None
        else:
            correction = CorrectionRequest.model_validate(attempt_two_payload)
            if correction.completed_attempt != 2 or correction.maximum_attempts != 2:
                raise AgentUiError("run_unavailable")

    if (
        correction.request_id != status.request_id
        or correction.manifest_id != status.manifest_id
        or correction.manifest_digest != status.manifest_digest
    ):
        raise AgentUiError("run_unavailable")

    if correction.completed_attempt == status.execution_attempt:
        if not _status_correction_matches(status, correction):
            raise AgentUiError("run_unavailable")
    elif not (
        status.execution_attempt == 2
        and correction.completed_attempt == 1
        and (
            status.status in {"failed", "decision_required"}
            or _is_authorized_retry_continuation_status(status)
        )
        and (
            (status.status == "failed" and status.correction is None)
            or (
                status.status == "decision_required"
                and _status_correction_matches(status, correction)
            )
            or (
                _is_authorized_retry_continuation_status(status)
                and _status_correction_matches(status, correction)
            )
        )
    ):
        raise AgentUiError("run_unavailable")

    approval: CorrectionApproval | None
    try:
        approval_payload = store.read_json(_CORRECTION_APPROVAL_ATTEMPT_TWO_PATH)
    except FileNotFoundError:
        approval = None
    else:
        approval = CorrectionApproval.model_validate(approval_payload)
        if attempt_one.action is not CorrectionAction.RETRY_IMPLEMENTATION:
            raise AgentUiError("run_unavailable")
        expected = CorrectionController.approve_retry(
            attempt_one,
            presented_correction_id=approval.correction_id,
            reviewer=approval.reviewer,
            comment=approval.comment,
        )
        if approval != expected:
            raise AgentUiError("run_unavailable")

    if status.execution_attempt == 2 and approval is None:
        raise AgentUiError("run_unavailable")
    retry_available = (
        status.execution_attempt == 1
        and status.status == "completed"
        and status.terminal_disposition == "recoverable_failure"
        and correction.action is CorrectionAction.RETRY_IMPLEMENTATION
    ) or _is_authorized_retry_continuation_status(status)
    if retry_available and status.execution_attempt == 1:
        verified = _verified_retry_request(store, status)
        if verified != attempt_one:
            raise AgentUiError("run_unavailable")
    elif retry_available:
        verified, verified_approval = _verified_authorized_retry_continuation(store, status)
        if verified != attempt_one or verified_approval != approval:
            raise AgentUiError("run_unavailable")

    try:
        report = ValidationReport.model_validate(
            store.read_json(f"{model_root}/report-attempt-{correction.completed_attempt}.json")
        )
    except FileNotFoundError:
        raise AgentUiError("run_unavailable") from None
    if correction.report_digest != artifact_digest(report):
        raise AgentUiError("run_unavailable")
    projected_failure_ids = correction_failure_ids(report)
    if not set(projected_failure_ids).issubset(correction.failed_check_ids):
        raise AgentUiError("run_unavailable")

    return AgentCorrectionView(
        correction_id=correction.correction_id,
        completed_attempt=correction.completed_attempt,
        authorized_attempt=correction.next_attempt,
        action=correction.action,
        failed_check_ids=projected_failure_ids,
        reason=correction.reason,
        retry_available=retry_available,
        approval=(
            None
            if approval is None
            else AgentCorrectionApprovalView(
                correction_id=approval.correction_id,
                reviewer=approval.reviewer,
                comment=approval.comment,
                authorized_attempt=2,
            )
        ),
    )


def _status_correction_matches(
    status: AgentRunStatus,
    correction: CorrectionRequest,
) -> bool:
    """Match the browser projection to the exact controller-owned status summary."""

    summary = status.correction
    return summary is not None and (
        summary.correction_id == correction.correction_id
        and summary.request_id == correction.request_id
        and summary.manifest_id == correction.manifest_id
        and summary.manifest_digest == correction.manifest_digest
        and summary.report_id == correction.report_id
        and summary.report_digest == correction.report_digest
        and summary.change_set_digest == correction.change_set_digest
        and summary.base_revision == correction.base_revision
        and summary.completed_attempt == correction.completed_attempt
        and summary.authorized_attempt == correction.next_attempt
        and summary.action is correction.action
    )


def _attempt_history(
    store: ArtifactStore,
    model_root: str,
    manifest: MigrationManifest,
    status: AgentRunStatus,
) -> tuple[AgentAttemptView, ...]:
    history: list[AgentAttemptView] = []
    for attempt in range(1, status.execution_attempt + 1):
        try:
            engineer = EngineerRun.model_validate(
                store.read_json(f"{model_root}/engineer-attempt-{attempt}.json")
            )
            report = ValidationReport.model_validate(
                store.read_json(f"{model_root}/report-attempt-{attempt}.json")
            )
        except FileNotFoundError:
            continue
        change_set = engineer.change_set
        if change_set is None or report.attempt != attempt:
            raise AgentUiError("run_unavailable")
        validate_report(report, manifest, change_set)
        projected_failure_ids = correction_failure_ids(report)
        correction_path = f"control/correction-request-attempt-{attempt}.json"
        try:
            correction_payload = store.read_json(correction_path)
        except FileNotFoundError:
            failed_check_ids = projected_failure_ids
        else:
            correction = CorrectionRequest.model_validate(correction_payload)
            if (
                correction.completed_attempt != attempt
                or correction.request_id != report.request_id
                or correction.manifest_id != report.manifest_id
                or correction.report_id != report.report_id
                or correction.report_digest != artifact_digest(report)
                or correction.change_set_id != change_set.change_set_id
                or correction.change_set_digest != artifact_digest(change_set)
                or correction.manifest_digest != artifact_digest(manifest)
            ):
                raise AgentUiError("run_unavailable")
            if not set(projected_failure_ids).issubset(correction.failed_check_ids):
                raise AgentUiError("run_unavailable")
            failed_check_ids = projected_failure_ids
        dependent_check_ids = validation_failure_dependencies(report).keys()
        diagnostic_ids = tuple(
            dict.fromkeys(
                diagnostic_id
                for result in report.results
                if (result.status.value != "passed" and result.check_id not in dependent_check_ids)
                for diagnostic_id in result.diagnostic_ids
            )
        )
        history.append(
            AgentAttemptView(
                attempt=attempt,
                disposition=report.disposition.value,
                failed_check_ids=failed_check_ids,
                diagnostic_ids=diagnostic_ids,
            )
        )
    return tuple(history)


def _prior_attempt_model_calls(
    store: ArtifactStore,
    model_root: str,
    manifest: MigrationManifest,
    status: AgentRunStatus,
) -> tuple[tuple[ModelCallRecord, Literal[1]], ...]:
    if status.execution_attempt != 2:
        return ()
    engineer = EngineerRun.model_validate(store.read_json(f"{model_root}/engineer-attempt-1.json"))
    report = ValidationReport.model_validate(store.read_json(f"{model_root}/report-attempt-1.json"))
    assessment = ValidatorAssessment.model_validate(
        store.read_json(f"{model_root}/validator-attempt-1.json")
    )
    correction = CorrectionRequest.model_validate(
        store.read_json(_CORRECTION_REQUEST_ATTEMPT_ONE_PATH)
    )
    change_set = engineer.change_set
    if change_set is None:
        raise AgentUiError("run_unavailable")
    validate_report(report, manifest, change_set)
    if (
        report.attempt != 1
        or report.disposition.value != "recoverable_failure"
        or correction.action is not CorrectionAction.RETRY_IMPLEMENTATION
        or correction.completed_attempt != 1
        or correction.next_attempt != 2
        or correction.manifest_digest != artifact_digest(manifest)
        or correction.report_digest != artifact_digest(report)
        or correction.change_set_digest != artifact_digest(change_set)
        or assessment.advisory.manifest_digest != artifact_digest(manifest)
        or assessment.advisory.change_set_digest != artifact_digest(change_set)
        or assessment.advisory.report_digest != artifact_digest(report)
        or assessment.authoritative_disposition is not report.disposition
    ):
        raise AgentUiError("run_unavailable")
    if assessment.model_call is None:
        return ((engineer.model_call, 1),)
    return ((engineer.model_call, 1), (assessment.model_call, 1))


def _human_decision_view(
    store: ArtifactStore,
    status: AgentRunStatus,
    manifest: MigrationManifest,
) -> AgentHumanDecisionView | None:
    try:
        payload = store.read_json(_MANIFEST_APPROVAL_PATH)
    except FileNotFoundError:
        if status.status == "awaiting_approval":
            return None
        if status.status == "decision_required" and manifest.status.value == "decision_required":
            return None
        raise AgentUiError("run_unavailable") from None
    approval = ManifestApproval.model_validate(payload)
    if (
        approval.request_id != manifest.request_id
        or approval.manifest_id != manifest.manifest_id
        or approval.manifest_digest != artifact_digest(manifest)
    ):
        raise AgentUiError("run_unavailable")
    if approval.selection not in {"approve", "reject", "modify"}:
        raise AgentUiError("run_unavailable")
    return AgentHumanDecisionView(
        selection=cast(Literal["approve", "reject", "modify"], approval.selection),
        reviewer=approval.reviewer,
        comment=approval.comment,
        manifest_digest=approval.manifest_digest,
    )


def _model_call_view(call: ModelCallRecord, *, attempt: int) -> AgentModelCallView:
    role = call.agent_version.partition("/")[0]
    if role not in {"architect", "engineer", "validator"}:
        raise AgentUiError("run_unavailable")
    typed_role = cast(Literal["architect", "engineer", "validator"], role)
    usage = call.usage
    return AgentModelCallView(
        role=typed_role,
        attempt=attempt,
        agent_version=call.agent_version,
        latency_ms=None if usage is None else usage.latency_ms,
        input_tokens=None if usage is None else usage.input_tokens,
        output_tokens=None if usage is None else usage.output_tokens,
        total_tokens=None if usage is None else usage.total_tokens,
        input_digest=call.input_digest,
        output_digest=call.output_digest,
    )


def _architect_planning_decision(
    architect: ArchitectRun,
    status: AgentRunStatus,
) -> AgentPlanningDecisionView | None:
    manifest = architect.proposal.manifest
    if manifest.status.value != "decision_required":
        return None
    if (
        status.status != "decision_required"
        or status.terminal_disposition != "decision_required"
        or status.interrupt is not None
    ):
        raise AgentUiError("run_unavailable")
    return AgentPlanningDecisionView(
        manifest_id=manifest.manifest_id,
        manifest_digest=artifact_digest(manifest),
        unresolved_questions=architect.agent_output.unresolved_questions,
        required_approvals=tuple(item.value for item in manifest.required_approvals),
        summary=(
            "The Architect returned an evidence-bound semantic recommendation with a material "
            "human-decision risk. The Controller expanded it into a valid decision-required "
            "manifest; the approval gate did not open, and Engineer and Validator were not invoked."
        ),
    )


def _implementation_intervention_view(
    intervention: ImplementationIntervention,
) -> AgentInterventionView:
    return AgentInterventionView(
        intervention_id=intervention.intervention_id,
        requested_action=intervention.requested_action.value,
        reason=intervention.reason,
        affected_paths=intervention.affected_paths,
        public_concerns=intervention.public_concerns,
        recommendation=intervention.recommendation,
    )


def _planning_intervention_view(
    intervention: PlanningIntervention,
) -> AgentInterventionView:
    return AgentInterventionView(
        intervention_id=intervention.intervention_id,
        requested_action=intervention.requested_action.value,
        reason=intervention.reason,
        affected_paths=intervention.affected_paths,
        public_concerns=tuple(item.summary for item in intervention.evidence),
        recommendation=intervention.recommendation,
    )


def _failure_view(status: AgentRunStatus) -> AgentFailureView | None:
    failure = status.failure
    if failure is None:
        return None
    reason_code = _resolved_failure_reason(failure)
    details = _FAILURE_DETAILS[reason_code]
    return AgentFailureView(
        operation=failure.operation,
        seam=failure.seam,
        category=failure.category,
        reason_code=reason_code,
        phase=details[0],
        response_received=details[1],
        schema_valid=details[2],
        policy_valid=details[3],
        attempt=failure.attempt,
        summary=details[4],
        guidance=details[5],
    )


def _resolved_failure_reason(failure: AgentRunFailure) -> AgentFailureReasonCode:
    if failure.reason_code is not None:
        return failure.reason_code
    if failure.seam == "deterministic_validator":
        return "deterministic_validation_failed"
    legacy_reasons: dict[str, AgentFailureReasonCode] = {
        "configuration": "configuration_invalid",
        "refusal": "provider_refusal",
        "incomplete": "response_incomplete",
        "invalid": "structured_output_invalid",
        "provider_unavailable": "provider_unavailable",
        "internal": "internal_failure",
    }
    return legacy_reasons[failure.category]


def _pre_manifest_terminal_stages(
    status: AgentRunStatus,
) -> tuple[AgentStageView, ...]:
    projected_failure = _failure_view(status)
    architect_detail = (
        projected_failure.summary
        if projected_failure is not None
        else "Controller preflight stopped before Architect model invocation and requested a decision."
    )
    return (
        AgentStageView(
            key="architect",
            label="Controller → Architect",
            state="blocked",
            detail=architect_detail,
        ),
        AgentStageView(
            key="approval",
            label="Manifest approval",
            state="blocked",
            detail="No migration manifest was available for approval.",
        ),
        AgentStageView(
            key="engineer",
            label="Engineer",
            state="blocked",
            detail="Engineer was not invoked without an approved manifest.",
        ),
        AgentStageView(
            key="validator",
            label="Validator",
            state="blocked",
            detail="Validator was not invoked without a migration candidate.",
        ),
    )


def _stages(
    status: AgentRunStatus,
    graph: DependencyGraph,
    wiki_trace: RetrievalTrace,
    candidate: AgentCandidateView | None,
    planning_decision: AgentPlanningDecisionView | None,
) -> tuple[AgentStageView, ...]:
    architecture = AgentStageView(
        key="architect",
        label="Controller → Architect",
        state="complete",
        detail=(
            "Controller bound the exact source inputs, "
            f"built {len(graph.nodes)} dependency nodes and {len(graph.edges)} edges, then "
            f"retrieved {len(wiki_trace.hits)} curated Wiki page(s). Architect selected "
            "bounded graph/Wiki IDs and returned a semantic recommendation; Controller expanded "
            "it into the exact manifest."
        ),
    )
    if status.status == "awaiting_approval":
        return (
            architecture,
            AgentStageView(
                key="approval",
                label="Manifest approval",
                state="active",
                detail="Review the exact bounded manifest before candidate creation.",
            ),
            AgentStageView(
                key="engineer",
                label="Engineer",
                state="pending",
                detail="No Engineer model output exists before approval.",
            ),
            AgentStageView(
                key="validator",
                label="Validator",
                state="pending",
                detail="Validation waits for an approved migration candidate.",
            ),
        )
    if status.status == "rejected":
        return (
            architecture,
            AgentStageView(
                key="approval",
                label="Manifest approval",
                state="complete",
                detail="The reviewer rejected this manifest.",
            ),
            AgentStageView(
                key="engineer",
                label="Engineer",
                state="blocked",
                detail="Rejection stopped the workflow before Engineer invocation.",
            ),
            AgentStageView(
                key="validator",
                label="Validator",
                state="blocked",
                detail="No candidate existed to validate.",
            ),
        )
    if status.status == "decision_required":
        if planning_decision is not None:
            return (
                AgentStageView(
                    key="architect",
                    label="Controller → Architect",
                    state="complete",
                    detail=(
                        "Architect returned a semantic recommendation with a material risk; "
                        "Controller expanded it into a digest-bound decision-required manifest."
                    ),
                ),
                AgentStageView(
                    key="approval",
                    label="Manifest approval",
                    state="blocked",
                    detail="The approve/reject manifest gate did not open for this plan status.",
                ),
                AgentStageView(
                    key="engineer",
                    label="Engineer",
                    state="blocked",
                    detail="Engineer was not invoked for a decision-required Architect plan.",
                ),
                AgentStageView(
                    key="validator",
                    label="Validator",
                    state="blocked",
                    detail="Validator was not invoked because no candidate exists.",
                ),
            )
        return (
            architecture,
            AgentStageView(
                key="approval",
                label="Manifest approval",
                state="complete",
                detail="The reviewer approved the exact persisted manifest.",
            ),
            AgentStageView(
                key="engineer",
                label="Engineer",
                state="blocked",
                detail=(
                    "Engineer stopped without writing files and requested a new human "
                    "scope or evidence decision."
                ),
            ),
            AgentStageView(
                key="validator",
                label="Validator",
                state="blocked",
                detail="No candidate existed to validate.",
            ),
        )
    if status.status == "failed" and status.failure is not None:
        engineer_completed = status.failure.seam in {"validator", "deterministic_validator"}
        if status.failure.seam == "validator":
            validator_detail = (
                "Controller-owned deterministic checks completed, but the Validator model "
                "advisory ended with a terminal failure."
            )
        elif status.failure.seam == "deterministic_validator":
            validator_detail = (
                "Controller-owned deterministic validation failed before a trusted report "
                "could complete."
            )
        else:
            validator_detail = "Validator was not invoked because Engineer did not complete."
        return (
            architecture,
            AgentStageView(
                key="approval",
                label="Manifest approval",
                state="complete",
                detail="The reviewer approved the exact persisted manifest.",
            ),
            AgentStageView(
                key="engineer",
                label="Engineer",
                state="complete" if engineer_completed else "blocked",
                detail=(
                    f"Engineer attempt {status.execution_attempt} produced an isolated candidate "
                    "before the later failure."
                    if engineer_completed
                    else (
                        f"Engineer attempt {status.execution_attempt} ended with a sanitized "
                        "terminal failure and wrote no candidate."
                    )
                ),
            ),
            AgentStageView(
                key="validator",
                label="Validator",
                state="blocked",
                detail=validator_detail,
            ),
        )
    if status.status == "completed" and candidate is not None:
        return (
            architecture,
            AgentStageView(
                key="approval",
                label="Manifest approval",
                state="complete",
                detail="The reviewer approved the exact persisted manifest.",
            ),
            AgentStageView(
                key="engineer",
                label="Engineer",
                state="complete",
                detail=(
                    f"Attempt {status.execution_attempt} created "
                    f"{len(candidate.changed_paths)} candidate file(s) in an isolated workspace."
                ),
            ),
            AgentStageView(
                key="validator",
                label="Checks + Validator advisory",
                state="complete",
                detail=(
                    f"Controller-owned local checks completed attempt {status.execution_attempt}; "
                    "the Validator advisory cannot change their disposition, and external "
                    "platform validation remains outside this UI's authority."
                ),
            ),
        )
    return (
        architecture,
        AgentStageView(
            key="approval",
            label="Manifest approval",
            state="complete",
            detail="The manifest gate has a terminal decision.",
        ),
        AgentStageView(
            key="engineer",
            label="Engineer",
            state="blocked",
            detail="The workflow did not produce a verified migration candidate.",
        ),
        AgentStageView(
            key="validator",
            label="Validator",
            state="blocked",
            detail="No completed validation evidence is available.",
        ),
    )


__all__ = [
    "AgentRunView",
    "AgentUiError",
    "AgentUiService",
]
