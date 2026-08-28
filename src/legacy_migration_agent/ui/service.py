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
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, cast

from legacy_migration_agent.agent_runtime.agent_definitions import load_agent_registry
from legacy_migration_agent.agent_runtime.correction import (
    CorrectionApproval,
    CorrectionController,
)
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectAgent,
    ArchitectConversationContext,
    ArchitectConversationMessage,
)
from legacy_migration_agent.agent_runtime.ollama_model import (
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    MAX_OLLAMA_TIMEOUT_SECONDS,
    MIN_OLLAMA_TIMEOUT_SECONDS,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelOutputError,
    ModelRuntimeError,
)
from legacy_migration_agent.application.agent_run import (
    AgentRunModelClients,
    AgentRunStatus,
    assert_agent_request_secret_free,
    build_local_ollama_model_clients,
    get_agent_run_status,
    has_verified_terminal_agent_run_history,
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
    MigrationRequest,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import ArtifactStore, artifact_digest
from legacy_migration_agent.core.observability import lifecycle_event
from legacy_migration_agent.core.policies import (
    PolicyViolation,
)
from legacy_migration_agent.core.redaction import SecretRedactor
from legacy_migration_agent.core.workspace import snapshot_tree
from legacy_migration_agent.ui.contracts import (
    AgentPlanningDecisionView as AgentPlanningDecisionView,
)
from legacy_migration_agent.ui.contracts import (
    AgentRunView,
    AgentUiError,
    JsonScalar,
)
from legacy_migration_agent.ui.projection import (
    RunViewProjector,
    _is_authorized_retry_continuation_status,
    _is_utc_timestamp,
    _valid_final_review_window,
    _validate_final_review_actor,
    _verified_authorized_retry_continuation,
    _verified_retry_request,
    is_verified_pre_manifest_terminal_without_model_record,
)
from legacy_migration_agent.workflow import ManifestApproval

_HANDLE_PATTERN: Final = re.compile(r"^[0-9a-f]{24}$")
_SERVICE_LOCKS_GUARD = threading.Lock()
_SERVICE_LOCKS: dict[tuple[Path, str], threading.RLock] = {}
_REVIEWER_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_OLLAMA_RUNTIME_PROVIDER: Final[Literal["ollama"]] = "ollama"
_OLLAMA_READINESS_TIMEOUT_SECONDS: Final = 3.0
_LOCAL_MODEL_APPROVER: Final = "local-ui-operator"
_CORRECTION_APPROVAL_ATTEMPT_TWO_PATH: Final = "control/correction-approval-attempt-2.json"


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
        self._projector = RunViewProjector(self._project_root, self._run_root)

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
                active_count = self._active_owned_run_count()
                if active_count >= self._max_runs:
                    lifecycle_event(
                        "ui.run.capacity_rejected",
                        level=logging.WARNING,
                        active_count=active_count,
                        max_runs=self._max_runs,
                    )
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
                view = self._projector.project(handle, reloaded)
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
            view = self._projector.project(handle, reloaded)
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
                current = self._projector.project(handle, status)
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
                view = self._projector.project(handle, reloaded)
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
                current = self._projector.project(handle, status)
                if current.final_review.status == "not_requested":
                    raise AgentUiError("final_review_unavailable")
                if current.final_review.status not in {"awaiting_final_review", "expired"}:
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
                view = self._projector.project(handle, reloaded)
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
                if selection == "reject" and self._projector.engineer_artifact_exists(reloaded):
                    raise AgentUiError("run_unavailable")
                view = self._projector.project(handle, reloaded)
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
                view = self._projector.project(handle, reloaded)
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
                view = self._projector.project(handle, status)
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
                base_files = self._candidate_base_files(handle, status)
                result = persist_candidate_export(
                    project_root=self._project_root,
                    platform=view.platform,
                    handle=handle,
                    attempt=cast(Literal[1, 2], candidate.attempt),
                    terminal_validation_disposition=validation.disposition,
                    manifest_digest=manifest.manifest_digest,
                    change_set_digest=candidate.change_set_digest,
                    files=tuple((item.path, item.content) for item in candidate.files),
                    base_files=base_files,
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
                status = self._load_status(handle)
                view = self._projector.project(handle, status)
                if view.candidate is None or not view.candidate.download_available:
                    raise AgentUiError("candidate_unavailable")
                return build_candidate_archive(
                    tuple((item.path, item.content) for item in view.candidate.files),
                    base_files=self._candidate_base_files(handle, status),
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

    def _candidate_base_files(
        self,
        handle: str,
        status: AgentRunStatus,
    ) -> tuple[tuple[str, str], ...]:
        """Read the exact frozen source bound to a run for its usable-project ZIP."""

        scenario = migration_scenario(status.platform)
        snapshot = snapshot_tree(self._source_root(scenario.source_root))
        request = MigrationRequest.model_validate(
            ArtifactStore(self._run_dir(handle) / "evidence").read_json("request.json")
        )
        if (
            request.request_id != status.request_id
            or request.platform is not status.platform
            or request.base_revision != snapshot.revision
        ):
            raise AgentUiError("candidate_unavailable")
        try:
            return tuple(
                (entry.path, entry.content.decode("utf-8", errors="strict"))
                for entry in snapshot.entries
            )
        except UnicodeDecodeError:
            raise AgentUiError("candidate_unavailable") from None

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
                and not is_verified_pre_manifest_terminal_without_model_record(run)
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
            return self._projector.project(handle, self._load_status(handle))

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
                run_id, thread_id = self._run_thread_ids(child.name)
                terminal = has_verified_terminal_agent_run_history(
                    self._project_root,
                    child,
                    run_id=run_id,
                    thread_id=thread_id,
                )
            except Exception as error:
                lifecycle_event(
                    "ui.run.capacity_entry_unverified",
                    level=logging.WARNING,
                    handle=child.name,
                    error_type=type(error).__name__,
                )
                count += 1
                continue
            if not terminal:
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


def _agent_requested_at() -> datetime:
    return datetime.now(UTC)


def _shared_service_lock(run_root: Path, key: str) -> threading.RLock:
    """Serialize one run or conversation across service instances in this process."""

    with _SERVICE_LOCKS_GUARD:
        return _SERVICE_LOCKS.setdefault((run_root, key), threading.RLock())


__all__ = [
    "AgentRunView",
    "AgentUiError",
    "AgentUiService",
]
