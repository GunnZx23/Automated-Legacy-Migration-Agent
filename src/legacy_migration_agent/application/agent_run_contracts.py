"""Portable contracts and identifiers for the supported agent-run service.

Keeping these provider-free models outside the orchestration facade lets query,
schema, and lifecycle code share one contract vocabulary without importing
model clients or platform composition.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, field_validator

from legacy_migration_agent.agent_runtime.correction import CorrectionAction
from legacy_migration_agent.contracts import (
    ApprovalAction,
    Identifier,
    MigrationRequest,
    Platform,
    Sha256Digest,
    StrictModel,
)
from legacy_migration_agent.core.run_session import AgentDefinitionDigests

AGENT_RUN_EVIDENCE_KIND = "agent-run-initialized"
AGENT_RUN_REQUEST_PATH = "request.json"
AGENT_RUN_CONFIG_PATH = "agent-run-config.json"
AGENT_RUN_LAUNCH_CONTRACT_PATH = "launch-contract.json"
AGENT_RUN_FAILURE_PATH = "agent-run-failure.json"
AGENT_RUN_FAILURE_KIND = "agent-run-failed"
AGENT_RUN_CONTROL_ROOT = "control"
AGENT_RUN_STATUS_ROOT = "status"
AGENT_RUN_MANIFEST_AUTHORIZED_KIND = "agent-run-manifest-authorized"
AGENT_RUN_CORRECTION_AUTHORIZED_KIND = "agent-run-correction-authorized"
AGENT_RUN_MANIFEST_APPROVAL_PATH = "control/manifest-approval.json"
AGENT_RUN_CORRECTION_APPROVAL_PATH = "control/correction-approval-attempt-2.json"
AGENT_RUN_MANIFEST_AUTHORIZATION_INTENT_KIND = "agent-run-manifest-authorization-intent"
AGENT_RUN_CORRECTION_AUTHORIZATION_INTENT_KIND = "agent-run-correction-authorization-intent"

AgentRunFailureCategory = Literal[
    "configuration",
    "refusal",
    "incomplete",
    "invalid",
    "provider_unavailable",
    "internal",
]
AgentRunFailureReason = Literal[
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
AgentRunFailureSeam = Literal[
    "architect",
    "engineer",
    "validator",
    "deterministic_validator",
]
AgentRunOperation = Literal["start", "resume", "retry"]

_AGENT_RUN_FAILURE_EXPLANATIONS: dict[AgentRunFailureReason, tuple[str, str]] = {
    "configuration_invalid": (
        "The controller rejected the local model configuration before a usable response.",
        "Check that the configured Ollama model is installed, then start a fresh run.",
    ),
    "provider_refusal": (
        "The local model declined to produce the requested structured role output.",
        "Refine the bounded migration request and start a fresh run.",
    ),
    "response_incomplete": (
        "The local model response ended before the structured role output was complete.",
        "Start a fresh run; reduce model load or allow a longer server-owned timeout if needed.",
    ),
    "structured_output_invalid": (
        "The local model responded, but its role output did not satisfy the typed contract.",
        "Start a fresh run and use the harness trace to identify the rejected role boundary.",
    ),
    "unauthorized_tool_call": (
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
        "The local Ollama model inventory could not prove the selected model identity.",
        "Confirm the exact model alias and digest with ollama list, then start a fresh run.",
    ),
    "provider_response_invalid": (
        "Ollama returned a response that failed the controller's provider-protocol checks.",
        "Confirm the local Ollama service and selected model, then start a fresh run.",
    ),
    "required_approval_missing": (
        "The Controller-expanded manifest was missing its required human-approval binding.",
        (
            "Start a fresh run and inspect the Controller expansion and scope-policy binding; "
            "the Architect does not author approval actions."
        ),
    ),
    "implementation_contract_invalid": (
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
        "The Controller-expanded manifest violated the frozen transformation boundary.",
        (
            "Start a fresh run. Use only frozen legacy files as inputs, cover every required "
            "source input, and assign every approved output to exactly one transformation; "
            "generated target files cannot become inputs to later steps."
        ),
    ),
    "unresolved_question_risk_missing": (
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
    "correction_delta_required": (
        "Engineer attempt 2 did not return the required changed-file correction delta.",
        (
            "Start a fresh run. On correction, Engineer must use the supplied validation and "
            "Wiki repair signals and return a file-plan delta instead of another intervention."
        ),
    ),
    "correction_no_material_changes": (
        "Engineer attempt 2 proposed updates, but their contents did not change attempt 1.",
        (
            "Start a fresh run and apply a material change to at least one allowed repair file; "
            "resubmitting unchanged attempt-1 content cannot satisfy correction."
        ),
    ),
    "correction_signal_coverage_missing": (
        "Engineer attempt 2 did not address every controller-classified repair signal.",
        (
            "Start a fresh run. The correction delta must touch an allowed file mapped to every "
            "reported repair signal before deterministic validation can run again."
        ),
    ),
    "correction_scope_invalid": (
        "Engineer attempt 2 proposed a path outside the controller-owned repair boundary.",
        (
            "Start a fresh run and restrict the correction delta to the allowed paths named by "
            "the correction contract; request a new manifest if broader scope is actually needed."
        ),
    ),
    "correction_identical_candidate": (
        "Engineer attempt 2 reconstructed the same candidate as attempt 1.",
        (
            "Start a fresh run and make a targeted content change that resolves the supplied "
            "repair signals before submitting the correction delta."
        ),
    ),
    "file_plan_scope_mismatch": (
        "The Engineer file plan did not match the manifest-approved output scope.",
        (
            "Start a fresh run. Engineer must produce exactly the manifest-approved outputs on "
            "attempt 1 and only controller-authorized repair paths on attempt 2."
        ),
    ),
    "file_plan_delta_mismatch": (
        "The isolated workspace changes did not match the Engineer file plan.",
        (
            "Start a fresh run. Every changed path must be declared in the file plan, and every "
            "declared update must be the exact change applied in the isolated workspace."
        ),
    ),
    "workspace_scope_mismatch": (
        "The isolated Engineer workspace was not bound to the exact manifest-approved scope.",
        (
            "Start a fresh run and verify the selected scenario, manifest, and immutable source "
            "revision agree before Engineer is invoked."
        ),
    ),
    "workspace_not_clean": (
        "The isolated Engineer workspace contained an unexpected pre-existing change.",
        (
            "Start a fresh run from the unchanged source revision; the harness requires a clean "
            "isolated workspace before applying the Engineer file plan."
        ),
    ),
    "attempt_two_scope_expansion_invalid": (
        "Engineer attempt 2 requested scope expansion without identifying a required outside path.",
        (
            "Start a fresh run. A scope-expansion intervention must name a specifically required "
            "path outside the approved manifest; otherwise Engineer must return the bounded delta."
        ),
    ),
    "policy_rejected": (
        "The role output passed its schema but failed a controller-owned policy check.",
        "Start a fresh run and inspect the public policy phase in the harness trace.",
    ),
    "provider_timeout": (
        "The local Ollama request exceeded the server-owned inference deadline.",
        (
            "Confirm Ollama is responsive, then restart with a longer timeout or a smaller "
            "installed model."
        ),
    ),
    "provider_unavailable": (
        "The controller could not complete the local Ollama request.",
        "Confirm Ollama is running and the configured model is installed, then start a fresh run.",
    ),
    "deterministic_validation_failed": (
        (
            "The controller-owned deterministic validation boundary could not produce a trusted "
            "report."
        ),
        "Inspect the harness stage and local validation environment before starting a fresh run.",
    ),
    "internal_failure": (
        "The workflow ended at a sanitized internal boundary without authorizing external action.",
        "Start a fresh run; if the failure repeats, inspect the durable sanitized evidence.",
    ),
}


def agent_run_failure_explanation(
    reason_code: AgentRunFailureReason,
    seam: AgentRunFailureSeam,
) -> tuple[str, str]:
    """Return fixed public prose without observing exception or model output text."""

    del seam
    return _AGENT_RUN_FAILURE_EXPLANATIONS[reason_code]


class AgentRunConfig(StrictModel):
    """Portable non-secret inputs needed to reproduce runtime composition."""

    schema_version: Literal["1.0"] = "1.0"
    preset_id: Identifier
    wiki_as_of: date


class AgentRunEvidenceAnchor(StrictModel):
    """Value independently anchored below non-portable session state."""

    schema_version: Literal["1.0"] = "1.0"
    run_id: Identifier
    thread_id: Identifier
    request_digest: Sha256Digest
    config_digest: Sha256Digest
    lifecycle_index_digest: Sha256Digest


class AgentRunLifecycleAnchor(StrictModel):
    """Independent state binding for one complete portable operation snapshot."""

    schema_version: Literal["1.0"] = "1.0"
    lifecycle_kind: Identifier
    run_id: Identifier
    thread_id: Identifier
    request_digest: Sha256Digest
    lifecycle_index_digest: Sha256Digest


class AgentRunCheckpointFieldDigest(StrictModel):
    """Canonical digest of one complete workflow-state field."""

    name: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value_digest: Sha256Digest


class AgentRunCheckpointInterruptProjection(StrictModel):
    """Exact identity and canonical value digest for one pending interrupt."""

    interrupt_id: str = Field(min_length=1, max_length=300)
    value_digest: Sha256Digest


class AgentRunCheckpointTaskProjection(StrictModel):
    """Complete digest projection of one LangGraph pending task."""

    task_id: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=160)
    path_digest: Sha256Digest
    error_digest: Sha256Digest
    interrupts: tuple[AgentRunCheckpointInterruptProjection, ...] = Field(max_length=8)
    state_digest: Sha256Digest
    result_digest: Sha256Digest


class AgentRunCheckpointProjection(StrictModel):
    """Portable binding for all security-relevant checkpoint state.

    The full canonical request is retained for an exact typed equality check.
    Every workflow value, including fields not surfaced by ``AgentRunStatus``,
    is covered by the sorted digest inventory. Pending routing and every
    LangGraph task attribute are independently projected as well.
    """

    schema_version: Literal["1.0"] = "1.0"
    run_id: Identifier
    thread_id: Identifier
    request: MigrationRequest
    values: tuple[AgentRunCheckpointFieldDigest, ...]
    next: tuple[str, ...] = Field(max_length=8)
    tasks: tuple[AgentRunCheckpointTaskProjection, ...] = Field(max_length=8)


class AgentRunAuthorizationIntent(StrictModel):
    """Runtime-anchored exact authority written before portable publication."""

    schema_version: Literal["1.0"] = "1.0"
    authorization_kind: Identifier
    run_id: Identifier
    thread_id: Identifier
    request_digest: Sha256Digest
    operation: Literal["resume", "retry"]
    control_path: str
    approval_digest: Sha256Digest


class AgentRunFailure(StrictModel):
    """Bounded terminal evidence for a sanitized operation failure."""

    schema_version: Literal["1.0"] = "1.0"
    failure_id: Identifier
    run_id: Identifier
    thread_id: Identifier
    request_id: Identifier
    operation: AgentRunOperation
    seam: AgentRunFailureSeam
    category: AgentRunFailureCategory
    reason_code: AgentRunFailureReason | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Controller-owned diagnostic for new failures. Missing only on legacy evidence, "
            "where the category and seam remain authoritative."
        ),
    )
    summary: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        exclude_if=lambda value: value is None,
        description="Fixed controller-owned public explanation; absent on legacy evidence.",
    )
    guidance: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        exclude_if=lambda value: value is None,
        description="Fixed controller-owned next step; absent on legacy evidence.",
    )
    attempt: int = Field(ge=1, le=2)
    terminal: Literal[True] = True
    retry_eligible: Literal[False] = False
    request_digest: Sha256Digest
    operation_input_digest: Sha256Digest
    session_context_digest: Sha256Digest
    source_revision: Sha256Digest
    agent_definition_digests: AgentDefinitionDigests


class AgentRunInterruptSummary(StrictModel):
    """Minimum exact manifest decision fields exposed to a human reviewer."""

    kind: Literal["manifest_approval"] = "manifest_approval"
    decision_id: Identifier
    request_id: Identifier
    manifest_id: Identifier
    manifest_digest: Sha256Digest
    requested_action: ApprovalAction
    options: tuple[Literal["approve", "reject", "modify"], ...]


class AgentRunCorrectionSummary(StrictModel):
    """Exact bounded retry fields, excluding free-form terminal prose."""

    correction_id: Identifier
    request_id: Identifier
    manifest_id: Identifier
    manifest_digest: Sha256Digest
    report_id: Identifier
    report_digest: Sha256Digest
    change_set_digest: Sha256Digest
    base_revision: str
    completed_attempt: int = Field(ge=1, le=2)
    authorized_attempt: int | None = Field(default=None, ge=2, le=2)
    action: CorrectionAction


class AgentRunStatus(StrictModel):
    """Bounded, portable run state; it intentionally contains no raw prompts."""

    schema_version: Literal["1.0"] = "1.0"
    run_id: Identifier
    thread_id: Identifier
    request_id: Identifier
    request_digest: Sha256Digest
    platform: Platform
    provider_id: str = Field(min_length=1, max_length=160)
    model_id: str = Field(min_length=1, max_length=300)
    status: str = Field(min_length=1, max_length=80)
    terminal_disposition: str | None = Field(default=None, max_length=80)
    execution_attempt: int = Field(ge=1, le=2)
    pending_nodes: tuple[str, ...] = Field(max_length=8)
    task_failed: bool
    manifest_id: Identifier | None = None
    manifest_digest: Sha256Digest | None = None
    interrupt: AgentRunInterruptSummary | None = None
    correction: AgentRunCorrectionSummary | None = None
    failure: AgentRunFailure | None = None

    @field_validator("pending_nodes")
    @classmethod
    def validate_pending_nodes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {
            "architect",
            "approval_gate",
            "decision_required",
            "engineer",
            "validator",
            "rejected",
            "modification_requested",
        }
        if any(value not in allowed for value in values):
            raise ValueError("run summary contains an unknown workflow node")
        return values


__all__ = [
    "AGENT_RUN_CONFIG_PATH",
    "AGENT_RUN_CONTROL_ROOT",
    "AGENT_RUN_CORRECTION_APPROVAL_PATH",
    "AGENT_RUN_CORRECTION_AUTHORIZATION_INTENT_KIND",
    "AGENT_RUN_CORRECTION_AUTHORIZED_KIND",
    "AGENT_RUN_EVIDENCE_KIND",
    "AGENT_RUN_FAILURE_KIND",
    "AGENT_RUN_FAILURE_PATH",
    "AGENT_RUN_LAUNCH_CONTRACT_PATH",
    "AGENT_RUN_MANIFEST_APPROVAL_PATH",
    "AGENT_RUN_MANIFEST_AUTHORIZATION_INTENT_KIND",
    "AGENT_RUN_MANIFEST_AUTHORIZED_KIND",
    "AGENT_RUN_REQUEST_PATH",
    "AGENT_RUN_STATUS_ROOT",
    "AgentRunAuthorizationIntent",
    "AgentRunCheckpointFieldDigest",
    "AgentRunCheckpointInterruptProjection",
    "AgentRunCheckpointProjection",
    "AgentRunCheckpointTaskProjection",
    "AgentRunConfig",
    "AgentRunCorrectionSummary",
    "AgentRunEvidenceAnchor",
    "AgentRunFailure",
    "AgentRunFailureCategory",
    "AgentRunFailureReason",
    "AgentRunFailureSeam",
    "AgentRunInterruptSummary",
    "AgentRunLifecycleAnchor",
    "AgentRunOperation",
    "AgentRunStatus",
    "agent_run_failure_explanation",
]
