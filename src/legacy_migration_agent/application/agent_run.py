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
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from legacy_migration_agent.agent_runtime.agent_definitions import (
    AgentRegistry,
    AgentRole,
    load_agent_registry,
)
from legacy_migration_agent.agent_runtime.claude_cli_model import (
    DEFAULT_CLAUDE_TIMEOUT_SECONDS,
    ClaudeCliStructuredModelClient,
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
    filesystem_workspace_factory,
)
from legacy_migration_agent.agent_runtime.ollama_model import (
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    OllamaStructuredModelClient,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelRuntimeError,
    OpenAIResponsesModelClient,
    StructuredModelClient,
)
from legacy_migration_agent.agent_runtime.run_artifact_paths import RunArtifactPaths
from legacy_migration_agent.application.agent_run_contracts import (
    AGENT_RUN_CONFIG_PATH,
    AGENT_RUN_CONTROL_ROOT,
    AGENT_RUN_CORRECTION_APPROVAL_PATH,
    AGENT_RUN_CORRECTION_AUTHORIZATION_INTENT_KIND,
    AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
    AGENT_RUN_EVIDENCE_KIND,
    AGENT_RUN_FAILURE_KIND,
    AGENT_RUN_FAILURE_PATH,
    AGENT_RUN_LAUNCH_CONTRACT_PATH,
    AGENT_RUN_MANIFEST_APPROVAL_PATH,
    AGENT_RUN_MANIFEST_AUTHORIZATION_INTENT_KIND,
    AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
    AGENT_RUN_REQUEST_PATH,
    AgentRunCheckpointProjection,
    AgentRunConfig,
    AgentRunCorrectionSummary,
    AgentRunEvidenceAnchor,
    AgentRunFailure,
    AgentRunFailureCategory,
    AgentRunFailureReason,
    AgentRunFailureSeam,
    AgentRunInterruptSummary,
    AgentRunOperation,
    AgentRunStatus,
    agent_run_failure_explanation,
)
from legacy_migration_agent.application.agent_run_contracts import (
    AgentRunAuthorizationIntent as _AgentRunAuthorizationIntent,
)
from legacy_migration_agent.application.agent_run_lifecycle import (
    _checkpoint_projection,
    _checkpoint_projection_path,
    _checkpoint_value_digest,
    _InFlightModelOperation,
    _pending_manifest_interrupt,
    _portable_artifact_exists,
    _portable_evidence_paths,
    _read_checkpoint_projection,
    _read_status_projection,
    _read_workflow_projection,
    _RunComponents,
    _status_from_components,
    _status_projection_path,
    _VerifiedOperationLifecycle,
    _workflow_artifact_exists,
    _workflow_projection_path,
)
from legacy_migration_agent.application.agent_run_lifecycle import (
    _freeze_operation_evidence as _freeze_operation_evidence_impl,
)
from legacy_migration_agent.application.agent_run_lifecycle import (
    _verify_latest_operation_evidence as _verify_latest_operation_evidence_impl,
)
from legacy_migration_agent.application.agent_run_models import (
    AgentRunModelClients,
    _ControlledOperationError,
    _elapsed_milliseconds,
    _NeverValidate,
    _operation_failure_reason,
    _OperationSeamTracker,
    _passive_models,
    _SanitizedDeterministicValidator,
    _SanitizedModelClient,
)
from legacy_migration_agent.application.agent_run_models import (
    build_claude_cli_model_clients as _build_claude_cli_model_clients,
)
from legacy_migration_agent.application.agent_run_models import (
    build_live_openai_model_clients as _build_live_openai_model_clients,
)
from legacy_migration_agent.application.agent_run_models import (
    build_local_ollama_model_clients as _build_local_ollama_model_clients,
)
from legacy_migration_agent.application.migration_scenarios import (
    CASE_WIKI_QUERY,
    MULESOFT_WIKI_QUERY,
    SALESFORCE_WIKI_QUERY,
    MigrationLaunchContract,
    require_canonical_launch_contract,
)
from legacy_migration_agent.contracts import (
    ChangeSet,
    DecisionRequest,
    EnvironmentKind,
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
from legacy_migration_agent.core.redaction import assert_no_request_secrets
from legacy_migration_agent.core.run_session import AgentDefinitionDigests, AgentRunSession
from legacy_migration_agent.core.scope_policy import PlatformAdapter
from legacy_migration_agent.core.workspace import (
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
    CASE_SALESFORCE_PLATFORM_ADAPTER,
    CASE_SOURCE_ENTRY,
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

_ORACLE_PATH_SEGMENTS = frozenset({"expected", "golden", "oracle"})
_ROLE_INVOCATION_LEASE_PATH = re.compile(
    r"^model-runs/(?P<request_id>[A-Za-z0-9][A-Za-z0-9._:-]{0,159})/"
    r"(?P<role>engineer|validator)-invocation-lease-attempt-(?P<attempt>[12])\.json$"
)
_MODEL_OPERATION_ATTEMPT_PATH = re.compile(
    r"^(?:model-runs/[^/]+/.+|control/correction-request)-attempt-([12])\.json$"
)
_TERMINAL_AGENT_RUN_STATUSES = frozenset(
    {
        "completed",
        "decision_required",
        "failed",
        "modification_requested",
        "rejected",
    }
)
_TERMINAL_OPERATION_LIFECYCLES = frozenset(
    {
        "agent-run-planned",
        "agent-run-resumed",
        "agent-run-retried",
        AGENT_RUN_FAILURE_KIND,
    }
)


def build_live_openai_model_clients(
    *,
    model_id: str,
    api_key_environment: str,
    approval: LiveModelApproval,
) -> AgentRunModelClients:
    """Build an approved remote bundle while preserving facade patch seams."""

    return _build_live_openai_model_clients(
        model_id=model_id,
        api_key_environment=api_key_environment,
        approval=approval,
        client_factory=OpenAIResponsesModelClient,
    )


def build_local_ollama_model_clients(
    *,
    model_id: str,
    approval: LiveModelApproval,
    timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS,
) -> AgentRunModelClients:
    """Build an approved loopback bundle while preserving facade patch seams."""

    return _build_local_ollama_model_clients(
        model_id=model_id,
        approval=approval,
        timeout_seconds=timeout_seconds,
        client_factory=OllamaStructuredModelClient,
    )


def build_claude_cli_model_clients(
    *,
    model_id: str,
    approval: LiveModelApproval,
    timeout_seconds: float = DEFAULT_CLAUDE_TIMEOUT_SECONDS,
) -> AgentRunModelClients:
    """Build an approved Claude CLI bundle while preserving facade patch seams."""

    return _build_claude_cli_model_clients(
        model_id=model_id,
        approval=approval,
        timeout_seconds=timeout_seconds,
        client_factory=ClaudeCliStructuredModelClient,
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
    preset = _preset_for(contract.scenario_id)
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


def has_verified_terminal_agent_run_history(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    thread_id: str,
) -> bool:
    """Classify capacity from immutable history, not mutable repository state.

    The capacity boundary needs to distinguish a durable terminal run from an
    incomplete one after source inputs, shipped scenarios, or agent prompts
    have legitimately evolved.  This verifier therefore checks the historical
    session binding, stored request/config/launch relationships, lifecycle
    index, runtime anchor, authorizations, and terminal projections without
    reading current source bytes, current scenario constants, current agent
    definitions, or mutable workflow state.

    A verified nonterminal lifecycle returns ``False``.  Malformed, corrupt,
    or internally inconsistent evidence raises :class:`PolicyViolation` so the
    UI can fail closed and count that owned run as active.
    """

    root = _safe_project_root(project_root)
    session = AgentRunSession.load_historical_evidence(root, run_dir)
    if session.context.run_id != run_id:
        raise PolicyViolation("run_id does not match the loaded run session")
    if session.context.thread_id != thread_id:
        raise PolicyViolation("thread_id does not match the loaded run session")
    request, _, _ = _verify_historical_run_evidence(session)
    lifecycle = _verify_latest_operation_evidence(
        session,
        request,
        pending_authorization=None,
    )

    # Authorization snapshots are durable transaction boundaries, not a
    # completed operation.  Their projected status may equal the prior
    # terminal status while an authorized resume/retry remains unfinished.
    if lifecycle.kind not in _TERMINAL_OPERATION_LIFECYCLES:
        return False

    status = lifecycle.status
    if status.status not in _TERMINAL_AGENT_RUN_STATUSES:
        return False
    if status.pending_nodes or status.interrupt is not None:
        raise PolicyViolation("terminal run history contains pending workflow work")

    if lifecycle.kind == AGENT_RUN_FAILURE_KIND:
        if (
            status.status != "failed"
            or status.failure is None
            or lifecycle.failure is None
            or status.failure != lifecycle.failure
        ):
            raise PolicyViolation("terminal failure history is internally inconsistent")
        return True

    if (
        lifecycle.failure is not None
        or status.failure is not None
        or status != lifecycle.workflow_status
        or status.task_failed
        or lifecycle.checkpoint.next
        or lifecycle.checkpoint.tasks
    ):
        raise PolicyViolation("terminal run history is internally inconsistent")
    return True


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
    preset = _preset_for(contract.scenario_id)
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
        failure_summary, failure_guidance = agent_run_failure_explanation(reason_code, seam)
        lifecycle_event(
            "workflow.operation.failed",
            level=logging.WARNING,
            operation=operation,
            attempt=attempt,
            seam=seam,
            category=category,
            reason_code=reason_code,
            failure_summary=failure_summary,
            failure_guidance=failure_guidance,
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
    summary, guidance = agent_run_failure_explanation(reason_code, seam)
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
        summary=summary,
        guidance=guidance,
        attempt=attempt,
        request_digest=session.context.request_digest,
        operation_input_digest=operation_input_digest,
        session_context_digest=artifact_digest(session.context),
        source_revision=session.context.source_revision,
        agent_definition_digests=session.context.agent_definition_digests,
    )
    session.store.write_json(AGENT_RUN_FAILURE_PATH, failure)
    return failure


# Runtime presets are keyed by scenario id (preset_id == scenario_id) so a
# platform may expose several bounded units. Each preset's fixed values must
# equal the canonical launch contract derived from the matching migration
# scenario; `_validate_launch_contract_preset` enforces that exact agreement.
_PRESETS_BY_SCENARIO: Final[dict[str, _PlatformPreset]] = {
    "salesforce-vf-to-lwc": _PlatformPreset(
        preset_id="salesforce-vf-to-lwc",
        runtime=SALESFORCE_RUNTIME_CONFIG,
        adapter=SALESFORCE_PLATFORM_ADAPTER,
        entry_path=SALESFORCE_SOURCE_ENTRY,
        target_runtime=SALESFORCE_TARGET_RUNTIME,
        source_version=SALESFORCE_API_RUNTIME,
        target_version=SALESFORCE_API_RUNTIME,
        wiki_query=SALESFORCE_WIKI_QUERY,
        wiki_max_primary_hits=1,
    ),
    "case-management-console": _PlatformPreset(
        preset_id="case-management-console",
        runtime=SALESFORCE_RUNTIME_CONFIG,
        adapter=CASE_SALESFORCE_PLATFORM_ADAPTER,
        entry_path=CASE_SOURCE_ENTRY,
        target_runtime=SALESFORCE_TARGET_RUNTIME,
        source_version=SALESFORCE_API_RUNTIME,
        target_version=SALESFORCE_API_RUNTIME,
        wiki_query=CASE_WIKI_QUERY,
        wiki_max_primary_hits=1,
    ),
    "mulesoft-mule3-to-mule4": _PlatformPreset(
        preset_id="mulesoft-mule3-to-mule4",
        runtime=MULESOFT_RUNTIME_CONFIG,
        adapter=MULESOFT_PLATFORM_ADAPTER,
        entry_path=MULE3_APP,
        target_runtime=MULESOFT_TARGET_RUNTIME,
        source_version=MULESOFT_SOURCE_VERSION,
        target_version=MULESOFT_TARGET_VERSION,
        wiki_query=MULESOFT_WIKI_QUERY,
        wiki_max_primary_hits=1,
    ),
}


def _preset_for(scenario_id: str) -> _PlatformPreset:
    """Resolve one bounded unit's runtime preset by scenario id, failing closed."""

    preset = _PRESETS_BY_SCENARIO.get(scenario_id)
    if preset is None:
        raise PolicyViolation(f"unsupported migration scenario: {scenario_id!r}")
    return preset


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


def _verify_historical_run_evidence(
    session: AgentRunSession,
) -> tuple[MigrationRequest, AgentRunConfig, MigrationLaunchContract]:
    """Verify stored launch bindings without comparing them to current presets."""

    session.verify_index(AGENT_RUN_EVIDENCE_KIND, exact=False)
    try:
        request = MigrationRequest.model_validate(session.store.read_json(AGENT_RUN_REQUEST_PATH))
        config = AgentRunConfig.model_validate(session.store.read_json(AGENT_RUN_CONFIG_PATH))
        launch_contract = MigrationLaunchContract.model_validate(
            session.store.read_json(AGENT_RUN_LAUNCH_CONTRACT_PATH)
        )
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation("historical agent-run portable evidence is malformed") from exc

    if artifact_digest(request) != session.context.request_digest:
        raise PolicyViolation("historical migration request differs from the run session")
    if not (config.preset_id == launch_contract.scenario_id == session.context.slice_id):
        raise PolicyViolation("historical scenario binding is inconsistent")
    if config.wiki_as_of != launch_contract.wiki_as_of:
        raise PolicyViolation("historical Wiki cutoff binding is inconsistent")
    if not (request.repository == launch_contract.source_root == session.context.source_root):
        raise PolicyViolation("historical source-root binding is inconsistent")
    if request.base_revision != session.context.source_revision:
        raise PolicyViolation("historical source revision differs from the run session")
    if request.platform is not launch_contract.platform:
        raise PolicyViolation("historical platform binding is inconsistent")
    if request.allowed_environment is not EnvironmentKind.LOCAL:
        raise PolicyViolation("historical run did not use the local environment boundary")
    expected_target = (
        launch_contract.entry_path,
        launch_contract.target_runtime,
        launch_contract.source_version,
        launch_contract.target_version,
        launch_contract.canonical_description,
    )
    actual_target = (
        request.target.entry_path,
        request.target.target_runtime,
        request.target.source_version,
        request.target.target_version,
        request.target.description,
    )
    if actual_target != expected_target:
        raise PolicyViolation("historical migration target binding is inconsistent")

    index_payload = session.store.read_json(f"indexes/{AGENT_RUN_EVIDENCE_KIND}.json")
    session.verify_runtime_anchor(
        AGENT_RUN_EVIDENCE_KIND,
        _evidence_anchor(session, request, config, index_payload),
    )
    return request, config, launch_contract


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
    preset = _preset_for(contract.scenario_id)
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

    path = RunArtifactPaths(request.request_id).architect
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
    elif recorded == "remote_provider_managed":
        identity_digest = model_call.resolved_runtime_identity_digest
        if identity_digest is None:
            raise PolicyViolation("stored remote runtime evidence has no identity digest")
        try:
            models.bind_recorded_runtime_identity(identity_digest)
        except ModelConfigurationError:
            raise PolicyViolation(
                "remote runtime identity differs from immutable run evidence"
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
    preset = _preset_for(config.preset_id)
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
        paths = RunArtifactPaths(request.request_id)
        present = tuple(
            _portable_artifact_exists(session, path)
            for path in (paths.architect, paths.architect_preflight)
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

        outcome_path = RunArtifactPaths(request.request_id).role_outcome(role, attempt)
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
    paths = RunArtifactPaths(request.request_id)
    for role in cast(tuple[Literal["engineer", "validator"], ...], ("engineer", "validator")):
        lease_path = paths.role_invocation_lease(role, attempt)
        outcome_path = paths.role_outcome(role, attempt)
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

    paths = RunArtifactPaths(components.request.request_id)
    expected_paths = {
        paths.engineer_invocation_lease(operation.attempt),
        paths.engineer(operation.attempt),
        paths.report(operation.attempt),
        paths.validator_invocation_lease(operation.attempt),
        paths.validator(operation.attempt),
    }
    try:
        validator_assessment = ValidatorAssessment.model_validate(
            components.session.store.read_json(paths.validator(operation.attempt))
        )
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation("terminal Validator advisory evidence is malformed") from exc
    if (
        validator_assessment.unavailable_receipt is not None
        and not validator_assessment.unavailable_receipt.attempted
    ):
        expected_paths.remove(paths.validator_invocation_lease(operation.attempt))
    if operation.attempt == 2:
        expected_paths.add(paths.engineer_correction)
        expected_paths.add(paths.correction_wiki)
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

    paths = RunArtifactPaths(components.request.request_id)
    expected_paths = {
        paths.engineer_invocation_lease(operation.attempt),
        paths.engineer(operation.attempt),
    }
    if operation.attempt == 2:
        expected_paths.add(paths.engineer_correction)
        expected_paths.add(paths.correction_wiki)
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


def _freeze_operation_evidence(
    session: AgentRunSession,
    kind: str,
    status: AgentRunStatus,
    workflow_status: AgentRunStatus,
    checkpoint: AgentRunCheckpointProjection,
) -> None:
    """Freeze one lifecycle while retaining the facade's fault-injection seam."""

    _freeze_operation_evidence_impl(
        session,
        kind,
        status,
        workflow_status,
        checkpoint,
        after_authorization_write=_after_authorization_write,
    )


def _verify_latest_operation_evidence(
    session: AgentRunSession,
    request: MigrationRequest,
    *,
    pending_authorization: ManifestApproval | CorrectionApproval | None,
) -> _VerifiedOperationLifecycle:
    """Verify lifecycle evidence with the facade's authorization verifier."""

    return _verify_latest_operation_evidence_impl(
        session,
        request,
        pending_authorization=pending_authorization,
        verify_authorization_intent=_verify_authorization_intent,
    )


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


__all__ = [
    "AGENT_RUN_CONTROL_ROOT",
    "AgentRunConfig",
    "AgentRunCorrectionSummary",
    "AgentRunFailure",
    "AgentRunInterruptSummary",
    "AgentRunModelClients",
    "AgentRunStatus",
    "build_local_ollama_model_clients",
    "build_claude_cli_model_clients",
    "build_live_openai_model_clients",
    "get_agent_run_status",
    "has_verified_terminal_agent_run_history",
    "assert_agent_request_secret_free",
    "prepare_agent_run_request",
    "resume_agent_run",
    "retry_agent_run",
    "start_agent_run",
]
