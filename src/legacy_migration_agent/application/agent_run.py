"""Supported, oracle-isolated runtime for the three Markdown migration agents.

This module is the production composition boundary.  It selects one shipped
Salesforce or MuleSoft preset, binds an immutable :class:`AgentRunSession`, and
runs the real Architect, Engineer, and Validator roles on the session's exact
SQLite thread.  Models are injectable for offline tests.  Live OpenAI use has
an explicit, fail-closed constructor and is never inferred from credentials.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, field_validator

from legacy_migration_agent.agent_runtime.agent_definitions import (
    AgentRegistry,
    AgentRole,
    load_agent_registry,
)
from legacy_migration_agent.agent_runtime.correction import (
    CorrectionAction,
    CorrectionApproval,
    CorrectionAttemptEvidence,
    CorrectionController,
    CorrectionRequest,
    validate_correction_attempt_evidence,
)
from legacy_migration_agent.agent_runtime.model_agents import ArchitectRun, ValidatorAssessment
from legacy_migration_agent.agent_runtime.model_workflow import (
    DeterministicValidator,
    ModelAgentWorkflowRoles,
    ModelWorkflowIntegrationError,
    RoleInvocationLease,
    SanitizedModelPolicyError,
    filesystem_workspace_factory,
)
from legacy_migration_agent.agent_runtime.ollama_model import (
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    OllamaStructuredModelClient,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelExecutionBoundary,
    ModelOutputError,
    ModelRefusalError,
    ModelRuntimeError,
    ModelUsageEvidence,
    OpenAIResponsesModelClient,
    OutputModel,
    StructuredModelClient,
    model_execution_boundary,
)
from legacy_migration_agent.application.migration_scenarios import (
    MULESOFT_WIKI_QUERY,
    SALESFORCE_WIKI_QUERY,
    MigrationLaunchContract,
    require_canonical_launch_contract,
)
from legacy_migration_agent.contracts import (
    ApprovalAction,
    ChangeSet,
    DecisionRequest,
    EnvironmentKind,
    Identifier,
    ImplementationIntervention,
    MigrationManifest,
    MigrationRequest,
    MigrationTarget,
    Platform,
    Sha256Digest,
    StrictModel,
    ValidationDisposition,
    ValidationReport,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.observability import lifecycle_event
from legacy_migration_agent.core.policies import (
    PolicyViolation,
    validate_change_set,
    validate_manifest_for_request,
    validate_report,
)
from legacy_migration_agent.core.redaction import (
    SecretRedactor,
    assert_no_request_secrets,
)
from legacy_migration_agent.core.run_session import AgentDefinitionDigests, AgentRunSession
from legacy_migration_agent.core.scope_policy import PlatformAdapter
from legacy_migration_agent.core.workspace import (
    IsolatedWorkspace,
    TreeSnapshot,
    WorkspaceViolation,
    snapshot_tree,
)
from legacy_migration_agent.graphs.graph_store import GraphSnapshotKey, GraphSnapshotStore
from legacy_migration_agent.knowledge.wiki import EXACT_DIAGNOSTIC_ID_PATTERN, LlmWiki
from legacy_migration_agent.platforms.mulesoft_local_checks import MULE3_APP
from legacy_migration_agent.platforms.mulesoft_runtime import (
    MULESOFT_PLATFORM_ADAPTER,
    MULESOFT_RUNTIME_CONFIG,
    MULESOFT_SOURCE_VERSION,
    MULESOFT_TARGET_RUNTIME,
    MULESOFT_TARGET_VERSION,
    build_mulesoft_local_validator,
)
from legacy_migration_agent.platforms.platform_runtime import (
    MigrationPlatformRuntime,
    RevisionBoundArchitectContextFactory,
)
from legacy_migration_agent.platforms.salesforce_runtime import (
    SALESFORCE_API_RUNTIME,
    SALESFORCE_PLATFORM_ADAPTER,
    SALESFORCE_RUNTIME_CONFIG,
    SALESFORCE_SOURCE_ENTRY,
    SALESFORCE_TARGET_RUNTIME,
    build_salesforce_local_validator,
)
from legacy_migration_agent.workflow import (
    ManifestApproval,
    implementation_intervention_decision_request,
    manifest_decision_request,
    validate_implementation_intervention_for_manifest,
)

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
_API_KEY_ENVIRONMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,159}$")
_ORACLE_PATH_SEGMENTS = frozenset({"expected", "golden", "oracle"})
_API_KEY_TOKEN = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b")
_PUBLIC_IDENTIFIER = re.compile(r"^[^\x00\r\n]{1,300}$")
_ROLE_INVOCATION_LEASE_PATH = re.compile(
    r"^model-runs/(?P<request_id>[A-Za-z0-9][A-Za-z0-9._:-]{0,159})/"
    r"(?P<role>engineer|validator)-invocation-lease-attempt-(?P<attempt>[12])\.json$"
)
_MODEL_OPERATION_ATTEMPT_PATH = re.compile(
    r"^(?:model-runs/[^/]+/.+|control/correction-request)-attempt-([12])\.json$"
)
_LIVE_AUTHORIZATION = object()

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
    is covered by the sorted digest inventory.  Pending routing and every
    LangGraph task attribute are independently projected as well.
    """

    schema_version: Literal["1.0"] = "1.0"
    run_id: Identifier
    thread_id: Identifier
    request: MigrationRequest
    values: tuple[AgentRunCheckpointFieldDigest, ...]
    next: tuple[str, ...] = Field(max_length=8)
    tasks: tuple[AgentRunCheckpointTaskProjection, ...] = Field(max_length=8)


class _AgentRunAuthorizationIntent(StrictModel):
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
    attempt: int = Field(ge=1, le=2)
    terminal: Literal[True] = True
    retry_eligible: Literal[False] = False
    request_digest: Sha256Digest
    operation_input_digest: Sha256Digest
    session_context_digest: Sha256Digest
    source_revision: Sha256Digest
    agent_definition_digests: AgentDefinitionDigests


class _ControlledOperationError(RuntimeError):
    """Fixed-message error safe to cross the durable workflow boundary."""

    def __init__(
        self,
        category: AgentRunFailureCategory,
        seam: AgentRunFailureSeam,
        reason_code: AgentRunFailureReason | None = None,
    ) -> None:
        self.category = category
        self.seam = seam
        self.reason_code = reason_code or _default_failure_reason(category, seam)
        super().__init__(f"agent_run_operation_failure:{seam}:{category}")


@dataclass
class _OperationSeamTracker:
    """Remember the active trusted role seam without retaining exception text."""

    seam: AgentRunFailureSeam | None = None
    attempt: int | None = None


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


@dataclass(frozen=True)
class AgentRunModelClients:
    """One three-role client bundle with a single immutable public identity.

    Use :meth:`offline` only with recorded or non-live test doubles.  Runtime
    bundles are created exclusively by one of the approved provider builders.
    """

    architect: StructuredModelClient
    engineer: StructuredModelClient
    validator: StructuredModelClient
    _live_authorization: object | None = None
    _live_approval: LiveModelApproval | None = None
    _provider_id: str = field(init=False, repr=False)
    _model_id: str = field(init=False, repr=False)
    _live_invocation: bool = field(init=False, repr=False)
    _execution_boundary: ModelExecutionBoundary = field(init=False, repr=False)

    def __post_init__(self) -> None:
        clients = (self.architect, self.engineer, self.validator)
        identities = {(client.provider, client.model_id) for client in clients}
        if len(identities) != 1:
            raise ModelConfigurationError(
                "all three role clients must use the same provider and model identity"
            )
        provider, model_id = next(iter(identities))
        _validate_public_model_identifier(provider, role="provider")
        _validate_public_model_identifier(model_id, role="model")
        live_flags = tuple(client.live_invocation for client in clients)
        if any(live_flags) != all(live_flags):
            raise ModelConfigurationError("role clients cannot mix live and offline invocation")
        boundaries = tuple(model_execution_boundary(client) for client in clients)
        if len(set(boundaries)) != 1:
            raise ModelConfigurationError("role clients cannot mix execution boundaries")
        boundary = boundaries[0]
        if boundary == "remote_no_store" and not all(live_flags):
            raise ModelConfigurationError("remote role clients must identify remote invocation")
        if boundary != "remote_no_store" and any(live_flags):
            raise ModelConfigurationError("non-remote role clients cannot claim remote invocation")
        if boundary != "offline_recorded":
            if self._live_authorization is not _LIVE_AUTHORIZATION:
                raise ModelConfigurationError(
                    "model-runtime clients must be created by an approved provider builder"
                )
            if not all(client is clients[0] for client in clients):
                raise ModelConfigurationError("model-runtime roles must share one approved client")
            if self._live_approval is None:
                raise ModelConfigurationError(
                    "model-runtime role clients require approval evidence"
                )
        elif self._live_authorization is not None:
            raise ModelConfigurationError("offline clients cannot carry live authorization")
        elif self._live_approval is not None:
            raise ModelConfigurationError("offline clients cannot carry live approval evidence")
        object.__setattr__(self, "_provider_id", provider)
        object.__setattr__(self, "_model_id", model_id)
        object.__setattr__(self, "_live_invocation", live_flags[0])
        object.__setattr__(self, "_execution_boundary", boundary)

    @classmethod
    def offline(
        cls,
        *,
        architect: StructuredModelClient,
        engineer: StructuredModelClient,
        validator: StructuredModelClient,
    ) -> AgentRunModelClients:
        """Bind provider-neutral, non-live model doubles for local tests."""

        clients = (architect, engineer, validator)
        if any(client.live_invocation for client in clients) or any(
            model_execution_boundary(client) != "offline_recorded" for client in clients
        ):
            raise ModelConfigurationError("offline model injection cannot contain a runtime client")
        return cls(architect=architect, engineer=engineer, validator=validator)

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def live_invocation(self) -> bool:
        return self._live_invocation

    @property
    def execution_boundary(self) -> ModelExecutionBoundary:
        return self._execution_boundary

    @property
    def live_approval(self) -> LiveModelApproval | None:
        return self._live_approval

    def bind_recorded_model_revision(self, revision: Sha256Digest) -> None:
        """Bind a local runtime only to immutable evidence loaded by the controller."""

        if self.execution_boundary != "local_loopback":
            raise ModelConfigurationError("only a local runtime can bind a local model revision")
        binder = getattr(self.architect, "bind_model_revision", None)
        if not callable(binder):
            raise ModelConfigurationError("local runtime cannot bind the recorded model revision")
        binder(revision)


class _SanitizedModelClient:
    """Prevent provider exceptions from crossing into durable state verbatim."""

    def __init__(
        self,
        client: StructuredModelClient,
        *,
        role: Literal["architect", "engineer", "validator"],
        provider_id: str,
        model_id: str,
        live_invocation: bool,
        execution_boundary: ModelExecutionBoundary,
        live_approval: LiveModelApproval | None,
        seam_tracker: _OperationSeamTracker,
    ) -> None:
        self._client = client
        self._role = role
        self._provider_id = provider_id
        self._model_id = model_id
        self._live_invocation = live_invocation
        self._execution_boundary = execution_boundary
        self._live_approval = live_approval
        self._seam_tracker = seam_tracker

    @property
    def provider(self) -> str:
        return self._provider_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def live_invocation(self) -> bool:
        return self._live_invocation

    @property
    def store_false_sent(self) -> bool:
        category: AgentRunFailureCategory | None = None
        value: bool | None = None
        try:
            observed = self._client.store_false_sent
            if not isinstance(observed, bool):
                raise TypeError("model storage evidence must be boolean")
            value = observed
        except Exception as error:
            category = _classify_model_exception(error)
        if category is not None:
            raise _ControlledOperationError(category, self._role)
        if value is None:  # pragma: no cover - defensive type guard
            raise _ControlledOperationError("internal", self._role)
        return value

    @property
    def execution_boundary(self) -> ModelExecutionBoundary:
        return self._execution_boundary

    @property
    def last_usage(self) -> ModelUsageEvidence | None:
        """Expose only a validated copy of the underlying call telemetry."""

        category: AgentRunFailureCategory | None = None
        usage: ModelUsageEvidence | None = None
        try:
            raw = getattr(self._client, "last_usage", None)
            if raw is not None:
                if not isinstance(raw, ModelUsageEvidence):
                    raise TypeError("model usage evidence has an invalid type")
                usage = ModelUsageEvidence.model_validate(raw.model_dump(mode="python"))
        except Exception as error:
            category = _classify_model_exception(error)
        if category is not None:
            raise _ControlledOperationError(category, self._role)
        return usage

    @property
    def model_revision(self) -> str | None:
        """Expose the underlying observed revision without provider prose."""

        category: AgentRunFailureCategory | None = None
        revision: str | None = None
        try:
            raw = getattr(self._client, "model_revision", None)
            if raw is not None and not isinstance(raw, str):
                raise TypeError("model revision evidence has an invalid type")
            revision = raw
        except Exception as error:
            category = _classify_model_exception(error)
        if category is not None:
            raise _ControlledOperationError(category, self._role)
        return revision

    @property
    def live_approval(self) -> LiveModelApproval | None:
        return self._live_approval

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[OutputModel],
    ) -> OutputModel:
        self._seam_tracker.seam = self._role
        started_ns = time.perf_counter_ns()
        lifecycle_event(
            "model.call.started",
            role=self._role,
            provider=self._provider_id,
            output_contract=output_type.__name__,
            attempt=self._seam_tracker.attempt,
        )
        category: AgentRunFailureCategory | None = None
        reason_code: AgentRunFailureReason | None = None
        parsed: OutputModel | None = None
        try:
            raw = self._client.parse(
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )
            parsed = output_type.model_validate(raw.model_dump(mode="python"))
        except Exception as error:
            category = _classify_model_exception(error)
            reason_code = _model_failure_reason(error, category)
        if category is not None:
            lifecycle_event(
                "model.call.failed",
                level=logging.WARNING,
                role=self._role,
                category=category,
                reason_code=reason_code,
                attempt=self._seam_tracker.attempt,
                elapsed_ms=_elapsed_milliseconds(started_ns),
            )
            # Raise after leaving the except block so the provider exception
            # is not retained as __context__ or an exception chain.
            raise _ControlledOperationError(category, self._role, reason_code)
        if parsed is None:  # pragma: no cover - Pydantic outputs cannot be None
            lifecycle_event(
                "model.call.failed",
                level=logging.ERROR,
                role=self._role,
                category="internal",
                reason_code="internal_failure",
                attempt=self._seam_tracker.attempt,
                elapsed_ms=_elapsed_milliseconds(started_ns),
            )
            raise _ControlledOperationError("internal", self._role)
        lifecycle_event(
            "model.call.completed",
            role=self._role,
            attempt=self._seam_tracker.attempt,
            elapsed_ms=_elapsed_milliseconds(started_ns),
        )
        return parsed


class _SanitizedDeterministicValidator:
    """Apply the fixed error boundary to controller validation seams."""

    def __init__(
        self,
        validator: DeterministicValidator,
        seam_tracker: _OperationSeamTracker,
    ) -> None:
        self._validator = validator
        self._seam_tracker = seam_tracker

    def __call__(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        workspace: IsolatedWorkspace,
        attempt: int,
    ) -> ValidationReport:
        self._seam_tracker.seam = "deterministic_validator"
        started_ns = time.perf_counter_ns()
        lifecycle_event(
            "validation.started",
            attempt=attempt,
            planned_checks=len(manifest.validation_plan),
            changed_files=len(change_set.changed_paths),
        )
        failed = False
        error_type: str | None = None
        report: ValidationReport | None = None
        try:
            raw = self._validator(request, manifest, change_set, workspace, attempt)
            report = ValidationReport.model_validate(raw.model_dump(mode="python"))
        except Exception as error:
            failed = True
            error_type = type(error).__name__
        if failed or report is None:
            lifecycle_event(
                "validation.failed",
                level=logging.ERROR,
                attempt=attempt,
                reason_code="deterministic_validation_failed",
                error_type=error_type,
                elapsed_ms=_elapsed_milliseconds(started_ns),
            )
            raise _ControlledOperationError("internal", "deterministic_validator")
        known_validation_ids = {
            identifier
            for result in report.results
            for identifier in (result.check_id, result.command_id)
        }
        for result in report.results:
            diagnostic_ids = ",".join(result.diagnostic_ids) or "none"
            failure_code = _validation_failure_code(result.summary)
            blocked_by = _validation_prerequisite_ids(
                result.summary,
                known_validation_ids,
            )
            lifecycle_event(
                "validation.check.completed",
                attempt=attempt,
                check_id=result.check_id,
                command_id=result.command_id,
                required=result.required,
                status=result.status.value,
                exit_code=(result.receipt.exit_code if result.receipt is not None else None),
                duration_ms=_validation_receipt_milliseconds(result),
                failure_code=failure_code,
                diagnostic_ids=diagnostic_ids,
                blocked_by=",".join(blocked_by) or "none",
            )
            for diagnostic_id in result.diagnostic_ids:
                lifecycle_event(
                    "validation.check.diagnostic",
                    attempt=attempt,
                    check_id=result.check_id,
                    diagnostic_id=diagnostic_id,
                )
            for prerequisite_id in blocked_by:
                lifecycle_event(
                    "validation.check.prerequisite_blocked",
                    attempt=attempt,
                    check_id=result.check_id,
                    prerequisite_id=prerequisite_id,
                )
        required_nonpass = tuple(
            result.check_id
            for result in report.results
            if result.required and result.status.value != "passed"
        )
        aggregate_diagnostics = tuple(
            dict.fromkeys(
                diagnostic_id
                for result in report.results
                for diagnostic_id in result.diagnostic_ids
            )
        )
        lifecycle_event(
            "validation.completed",
            attempt=attempt,
            disposition=report.disposition.value,
            checks=len(report.results),
            passed=sum(result.status.value == "passed" for result in report.results),
            failed=sum(result.status.value == "failed" for result in report.results),
            unavailable=sum(result.status.value == "unavailable" for result in report.results),
            nonterminal=sum(result.status.value == "nonterminal" for result in report.results),
            required_nonpass=",".join(required_nonpass) or "none",
            diagnostic_ids=",".join(aggregate_diagnostics) or "none",
            elapsed_ms=_elapsed_milliseconds(started_ns),
        )
        return report


def _classify_model_exception(error: Exception) -> AgentRunFailureCategory:
    if isinstance(error, ModelConfigurationError):
        return "configuration"
    if isinstance(error, ModelRefusalError):
        return "refusal"
    if isinstance(error, ModelOutputError):
        first_argument = error.args[0] if error.args else None
        if isinstance(first_argument, str) and first_argument.startswith(
            "model response was incomplete"
        ):
            return "incomplete"
        return "invalid"
    if isinstance(error, (TypeError, ValueError)):
        return "invalid"
    return "provider_unavailable"


def _default_failure_reason(
    category: AgentRunFailureCategory,
    seam: AgentRunFailureSeam,
) -> AgentRunFailureReason:
    if seam == "deterministic_validator":
        return "deterministic_validation_failed"
    reasons: dict[AgentRunFailureCategory, AgentRunFailureReason] = {
        "configuration": "configuration_invalid",
        "refusal": "provider_refusal",
        "incomplete": "response_incomplete",
        "invalid": "structured_output_invalid",
        "provider_unavailable": "provider_unavailable",
        "internal": "internal_failure",
    }
    return reasons[category]


def _model_failure_reason(
    error: Exception,
    category: AgentRunFailureCategory,
) -> AgentRunFailureReason:
    if (
        category == "provider_unavailable"
        and isinstance(error, ModelRuntimeError)
        and str(error) == "local Ollama request exceeded its deadline"
    ):
        return "provider_timeout"
    if isinstance(error, ModelOutputError):
        message = str(error)
        if message == "local Ollama response attempted an unauthorized tool call":
            return "unauthorized_tool_call"
        if message.startswith("local Ollama inventory"):
            return "model_inventory_invalid"
        if (
            message == "model structured output failed schema validation"
            or message.startswith("local Ollama structured output")
            or message.startswith("local Ollama structured text")
            or message == "local Ollama response did not contain structured text"
        ):
            return "structured_output_invalid"
        if category == "invalid":
            return "provider_response_invalid"
    return _default_failure_reason(category, "architect")


def _operation_failure_reason(
    error: Exception,
    category: AgentRunFailureCategory,
    seam: AgentRunFailureSeam,
) -> AgentRunFailureReason:
    if seam == "deterministic_validator":
        return "deterministic_validation_failed"
    if isinstance(error, SanitizedModelPolicyError):
        return error.reason_code
    if isinstance(error, PolicyViolation):
        if seam == "architect" and str(error).startswith(
            "manifest implementation contract does not match"
        ):
            return "implementation_contract_invalid"
        if seam == "architect" and str(error).startswith(
            "manifest omits scope-required approval actions:"
        ):
            return "required_approval_missing"
        if seam == "architect" and _is_transformation_scope_failure(str(error)):
            return "transformation_scope_invalid"
        return "policy_rejected"
    return _default_failure_reason(category, seam)


def _is_transformation_scope_failure(message: str) -> bool:
    return message.startswith(
        (
            "Architect manifest approved paths must exactly equal transformation outputs",
            "manifest transformation ",
            "manifest transformations ",
            "manifest approved paths do not match the caller-owned output boundary",
            "each approved output must be owned by exactly one transformation",
            "manifest must contain at least one transformation",
        )
    )


def build_live_openai_model_clients(
    *,
    model_id: str,
    api_key_environment: str,
    approval: LiveModelApproval,
) -> AgentRunModelClients:
    """Create a live, tool-free, ``store=False`` OpenAI bundle after all gates.

    The credential is read only from the named environment variable.  Neither
    its name nor its value is returned or persisted by the run service.
    Construction imports and configures the SDK but performs no provider call.
    """

    normalized_model = model_id.strip()
    if not normalized_model:
        raise ModelConfigurationError("an explicit model_id is required")
    variable = api_key_environment.strip()
    if _API_KEY_ENVIRONMENT.fullmatch(variable) is None:
        raise ModelConfigurationError("API key environment variable name is invalid")
    if not approval.approved_by.strip():
        raise ModelConfigurationError("live model approval requires a nonempty approved_by")
    api_key = os.environ.get(variable)
    if api_key is None or not api_key.strip():
        raise ModelConfigurationError(
            "the approved API key environment variable is missing or empty"
        )
    client = OpenAIResponsesModelClient(
        normalized_model,
        live_invocation=True,
        api_key=api_key,
        approval=approval,
    )
    return AgentRunModelClients(
        architect=client,
        engineer=client,
        validator=client,
        _live_authorization=_LIVE_AUTHORIZATION,
        _live_approval=LiveModelApproval.model_validate(approval.model_dump(mode="python")),
    )


def build_local_ollama_model_clients(
    *,
    model_id: str,
    approval: LiveModelApproval,
    timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS,
) -> AgentRunModelClients:
    """Create one approved, tool-free client for loopback Ollama inference.

    The adapter fixes the endpoint to ``127.0.0.1`` and accepts no credentials,
    remote URL, tools, shell, filesystem, deployment, or platform authority.
    Construction performs no model call; availability is checked on first use.
    """

    client = OllamaStructuredModelClient(
        model_id,
        approval=approval,
        timeout_seconds=timeout_seconds,
    )
    return AgentRunModelClients(
        architect=client,
        engineer=client,
        validator=client,
        _live_authorization=_LIVE_AUTHORIZATION,
        _live_approval=LiveModelApproval.model_validate(approval.model_dump(mode="python")),
    )


def start_agent_run(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    thread_id: str,
    launch_contract: MigrationLaunchContract,
    request: MigrationRequest | Mapping[str, Any],
    models: AgentRunModelClients,
    trusted_validator: DeterministicValidator | None = None,
) -> AgentRunStatus:
    """Initialize one run and stop at its first terminal state or interrupt.

    ``trusted_validator`` is a controller/test composition hook.  It is not
    exposed by the CLI, persisted as authority, or selected by model output.
    """

    prepared = _prepare_agent_run_start(
        project_root,
        run_dir,
        launch_contract=launch_contract,
        request=request,
        models=models,
    )
    session = AgentRunSession.initialize(
        prepared.root,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        slice_id=prepared.contract.scenario_id,
        source_root=prepared.contract.source_root,
        request_digest=artifact_digest(prepared.request),
        agent_definition_digests=prepared.definition_digests,
        provider_id=prepared.provider_id,
        model_id=prepared.model_id,
    )
    return _complete_agent_run_start(
        session,
        prepared,
        models=models,
        trusted_validator=trusted_validator,
    )


def recover_incomplete_agent_run_start(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    thread_id: str,
    launch_contract: MigrationLaunchContract,
    request: MigrationRequest | Mapping[str, Any],
    models: AgentRunModelClients,
    trusted_validator: DeterministicValidator | None = None,
) -> AgentRunStatus:
    """Finish one exact run bootstrap that stopped before workflow dispatch.

    This recovery path is deliberately narrower than ``start_agent_run``.  It
    accepts only a fully initialized session whose immutable identity matches
    the caller and whose durable trees contain an exact prefix of the
    controller-owned bootstrap writes.  Any checkpoint advance, model artifact,
    operation lifecycle, unexpected file, or foreign binding fails closed.
    """

    prepared = _prepare_agent_run_start(
        project_root,
        run_dir,
        launch_contract=launch_contract,
        request=request,
        models=models,
    )
    session = AgentRunSession.load(prepared.root, run_dir)
    _verify_incomplete_start_session(
        session,
        prepared,
        run_id=run_id,
        thread_id=thread_id,
    )
    return _complete_agent_run_start(
        session,
        prepared,
        models=models,
        trusted_validator=trusted_validator,
    )


def _complete_agent_run_start(
    session: AgentRunSession,
    prepared: _AgentRunStart,
    *,
    models: AgentRunModelClients,
    trusted_validator: DeterministicValidator | None,
) -> AgentRunStatus:
    _write_run_evidence(
        session,
        prepared.request,
        prepared.config,
        prepared.contract,
    )
    components = _compose(
        session,
        prepared.request,
        prepared.config,
        prepared.registry,
        models,
        trusted_validator=trusted_validator,
    )
    return _execute_operation(
        components,
        operation="start",
        attempt=1,
        operation_input_digest=artifact_digest(prepared.request),
        success_kind="agent-run-planned",
        invoke=lambda: components.workflow.start(
            prepared.request,
            thread_id=session.context.thread_id,
        ),
    )


def prepare_agent_run_request(
    project_root: Path,
    *,
    request_id: str,
    launch_contract: MigrationLaunchContract,
    requested_at: datetime,
) -> MigrationRequest:
    """Create the exact preset request for the current source-tree bytes.

    This is a local preparation step only. It performs no model, network,
    platform, Git, validation, or approval action. The returned request remains
    subject to the same immutable revision and preset checks at run start.
    """

    contract = _canonical_launch_contract(launch_contract)
    assert_agent_request_secret_free({"request_id": request_id})
    root = _safe_project_root(project_root)
    preset = _preset_for(contract.platform)
    source = _safe_source_root(root, contract.source_root)
    request = MigrationRequest(
        request_id=request_id,
        platform=contract.platform,
        repository=contract.source_root,
        base_revision=snapshot_tree(source).revision,
        target=MigrationTarget(
            entry_path=contract.entry_path,
            target_runtime=contract.target_runtime,
            source_version=contract.source_version,
            target_version=contract.target_version,
            description=contract.canonical_description,
        ),
        allowed_environment=EnvironmentKind.LOCAL,
        requested_at=requested_at,
    )
    _validate_preset_request(request, contract, preset)
    return request


def assert_agent_request_secret_free(value: object) -> None:
    """Reject secret-shaped request input before request or run persistence.

    Kept as the application-facing name while classification remains shared
    with the UI and append-only conversation persistence boundaries.
    """

    assert_no_request_secrets(value, boundary="migration request")


def resume_agent_run(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    thread_id: str,
    approval: ManifestApproval | Mapping[str, Any],
    models: AgentRunModelClients | None = None,
    request: MigrationRequest | Mapping[str, Any] | None = None,
    trusted_validator: DeterministicValidator | None = None,
) -> AgentRunStatus:
    """Resume the exact pending manifest decision on the canonical request."""

    parsed_approval = (
        approval
        if isinstance(approval, ManifestApproval)
        else ManifestApproval.model_validate(dict(approval))
    )
    if parsed_approval.selection == "approve" and models is None:
        raise ModelConfigurationError("manifest approval requires bound role model clients")
    components = _load_components(
        project_root,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        models=models,
        request=request,
        trusted_validator=trusted_validator,
        pending_authorization=parsed_approval,
    )
    components.session.validate_portable_evidence(parsed_approval)
    return _execute_operation(
        components,
        operation="resume",
        attempt=1,
        operation_input_digest=artifact_digest(parsed_approval),
        success_kind="agent-run-resumed",
        control_evidence=((AGENT_RUN_MANIFEST_APPROVAL_PATH, parsed_approval),),
        invoke=lambda: (
            None
            if components.in_flight_terminal
            else components.workflow.continue_local_failure(thread_id=thread_id)
            if components.in_flight_operation is not None
            else components.workflow.resume(parsed_approval, thread_id=thread_id)
        ),
    )


def retry_agent_run(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    thread_id: str,
    approval: CorrectionApproval | Mapping[str, Any],
    models: AgentRunModelClients,
    request: MigrationRequest | Mapping[str, Any] | None = None,
    trusted_validator: DeterministicValidator | None = None,
) -> AgentRunStatus:
    """Authorize only the checkpoint's exact bounded correction attempt two."""

    parsed_approval = (
        approval
        if isinstance(approval, CorrectionApproval)
        else CorrectionApproval.model_validate(dict(approval))
    )
    components = _load_components(
        project_root,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        models=models,
        request=request,
        trusted_validator=trusted_validator,
        pending_authorization=parsed_approval,
    )
    components.session.validate_portable_evidence(parsed_approval)
    return _execute_operation(
        components,
        operation="retry",
        attempt=2,
        operation_input_digest=artifact_digest(parsed_approval),
        success_kind="agent-run-retried",
        control_evidence=((AGENT_RUN_CORRECTION_APPROVAL_PATH, parsed_approval),),
        invoke=lambda: (
            None
            if components.in_flight_terminal
            else components.workflow.continue_local_failure(thread_id=thread_id)
            if components.in_flight_operation is not None
            else components.workflow.retry_recoverable(
                parsed_approval,
                thread_id=thread_id,
            )
        ),
    )


def get_agent_run_status(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    thread_id: str,
    request: MigrationRequest | Mapping[str, Any] | None = None,
) -> AgentRunStatus:
    """Read and verify exact-thread state without a provider or model call."""

    components = _load_components(
        project_root,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        models=None,
        request=request,
        trusted_validator=None,
        read_only=True,
    )
    return _status_from_components(components)


@dataclass(frozen=True)
class _PlatformPreset:
    preset_id: str
    runtime: MigrationPlatformRuntime
    adapter: PlatformAdapter
    entry_path: str
    target_runtime: str
    source_version: str
    target_version: str
    wiki_query: str
    wiki_max_primary_hits: int


@dataclass(frozen=True)
class _AgentRunStart:
    """Validated controller inputs shared by fresh and recovery start paths."""

    root: Path
    contract: MigrationLaunchContract
    request: MigrationRequest
    preset: _PlatformPreset
    registry: AgentRegistry
    definition_digests: AgentDefinitionDigests
    provider_id: str
    model_id: str
    config: AgentRunConfig


def _prepare_agent_run_start(
    project_root: Path,
    run_dir: Path,
    *,
    launch_contract: MigrationLaunchContract,
    request: MigrationRequest | Mapping[str, Any],
    models: AgentRunModelClients,
) -> _AgentRunStart:
    """Validate every external binding before creating or recovering a run."""

    contract = _canonical_launch_contract(launch_contract)
    assert_agent_request_secret_free(request)
    parsed_request = _parse_request(request)
    preset = _preset_for(contract.platform)
    _validate_preset_request(parsed_request, contract, preset)
    root = _safe_project_root(project_root)
    _validate_run_location(root, run_dir, contract.source_root)
    registry = load_agent_registry(root / "agents")
    definition_digests = _definition_digests(registry)
    source = _safe_source_root(root, contract.source_root)
    if snapshot_tree(source).revision != parsed_request.base_revision:
        raise PolicyViolation("migration request revision does not match current source bytes")
    _preflight_wiki(root, parsed_request, preset, contract.wiki_as_of)

    # Accessing bundle properties validates provider/model identity before the
    # first fresh-run filesystem mutation or recovery decision.
    provider_id = models.provider_id
    model_id = models.model_id
    return _AgentRunStart(
        root=root,
        contract=contract,
        request=parsed_request,
        preset=preset,
        registry=registry,
        definition_digests=definition_digests,
        provider_id=provider_id,
        model_id=model_id,
        config=AgentRunConfig(
            preset_id=contract.scenario_id,
            wiki_as_of=contract.wiki_as_of,
        ),
    )


def _verify_incomplete_start_session(
    session: AgentRunSession,
    prepared: _AgentRunStart,
    *,
    run_id: str,
    thread_id: str,
) -> None:
    """Accept only an exact, unadvanced prefix of controller bootstrap writes."""

    expected_binding = (
        run_id,
        thread_id,
        prepared.contract.scenario_id,
        prepared.contract.source_root,
        artifact_digest(prepared.request),
        prepared.definition_digests,
        prepared.provider_id,
        prepared.model_id,
        prepared.request.base_revision,
    )
    actual_binding = (
        session.context.run_id,
        session.context.thread_id,
        session.context.slice_id,
        session.context.source_root,
        session.context.request_digest,
        session.context.agent_definition_digests,
        session.context.provider_id,
        session.context.model_id,
        session.context.source_revision,
    )
    if actual_binding != expected_binding:
        raise PolicyViolation("incomplete run bootstrap differs from the requested run binding")

    session.verify_index("initialized", exact=False)
    session.verify_source_revision()
    evidence = _start_bootstrap_snapshot(session.evidence_dir, role="portable evidence")
    portable_paths = frozenset(entry.path for entry in evidence.entries)
    write_order = (
        "run-context.json",
        "indexes/initialized.json",
        AGENT_RUN_REQUEST_PATH,
        AGENT_RUN_CONFIG_PATH,
        AGENT_RUN_LAUNCH_CONTRACT_PATH,
        f"indexes/{AGENT_RUN_EVIDENCE_KIND}.json",
    )
    valid_prefixes = {frozenset(write_order[:length]) for length in range(2, len(write_order) + 1)}
    if portable_paths not in valid_prefixes:
        raise PolicyViolation("incomplete run bootstrap has an invalid portable evidence inventory")
    if frozenset(evidence.directories) not in {
        frozenset({"indexes"}),
        frozenset({"graphs", "indexes"}),
    }:
        raise PolicyViolation("incomplete run bootstrap has unexpected evidence directories")

    state = _start_bootstrap_snapshot(session.state_dir, role="runtime state")
    state_files = frozenset(entry.path for entry in state.entries)
    base_state_files = frozenset({"checkpoints.sqlite3", "runtime.json"})
    initialized_anchor = f"anchors/{AGENT_RUN_EVIDENCE_KIND}.json"
    allowed_state_files = {base_state_files, base_state_files | {initialized_anchor}}
    if state_files not in allowed_state_files or state.directories != ("anchors",):
        raise PolicyViolation("incomplete run bootstrap has unexpected runtime state")
    checkpoint = state.by_path()["checkpoints.sqlite3"]
    if checkpoint.content:
        raise PolicyViolation("incomplete run bootstrap checkpoint already advanced")

    for root, role in (
        (session.workspaces_dir, "workspace"),
        (session.scratch_dir, "scratch"),
    ):
        snapshot = _start_bootstrap_snapshot(root, role=role)
        if snapshot.entries or snapshot.directories:
            raise PolicyViolation(f"incomplete run bootstrap has unexpected {role} state")

    anchor_present = initialized_anchor in state_files
    full_portable_inventory = portable_paths == frozenset(write_order)
    if anchor_present and not full_portable_inventory:
        raise PolicyViolation("incomplete run bootstrap anchor precedes complete evidence")
    if anchor_present:
        stored_request, stored_config, stored_contract = _verify_run_evidence(session)
        if (
            stored_request != prepared.request
            or stored_config != prepared.config
            or stored_contract != prepared.contract
        ):
            raise PolicyViolation("incomplete run bootstrap evidence differs from current inputs")


def _start_bootstrap_snapshot(root: Path, *, role: str) -> TreeSnapshot:
    try:
        return snapshot_tree(root)
    except (OSError, WorkspaceViolation) as exc:
        raise PolicyViolation(f"incomplete run bootstrap {role} is unsafe") from exc


@dataclass(frozen=True)
class _RunComponents:
    session: AgentRunSession
    request: MigrationRequest
    config: AgentRunConfig
    workflow: Any
    model_roles: ModelAgentWorkflowRoles
    seam_tracker: _OperationSeamTracker
    failure: AgentRunFailure | None = None
    in_flight_operation: _InFlightModelOperation | None = None
    in_flight_terminal: bool = False


@dataclass(frozen=True)
class _VerifiedOperationLifecycle:
    """One runtime-anchored operation and its exact workflow projection."""

    kind: str
    status: AgentRunStatus
    workflow_status: AgentRunStatus
    checkpoint: AgentRunCheckpointProjection
    failure: AgentRunFailure | None = None


@dataclass(frozen=True)
class _PartialAuthorization:
    """One runtime-anchored authorization whose lifecycle is not complete."""

    kind: str
    approval: ManifestApproval | CorrectionApproval | None


@dataclass(frozen=True)
class _InFlightModelOperation:
    """One attempt that advanced beyond its last portable lifecycle."""

    attempt: Literal[1, 2]
    pending_seam: Literal["engineer", "validator"] | None
    artifact_paths: tuple[str, ...]
    portable_freeze_prefix: tuple[str, ...]
    controller_failed_before_provider: bool = False


def _execute_operation(
    components: _RunComponents,
    *,
    operation: AgentRunOperation,
    attempt: int,
    operation_input_digest: Sha256Digest,
    success_kind: str,
    invoke: Callable[[], object],
    control_evidence: tuple[tuple[str, StrictModel], ...] = (),
) -> AgentRunStatus:
    started_ns = time.perf_counter_ns()
    components.seam_tracker.attempt = attempt
    lifecycle_event(
        "workflow.operation.started",
        operation=operation,
        attempt=attempt,
    )
    if control_evidence:
        in_flight = components.in_flight_operation
        if in_flight is None:
            _persist_authorization_boundary(
                components,
                operation=operation,
                control_evidence=control_evidence,
            )
            # Kept as an explicit seam so a process interruption after durable
            # authorization but before workflow invocation is recoverable by
            # resubmitting the exact same approval.
            _after_authorization_boundary()
        elif (operation, attempt) not in {("resume", 1), ("retry", 2)}:
            raise PolicyViolation("in-flight operation cannot change authorization path")

    controlled: (
        tuple[
            AgentRunFailureCategory,
            AgentRunFailureSeam,
            AgentRunFailureReason,
        ]
        | None
    ) = None
    try:
        invoke()
    except _ControlledOperationError as error:
        controlled = (error.category, error.seam, error.reason_code)
    except Exception as error:
        if (
            components.in_flight_operation is None
            or components.in_flight_operation.pending_seam is None
        ) and isinstance(error, ModelWorkflowIntegrationError):
            concurrent_seam = _concurrent_role_invocation_seam(
                components.session,
                components.request,
                attempt=attempt,
            )
            if concurrent_seam is not None:
                raise PolicyViolation(
                    f"agent run operation is already in progress at the {concurrent_seam} seam"
                ) from None
        seam = components.seam_tracker.seam
        if seam is None:
            lifecycle_event(
                "workflow.operation.failed",
                level=logging.ERROR,
                operation=operation,
                attempt=attempt,
                seam="controller",
                category="internal",
                reason_code="unclassified_controller_error",
                error_type=type(error).__name__,
                elapsed_ms=_elapsed_milliseconds(started_ns),
            )
            raise
        category: AgentRunFailureCategory = (
            "invalid" if isinstance(error, (PolicyViolation, TypeError, ValueError)) else "internal"
        )
        controlled = (category, seam, _operation_failure_reason(error, category, seam))
    if controlled is not None:
        category, seam, reason_code = controlled
        lifecycle_event(
            "workflow.operation.failed",
            level=logging.WARNING,
            operation=operation,
            attempt=attempt,
            seam=seam,
            category=category,
            reason_code=reason_code,
            elapsed_ms=_elapsed_milliseconds(started_ns),
        )
        failure = _persist_operation_failure(
            components.session,
            components.request,
            operation=operation,
            seam=seam,
            category=category,
            reason_code=reason_code,
            attempt=attempt,
            operation_input_digest=operation_input_digest,
        )
        failed = _RunComponents(
            session=components.session,
            request=components.request,
            config=components.config,
            workflow=components.workflow,
            model_roles=components.model_roles,
            seam_tracker=components.seam_tracker,
            failure=failure,
            in_flight_operation=components.in_flight_operation,
            in_flight_terminal=components.in_flight_terminal,
        )
        status = _status_from_components(failed)
        workflow_status = _status_from_components(failed, surface_failure=False)
        _freeze_operation_evidence(
            components.session,
            AGENT_RUN_FAILURE_KIND,
            status,
            workflow_status,
            _checkpoint_projection(failed),
        )
        return status
    _persist_checkpoint_control_evidence(components)
    status = _status_from_components(components)
    _freeze_operation_evidence(
        components.session,
        success_kind,
        status,
        status,
        _checkpoint_projection(components),
    )
    lifecycle_event(
        "workflow.operation.completed",
        operation=operation,
        attempt=attempt,
        workflow_status=status.status,
        disposition=status.terminal_disposition,
        elapsed_ms=_elapsed_milliseconds(started_ns),
    )
    return status


def _elapsed_milliseconds(started_ns: int) -> int:
    return max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)


def _validation_receipt_milliseconds(result: Any) -> int | None:
    receipt = result.receipt
    if receipt is None:
        return None
    return max(0, int((receipt.ended_at - receipt.started_at).total_seconds() * 1000))


def _validation_failure_code(summary: str) -> str | None:
    match = re.search(r"\bfailure[-_]code=([a-z][a-z0-9_.:-]{0,159})\b", summary)
    return match.group(1) if match is not None else None


def _validation_prerequisite_ids(
    summary: str,
    known_validation_ids: set[str],
) -> tuple[str, ...]:
    marker = "required prerequisite checks did not pass:"
    if marker not in summary:
        return ()
    tail = summary.rsplit(marker, 1)[1].split(".", 1)[0]
    return tuple(
        dict.fromkeys(
            identifier.strip()
            for identifier in tail.split(",")
            if identifier.strip() in known_validation_ids
        )
    )


def _persist_authorization_boundary(
    components: _RunComponents,
    *,
    operation: AgentRunOperation,
    control_evidence: tuple[tuple[str, StrictModel], ...],
) -> None:
    """Validate and freeze exact human authority before downstream execution."""

    if operation == "resume":
        if len(control_evidence) != 1 or not isinstance(control_evidence[0][1], ManifestApproval):
            raise PolicyViolation("manifest authorization evidence is incomplete")
        _validate_manifest_authorization_boundary(
            components,
            control_evidence[0][1],
        )
        kind = AGENT_RUN_MANIFEST_AUTHORIZED_KIND
    elif operation == "retry":
        if len(control_evidence) != 1 or not isinstance(control_evidence[0][1], CorrectionApproval):
            raise PolicyViolation("correction authorization evidence is incomplete")
        _validate_correction_authorization_boundary(
            components,
            control_evidence[0][1],
        )
        kind = AGENT_RUN_CORRECTION_AUTHORIZED_KIND
    else:
        raise PolicyViolation("run start cannot contain authorization evidence")

    _bind_authorization_intent(
        components.session,
        operation=operation,
        kind=kind,
        control_path=control_evidence[0][0],
        approval=control_evidence[0][1],
    )
    _after_authorization_write(kind, "intent")
    _persist_control_evidence(components.session, control_evidence)
    _after_authorization_write(kind, "approval")
    status = _status_from_components(components)
    _freeze_operation_evidence(
        components.session,
        kind,
        status,
        status,
        _checkpoint_projection(components),
    )


def _after_authorization_boundary() -> None:
    """No-op crash seam after durable authorization and before workflow work."""


def _after_authorization_write(kind: str, step: str) -> None:
    """No-op test seam after one immutable authorization transaction write."""

    del kind, step


def _authorization_intent_kind(kind: str) -> str:
    if kind == AGENT_RUN_MANIFEST_AUTHORIZED_KIND:
        return AGENT_RUN_MANIFEST_AUTHORIZATION_INTENT_KIND
    if kind == AGENT_RUN_CORRECTION_AUTHORIZED_KIND:
        return AGENT_RUN_CORRECTION_AUTHORIZATION_INTENT_KIND
    raise PolicyViolation("unknown authorization lifecycle kind")


def _authorization_intent(
    session: AgentRunSession,
    *,
    operation: Literal["resume", "retry"],
    kind: str,
    control_path: str,
    approval: ManifestApproval | CorrectionApproval,
) -> _AgentRunAuthorizationIntent:
    return _AgentRunAuthorizationIntent(
        authorization_kind=kind,
        run_id=session.context.run_id,
        thread_id=session.context.thread_id,
        request_digest=session.context.request_digest,
        operation=operation,
        control_path=validate_relative_path(control_path),
        approval_digest=artifact_digest(approval),
    )


def _bind_authorization_intent(
    session: AgentRunSession,
    *,
    operation: Literal["resume", "retry"],
    kind: str,
    control_path: str,
    approval: ManifestApproval | CorrectionApproval,
) -> None:
    """Bind exact caller authority outside the rewritable portable tree first."""

    intent = _authorization_intent(
        session,
        operation=operation,
        kind=kind,
        control_path=control_path,
        approval=approval,
    )
    intent_kind = _authorization_intent_kind(kind)
    if session.has_runtime_anchor(intent_kind):
        try:
            session.verify_runtime_anchor(intent_kind, intent)
        except PolicyViolation as exc:
            raise PolicyViolation(
                "approval differs from the immutable authorization intent"
            ) from exc
        return
    session.bind_runtime_anchor(intent_kind, intent)


def _verify_authorization_intent(
    session: AgentRunSession,
    *,
    operation: Literal["resume", "retry"],
    kind: str,
    control_path: str,
    approval: ManifestApproval | CorrectionApproval,
) -> None:
    intent = _authorization_intent(
        session,
        operation=operation,
        kind=kind,
        control_path=control_path,
        approval=approval,
    )
    try:
        session.verify_runtime_anchor(_authorization_intent_kind(kind), intent)
    except (FileNotFoundError, PolicyViolation) as exc:
        raise PolicyViolation("approval differs from the immutable authorization intent") from exc


def _validate_manifest_authorization_boundary(
    components: _RunComponents,
    approval: ManifestApproval,
) -> None:
    """Bind one approval to the exact anchored pending interrupt request."""

    snapshot = components.workflow.snapshot(thread_id=components.session.context.thread_id)
    pending = _pending_manifest_interrupt(snapshot)
    if pending is None:
        raise PolicyViolation("thread does not have a pending manifest approval interrupt")
    decision, manifest_id, manifest_digest = pending
    try:
        persisted = DecisionRequest.model_validate(
            components.session.store.read_json(_decision_request_path(decision.decision_id))
        )
    except FileNotFoundError as exc:
        raise PolicyViolation(
            "pending manifest approval lacks anchored decision-request evidence"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("manifest decision-request evidence is malformed") from exc
    if persisted != decision:
        raise PolicyViolation("pending manifest decision differs from portable lifecycle evidence")
    if approval.decision_id != decision.decision_id:
        raise PolicyViolation("approval belongs to a different decision")
    if approval.request_id != decision.request_id:
        raise PolicyViolation("approval belongs to a different migration request")
    if approval.manifest_id != manifest_id:
        raise PolicyViolation("approval belongs to a different manifest")
    if approval.manifest_digest != manifest_digest:
        raise PolicyViolation("approval manifest digest does not match")
    if approval.requested_action is not decision.requested_action:
        raise PolicyViolation("approval authorizes a different action")
    if approval.selection not in decision.options:
        raise PolicyViolation("manifest approval selects an unavailable decision option")


def _validate_correction_authorization_boundary(
    components: _RunComponents,
    approval: CorrectionApproval,
) -> None:
    """Bind one retry approval to the exact anchored correction request."""

    snapshot = components.workflow.snapshot(thread_id=components.session.context.thread_id)
    if any(task.interrupts for task in snapshot.tasks):
        raise PolicyViolation("manifest approval must complete before correction")
    values = cast(dict[str, Any], snapshot.values)
    raw_request = values.get("correction_request")
    if raw_request is None:
        raise PolicyViolation("workflow lacks a correction request")
    try:
        request = CorrectionRequest.model_validate(raw_request)
        persisted = CorrectionRequest.model_validate(
            components.session.store.read_json(_correction_request_path(request.completed_attempt))
        )
    except FileNotFoundError as exc:
        raise PolicyViolation(
            "correction approval lacks anchored correction-request evidence"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("correction request evidence is malformed") from exc
    if persisted != request:
        raise PolicyViolation("checkpointed correction differs from lifecycle evidence")
    expected = CorrectionController.approve_retry(
        request,
        presented_correction_id=approval.correction_id,
        reviewer=approval.reviewer,
        comment=approval.comment,
    )
    if approval != expected:
        raise PolicyViolation("correction approval differs from the exact retry request")


def _persist_control_evidence(
    session: AgentRunSession,
    artifacts: tuple[tuple[str, StrictModel], ...],
) -> None:
    """Persist caller-supplied decisions without granting any new authority.

    Boundary validation occurs before this function. Repeated writes are
    accepted only when the canonical bytes are identical.
    """

    for path, value in artifacts:
        _write_exact_control_artifact(session, path, value)


def _persist_checkpoint_control_evidence(components: _RunComponents) -> None:
    """Copy typed checkpoint decisions into the portable lifecycle index."""

    snapshot = components.workflow.snapshot(thread_id=components.session.context.thread_id)
    values = cast(dict[str, Any], snapshot.values)
    raw_decision = values.get("decision_request")
    if raw_decision is not None:
        decision = DecisionRequest.model_validate(raw_decision)
        _write_exact_control_artifact(
            components.session,
            _decision_request_path(decision.decision_id),
            decision,
        )
    pending = _pending_manifest_interrupt(snapshot)
    if pending is not None:
        decision, _, _ = pending
        _write_exact_control_artifact(
            components.session,
            _decision_request_path(decision.decision_id),
            decision,
        )
    raw_correction = values.get("correction_request")
    if raw_correction is not None:
        correction = CorrectionRequest.model_validate(raw_correction)
        _write_exact_control_artifact(
            components.session,
            _correction_request_path(correction.completed_attempt),
            correction,
        )


def _decision_request_path(decision_id: str) -> str:
    return f"{AGENT_RUN_CONTROL_ROOT}/decision-request-{decision_id}.json"


def _correction_request_path(completed_attempt: int) -> str:
    return f"{AGENT_RUN_CONTROL_ROOT}/correction-request-attempt-{completed_attempt}.json"


def _write_exact_control_artifact(
    session: AgentRunSession,
    path: str,
    value: StrictModel,
) -> None:
    session.validate_portable_evidence(value)
    try:
        existing = session.store.read_json(path)
    except FileNotFoundError:
        session.store.write_json(path, value)
        return
    if artifact_digest(existing) != artifact_digest(value):
        raise PolicyViolation("portable control evidence differs from the consumed decision")


def _persist_operation_failure(
    session: AgentRunSession,
    request: MigrationRequest,
    *,
    operation: AgentRunOperation,
    seam: AgentRunFailureSeam,
    category: AgentRunFailureCategory,
    reason_code: AgentRunFailureReason,
    attempt: int,
    operation_input_digest: Sha256Digest,
) -> AgentRunFailure:
    identity = artifact_digest(
        {
            "run_id": session.context.run_id,
            "thread_id": session.context.thread_id,
            "request_digest": session.context.request_digest,
            "operation": operation,
            "seam": seam,
            "category": category,
            "reason_code": reason_code,
            "attempt": attempt,
            "operation_input_digest": operation_input_digest,
        }
    ).removeprefix("sha256:")[:32]
    failure = AgentRunFailure(
        failure_id=f"failure-{identity}",
        run_id=session.context.run_id,
        thread_id=session.context.thread_id,
        request_id=request.request_id,
        operation=operation,
        seam=seam,
        category=category,
        reason_code=reason_code,
        attempt=attempt,
        request_digest=session.context.request_digest,
        operation_input_digest=operation_input_digest,
        session_context_digest=artifact_digest(session.context),
        source_revision=session.context.source_revision,
        agent_definition_digests=session.context.agent_definition_digests,
    )
    session.store.write_json(AGENT_RUN_FAILURE_PATH, failure)
    return failure


def _preset_for(platform: Platform) -> _PlatformPreset:
    if platform is Platform.SALESFORCE:
        return _PlatformPreset(
            preset_id="salesforce-vf-to-lwc",
            runtime=SALESFORCE_RUNTIME_CONFIG,
            adapter=SALESFORCE_PLATFORM_ADAPTER,
            entry_path=SALESFORCE_SOURCE_ENTRY,
            target_runtime=SALESFORCE_TARGET_RUNTIME,
            source_version=SALESFORCE_API_RUNTIME,
            target_version=SALESFORCE_API_RUNTIME,
            wiki_query=SALESFORCE_WIKI_QUERY,
            wiki_max_primary_hits=1,
        )
    if platform is Platform.MULESOFT:
        return _PlatformPreset(
            preset_id="mulesoft-mule3-to-mule4",
            runtime=MULESOFT_RUNTIME_CONFIG,
            adapter=MULESOFT_PLATFORM_ADAPTER,
            entry_path=MULE3_APP,
            target_runtime=MULESOFT_TARGET_RUNTIME,
            source_version=MULESOFT_SOURCE_VERSION,
            target_version=MULESOFT_TARGET_VERSION,
            wiki_query=MULESOFT_WIKI_QUERY,
            wiki_max_primary_hits=1,
        )
    raise PolicyViolation(f"unsupported migration platform: {platform}")


def _canonical_launch_contract(
    value: MigrationLaunchContract,
) -> MigrationLaunchContract:
    """Validate an exact typed contract before any run or request side effect."""

    if not isinstance(value, MigrationLaunchContract):
        raise PolicyViolation("migration launch contract must be an exact typed contract")
    assert_agent_request_secret_free(value.model_dump(mode="python"))
    try:
        return require_canonical_launch_contract(value)
    except (KeyError, TypeError, ValueError):
        raise PolicyViolation("migration launch contract is not canonical") from None


def _validate_launch_contract_preset(
    contract: MigrationLaunchContract,
    preset: _PlatformPreset,
) -> None:
    """Require the canonical launch contract and runtime preset to agree exactly."""

    if contract.scenario_id != preset.preset_id or contract.platform is not preset.runtime.platform:
        raise PolicyViolation("migration scenario and platform preset configuration drifted")
    expected = (
        preset.entry_path,
        preset.target_runtime,
        preset.source_version,
        preset.target_version,
        preset.runtime.analyzer_version,
        preset.adapter.adapter_id,
        preset.wiki_query,
        preset.wiki_max_primary_hits,
        preset.adapter.scope_policy.required_source_input_paths,
        preset.adapter.scope_policy.approved_output_paths,
        preset.adapter.scope_policy_digest,
    )
    actual = (
        contract.entry_path,
        contract.target_runtime,
        contract.source_version,
        contract.target_version,
        contract.analyzer_version,
        contract.adapter_id,
        contract.wiki_query,
        contract.wiki_max_primary_hits,
        contract.required_source_input_paths,
        contract.approved_output_paths,
        contract.scope_policy_digest,
    )
    if actual != expected:
        raise PolicyViolation("migration launch contract and platform preset differ")


def _validate_preset_request(
    request: MigrationRequest,
    contract: MigrationLaunchContract,
    preset: _PlatformPreset,
) -> None:
    _validate_launch_contract_preset(contract, preset)
    if request.platform is not contract.platform:
        raise PolicyViolation("migration request platform differs from the launch contract")
    if request.platform is not preset.runtime.platform:
        raise PolicyViolation("migration request does not match the selected platform preset")
    if request.repository != contract.source_root:
        raise PolicyViolation("migration request repository must equal the selected source root")
    expected = (
        contract.entry_path,
        contract.target_runtime,
        contract.source_version,
        contract.target_version,
    )
    actual = (
        request.target.entry_path,
        request.target.target_runtime,
        request.target.source_version,
        request.target.target_version,
    )
    if actual != expected:
        raise PolicyViolation("migration request target does not match the shipped platform preset")
    if request.allowed_environment.value != "local":
        raise PolicyViolation("agent runs support only the local validation environment")
    if request.target.description != contract.canonical_description:
        raise PolicyViolation("migration request description differs from the launch contract")
    preset.adapter.validate_request(request)


def _definition_digests(registry: AgentRegistry) -> AgentDefinitionDigests:
    return AgentDefinitionDigests(
        architect=registry.get(AgentRole.ARCHITECT).definition_digest,
        engineer=registry.get(AgentRole.ENGINEER).definition_digest,
        validator=registry.get(AgentRole.VALIDATOR).definition_digest,
    )


def _validate_run_location(project_root: Path, run_dir: Path, source_root: str) -> None:
    if run_dir.is_absolute():
        normalized_absolute = Path(os.path.abspath(run_dir))
        try:
            relative = normalized_absolute.relative_to(project_root).as_posix()
        except ValueError as exc:
            raise PolicyViolation("run directory must be inside the project root") from exc
    else:
        relative = validate_relative_path(run_dir.as_posix())
    parts = tuple(relative.split("/"))
    if any(part.casefold() in {"expected", "golden", "oracle"} for part in parts):
        raise PolicyViolation("run directory cannot contain expected, golden, or oracle segments")
    protected = (source_root, "agents", "knowledge/wiki")
    for path in protected:
        protected_parts = tuple(path.split("/"))
        if (
            parts[: len(protected_parts)] == protected_parts
            or protected_parts[: len(parts)] == parts
        ):
            raise PolicyViolation("run directory cannot overlap source, agents, or Wiki content")


def _preflight_wiki(
    project_root: Path,
    request: MigrationRequest,
    preset: _PlatformPreset,
    as_of: date,
) -> None:
    trace = LlmWiki.load(project_root / "knowledge" / "wiki").search(
        preset.wiki_query,
        platform=request.platform,
        source_version=request.target.source_version,
        target_version=request.target.target_version,
        max_primary_hits=preset.wiki_max_primary_hits,
        as_of=as_of,
        max_age_days=365,
        required_exact_ids=tuple(
            sorted(
                token
                for token in preset.wiki_query.split()
                if EXACT_DIAGNOSTIC_ID_PATTERN.fullmatch(token) is not None
            )
        ),
    )
    if not trace.hits:
        raise PolicyViolation("version-filtered Wiki retrieval returned no Architect evidence")


def _write_run_evidence(
    session: AgentRunSession,
    request: MigrationRequest,
    config: AgentRunConfig,
    launch_contract: MigrationLaunchContract,
) -> None:
    session.store.write_json(AGENT_RUN_REQUEST_PATH, request)
    session.store.write_json(AGENT_RUN_CONFIG_PATH, config)
    session.store.write_json(AGENT_RUN_LAUNCH_CONTRACT_PATH, launch_contract)
    indexed = (
        "run-context.json",
        AGENT_RUN_REQUEST_PATH,
        AGENT_RUN_CONFIG_PATH,
        AGENT_RUN_LAUNCH_CONTRACT_PATH,
    )
    session.write_index(AGENT_RUN_EVIDENCE_KIND, indexed)
    index_payload = session.store.read_json(f"indexes/{AGENT_RUN_EVIDENCE_KIND}.json")
    session.bind_runtime_anchor(
        AGENT_RUN_EVIDENCE_KIND,
        _evidence_anchor(session, request, config, index_payload),
    )
    _verify_run_evidence(session)


def _verify_run_evidence(
    session: AgentRunSession,
) -> tuple[MigrationRequest, AgentRunConfig, MigrationLaunchContract]:
    session.verify_index(AGENT_RUN_EVIDENCE_KIND, exact=False)
    try:
        request = MigrationRequest.model_validate(session.store.read_json(AGENT_RUN_REQUEST_PATH))
        config = AgentRunConfig.model_validate(session.store.read_json(AGENT_RUN_CONFIG_PATH))
        launch_contract = MigrationLaunchContract.model_validate(
            session.store.read_json(AGENT_RUN_LAUNCH_CONTRACT_PATH)
        )
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation("agent-run portable evidence is malformed") from exc
    canonical_contract = _canonical_launch_contract(launch_contract)
    if artifact_digest(request) != session.context.request_digest:
        raise PolicyViolation("canonical migration request differs from the run session")
    if (
        config.preset_id != canonical_contract.scenario_id
        or session.context.slice_id != canonical_contract.scenario_id
    ):
        raise PolicyViolation("agent-run preset differs from the run session")
    if config.wiki_as_of != canonical_contract.wiki_as_of:
        raise PolicyViolation("agent-run Wiki cutoff differs from the launch contract")
    if session.context.source_root != canonical_contract.source_root:
        raise PolicyViolation("agent-run source root differs from the launch contract")
    index_payload = session.store.read_json(f"indexes/{AGENT_RUN_EVIDENCE_KIND}.json")
    session.verify_runtime_anchor(
        AGENT_RUN_EVIDENCE_KIND,
        _evidence_anchor(session, request, config, index_payload),
    )
    return request, config, canonical_contract


def _evidence_anchor(
    session: AgentRunSession,
    request: MigrationRequest,
    config: AgentRunConfig,
    index_payload: object,
) -> AgentRunEvidenceAnchor:
    return AgentRunEvidenceAnchor(
        run_id=session.context.run_id,
        thread_id=session.context.thread_id,
        request_digest=artifact_digest(request),
        config_digest=artifact_digest(config),
        lifecycle_index_digest=artifact_digest(index_payload),
    )


def _load_components(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    thread_id: str,
    models: AgentRunModelClients | None,
    request: MigrationRequest | Mapping[str, Any] | None,
    trusted_validator: DeterministicValidator | None,
    read_only: bool = False,
    pending_authorization: ManifestApproval | CorrectionApproval | None = None,
) -> _RunComponents:
    root = _safe_project_root(project_root)
    session = AgentRunSession.load(root, run_dir)
    if session.context.run_id != run_id:
        raise PolicyViolation("run_id does not match the loaded run session")
    if session.context.thread_id != thread_id:
        raise PolicyViolation("thread_id does not match the loaded run session")
    canonical_request, config, contract = _verify_run_evidence(session)
    lifecycle = _verify_latest_operation_evidence(
        session,
        canonical_request,
        pending_authorization=pending_authorization,
    )
    failure = lifecycle.failure
    if request is not None and _parse_request(request) != canonical_request:
        raise PolicyViolation("caller request differs from immutable run evidence")
    preset = _preset_for(canonical_request.platform)
    _validate_preset_request(canonical_request, contract, preset)
    if config.preset_id != preset.preset_id:
        raise PolicyViolation("stored run preset does not match the canonical request")
    registry = load_agent_registry(root / "agents")
    if _definition_digests(registry) != session.context.agent_definition_digests:
        raise PolicyViolation("current agent definitions differ from the run session")
    session.enable_inflight_model_routing()
    in_flight_operation = _verify_role_invocation_leases(
        session,
        canonical_request,
        registry,
        lifecycle_kind=lifecycle.kind,
    )
    if failure is not None and not read_only:
        raise PolicyViolation(
            "agent run is terminally failed; inspect status instead of resuming or retrying"
        )
    if in_flight_operation is not None and pending_authorization is not None:
        _verify_in_flight_resubmission_authority(
            session,
            in_flight_operation,
            pending_authorization,
        )

    if models is None:
        bound_models = _passive_models(session.context.provider_id, session.context.model_id)
    else:
        if (
            models.provider_id != session.context.provider_id
            or models.model_id != session.context.model_id
        ):
            raise PolicyViolation("model provider or model ID differs from the run session")
        _verify_recorded_execution_boundary(session, canonical_request, models)
        bound_models = models
    components = _compose(
        session,
        canonical_request,
        config,
        registry,
        bound_models,
        trusted_validator=trusted_validator,
        read_only=read_only,
        disable_execution=models is None,
        require_existing_graph_evidence=True,
        failure=failure,
        recovery_seam=(
            in_flight_operation.pending_seam if in_flight_operation is not None else None
        ),
        in_flight_operation=in_flight_operation,
    )
    actual_status = _status_from_components(components)
    actual_workflow_status = _status_from_components(components, surface_failure=False)
    actual_checkpoint = _checkpoint_projection(components)
    if in_flight_operation is None:
        status_mismatch = actual_status != lifecycle.status
        workflow_mismatch = actual_workflow_status != lifecycle.workflow_status
        checkpoint_mismatch = actual_checkpoint != lifecycle.checkpoint
        if status_mismatch or workflow_mismatch or checkpoint_mismatch:
            recovery = _authorized_controller_failure_recovery(
                components,
                lifecycle,
                actual_status=actual_status,
                actual_workflow_status=actual_workflow_status,
                actual_checkpoint=actual_checkpoint,
            )
            if recovery is None:
                if status_mismatch:
                    raise PolicyViolation(
                        "workflow state projection differs from immutable lifecycle evidence"
                    )
                if workflow_mismatch:
                    raise PolicyViolation(
                        "underlying workflow projection differs from immutable lifecycle evidence"
                    )
                raise PolicyViolation(
                    "complete checkpoint projection differs from immutable lifecycle evidence"
                )
            if pending_authorization is not None:
                _verify_in_flight_resubmission_authority(
                    session,
                    recovery,
                    pending_authorization,
                )
            components = replace(components, in_flight_operation=recovery)
            in_flight_terminal = _verify_in_flight_workflow_projection(
                components,
                lifecycle,
                recovery,
                actual_status=actual_status,
                actual_workflow_status=actual_workflow_status,
                actual_checkpoint=actual_checkpoint,
            )
            components = replace(components, in_flight_terminal=in_flight_terminal)
    else:
        in_flight_terminal = _verify_in_flight_workflow_projection(
            components,
            lifecycle,
            in_flight_operation,
            actual_status=actual_status,
            actual_workflow_status=actual_workflow_status,
            actual_checkpoint=actual_checkpoint,
        )
        components = replace(components, in_flight_terminal=in_flight_terminal)
    return components


def _authorized_controller_failure_recovery(
    components: _RunComponents,
    lifecycle: _VerifiedOperationLifecycle,
    *,
    actual_status: AgentRunStatus,
    actual_workflow_status: AgentRunStatus,
    actual_checkpoint: AgentRunCheckpointProjection,
) -> _InFlightModelOperation | None:
    """Recognize only attempt two's authorized pre-provider controller crash."""

    if lifecycle.kind != AGENT_RUN_CORRECTION_AUTHORIZED_KIND or lifecycle.failure is not None:
        return None
    base = lifecycle.status
    if (
        base != lifecycle.workflow_status
        or base.status != "completed"
        or base.terminal_disposition != ValidationDisposition.RECOVERABLE_FAILURE.value
        or base.execution_attempt != 1
        or base.pending_nodes
        or base.task_failed
        or base.interrupt is not None
        or base.correction is None
        or base.correction.action is not CorrectionAction.RETRY_IMPLEMENTATION
    ):
        return None
    if (
        actual_status != actual_workflow_status
        or actual_status.status != "implementing"
        or actual_status.terminal_disposition != ValidationDisposition.RECOVERABLE_FAILURE.value
        or actual_status.execution_attempt != 2
        or actual_status.pending_nodes != ("engineer",)
        or not actual_status.task_failed
        or actual_status.interrupt is not None
        or actual_status.failure is not None
        or actual_status.correction != base.correction
    ):
        return None
    if (
        actual_checkpoint.request != components.request
        or actual_checkpoint.next != ("engineer",)
        or len(actual_checkpoint.tasks) != 1
    ):
        return None
    task = actual_checkpoint.tasks[0]
    null_digest = _checkpoint_value_digest(None)
    if (
        task.name != "engineer"
        or task.interrupts
        or task.error_digest == null_digest
        or task.state_digest != null_digest
        or task.result_digest != null_digest
    ):
        return None
    return _InFlightModelOperation(
        attempt=2,
        pending_seam=None,
        artifact_paths=(),
        portable_freeze_prefix=(),
        controller_failed_before_provider=True,
    )


def _verify_recorded_execution_boundary(
    session: AgentRunSession,
    request: MigrationRequest,
    models: AgentRunModelClients,
) -> None:
    """Prevent a resume from swapping recorded, remote, and local runtimes."""

    path = f"model-runs/{request.request_id}/architect.json"
    try:
        payload = session.store.read_json(path)
    except FileNotFoundError:
        # No Architect invocation exists for a preflight-only terminal run.
        return
    try:
        model_call = ArchitectRun.model_validate(payload).model_call
        recorded = model_call.resolved_execution_boundary
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("stored Architect model evidence is invalid") from exc
    if recorded != models.execution_boundary:
        raise PolicyViolation("model execution boundary differs from the run session")
    if recorded == "local_loopback":
        if model_call.model_revision is None:
            raise PolicyViolation("stored local model evidence has no observed revision")
        try:
            models.bind_recorded_model_revision(model_call.model_revision)
        except ModelConfigurationError:
            raise PolicyViolation(
                "local model revision differs from immutable run evidence"
            ) from None
        except ModelRuntimeError:
            raise


def _compose(
    session: AgentRunSession,
    request: MigrationRequest,
    config: AgentRunConfig,
    registry: AgentRegistry,
    models: AgentRunModelClients,
    *,
    trusted_validator: DeterministicValidator | None,
    read_only: bool = False,
    disable_execution: bool = False,
    require_existing_graph_evidence: bool = False,
    failure: AgentRunFailure | None = None,
    recovery_seam: Literal["engineer", "validator"] | None = None,
    in_flight_operation: _InFlightModelOperation | None = None,
) -> _RunComponents:
    if (
        models.provider_id != session.context.provider_id
        or models.model_id != session.context.model_id
    ):
        raise PolicyViolation("role model identity does not match the run session")
    session.enable_inflight_model_routing()
    preset = _preset_for(request.platform)
    graph_root = session.evidence_dir / "graphs"
    if read_only:
        try:
            metadata = graph_root.lstat()
        except FileNotFoundError as exc:
            raise PolicyViolation("run has no dependency-graph evidence") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PolicyViolation("run dependency-graph evidence root is unsafe")
    graph_store = GraphSnapshotStore(graph_root)
    graph_key = GraphSnapshotKey(
        platform=request.platform,
        source_revision=request.base_revision,
        entry_paths=(preset.entry_path,),
        analyzer_version=preset.runtime.analyzer_version,
    )
    graph_index_kind = "graph-" + graph_key.cache_digest.removeprefix("sha256:")[:32]
    if require_existing_graph_evidence and not session.has_runtime_anchor(graph_index_kind):
        raise PolicyViolation("run has no immutable dependency-graph lifecycle anchor")
    factory = RevisionBoundArchitectContextFactory.for_session(
        session,
        request,
        registry,
        graph_store=graph_store,
        runtime=preset.runtime,
        entry_paths=(preset.entry_path,),
        wiki_root=session.project_root / "knowledge" / "wiki",
        wiki_query=preset.wiki_query,
        wiki_as_of=config.wiki_as_of,
        wiki_max_primary_hits=preset.wiki_max_primary_hits,
        platform_adapter=preset.adapter,
    )
    if read_only or disable_execution:
        validator: DeterministicValidator = _NeverValidate()
    elif trusted_validator is not None:
        validator = trusted_validator
    elif request.platform is Platform.SALESFORCE:
        validator = build_salesforce_local_validator(
            session,
            registry,
            timeout_seconds=120.0,
        )
    else:
        validator = build_mulesoft_local_validator(session)
    seam_tracker = _OperationSeamTracker(seam=recovery_seam)
    architect_model, engineer_model, validator_model = _sanitized_models(
        models,
        seam_tracker,
    )
    roles = ModelAgentWorkflowRoles(
        registry,
        architect_model=architect_model,
        engineer_model=engineer_model,
        validator_model=validator_model,
        architect_context_factory=factory,
        correction_wiki_retriever=factory.retrieve_correction_wiki,
        workspace_factory=filesystem_workspace_factory(
            session.source_root,
            temp_parent=session.workspaces_dir,
        ),
        deterministic_validator=_SanitizedDeterministicValidator(validator, seam_tracker),
        artifact_store=session.store,
    )
    if require_existing_graph_evidence:
        model_root = f"model-runs/{request.request_id}"
        architect_path = f"{model_root}/architect.json"
        preflight_path = f"{model_root}/architect-preflight.json"
        present = tuple(
            _portable_artifact_exists(session, path) for path in (architect_path, preflight_path)
        )
        architect_failed = failure is not None and failure.seam == "architect"
        expected_outcomes = 0 if architect_failed else 1
        if present.count(True) != expected_outcomes:
            raise PolicyViolation("run has an invalid immutable Architect outcome artifact count")
        # The real Architect role follows its replay branch because existence
        # was proven above.  This verifies current source/graph/Wiki input, prompt
        # digest, and proposal bytes without invoking any model client.
        if not architect_failed:
            roles.architect(request)
    workflow = factory.bind_workflow_roles(roles).build(maximum_execution_attempts=2)
    return _RunComponents(
        session=session,
        request=request,
        config=config,
        workflow=workflow,
        model_roles=roles,
        seam_tracker=seam_tracker,
        failure=failure,
        in_flight_operation=in_flight_operation,
    )


def _verify_role_invocation_leases(
    session: AgentRunSession,
    request: MigrationRequest,
    registry: AgentRegistry,
    *,
    lifecycle_kind: str,
) -> _InFlightModelOperation | None:
    """Validate durable claims and identify one interrupted model operation.

    The lease's self-digest is validated by ``RoleInvocationLease``.  This
    controller layer additionally binds its public identity to the loaded run,
    current agent definition, generated path, and attempt.  The model workflow
    performs the final exact input-evidence comparison before any replay path
    can proceed.
    """

    try:
        lifecycle_index = cast(
            Mapping[str, object],
            session.store.read_json(f"indexes/{lifecycle_kind}.json"),
        )
        indexed_paths = {
            str(cast(Mapping[str, object], item)["path"])
            for item in cast(tuple[object, ...] | list[object], lifecycle_index["artifacts"])
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation("base lifecycle inventory is malformed") from exc

    unbound: list[str] = []
    attempts: set[int] = set()
    for path in session.model_operation_artifact_paths():
        parts = path.split("/")
        if parts[0] == "model-runs" and (len(parts) != 3 or parts[1] != request.request_id):
            raise PolicyViolation("runtime model-operation artifact belongs to another request")
        match = _MODEL_OPERATION_ATTEMPT_PATH.fullmatch(path)
        if match is None:  # pragma: no cover - run-session inventory is stricter
            raise PolicyViolation("runtime model-operation artifact path is invalid")
        if path not in indexed_paths:
            unbound.append(path)
            attempts.add(int(match.group(1)))
        elif not session.is_model_operation_artifact_promoted(path):
            raise PolicyViolation("indexed model-operation artifact lacks its portable copy")

    pending: list[tuple[Literal["engineer", "validator"], Literal[1, 2]]] = []
    for path in session.role_invocation_lease_paths():
        match = _ROLE_INVOCATION_LEASE_PATH.fullmatch(path)
        if match is None:  # pragma: no cover - run-session inventory checks this first
            raise PolicyViolation("runtime invocation lease path is invalid")
        role = cast(Literal["engineer", "validator"], match.group("role"))
        attempt = cast(Literal[1, 2], int(match.group("attempt")))
        try:
            lease = RoleInvocationLease.model_validate(session.store.read_json(path))
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("runtime invocation lease is malformed") from exc
        definition = registry.get(AgentRole(role))
        expected = (
            role,
            request.request_id,
            artifact_digest(request),
            attempt,
            definition.version,
            definition.definition_digest,
        )
        actual = (
            lease.binding.role,
            lease.binding.request_id,
            lease.binding.request_digest,
            lease.binding.attempt,
            lease.binding.agent_version,
            lease.binding.agent_definition_digest,
        )
        if actual != expected or match.group("request_id") != request.request_id:
            raise PolicyViolation("runtime invocation lease differs from the loaded run")

        outcome_path = f"model-runs/{request.request_id}/{role}-attempt-{attempt}.json"
        if path in unbound and not _workflow_artifact_exists(session, outcome_path):
            pending.append((role, attempt))

    if len(pending) > 1:
        raise PolicyViolation("run contains multiple incomplete provider invocations")
    if not unbound:
        return None
    if len(attempts) != 1:
        raise PolicyViolation("in-flight model-operation artifacts span multiple attempts")
    attempt = cast(Literal[1, 2], attempts.pop())
    if pending and pending[0][1] != attempt:
        raise PolicyViolation("incomplete provider invocation belongs to another attempt")
    operation = _InFlightModelOperation(
        attempt=attempt,
        pending_seam=pending[0][0] if pending else None,
        artifact_paths=tuple(sorted(unbound)),
        portable_freeze_prefix=(),
    )
    return replace(
        operation,
        portable_freeze_prefix=_verify_in_flight_portable_inventory(
            session,
            lifecycle_kind=lifecycle_kind,
            operation=operation,
        ),
    )


def _verify_in_flight_portable_inventory(
    session: AgentRunSession,
    *,
    lifecycle_kind: str,
    operation: _InFlightModelOperation,
) -> tuple[str, ...]:
    """Allow only the exact controller write prefix after the base lifecycle."""

    try:
        lifecycle_index = cast(
            Mapping[str, object],
            session.store.read_json(f"indexes/{lifecycle_kind}.json"),
        )
        indexed_paths = {
            str(cast(Mapping[str, object], item)["path"])
            for item in cast(tuple[object, ...] | list[object], lifecycle_index["artifacts"])
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation("base lifecycle inventory is malformed") from exc
    base_index_path = f"indexes/{lifecycle_kind}.json"
    current_paths = set(_portable_evidence_paths(session.evidence_dir))
    current_paths.discard(base_index_path)
    extras = current_paths - indexed_paths

    promoted = tuple(
        path
        for path in operation.artifact_paths
        if session.is_model_operation_artifact_promoted(path)
    )
    if promoted != operation.artifact_paths[: len(promoted)]:
        raise PolicyViolation("in-flight model-operation promotion is not an exact prefix")
    success_kind = "agent-run-resumed" if operation.attempt == 1 else "agent-run-retried"
    freeze_order = (
        _status_projection_path(success_kind),
        _workflow_projection_path(success_kind),
        _checkpoint_projection_path(success_kind),
        f"indexes/{success_kind}.json",
    )
    remaining = extras - set(promoted)
    decision_paths = tuple(
        sorted(
            path
            for path in remaining
            if path.startswith("control/decision-request-") and path.endswith(".json")
        )
    )
    if len(decision_paths) > 1:
        raise PolicyViolation("in-flight operation contains multiple new decisions")
    remaining -= set(decision_paths)
    valid_prefixes = {frozenset(freeze_order[:length]) for length in range(len(freeze_order) + 1)}
    if frozenset(remaining) not in valid_prefixes:
        raise PolicyViolation("in-flight operation contains an unexpected portable artifact")
    if remaining and promoted != operation.artifact_paths:
        raise PolicyViolation("terminal lifecycle writes require complete transaction promotion")
    return (*decision_paths, *(path for path in freeze_order if path in remaining))


def _concurrent_role_invocation_seam(
    session: AgentRunSession,
    request: MigrationRequest,
    *,
    attempt: int,
) -> Literal["engineer", "validator"] | None:
    """Identify a lease won after this caller loaded the base lifecycle."""

    pending: list[Literal["engineer", "validator"]] = []
    for role in cast(tuple[Literal["engineer", "validator"], ...], ("engineer", "validator")):
        lease_path = (
            f"model-runs/{request.request_id}/{role}-invocation-lease-attempt-{attempt}.json"
        )
        outcome_path = f"model-runs/{request.request_id}/{role}-attempt-{attempt}.json"
        if lease_path in session.role_invocation_lease_paths() and not _workflow_artifact_exists(
            session, outcome_path
        ):
            pending.append(role)
    if len(pending) > 1:
        raise PolicyViolation("agent run has multiple concurrent provider invocations")
    return pending[0] if pending else None


def _verify_in_flight_workflow_projection(
    components: _RunComponents,
    lifecycle: _VerifiedOperationLifecycle,
    operation: _InFlightModelOperation,
    *,
    actual_status: AgentRunStatus,
    actual_workflow_status: AgentRunStatus,
    actual_checkpoint: AgentRunCheckpointProjection,
) -> bool:
    """Validate the only checkpoint advance allowed after an authorized crash.

    Exact role input binding is rechecked by ``ModelAgentWorkflowRoles`` before
    it can replay an artifact or encounter an existing provider lease.  This
    boundary proves that the durable LangGraph checkpoint is on that same
    authorized role and attempt; it never relaxes request, manifest, approval,
    task, or typed correction evidence.
    """

    expected_lifecycle = (
        AGENT_RUN_MANIFEST_AUTHORIZED_KIND
        if operation.attempt == 1
        else AGENT_RUN_CORRECTION_AUTHORIZED_KIND
    )
    if lifecycle.kind != expected_lifecycle or lifecycle.failure is not None:
        raise PolicyViolation("in-flight model operation has the wrong base lifecycle")
    if actual_status != actual_workflow_status:
        raise PolicyViolation("in-flight workflow cannot contain a surfaced failure")

    snapshot = components.workflow.snapshot(thread_id=components.session.context.thread_id)
    pending_nodes = tuple(str(node) for node in snapshot.next)
    terminal = not pending_nodes
    if not terminal and (
        len(pending_nodes) != 1 or pending_nodes[0] not in {"engineer", "validator"}
    ):
        raise PolicyViolation("in-flight workflow has an invalid pending role")
    if actual_status.execution_attempt != operation.attempt:
        raise PolicyViolation("in-flight workflow belongs to another attempt")
    node: Literal["engineer", "validator"] | None = None
    if terminal:
        if operation.pending_seam is not None:
            raise PolicyViolation("terminal in-flight workflow has an incomplete provider lease")
        if (
            actual_status.status not in {"completed", "decision_required"}
            or actual_status.pending_nodes
            or actual_status.task_failed
            or actual_status.interrupt is not None
            or tuple(snapshot.tasks)
        ):
            raise PolicyViolation("terminal in-flight workflow status is invalid")
    else:
        node = cast(Literal["engineer", "validator"], pending_nodes[0])
        if operation.pending_seam is not None and operation.pending_seam != node:
            raise PolicyViolation("provider invocation lease differs from the pending role")
        expected_status = "implementing" if node == "engineer" else "validating"
        if (
            actual_status.status != expected_status
            or actual_status.pending_nodes != (node,)
            or actual_status.task_failed != operation.controller_failed_before_provider
            or actual_status.interrupt is not None
        ):
            raise PolicyViolation("in-flight workflow status is invalid")

    expected_binding = (
        lifecycle.status.run_id,
        lifecycle.status.thread_id,
        lifecycle.status.request_id,
        lifecycle.status.request_digest,
        lifecycle.status.platform,
        lifecycle.status.provider_id,
        lifecycle.status.model_id,
        lifecycle.status.manifest_id,
        lifecycle.status.manifest_digest,
    )
    actual_binding = (
        actual_status.run_id,
        actual_status.thread_id,
        actual_status.request_id,
        actual_status.request_digest,
        actual_status.platform,
        actual_status.provider_id,
        actual_status.model_id,
        actual_status.manifest_id,
        actual_status.manifest_digest,
    )
    if actual_binding != expected_binding:
        raise PolicyViolation("in-flight workflow binding differs from its authorization")
    expected_next = () if terminal else (cast(str, node),)
    if actual_checkpoint.request != components.request or actual_checkpoint.next != expected_next:
        raise PolicyViolation("in-flight checkpoint request or route is invalid")
    if terminal:
        if actual_checkpoint.tasks:
            raise PolicyViolation("terminal in-flight checkpoint cannot contain tasks")
    else:
        if len(actual_checkpoint.tasks) != 1:
            raise PolicyViolation("in-flight checkpoint must contain one role task")
        task = actual_checkpoint.tasks[0]
        null_digest = _checkpoint_value_digest(None)
        if (
            task.name != node
            or task.interrupts
            or task.state_digest != null_digest
            or task.result_digest != null_digest
        ):
            raise PolicyViolation("in-flight checkpoint task is invalid")
        if operation.controller_failed_before_provider is (task.error_digest == null_digest):
            raise PolicyViolation("in-flight checkpoint task failure evidence is invalid")

    values = cast(Mapping[str, object], snapshot.values)
    try:
        checkpoint_request = MigrationRequest.model_validate(values["request"])
        manifest = MigrationManifest.model_validate(values["manifest"])
        manifest_approval = ManifestApproval.model_validate(values["approval"])
        decision = DecisionRequest.model_validate(values["decision_request"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation("in-flight checkpoint lacks typed approved inputs") from exc
    if checkpoint_request != components.request:
        raise PolicyViolation("in-flight checkpoint request differs from the run")
    validate_manifest_for_request(manifest, components.request)
    manifest_digest_value = artifact_digest(manifest)
    if (
        values.get("manifest_digest") != manifest_digest_value
        or manifest_digest_value != actual_status.manifest_digest
    ):
        raise PolicyViolation("in-flight checkpoint manifest binding is invalid")
    try:
        persisted_manifest_approval = ManifestApproval.model_validate(
            components.session.store.read_json(AGENT_RUN_MANIFEST_APPROVAL_PATH)
        )
        expected_manifest_decision = manifest_decision_request(
            components.request,
            manifest,
            manifest_digest_value,
        )
        persisted_manifest_decision = DecisionRequest.model_validate(
            components.session.store.read_json(
                _decision_request_path(expected_manifest_decision.decision_id)
            )
        )
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation("in-flight checkpoint lacks anchored manifest authority") from exc
    if (
        manifest_approval != persisted_manifest_approval
        or persisted_manifest_decision != expected_manifest_decision
        or values.get("approval_outcome") != "approve"
    ):
        raise PolicyViolation("in-flight checkpoint manifest authority differs")
    if decision != expected_manifest_decision and not (
        terminal and actual_status.status == "decision_required"
    ):
        raise PolicyViolation("in-flight checkpoint decision differs from manifest authority")

    change_set: ChangeSet | None = None
    if values.get("change_set") is not None:
        try:
            change_set = ChangeSet.model_validate(values["change_set"])
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("in-flight checkpoint change set is malformed") from exc
        validate_change_set(change_set, manifest)
    if node == "validator" and change_set is None:
        raise PolicyViolation("Validator in-flight checkpoint lacks an Engineer change set")

    raw_attempt = values.get("execution_attempt", 1)
    if raw_attempt != operation.attempt:
        raise PolicyViolation("in-flight checkpoint execution attempt differs")
    if operation.attempt == 1:
        if any(name in values for name in ("correction_approval", "correction_attempt_evidence")):
            raise PolicyViolation("attempt one cannot contain correction authority")
        if terminal:
            _verify_terminal_in_flight_workflow(
                components,
                operation,
                actual_status=actual_status,
                actual_checkpoint=actual_checkpoint,
                values=values,
                manifest=manifest,
                change_set=change_set,
                correction_evidence=None,
            )
        return terminal

    try:
        correction_approval = CorrectionApproval.model_validate(values["correction_approval"])
        correction_evidence = CorrectionAttemptEvidence.model_validate(
            values["correction_attempt_evidence"]
        )
        persisted_correction_approval = CorrectionApproval.model_validate(
            components.session.store.read_json(AGENT_RUN_CORRECTION_APPROVAL_PATH)
        )
    except (KeyError, FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation("attempt-two checkpoint lacks typed correction authority") from exc
    validate_correction_attempt_evidence(
        correction_evidence,
        components.request,
        manifest,
    )
    if correction_approval != persisted_correction_approval:
        raise PolicyViolation("attempt-two checkpoint correction evidence differs")
    if terminal:
        _verify_terminal_in_flight_workflow(
            components,
            operation,
            actual_status=actual_status,
            actual_checkpoint=actual_checkpoint,
            values=values,
            manifest=manifest,
            change_set=change_set,
            correction_evidence=correction_evidence,
        )
        return True

    try:
        correction_request = CorrectionRequest.model_validate(values["correction_request"])
        prior_report = ValidationReport.model_validate(values["validation_report"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation("attempt-two checkpoint lacks prior correction evidence") from exc
    validate_report(prior_report, manifest, correction_evidence.prior_change_set)
    if (
        correction_evidence.correction_request != correction_request
        or correction_evidence.prior_validation_report != prior_report
        or (node == "engineer" and correction_evidence.prior_change_set != change_set)
    ):
        raise PolicyViolation("attempt-two checkpoint correction evidence differs")
    return False


def _verify_terminal_in_flight_workflow(
    components: _RunComponents,
    operation: _InFlightModelOperation,
    *,
    actual_status: AgentRunStatus,
    actual_checkpoint: AgentRunCheckpointProjection,
    values: Mapping[str, object],
    manifest: MigrationManifest,
    change_set: ChangeSet | None,
    correction_evidence: CorrectionAttemptEvidence | None,
) -> None:
    """Prove a committed terminal checkpoint can finish its interrupted freeze."""

    if values.get("implementation_intervention") is not None:
        _verify_terminal_engineer_intervention(
            components,
            operation,
            actual_status=actual_status,
            values=values,
            manifest=manifest,
            correction_evidence=correction_evidence,
        )
        _verify_partial_terminal_freeze(
            components,
            operation,
            actual_status=actual_status,
            actual_checkpoint=actual_checkpoint,
        )
        return
    if change_set is None:
        raise PolicyViolation("terminal in-flight checkpoint lacks an Engineer change set")
    try:
        report = ValidationReport.model_validate(values["validation_report"])
        correction = CorrectionRequest.model_validate(values["correction_request"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation(
            "terminal in-flight checkpoint lacks typed validation evidence"
        ) from exc
    validate_report(report, manifest, change_set)
    if report.attempt != operation.attempt:
        raise PolicyViolation("terminal validation report belongs to another attempt")
    expected_correction = CorrectionController(maximum_attempts=2).evaluate(
        manifest,
        change_set,
        report,
    )
    if (
        correction != expected_correction
        or values.get("terminal_disposition") != report.disposition
        or values.get("terminal_reason") != expected_correction.reason
        or actual_status.terminal_disposition != report.disposition.value
    ):
        raise PolicyViolation("terminal in-flight disposition or correction is invalid")

    model_root = f"model-runs/{components.request.request_id}"
    expected_paths = {
        f"{model_root}/engineer-invocation-lease-attempt-{operation.attempt}.json",
        f"{model_root}/engineer-attempt-{operation.attempt}.json",
        f"{model_root}/report-attempt-{operation.attempt}.json",
        f"{model_root}/validator-invocation-lease-attempt-{operation.attempt}.json",
        f"{model_root}/validator-attempt-{operation.attempt}.json",
    }
    try:
        validator_assessment = ValidatorAssessment.model_validate(
            components.session.store.read_json(
                f"{model_root}/validator-attempt-{operation.attempt}.json"
            )
        )
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation("terminal Validator advisory evidence is malformed") from exc
    if (
        validator_assessment.unavailable_receipt is not None
        and not validator_assessment.unavailable_receipt.attempted
    ):
        expected_paths.remove(
            f"{model_root}/validator-invocation-lease-attempt-{operation.attempt}.json"
        )
    if operation.attempt == 2:
        expected_paths.add(f"{model_root}/engineer-correction-attempt-2.json")
        expected_paths.add(f"{model_root}/correction-wiki-attempt-2.json")
    control_path = _correction_request_path(operation.attempt)
    actual_paths = set(operation.artifact_paths)
    if actual_paths not in (expected_paths, expected_paths | {control_path}):
        raise PolicyViolation("terminal in-flight model-operation inventory is invalid")
    if control_path in actual_paths:
        try:
            persisted_correction = CorrectionRequest.model_validate(
                components.session.store.read_json(control_path)
            )
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise PolicyViolation("terminal correction control evidence is malformed") from exc
        if persisted_correction != expected_correction:
            raise PolicyViolation("terminal correction control evidence differs from checkpoint")
    if any(
        path.startswith("control/decision-request-") for path in operation.portable_freeze_prefix
    ):
        raise PolicyViolation("validation terminal cannot add decision-request evidence")

    try:
        replayed_change = components.model_roles.engineer(
            components.request,
            manifest,
            attempt=operation.attempt,
            correction=correction_evidence,
        )
        if replayed_change != change_set:
            raise PolicyViolation("terminal Engineer replay differs from checkpoint")
        replayed_report = components.model_roles.validator(
            components.request,
            manifest,
            change_set,
            attempt=operation.attempt,
            correction=correction_evidence,
        )
    except (ModelWorkflowIntegrationError, TypeError, ValueError) as exc:
        raise PolicyViolation(
            "terminal model-operation evidence cannot be replayed safely"
        ) from exc
    if replayed_report != report:
        raise PolicyViolation("terminal Validator replay differs from checkpoint")

    _verify_partial_terminal_freeze(
        components,
        operation,
        actual_status=actual_status,
        actual_checkpoint=actual_checkpoint,
    )


def _verify_terminal_engineer_intervention(
    components: _RunComponents,
    operation: _InFlightModelOperation,
    *,
    actual_status: AgentRunStatus,
    values: Mapping[str, object],
    manifest: MigrationManifest,
    correction_evidence: CorrectionAttemptEvidence | None,
) -> None:
    try:
        intervention = ImplementationIntervention.model_validate(
            values["implementation_intervention"]
        )
        decision = DecisionRequest.model_validate(values["decision_request"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation("terminal Engineer intervention evidence is malformed") from exc
    validate_implementation_intervention_for_manifest(
        intervention,
        components.request,
        manifest,
    )
    intervention_digest = artifact_digest(intervention)
    expected_decision = implementation_intervention_decision_request(
        components.request,
        manifest,
        intervention,
        intervention_digest,
    )
    if (
        values.get("implementation_intervention_digest") != intervention_digest
        or decision != expected_decision
        or actual_status.status != "decision_required"
        or actual_status.terminal_disposition != "decision_required"
        or values.get("terminal_disposition") != ValidationDisposition.DECISION_REQUIRED
        or values.get("terminal_reason") != expected_decision.reason
    ):
        raise PolicyViolation("terminal Engineer intervention projection is invalid")
    decision_path = _decision_request_path(expected_decision.decision_id)
    if decision_path in operation.portable_freeze_prefix:
        try:
            persisted_decision = DecisionRequest.model_validate(
                components.session.store.read_json(decision_path)
            )
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise PolicyViolation("terminal Engineer decision evidence is malformed") from exc
        if persisted_decision != expected_decision:
            raise PolicyViolation("terminal Engineer decision differs from checkpoint")
    elif any(
        path.startswith("control/decision-request-") for path in operation.portable_freeze_prefix
    ):
        raise PolicyViolation("terminal Engineer decision path differs from checkpoint")

    model_root = f"model-runs/{components.request.request_id}"
    expected_paths = {
        f"{model_root}/engineer-invocation-lease-attempt-{operation.attempt}.json",
        f"{model_root}/engineer-attempt-{operation.attempt}.json",
    }
    if operation.attempt == 2:
        expected_paths.add(f"{model_root}/engineer-correction-attempt-2.json")
        expected_paths.add(f"{model_root}/correction-wiki-attempt-2.json")
    if set(operation.artifact_paths) != expected_paths:
        raise PolicyViolation("terminal Engineer intervention inventory is invalid")
    try:
        replayed = components.model_roles.engineer(
            components.request,
            manifest,
            attempt=operation.attempt,
            correction=correction_evidence,
        )
    except (ModelWorkflowIntegrationError, TypeError, ValueError) as exc:
        raise PolicyViolation("terminal Engineer intervention cannot be replayed safely") from exc
    if replayed != intervention:
        raise PolicyViolation("terminal Engineer intervention replay differs from checkpoint")


def _verify_partial_terminal_freeze(
    components: _RunComponents,
    operation: _InFlightModelOperation,
    *,
    actual_status: AgentRunStatus,
    actual_checkpoint: AgentRunCheckpointProjection,
) -> None:
    success_kind = "agent-run-resumed" if operation.attempt == 1 else "agent-run-retried"
    if _status_projection_path(success_kind) in operation.portable_freeze_prefix:
        if (
            _read_status_projection(components.session, success_kind, components.request)
            != actual_status
        ):
            raise PolicyViolation("partial terminal status differs from checkpoint")
    if _workflow_projection_path(success_kind) in operation.portable_freeze_prefix:
        if (
            _read_workflow_projection(components.session, success_kind, components.request)
            != actual_status
        ):
            raise PolicyViolation("partial terminal workflow status differs from checkpoint")
    if _checkpoint_projection_path(success_kind) in operation.portable_freeze_prefix:
        if (
            _read_checkpoint_projection(components.session, success_kind, components.request)
            != actual_checkpoint
        ):
            raise PolicyViolation("partial terminal checkpoint differs from current checkpoint")
    if f"indexes/{success_kind}.json" in operation.portable_freeze_prefix:
        components.session.verify_index(success_kind, exact=True)


def _verify_in_flight_resubmission_authority(
    session: AgentRunSession,
    operation: _InFlightModelOperation,
    presented: ManifestApproval | CorrectionApproval,
) -> None:
    """Require byte-equivalent authority when re-entering an advanced checkpoint."""

    if operation.attempt == 1:
        if not isinstance(presented, ManifestApproval):
            raise PolicyViolation("in-flight attempt one requires manifest approval")
        path = AGENT_RUN_MANIFEST_APPROVAL_PATH
        model_type: type[ManifestApproval | CorrectionApproval] = ManifestApproval
    else:
        if not isinstance(presented, CorrectionApproval):
            raise PolicyViolation("in-flight attempt two requires correction approval")
        path = AGENT_RUN_CORRECTION_APPROVAL_PATH
        model_type = CorrectionApproval
    try:
        persisted = model_type.model_validate(session.store.read_json(path))
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation("in-flight operation lacks anchored authorization") from exc
    if persisted != presented:
        raise PolicyViolation("approval differs from the in-flight authorization")


def _sanitized_models(
    models: AgentRunModelClients,
    seam_tracker: _OperationSeamTracker,
) -> tuple[StructuredModelClient, StructuredModelClient, StructuredModelClient]:
    live_invocation = models.live_invocation
    execution_boundary = models.execution_boundary
    return (
        _SanitizedModelClient(
            models.architect,
            role="architect",
            provider_id=models.provider_id,
            model_id=models.model_id,
            live_invocation=live_invocation,
            execution_boundary=execution_boundary,
            live_approval=models.live_approval,
            seam_tracker=seam_tracker,
        ),
        _SanitizedModelClient(
            models.engineer,
            role="engineer",
            provider_id=models.provider_id,
            model_id=models.model_id,
            live_invocation=live_invocation,
            execution_boundary=execution_boundary,
            live_approval=models.live_approval,
            seam_tracker=seam_tracker,
        ),
        _SanitizedModelClient(
            models.validator,
            role="validator",
            provider_id=models.provider_id,
            model_id=models.model_id,
            live_invocation=live_invocation,
            execution_boundary=execution_boundary,
            live_approval=models.live_approval,
            seam_tracker=seam_tracker,
        ),
    )


def _portable_artifact_exists(session: AgentRunSession, path: str) -> bool:
    try:
        session.store.read_json(path)
    except FileNotFoundError:
        return False
    return True


def _workflow_artifact_exists(session: AgentRunSession, path: str) -> bool:
    try:
        session.store.read_json(path)
    except FileNotFoundError:
        return False
    return True


def _canonical_checkpoint_value(value: object) -> object:
    """Convert one strictly deserialized checkpoint value to canonical JSON."""

    if isinstance(value, BaseModel):
        return _canonical_checkpoint_value(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return _canonical_checkpoint_value(value.value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PolicyViolation("checkpoint mappings require string keys")
            normalized[key] = _canonical_checkpoint_value(item)
        return normalized
    if isinstance(value, (tuple, list)):
        return [_canonical_checkpoint_value(item) for item in value]
    if isinstance(value, BaseException):
        return {
            "exception_type": f"{type(value).__module__}.{type(value).__qualname__}",
            "arguments": [_canonical_checkpoint_value(item) for item in value.args],
        }
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise PolicyViolation(
        "checkpoint contains an unsupported value type: "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def _checkpoint_value_digest(value: object) -> Sha256Digest:
    return artifact_digest(_canonical_checkpoint_value(value))


def _checkpoint_projection(components: _RunComponents) -> AgentRunCheckpointProjection:
    """Project the complete current checkpoint without exposing opaque values."""

    snapshot = components.workflow.snapshot(thread_id=components.session.context.thread_id)
    values = cast(Mapping[str, object], snapshot.values)
    try:
        checkpoint_request = MigrationRequest.model_validate(values["request"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation("checkpoint lacks a canonical migration request") from exc
    if checkpoint_request != components.request:
        raise PolicyViolation("checkpoint request differs from canonical run evidence")

    field_digests = tuple(
        AgentRunCheckpointFieldDigest(
            name=name,
            value_digest=_checkpoint_value_digest(value),
        )
        for name, value in sorted(values.items())
    )
    tasks: list[AgentRunCheckpointTaskProjection] = []
    for task in snapshot.tasks:
        interrupts = tuple(
            AgentRunCheckpointInterruptProjection(
                interrupt_id=str(pending.id),
                value_digest=_checkpoint_value_digest(pending.value),
            )
            for pending in task.interrupts
        )
        tasks.append(
            AgentRunCheckpointTaskProjection(
                task_id=str(task.id),
                name=str(task.name),
                path_digest=_checkpoint_value_digest(task.path),
                error_digest=_checkpoint_value_digest(task.error),
                interrupts=interrupts,
                state_digest=_checkpoint_value_digest(task.state),
                result_digest=_checkpoint_value_digest(task.result),
            )
        )
    projection = AgentRunCheckpointProjection(
        run_id=components.session.context.run_id,
        thread_id=components.session.context.thread_id,
        request=checkpoint_request,
        values=field_digests,
        next=tuple(str(node) for node in snapshot.next),
        tasks=tuple(tasks),
    )
    components.session.validate_portable_evidence(projection)
    return projection


def _freeze_operation_evidence(
    session: AgentRunSession,
    kind: str,
    status: AgentRunStatus,
    workflow_status: AgentRunStatus,
    checkpoint: AgentRunCheckpointProjection,
) -> None:
    """Freeze portable artifacts together with the exact workflow projection."""

    # Authorization snapshots are deliberately pre-execution boundaries.  An
    # exact resubmission after process loss may already have an uncompleted
    # runtime lease; promoting it here would widen the immutable authorization
    # index before the controller can turn that claim into a terminal failure.
    if kind not in {
        AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
        AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
    }:
        session.promote_model_operation_artifacts()
    session.store.write_json(_status_projection_path(kind), status)
    _after_authorization_write(kind, "status")
    session.store.write_json(_workflow_projection_path(kind), workflow_status)
    _after_authorization_write(kind, "workflow_status")
    session.store.write_json(_checkpoint_projection_path(kind), checkpoint)
    _after_authorization_write(kind, "checkpoint")
    session.write_index(kind)
    _after_authorization_write(kind, "index")
    index_payload = session.store.read_json(f"indexes/{kind}.json")
    anchor = AgentRunLifecycleAnchor(
        lifecycle_kind=kind,
        run_id=session.context.run_id,
        thread_id=session.context.thread_id,
        request_digest=session.context.request_digest,
        lifecycle_index_digest=artifact_digest(index_payload),
    )
    session.bind_runtime_anchor(kind, anchor)
    _after_authorization_write(kind, "anchor")
    session.verify_index(kind, exact=True)
    session.verify_runtime_anchor(kind, anchor)


def _status_projection_path(kind: str) -> str:
    return f"{AGENT_RUN_STATUS_ROOT}/{kind}.json"


def _workflow_projection_path(kind: str) -> str:
    return f"{AGENT_RUN_STATUS_ROOT}/{kind}-workflow.json"


def _checkpoint_projection_path(kind: str) -> str:
    return f"{AGENT_RUN_STATUS_ROOT}/{kind}-checkpoint.json"


def _verify_latest_operation_evidence(
    session: AgentRunSession,
    request: MigrationRequest,
    *,
    pending_authorization: ManifestApproval | CorrectionApproval | None,
) -> _VerifiedOperationLifecycle:
    if session.has_runtime_anchor(AGENT_RUN_FAILURE_KIND):
        _verify_operation_lifecycle(session, AGENT_RUN_FAILURE_KIND, exact=True)
        try:
            failure = AgentRunFailure.model_validate(
                session.store.read_json(AGENT_RUN_FAILURE_PATH)
            )
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("agent-run failure evidence is malformed") from exc
        expected = (
            session.context.run_id,
            session.context.thread_id,
            request.request_id,
            session.context.request_digest,
            artifact_digest(session.context),
            session.context.source_revision,
            session.context.agent_definition_digests,
        )
        actual = (
            failure.run_id,
            failure.thread_id,
            failure.request_id,
            failure.request_digest,
            failure.session_context_digest,
            failure.source_revision,
            failure.agent_definition_digests,
        )
        if actual != expected:
            raise PolicyViolation("agent-run failure evidence differs from the run session")
        status = _read_status_projection(session, AGENT_RUN_FAILURE_KIND, request)
        workflow_status = _read_workflow_projection(
            session,
            AGENT_RUN_FAILURE_KIND,
            request,
        )
        if status.failure != failure:
            raise PolicyViolation("failed status projection differs from failure evidence")
        checkpoint = _read_checkpoint_projection(
            session,
            AGENT_RUN_FAILURE_KIND,
            request,
        )
        _verify_completed_authorizations_for_lifecycle(
            session,
            request,
            manifest_required=failure.operation in {"resume", "retry"},
            correction_required=failure.operation == "retry",
        )
        return _VerifiedOperationLifecycle(
            kind=AGENT_RUN_FAILURE_KIND,
            status=status,
            workflow_status=workflow_status,
            checkpoint=checkpoint,
            failure=failure,
        )

    latest: str | None = None
    for kind in (
        "agent-run-retried",
        AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
        "agent-run-resumed",
        AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
        "agent-run-planned",
    ):
        if session.has_runtime_anchor(kind):
            latest = kind
            break
    if latest is None:
        raise PolicyViolation("run has no completed agent operation lifecycle evidence")
    # A completed run may later acquire one governed final-review lifecycle.
    # In that case the original operation inventory remains immutable but is
    # intentionally no longer the complete artifact set.  Verify the newer
    # lifecycle exactly before relaxing only the older inventory comparison.
    has_final_review = _verify_subsequent_final_review(session)
    partial = _partial_authorization(session)
    if partial is not None and has_final_review:
        raise PolicyViolation("partial authorization cannot follow final review")
    runtime_model_paths = session.model_operation_artifact_paths()
    try:
        latest_index = cast(
            Mapping[str, object],
            session.store.read_json(f"indexes/{latest}.json"),
        )
        latest_indexed_paths = {
            str(cast(Mapping[str, object], item)["path"])
            for item in cast(tuple[object, ...] | list[object], latest_index["artifacts"])
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation("latest lifecycle inventory is malformed") from exc
    has_unbound_runtime_transaction = any(
        path not in latest_indexed_paths for path in runtime_model_paths
    )
    _verify_operation_lifecycle(
        session,
        latest,
        exact=not has_final_review and partial is None and not has_unbound_runtime_transaction,
    )
    status = _read_status_projection(session, latest, request)
    workflow_status = _read_workflow_projection(session, latest, request)
    checkpoint = _read_checkpoint_projection(session, latest, request)
    _verify_completed_authorizations_for_lifecycle(
        session,
        request,
        manifest_required=latest
        in {
            AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
            "agent-run-resumed",
            AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
            "agent-run-retried",
        },
        correction_required=latest in {AGENT_RUN_CORRECTION_AUTHORIZED_KIND, "agent-run-retried"},
    )
    if partial is not None:
        _verify_partial_authorization(
            session,
            request,
            base_kind=latest,
            base_status=status,
            base_workflow_status=workflow_status,
            base_checkpoint=checkpoint,
            partial=partial,
            pending_authorization=pending_authorization,
        )
    return _VerifiedOperationLifecycle(
        kind=latest,
        status=status,
        workflow_status=workflow_status,
        checkpoint=checkpoint,
    )


def _partial_authorization(
    session: AgentRunSession,
) -> _PartialAuthorization | None:
    """Return one intent-anchored but incomplete authorization transaction."""

    manifest_intent = session.has_runtime_anchor(AGENT_RUN_MANIFEST_AUTHORIZATION_INTENT_KIND)
    correction_intent = session.has_runtime_anchor(AGENT_RUN_CORRECTION_AUTHORIZATION_INTENT_KIND)
    manifest_approval = _portable_artifact_exists(session, AGENT_RUN_MANIFEST_APPROVAL_PATH)
    correction_approval = _portable_artifact_exists(session, AGENT_RUN_CORRECTION_APPROVAL_PATH)
    if manifest_approval and not manifest_intent:
        raise PolicyViolation("manifest authorization lacks an immutable runtime intent")
    if correction_approval and not correction_intent:
        raise PolicyViolation("correction authorization lacks an immutable runtime intent")
    manifest_partial = manifest_intent and not session.has_runtime_anchor(
        AGENT_RUN_MANIFEST_AUTHORIZED_KIND
    )
    correction_partial = correction_intent and not session.has_runtime_anchor(
        AGENT_RUN_CORRECTION_AUTHORIZED_KIND
    )
    if manifest_partial and correction_partial:
        raise PolicyViolation("run contains multiple partial authorization transactions")
    if manifest_partial:
        approval: ManifestApproval | None = None
        if manifest_approval:
            try:
                approval = ManifestApproval.model_validate(
                    session.store.read_json(AGENT_RUN_MANIFEST_APPROVAL_PATH)
                )
            except (TypeError, ValueError) as exc:
                raise PolicyViolation("partial manifest authorization is malformed") from exc
            session.validate_portable_evidence(approval)
            _verify_authorization_intent(
                session,
                operation="resume",
                kind=AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
                control_path=AGENT_RUN_MANIFEST_APPROVAL_PATH,
                approval=approval,
            )
        return _PartialAuthorization(
            kind=AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
            approval=approval,
        )
    if correction_partial:
        correction: CorrectionApproval | None = None
        if correction_approval:
            try:
                correction = CorrectionApproval.model_validate(
                    session.store.read_json(AGENT_RUN_CORRECTION_APPROVAL_PATH)
                )
            except (TypeError, ValueError) as exc:
                raise PolicyViolation("partial correction authorization is malformed") from exc
            session.validate_portable_evidence(correction)
            _verify_authorization_intent(
                session,
                operation="retry",
                kind=AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
                control_path=AGENT_RUN_CORRECTION_APPROVAL_PATH,
                approval=correction,
            )
        return _PartialAuthorization(
            kind=AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
            approval=correction,
        )
    return None


def _verify_partial_authorization(
    session: AgentRunSession,
    request: MigrationRequest,
    *,
    base_kind: str,
    base_status: AgentRunStatus,
    base_workflow_status: AgentRunStatus,
    base_checkpoint: AgentRunCheckpointProjection,
    partial: _PartialAuthorization,
    pending_authorization: ManifestApproval | CorrectionApproval | None,
) -> None:
    """Verify every possible prefix of the immutable authorization writes."""

    partial_kind = partial.kind
    approval = partial.approval
    if partial_kind == AGENT_RUN_MANIFEST_AUTHORIZED_KIND:
        if base_kind != "agent-run-planned":
            raise PolicyViolation("partial manifest authorization has the wrong base lifecycle")
        if approval is not None and not isinstance(approval, ManifestApproval):
            raise PolicyViolation("partial manifest authorization has the wrong evidence type")
        approval_path = AGENT_RUN_MANIFEST_APPROVAL_PATH
        operation: Literal["resume", "retry"] = "resume"
    elif partial_kind == AGENT_RUN_CORRECTION_AUTHORIZED_KIND:
        if base_kind != "agent-run-resumed":
            raise PolicyViolation("partial correction authorization has the wrong base lifecycle")
        if approval is not None and not isinstance(approval, CorrectionApproval):
            raise PolicyViolation("partial correction authorization has the wrong evidence type")
        approval_path = AGENT_RUN_CORRECTION_APPROVAL_PATH
        operation = "retry"
    else:  # pragma: no cover - internal caller supplies only two constants
        raise AssertionError(f"unknown partial authorization kind: {partial_kind}")
    if (
        approval is not None
        and pending_authorization is not None
        and pending_authorization != approval
    ):
        raise PolicyViolation("approval differs from the partial authorization transaction")
    presented = approval if approval is not None else pending_authorization
    if presented is not None:
        if operation == "resume" and not isinstance(presented, ManifestApproval):
            raise PolicyViolation("manifest authorization has the wrong evidence type")
        if operation == "retry" and not isinstance(presented, CorrectionApproval):
            raise PolicyViolation("correction authorization has the wrong evidence type")
        _verify_authorization_intent(
            session,
            operation=operation,
            kind=partial_kind,
            control_path=approval_path,
            approval=presented,
        )

    base_index_path = f"indexes/{base_kind}.json"
    try:
        base_index = session.store.read_json(base_index_path)
        base_paths = {
            str(item["path"]) for item in cast(list[dict[str, Any]], base_index["artifacts"])
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation("base lifecycle inventory is malformed") from exc
    current_paths = set(_portable_evidence_paths(session.evidence_dir))
    current_paths.discard(base_index_path)
    extra_paths = current_paths - base_paths
    write_order = (
        approval_path,
        _status_projection_path(partial_kind),
        _workflow_projection_path(partial_kind),
        _checkpoint_projection_path(partial_kind),
        f"indexes/{partial_kind}.json",
    )
    valid_prefixes = {frozenset(write_order[:length]) for length in range(0, 6)}
    if frozenset(extra_paths) not in valid_prefixes:
        raise PolicyViolation("partial authorization contains an unexpected artifact inventory")

    if _status_projection_path(partial_kind) in extra_paths:
        if _read_status_projection(session, partial_kind, request) != base_status:
            raise PolicyViolation("partial authorization status differs from its base lifecycle")
    if _workflow_projection_path(partial_kind) in extra_paths:
        if _read_workflow_projection(session, partial_kind, request) != base_workflow_status:
            raise PolicyViolation(
                "partial authorization workflow status differs from its base lifecycle"
            )
    if _checkpoint_projection_path(partial_kind) in extra_paths:
        if _read_checkpoint_projection(session, partial_kind, request) != base_checkpoint:
            raise PolicyViolation(
                "partial authorization checkpoint differs from its base lifecycle"
            )
    partial_index_path = f"indexes/{partial_kind}.json"
    if partial_index_path in extra_paths:
        session.verify_index(partial_kind, exact=True)


def _verify_completed_authorizations_for_lifecycle(
    session: AgentRunSession,
    request: MigrationRequest,
    *,
    manifest_required: bool,
    correction_required: bool,
) -> None:
    """Require each consumed human decision to match its prepublication intent."""

    if not manifest_required and session.has_runtime_anchor(AGENT_RUN_MANIFEST_AUTHORIZED_KIND):
        raise PolicyViolation("manifest authorization is inconsistent with the lifecycle")
    if not correction_required and session.has_runtime_anchor(AGENT_RUN_CORRECTION_AUTHORIZED_KIND):
        raise PolicyViolation("correction authorization is inconsistent with the lifecycle")
    if manifest_required:
        if not session.has_runtime_anchor(AGENT_RUN_MANIFEST_AUTHORIZED_KIND):
            raise PolicyViolation("lifecycle lacks completed manifest authorization")
        try:
            manifest = ManifestApproval.model_validate(
                session.store.read_json(AGENT_RUN_MANIFEST_APPROVAL_PATH)
            )
        except FileNotFoundError as exc:
            raise PolicyViolation(
                "completed manifest authorization lacks approval evidence"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("completed manifest authorization is malformed") from exc
        if manifest.request_id != request.request_id:
            raise PolicyViolation("manifest authorization belongs to another request")
        _verify_authorization_intent(
            session,
            operation="resume",
            kind=AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
            control_path=AGENT_RUN_MANIFEST_APPROVAL_PATH,
            approval=manifest,
        )
    if correction_required:
        if not session.has_runtime_anchor(AGENT_RUN_CORRECTION_AUTHORIZED_KIND):
            raise PolicyViolation("lifecycle lacks completed correction authorization")
        try:
            correction = CorrectionApproval.model_validate(
                session.store.read_json(AGENT_RUN_CORRECTION_APPROVAL_PATH)
            )
        except FileNotFoundError as exc:
            raise PolicyViolation(
                "completed correction authorization lacks approval evidence"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("completed correction authorization is malformed") from exc
        if correction.request_id != request.request_id:
            raise PolicyViolation("correction authorization belongs to another request")
        _verify_authorization_intent(
            session,
            operation="retry",
            kind=AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
            control_path=AGENT_RUN_CORRECTION_APPROVAL_PATH,
            approval=correction,
        )


def _portable_evidence_paths(root: Path) -> tuple[str, ...]:
    """Safely enumerate the portable JSON inventory without following links."""

    paths: list[str] = []

    def scan(directory: Path) -> None:
        with os.scandir(directory) as children:
            ordered = sorted(children, key=lambda child: child.name)
        for child in ordered:
            metadata = child.stat(follow_symlinks=False)
            relative = Path(child.path).relative_to(root).as_posix()
            if stat.S_ISLNK(metadata.st_mode):
                raise PolicyViolation(f"portable evidence cannot contain symlinks: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                scan(Path(child.path))
                continue
            if not stat.S_ISREG(metadata.st_mode) or not relative.endswith(".json"):
                raise PolicyViolation(
                    f"portable evidence contains an unsupported artifact: {relative}"
                )
            paths.append(validate_relative_path(relative))

    scan(root)
    return tuple(sorted(paths))


def _verify_operation_lifecycle(
    session: AgentRunSession,
    kind: str,
    *,
    exact: bool,
) -> None:
    session.verify_index(kind, exact=exact)
    index_payload = session.store.read_json(f"indexes/{kind}.json")
    session.verify_runtime_anchor(
        kind,
        AgentRunLifecycleAnchor(
            lifecycle_kind=kind,
            run_id=session.context.run_id,
            thread_id=session.context.thread_id,
            request_digest=session.context.request_digest,
            lifecycle_index_digest=artifact_digest(index_payload),
        ),
    )


def _read_status_projection(
    session: AgentRunSession,
    kind: str,
    request: MigrationRequest,
) -> AgentRunStatus:
    try:
        status = AgentRunStatus.model_validate(
            session.store.read_json(_status_projection_path(kind))
        )
    except FileNotFoundError as exc:
        raise PolicyViolation("agent-run lifecycle lacks a status projection") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("agent-run status projection is malformed") from exc
    _validate_status_projection_binding(session, request, status)
    return status


def _validate_status_projection_binding(
    session: AgentRunSession,
    request: MigrationRequest,
    status: AgentRunStatus,
) -> None:
    expected = (
        session.context.run_id,
        session.context.thread_id,
        request.request_id,
        session.context.request_digest,
        request.platform,
        session.context.provider_id,
        session.context.model_id,
    )
    actual = (
        status.run_id,
        status.thread_id,
        status.request_id,
        status.request_digest,
        status.platform,
        status.provider_id,
        status.model_id,
    )
    if actual != expected:
        raise PolicyViolation("agent-run status projection differs from the run session")


def _read_workflow_projection(
    session: AgentRunSession,
    kind: str,
    request: MigrationRequest,
) -> AgentRunStatus:
    try:
        status = AgentRunStatus.model_validate(
            session.store.read_json(_workflow_projection_path(kind))
        )
    except FileNotFoundError as exc:
        raise PolicyViolation("agent-run lifecycle lacks a workflow projection") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("agent-run workflow projection is malformed") from exc
    _validate_status_projection_binding(session, request, status)
    return status


def _read_checkpoint_projection(
    session: AgentRunSession,
    kind: str,
    request: MigrationRequest,
) -> AgentRunCheckpointProjection:
    try:
        checkpoint = AgentRunCheckpointProjection.model_validate(
            session.store.read_json(_checkpoint_projection_path(kind))
        )
    except FileNotFoundError as exc:
        raise PolicyViolation("agent-run lifecycle lacks a checkpoint projection") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("agent-run checkpoint projection is malformed") from exc
    expected = (
        session.context.run_id,
        session.context.thread_id,
        request,
    )
    actual = (
        checkpoint.run_id,
        checkpoint.thread_id,
        checkpoint.request,
    )
    if actual != expected:
        raise PolicyViolation("agent-run checkpoint projection differs from the run session")
    return checkpoint


def _verify_subsequent_final_review(session: AgentRunSession) -> bool:
    """Verify a later final-review checkpoint without invoking any provider."""

    from legacy_migration_agent.application.final_review import (  # Local import keeps the lifecycle modules acyclic.
        FINAL_REVIEW_DECIDED_KIND,
        FINAL_REVIEW_REQUESTED_KIND,
        get_final_review_status,
    )

    has_requested = session.has_runtime_anchor(FINAL_REVIEW_REQUESTED_KIND)
    has_decided = session.has_runtime_anchor(FINAL_REVIEW_DECIDED_KIND)
    if not has_requested and not has_decided:
        return False
    if has_decided and not has_requested:
        raise PolicyViolation("final-review decision exists without its request lifecycle")
    get_final_review_status(session)
    return True


def _pending_manifest_interrupt(
    snapshot: Any,
) -> tuple[DecisionRequest, str, Sha256Digest] | None:
    """Return and internally cross-check the one exact manifest interrupt."""

    candidates: list[Mapping[str, Any]] = []
    for task in snapshot.tasks:
        for pending in task.interrupts:
            value = pending.value
            if not isinstance(value, Mapping) or value.get("kind") != "manifest_approval":
                raise PolicyViolation("workflow contains an unknown pending interrupt")
            candidates.append(value)
    if not candidates:
        return None
    if len(candidates) != 1:
        raise PolicyViolation("workflow contains multiple pending manifest interrupts")

    values = cast(dict[str, Any], snapshot.values)
    try:
        decision = DecisionRequest.model_validate(candidates[0]["decision"])
        request = MigrationRequest.model_validate(values["request"])
        manifest = MigrationManifest.model_validate(values["manifest"])
        manifest_digest = cast(Sha256Digest, values["manifest_digest"])
        interrupt_manifest_id = str(candidates[0]["manifest_id"])
        interrupt_manifest_digest = str(candidates[0]["manifest_digest"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation("pending manifest interrupt is malformed") from exc
    if artifact_digest(manifest) != manifest_digest:
        raise PolicyViolation("checkpointed manifest digest does not match manifest")
    expected = (
        request.request_id,
        manifest.manifest_id,
        manifest_digest,
        ApprovalAction.APPROVE_MANIFEST,
    )
    actual = (
        decision.request_id,
        interrupt_manifest_id,
        interrupt_manifest_digest,
        decision.requested_action,
    )
    if actual != expected:
        raise PolicyViolation("pending manifest interrupt differs from checkpoint state")
    return decision, manifest.manifest_id, manifest_digest


def _status_from_components(
    components: _RunComponents,
    *,
    surface_failure: bool = True,
) -> AgentRunStatus:
    snapshot = components.workflow.snapshot(thread_id=components.session.context.thread_id)
    values = cast(dict[str, Any], snapshot.values)
    manifest = values.get("manifest")
    manifest_id = getattr(manifest, "manifest_id", None)
    manifest_digest = values.get("manifest_digest")
    interrupt_summary: AgentRunInterruptSummary | None = None
    pending = _pending_manifest_interrupt(snapshot)
    if pending is not None:
        decision, pending_manifest_id, pending_manifest_digest = pending
        interrupt_summary = AgentRunInterruptSummary(
            decision_id=decision.decision_id,
            request_id=decision.request_id,
            manifest_id=pending_manifest_id,
            manifest_digest=pending_manifest_digest,
            requested_action=decision.requested_action,
            options=cast(
                tuple[Literal["approve", "reject", "modify"], ...],
                decision.options,
            ),
        )
    correction_summary: AgentRunCorrectionSummary | None = None
    raw_correction = values.get("correction_request")
    if raw_correction is not None:
        correction = CorrectionRequest.model_validate(raw_correction)
        correction_summary = AgentRunCorrectionSummary(
            correction_id=correction.correction_id,
            request_id=correction.request_id,
            manifest_id=correction.manifest_id,
            manifest_digest=correction.manifest_digest,
            report_id=correction.report_id,
            report_digest=correction.report_digest,
            change_set_digest=correction.change_set_digest,
            base_revision=correction.base_revision,
            completed_attempt=correction.completed_attempt,
            authorized_attempt=correction.next_attempt,
            action=correction.action,
        )
    failure = components.failure
    failed_terminal = failure is not None and surface_failure
    status = AgentRunStatus(
        run_id=components.session.context.run_id,
        thread_id=components.session.context.thread_id,
        request_id=components.request.request_id,
        request_digest=components.session.context.request_digest,
        platform=components.request.platform,
        provider_id=components.session.context.provider_id,
        model_id=components.session.context.model_id,
        status="failed" if failed_terminal else str(values.get("status", "unknown")),
        terminal_disposition=(
            "controlled_failure"
            if failed_terminal
            else (
                str(values["terminal_disposition"])
                if values.get("terminal_disposition") is not None
                else None
            )
        ),
        execution_attempt=(
            failure.attempt
            if failure is not None and surface_failure
            else int(values.get("execution_attempt", 1))
        ),
        pending_nodes=() if failed_terminal else tuple(snapshot.next),
        task_failed=(
            True
            if failed_terminal
            else any(getattr(task, "error", None) is not None for task in snapshot.tasks)
        ),
        manifest_id=manifest_id,
        manifest_digest=manifest_digest,
        interrupt=None if failed_terminal else interrupt_summary,
        correction=None if failed_terminal else correction_summary,
        failure=failure if failed_terminal else None,
    )
    components.session.validate_portable_evidence(status)
    return status


def _parse_request(value: MigrationRequest | Mapping[str, Any]) -> MigrationRequest:
    if isinstance(value, MigrationRequest):
        return MigrationRequest.model_validate(value.model_dump(mode="python"))
    return MigrationRequest.model_validate(dict(value))


def _safe_project_root(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PolicyViolation("project root does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PolicyViolation("project root must be a non-symlink directory")
    return path.resolve(strict=True)


def _safe_source_root(project_root: Path, relative: str) -> Path:
    if any(part.casefold() in _ORACLE_PATH_SEGMENTS for part in relative.split("/")):
        raise PolicyViolation("source root cannot contain expected, golden, or oracle segments")
    candidate = project_root.joinpath(*relative.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project_root)
        metadata = candidate.lstat()
    except (FileNotFoundError, ValueError) as exc:
        raise PolicyViolation("source root is missing or escapes the project root") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PolicyViolation("source root must be a non-symlink directory")
    return resolved


def _validate_public_model_identifier(value: str, *, role: str) -> None:
    if value != value.strip() or _PUBLIC_IDENTIFIER.fullmatch(value) is None:
        raise ModelConfigurationError(f"{role} identity is blank, oversized, or contains controls")
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", value):
        raise ModelConfigurationError(f"{role} identity cannot contain an absolute path")
    if SecretRedactor().redact(value).changed or _API_KEY_TOKEN.search(value) is not None:
        raise ModelConfigurationError(f"{role} identity cannot contain credential material")


class _PassiveModel:
    """Identity-only client used to deserialize status or reject a manifest."""

    def __init__(self, provider: str, model_id: str) -> None:
        self._provider = provider
        self._model_id = model_id

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def live_invocation(self) -> bool:
        return False

    @property
    def store_false_sent(self) -> bool:
        return False

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[OutputModel],
    ) -> OutputModel:
        del system_prompt, input_value, output_type
        raise ModelConfigurationError("provider-free run inspection cannot invoke a model")


def _passive_models(provider: str, model_id: str) -> AgentRunModelClients:
    client = _PassiveModel(provider, model_id)
    return AgentRunModelClients.offline(
        architect=client,
        engineer=client,
        validator=client,
    )


class _NeverValidate:
    def __call__(self, *args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise PolicyViolation("read-only run inspection cannot invoke validation")


__all__ = [
    "AGENT_RUN_CONTROL_ROOT",
    "AgentRunConfig",
    "AgentRunCorrectionSummary",
    "AgentRunFailure",
    "AgentRunInterruptSummary",
    "AgentRunModelClients",
    "AgentRunStatus",
    "build_local_ollama_model_clients",
    "build_live_openai_model_clients",
    "get_agent_run_status",
    "assert_agent_request_secret_free",
    "prepare_agent_run_request",
    "resume_agent_run",
    "retry_agent_run",
    "start_agent_run",
]
