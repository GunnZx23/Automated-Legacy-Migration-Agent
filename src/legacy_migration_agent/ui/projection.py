"""Fail-closed projection of verified migration-run evidence into UI contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, cast

from legacy_migration_agent import contracts as domain_contracts
from legacy_migration_agent.agent_runtime import correction as correction_contracts
from legacy_migration_agent.agent_runtime import model_agents
from legacy_migration_agent.agent_runtime.openai_model import ModelCallRecord
from legacy_migration_agent.agent_runtime.run_artifact_paths import RunArtifactPaths
from legacy_migration_agent.application import final_review, run_query
from legacy_migration_agent.application.agent_run import AgentRunFailure, AgentRunStatus
from legacy_migration_agent.application.migration_scenarios import migration_scenario_by_id
from legacy_migration_agent.core.integrity import ArtifactStore, artifact_digest
from legacy_migration_agent.core.policies import validate_change_set, validate_report
from legacy_migration_agent.core.redaction import SecretRedactor
from legacy_migration_agent.core.run_session import AgentRunContext, AgentRunSession
from legacy_migration_agent.graphs.graph_contracts import DependencyGraph
from legacy_migration_agent.knowledge.wiki import RetrievalTrace
from legacy_migration_agent.ui import contracts as ui_contracts
from legacy_migration_agent.workflow import ManifestApproval

_FINAL_REVIEW_ACTOR_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,159}$")
_MAX_FINAL_REVIEW_WINDOW: Final = timedelta(days=14)
_UiProvider = Literal["ollama", "claude-cli"]
_UiBoundary = Literal["local_loopback", "remote_provider_managed"]
_UiMode = Literal["local_ollama", "remote_claude_cli"]
_OLLAMA_RUNTIME_PROVIDER: Final[Literal["ollama"]] = "ollama"
_CLAUDE_RUNTIME_PROVIDER: Final[Literal["claude-cli"]] = "claude-cli"
_MANIFEST_APPROVAL_PATH: Final = "control/manifest-approval.json"
_CORRECTION_REQUEST_ATTEMPT_ONE_PATH: Final = "control/correction-request-attempt-1.json"
_CORRECTION_APPROVAL_ATTEMPT_TWO_PATH: Final = "control/correction-approval-attempt-2.json"
_LOCAL_BOUNDARY_NOTICE: Final = (
    "Real structured-output inference ran through the server-owned loopback Ollama endpoint "
    "against a fixed synthetic source fixture. The agent creates an isolated migration "
    "candidate only; no Salesforce org, Mule runtime, source mutation, deployment, Git action, "
    "or publication is invoked."
)
_CLAUDE_BOUNDARY_NOTICE: Final = (
    "Real structured-output inference ran through the server-owned Claude CLI against a "
    "fixed synthetic source fixture. Inference is remote and provider-managed; the CLI disables "
    "local session persistence, tools, MCP, browser integration, and project customizations, "
    "but does not claim provider-side zero retention. The agent creates an isolated migration "
    "candidate only; no Salesforce org, Mule runtime, source mutation, deployment, Git action, "
    "or publication is invoked."
)
_RUNTIME_PRESENTATION: Final[dict[_UiProvider, tuple[_UiBoundary, _UiMode, str, str]]] = {
    _OLLAMA_RUNTIME_PROVIDER: (
        "local_loopback",
        "local_ollama",
        "loopback Ollama",
        _LOCAL_BOUNDARY_NOTICE,
    ),
    _CLAUDE_RUNTIME_PROVIDER: (
        "remote_provider_managed",
        "remote_claude_cli",
        "remote Claude CLI",
        _CLAUDE_BOUNDARY_NOTICE,
    ),
}
_HANDLE_PATTERN: Final = re.compile(r"^[0-9a-f]{24}$")

_FAILURE_DETAILS: Final[
    dict[
        ui_contracts.AgentFailureReasonCode,
        tuple[ui_contracts.AgentFailurePhase, bool | None, bool | None, bool | None, str, str],
    ]
] = {
    "configuration_invalid": (
        "configuration",
        None,
        None,
        None,
        "The controller rejected the configured model runtime before a usable response.",
        "Check the configured provider, model identity, and approval state, then start a fresh run.",
    ),
    "provider_refusal": (
        "model_inference",
        True,
        False,
        None,
        "The configured model declined to produce the requested structured role output.",
        "Refine the bounded migration request and start a fresh run.",
    ),
    "response_incomplete": (
        "structured_output",
        True,
        False,
        None,
        "The model response ended before the structured role output was complete.",
        "Start a fresh run; reduce model load or allow a longer server-owned timeout if needed.",
    ),
    "structured_output_invalid": (
        "structured_output",
        True,
        False,
        None,
        "The model responded, but its role output did not satisfy the typed contract.",
        "Start a fresh run and use the harness trace to identify the rejected role boundary.",
    ),
    "unauthorized_tool_call": (
        "structured_output",
        True,
        False,
        None,
        (
            "The model returned a native provider tool call instead of the required "
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
        "The configured runtime could not prove the selected model identity.",
        "Confirm the server-selected provider and model identity, then start a fresh run.",
    ),
    "provider_response_invalid": (
        "provider_response",
        None,
        None,
        None,
        "The provider returned a response that failed controller-owned protocol checks.",
        "Confirm the configured provider and selected model, then start a fresh run.",
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
    "correction_delta_required": (
        "policy_validation",
        True,
        True,
        False,
        "Engineer attempt 2 did not return the required changed-file correction delta.",
        (
            "Start a fresh run. On correction, Engineer must use the supplied validation and "
            "Wiki repair signals and return a file-plan delta instead of another intervention."
        ),
    ),
    "correction_no_material_changes": (
        "policy_validation",
        True,
        True,
        False,
        "Engineer attempt 2 proposed updates, but their contents did not change attempt 1.",
        (
            "Start a fresh run and apply a material change to at least one allowed repair file; "
            "resubmitting unchanged attempt-1 content cannot satisfy correction."
        ),
    ),
    "correction_signal_coverage_missing": (
        "policy_validation",
        True,
        True,
        False,
        "Engineer attempt 2 did not address every controller-classified repair signal.",
        (
            "Start a fresh run. The correction delta must touch an allowed file mapped to every "
            "reported repair signal before deterministic validation can run again."
        ),
    ),
    "correction_scope_invalid": (
        "policy_validation",
        True,
        True,
        False,
        "Engineer attempt 2 proposed a path outside the controller-owned repair boundary.",
        (
            "Start a fresh run and restrict the correction delta to the allowed paths named by "
            "the correction contract; request a new manifest if broader scope is actually needed."
        ),
    ),
    "correction_identical_candidate": (
        "policy_validation",
        True,
        True,
        False,
        "Engineer attempt 2 reconstructed the same candidate as attempt 1.",
        (
            "Start a fresh run and make a targeted content change that resolves the supplied "
            "repair signals before submitting the correction delta."
        ),
    ),
    "file_plan_scope_mismatch": (
        "policy_validation",
        True,
        True,
        False,
        "The Engineer file plan did not match the manifest-approved output scope.",
        (
            "Start a fresh run. Engineer must produce exactly the manifest-approved outputs on "
            "attempt 1 and only controller-authorized repair paths on attempt 2."
        ),
    ),
    "file_plan_delta_mismatch": (
        "policy_validation",
        True,
        True,
        False,
        "The isolated workspace changes did not match the Engineer file plan.",
        (
            "Start a fresh run. Every changed path must be declared in the file plan, and every "
            "declared update must be the exact change applied in the isolated workspace."
        ),
    ),
    "workspace_scope_mismatch": (
        "policy_validation",
        True,
        True,
        False,
        "The isolated Engineer workspace was not bound to the exact manifest-approved scope.",
        (
            "Start a fresh run and verify the selected scenario, manifest, and immutable source "
            "revision agree before Engineer is invoked."
        ),
    ),
    "workspace_not_clean": (
        "policy_validation",
        True,
        True,
        False,
        "The isolated Engineer workspace contained an unexpected pre-existing change.",
        (
            "Start a fresh run from the unchanged source revision; the harness requires a clean "
            "isolated workspace before applying the Engineer file plan."
        ),
    ),
    "attempt_two_scope_expansion_invalid": (
        "policy_validation",
        True,
        True,
        False,
        "Engineer attempt 2 requested scope expansion without identifying a required outside path.",
        (
            "Start a fresh run. A scope-expansion intervention must name a specifically required "
            "path outside the approved manifest; otherwise Engineer must return the bounded delta."
        ),
    ),
    "output_evidence_local_path": (
        "role_artifact_persistence",
        True,
        True,
        False,
        (
            "The role output could not be stored as portable evidence because it contained "
            "local filesystem notation."
        ),
        (
            "Start a fresh run and keep authored narrative limited to repository paths, API "
            "routes, or other portable prose without local filesystem locations."
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
        "The model request exceeded the server-owned inference deadline.",
        "Confirm the configured provider is responsive, then restart with a longer timeout.",
    ),
    "provider_unavailable": (
        "model_inference",
        None,
        None,
        None,
        "The controller could not complete the configured model request.",
        "Confirm the configured provider and model are available, then start a fresh run.",
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


class RunViewProjector:
    """Read and validate durable run evidence, then build its public UI view."""

    def __init__(self, project_root: Path, run_root: Path) -> None:
        self._project_root = project_root
        self._run_root = run_root

    def project(self, handle: str, status: AgentRunStatus) -> ui_contracts.AgentRunView:
        store = ArtifactStore(self._run_root / handle / "evidence")
        run_context = AgentRunContext.model_validate(store.read_json("run-context.json"))
        try:
            scenario = migration_scenario_by_id(run_context.slice_id)
        except KeyError:
            raise ui_contracts.AgentUiError("run_unavailable") from None
        if scenario.platform is not status.platform:
            raise ui_contracts.AgentUiError("run_unavailable")
        snapshots = self._verified_run_snapshots(handle, status)
        latest_snapshot = snapshots[max(snapshots)] if snapshots else None
        request = (
            latest_snapshot.request
            if latest_snapshot is not None
            else domain_contracts.MigrationRequest.model_validate(store.read_json("request.json"))
        )
        if request.request_id != status.request_id or request.platform is not status.platform:
            raise ui_contracts.AgentUiError("run_unavailable")
        model_paths = RunArtifactPaths(status.request_id)
        graph: DependencyGraph | None
        wiki_trace: RetrievalTrace | None
        if latest_snapshot is not None:
            graph = latest_snapshot.dependency_graph
            wiki_trace = latest_snapshot.wiki_trace
            architect = latest_snapshot.architect
        else:
            graph, wiki_trace = self._context_evidence(store, model_paths)
            try:
                architect_payload = store.read_json(model_paths.architect)
            except FileNotFoundError:
                return self._project_pre_manifest_terminal(
                    handle,
                    status,
                    scenario_id=scenario.scenario_id,
                    scenario_title=scenario.title,
                    request=request,
                    store=store,
                    model_paths=model_paths,
                    graph=graph,
                    wiki_trace=wiki_trace,
                )
            architect = model_agents.ArchitectRun.model_validate(architect_payload)
        manifest = architect.proposal.manifest
        if status.manifest_id != manifest.manifest_id or status.manifest_digest != artifact_digest(
            manifest
        ):
            raise ui_contracts.AgentUiError("run_unavailable")
        if graph is None or wiki_trace is None:
            raise ui_contracts.AgentUiError("run_unavailable")
        planning_decision = _architect_planning_decision(architect, status)
        candidate, validation, terminal_calls, intervention = self._terminal_evidence(
            store,
            model_paths,
            manifest,
            status,
            snapshots,
        )
        correction = _correction_view(
            store,
            status,
            model_paths=model_paths,
            snapshots=snapshots,
        )
        attempt_history = _attempt_history(store, model_paths, manifest, status, snapshots)
        prior_calls = _prior_attempt_model_calls(
            store,
            model_paths,
            manifest,
            status,
            snapshots,
        )
        model_calls = (
            architect.model_call,
            *tuple(call for call, _ in prior_calls),
            *terminal_calls,
        )
        boundaries = self._project_boundaries(status, model_calls)
        human_decision = _human_decision_view(store, status, manifest)
        return ui_contracts.AgentRunView(
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
            metrics=ui_contracts.AgentMetricsView(
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
            evidence=ui_contracts.AgentEvidenceView(
                wiki_hits=tuple(
                    ui_contracts.AgentWikiHitView(
                        page_id=hit.page_id, title=hit.title, path=hit.path
                    )
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
                self._run_root / handle,
                status,
                store,
            ),
            failure=_failure_view(status),
        )

    def _terminal_evidence(
        self,
        store: ArtifactStore,
        model_paths: RunArtifactPaths,
        manifest: domain_contracts.MigrationManifest,
        status: AgentRunStatus,
        snapshots: Mapping[int, run_query.VerifiedRunSnapshot],
    ) -> tuple[
        ui_contracts.AgentCandidateView | None,
        ui_contracts.AgentValidationView | None,
        tuple[ModelCallRecord, ...],
        ui_contracts.AgentInterventionView | None,
    ]:
        attempt = status.execution_attempt
        if _is_authorized_retry_continuation_status(status):
            snapshot = snapshots.get(1)
            if snapshot is None:
                raise ui_contracts.AgentUiError("run_unavailable")
            engineer = snapshot.engineer
            report = snapshot.report
            assessment = snapshot.validator
            if report.disposition is not domain_contracts.ValidationDisposition.RECOVERABLE_FAILURE:
                raise ui_contracts.AgentUiError("run_unavailable")
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
                    store.read_json(model_paths.engineer(attempt))
                except FileNotFoundError:
                    pass
                else:
                    raise ui_contracts.AgentUiError("run_unavailable")
                return None, None, (), None
            engineer = model_agents.EngineerRun.model_validate(
                store.read_json(model_paths.engineer(attempt))
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
                raise ui_contracts.AgentUiError("run_unavailable")
            return (
                (_prior_candidate_view(snapshots, manifest) if attempt == 2 else None),
                None,
                (engineer.model_call,),
                _implementation_intervention_view(intervention),
            )
        if status.status == "failed":
            failure = status.failure
            if failure is None or status.terminal_disposition != "controlled_failure":
                raise ui_contracts.AgentUiError("run_unavailable")
            engineer_path = model_paths.engineer(attempt)
            try:
                engineer_payload = store.read_json(engineer_path)
            except FileNotFoundError:
                failed_engineer: model_agents.EngineerRun | None = None
            else:
                failed_engineer = model_agents.EngineerRun.model_validate(engineer_payload)
            if failure.seam in {"validator", "deterministic_validator"} and failed_engineer is None:
                raise ui_contracts.AgentUiError("run_unavailable")
            if failure.seam == "engineer" and failed_engineer is not None:
                raise ui_contracts.AgentUiError("run_unavailable")
            terminal_calls = () if failed_engineer is None else (failed_engineer.model_call,)
            if failed_engineer is None:
                return (
                    (_prior_candidate_view(snapshots, manifest) if attempt == 2 else None),
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
            report = domain_contracts.ValidationReport.model_validate(
                store.read_json(model_paths.report(attempt))
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
        snapshot = snapshots.get(attempt)
        if snapshot is None:
            raise ui_contracts.AgentUiError("run_unavailable")
        engineer = snapshot.engineer
        report = snapshot.report
        assessment = snapshot.validator
        if status.terminal_disposition != report.disposition.value:
            raise ui_contracts.AgentUiError("run_unavailable")
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
        model_paths: RunArtifactPaths,
    ) -> tuple[DependencyGraph | None, RetrievalTrace | None]:
        try:
            graph_payload = store.read_json(model_paths.dependency_graph)
        except FileNotFoundError:
            graph = None
        else:
            graph = DependencyGraph.model_validate(graph_payload)
        try:
            wiki_payload = store.read_json(model_paths.wiki_trace)
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
        request: domain_contracts.MigrationRequest,
        store: ArtifactStore,
        model_paths: RunArtifactPaths,
        graph: DependencyGraph | None,
        wiki_trace: RetrievalTrace | None,
    ) -> ui_contracts.AgentRunView:
        intervention: ui_contracts.AgentInterventionView | None = None
        if status.status == "decision_required" and status.failure is None:
            planning = domain_contracts.PlanningIntervention.model_validate(
                store.read_json(model_paths.architect_preflight)
            )
            if (
                planning.request_id != request.request_id
                or planning.request_digest != artifact_digest(request)
                or planning.platform is not request.platform
                or planning.base_revision != request.base_revision
                or status.terminal_disposition != "decision_required"
            ):
                raise ui_contracts.AgentUiError("run_unavailable")
            intervention = _planning_intervention_view(planning)
        elif (
            status.status != "failed"
            or status.failure is None
            or status.failure.seam != "architect"
            or status.terminal_disposition != "controlled_failure"
        ):
            raise ui_contracts.AgentUiError("run_unavailable")
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
        return ui_contracts.AgentRunView(
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
            metrics=ui_contracts.AgentMetricsView(
                graph_nodes=graph_nodes,
                graph_edges=graph_edges,
                wiki_hits=len(wiki_hits),
                changed_files=0,
            ),
            manifest=None,
            evidence=ui_contracts.AgentEvidenceView(
                wiki_hits=tuple(
                    ui_contracts.AgentWikiHitView(
                        page_id=hit.page_id, title=hit.title, path=hit.path
                    )
                    for hit in wiki_hits
                ),
                graph_summary=graph_summary,
            ),
            candidate=None,
            validation=None,
            intervention=intervention,
            final_review=_final_review_view(
                self._project_root,
                self._run_root / handle,
                status,
                store,
            ),
            failure=_failure_view(status),
        )

    def _project_boundaries(
        self,
        status: AgentRunStatus,
        calls: tuple[ModelCallRecord, ...],
    ) -> ui_contracts.AgentBoundariesView:
        """Project runtime truth only after cross-checking persisted role calls."""

        if not calls or any(call.execution_boundary is None for call in calls):
            raise ui_contracts.AgentUiError("run_unavailable")
        identities = {(call.provider, call.model_id) for call in calls}
        boundaries = {call.resolved_execution_boundary for call in calls}
        if identities != {(status.provider_id, status.model_id)} or len(boundaries) != 1:
            raise ui_contracts.AgentUiError("run_unavailable")
        execution_boundary = next(iter(boundaries))
        revisions = {call.model_revision for call in calls}
        if len(revisions) != 1:
            raise ui_contracts.AgentUiError("run_unavailable")
        model_revision = next(iter(revisions))
        runtime_identities = {call.resolved_runtime_identity_digest for call in calls}
        if len(runtime_identities) != 1 or None in runtime_identities:
            raise ui_contracts.AgentUiError("run_unavailable")
        runtime_identity_digest = next(iter(runtime_identities))
        runtime = _runtime_presentation(status.provider_id)
        if runtime is not None and execution_boundary == runtime[0]:
            if runtime[0] == "local_loopback" and model_revision is None:
                raise ui_contracts.AgentUiError("run_unavailable")
            active_failure_seam = None if status.failure is None else status.failure.seam
            return ui_contracts.AgentBoundariesView(
                provider_attempted=True,
                provider_invoked=True,
                model_call_record_persisted=True,
                structured_response_accepted=active_failure_seam
                not in {
                    "engineer",
                    "validator",
                },
                provider_id=cast(_UiProvider, status.provider_id),
                model_id=status.model_id,
                model_revision=model_revision,
                runtime_identity_digest=runtime_identity_digest,
                execution_boundary=runtime[0],
                mode=runtime[1],
                notice=runtime[3],
            )
        raise ui_contracts.AgentUiError("run_unavailable")

    def _project_boundaries_without_call(
        self,
        status: AgentRunStatus,
    ) -> ui_contracts.AgentBoundariesView:
        runtime = _runtime_presentation(status.provider_id)
        if runtime is not None:
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
                    f"A {runtime[2]} response passed structured-output validation, then the "
                    "controller rejected it at the policy boundary. No model-call record was "
                    "claimed and no external action was invoked."
                )
            elif attempted:
                notice = (
                    f"A {runtime[2]} call was attempted, but no accepted structured response "
                    "or model-call record was persisted. No external action was invoked."
                )
            else:
                notice = (
                    f"The controller stopped before invoking the configured {runtime[2]} "
                    "model. No external action was invoked."
                )
            return ui_contracts.AgentBoundariesView(
                provider_attempted=attempted,
                provider_invoked=True if response_accepted else None if attempted else False,
                model_call_record_persisted=False,
                structured_response_accepted=response_accepted,
                provider_id=cast(_UiProvider, status.provider_id),
                model_id=status.model_id,
                model_revision=None,
                runtime_identity_digest=None,
                execution_boundary=runtime[0],
                mode=runtime[1],
                notice=notice,
            )
        raise ui_contracts.AgentUiError("run_unavailable")

    def _verified_run_snapshots(
        self,
        handle: str,
        status: AgentRunStatus,
    ) -> dict[int, run_query.VerifiedRunSnapshot]:
        """Load complete attempt evidence once for all UI projections."""

        if status.status == "completed":
            attempts = tuple(range(1, status.execution_attempt + 1))
        elif status.execution_attempt == 2:
            attempts = (1,)
        else:
            return {}
        session = AgentRunSession.load(self._project_root, self._run_root / handle)
        snapshots = run_query.load_verified_run_snapshots(session, attempts=attempts)
        return {snapshot.report.attempt: snapshot for snapshot in snapshots}

    def engineer_artifact_exists(self, status: AgentRunStatus) -> bool:
        path = (
            self._run_root
            / _handle_from_run_id(status.run_id)
            / "evidence"
            / RunArtifactPaths(status.request_id).engineer(1)
        )
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True


def is_verified_pre_manifest_terminal_without_model_record(run: ui_contracts.AgentRunView) -> bool:
    """Accept only a bound pre-manifest terminal run with no model record."""

    if (
        run.execution_attempt != 1
        or run.manifest is not None
        or run.candidate is not None
        or run.validation is not None
        or run.model_calls
        or run.boundaries.model_call_record_persisted
        or run.boundaries.model_revision is not None
        or run.boundaries.runtime_identity_digest is not None
        or (
            run.boundaries.provider_id,
            run.boundaries.execution_boundary,
            run.boundaries.mode,
        )
        not in {
            ("ollama", "local_loopback", "local_ollama"),
            ("claude-cli", "remote_provider_managed", "remote_claude_cli"),
        }
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


def _runtime_presentation(
    provider_id: str,
) -> tuple[_UiBoundary, _UiMode, str, str] | None:
    if provider_id not in _RUNTIME_PRESENTATION:
        return None
    return _RUNTIME_PRESENTATION[provider_id]


def _validate_final_review_actor(value: object) -> str:
    if not isinstance(value, str) or _FINAL_REVIEW_ACTOR_PATTERN.fullmatch(value) is None:
        raise ui_contracts.AgentUiError("invalid_reviewer")
    if SecretRedactor().redact(value).changed:
        raise ui_contracts.AgentUiError("secret_material")
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
        and status.terminal_disposition
        == domain_contracts.ValidationDisposition.READY_FOR_HUMAN_REVIEW.value
        and status.failure is None
        and not status.pending_nodes
    )


def _final_review_view(
    project_root: Path,
    run_dir: Path,
    run_status: AgentRunStatus,
    store: ArtifactStore,
) -> ui_contracts.AgentFinalReviewView:
    """Verify and project final-review evidence without treating it as authority."""

    eligible = _final_review_eligible(run_status)
    try:
        request_payload = store.read_json(final_review.FINAL_REVIEW_REQUEST_PATH)
    except FileNotFoundError:
        # A lifecycle index or later artifact without its request is corruption, not a
        # fresh review opportunity.
        for path in (
            "indexes/final-review-requested.json",
            "indexes/final-review-decided.json",
            final_review.FINAL_REVIEW_DECISION_PATH,
            final_review.FINAL_REVIEW_RECORD_PATH,
        ):
            try:
                store.read_json(path)
            except FileNotFoundError:
                continue
            raise ui_contracts.AgentUiError("run_unavailable") from None
        return ui_contracts.AgentFinalReviewView(
            status="not_requested",
            eligible=eligible,
            can_request=eligible,
            can_decide=False,
        )

    request = final_review.FinalReviewRequest.model_validate(request_payload)
    status = final_review.get_final_review_status_for_run(
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
        raise ui_contracts.AgentUiError("run_unavailable")

    if status.status in {"awaiting_final_review", "expired"}:
        for path in (
            final_review.FINAL_REVIEW_DECISION_PATH,
            final_review.FINAL_REVIEW_RECORD_PATH,
        ):
            try:
                store.read_json(path)
            except FileNotFoundError:
                continue
            raise ui_contracts.AgentUiError("run_unavailable") from None
        return ui_contracts.AgentFinalReviewView(
            status=status.status,
            eligible=eligible,
            can_request=False,
            can_decide=eligible,
            review_id=request.review_id,
            requester=request.requester,
            designated_reviewer=request.designated_reviewer,
            requested_at=request.requested_at,
            expires_at=request.expires_at,
        )

    decision = final_review.FinalReviewDecision.model_validate(
        store.read_json(final_review.FINAL_REVIEW_DECISION_PATH)
    )
    record = final_review.FinalReviewRecord.model_validate(
        store.read_json(final_review.FINAL_REVIEW_RECORD_PATH)
    )
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
        raise ui_contracts.AgentUiError("run_unavailable")
    return ui_contracts.AgentFinalReviewView(
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


def _handle_from_run_id(run_id: str) -> str:
    prefix = "ui-run-"
    if not run_id.startswith(prefix):
        raise ui_contracts.AgentUiError("run_unavailable")
    handle = run_id.removeprefix(prefix)
    if _HANDLE_PATTERN.fullmatch(handle) is None:
        raise ui_contracts.AgentUiError("run_unavailable")
    return handle


def _manifest_view(architect: model_agents.ArchitectRun) -> ui_contracts.AgentManifestView:
    manifest = architect.proposal.manifest
    return ui_contracts.AgentManifestView(
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
            ui_contracts.AgentTransformationView(
                step_id=item.step_id,
                description=item.description,
                input_paths=item.input_paths,
                output_paths=item.output_paths,
            )
            for item in manifest.transformations
        ),
        validation_commands=tuple(
            ui_contracts.AgentValidationCommandView(
                check_id=item.check_id,
                command_id=item.command_id,
                purpose=item.purpose,
                environment=item.environment.value,
                required=item.required,
            )
            for item in manifest.validation_plan
        ),
        risks=tuple(
            ui_contracts.AgentRiskView(
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
    snapshots: Mapping[int, run_query.VerifiedRunSnapshot],
    manifest: domain_contracts.MigrationManifest,
) -> ui_contracts.AgentCandidateView:
    """Return the verified attempt-one candidate as read-only retry evidence."""

    snapshot = snapshots.get(1)
    if snapshot is None:
        raise ui_contracts.AgentUiError("run_unavailable")
    engineer = snapshot.engineer
    report = snapshot.report
    if report.disposition is not domain_contracts.ValidationDisposition.RECOVERABLE_FAILURE:
        raise ui_contracts.AgentUiError("run_unavailable")
    return _candidate_view(
        engineer,
        manifest,
        attempt=1,
        download_available=False,
    )


def _candidate_view(
    engineer: model_agents.EngineerRun,
    manifest: domain_contracts.MigrationManifest,
    *,
    attempt: int,
    download_available: bool,
) -> ui_contracts.AgentCandidateView:
    change_set = engineer.change_set
    file_plan = engineer.file_plan
    if change_set is None or file_plan is None or engineer.intervention is not None:
        raise ui_contracts.AgentUiError("run_unavailable")
    validate_change_set(change_set, manifest)
    if tuple(sorted(change_set.changed_paths)) != tuple(
        sorted(update.path for update in file_plan.updates)
    ) or tuple(sorted(change_set.changed_paths)) != tuple(sorted(manifest.approved_paths)):
        raise ui_contracts.AgentUiError("run_unavailable")
    return ui_contracts.AgentCandidateView(
        attempt=attempt,
        download_available=download_available,
        change_set_digest=artifact_digest(change_set),
        changed_paths=change_set.changed_paths,
        unified_diff=change_set.unified_diff,
        files=tuple(
            ui_contracts.AgentCandidateFileView(path=update.path, content=update.content)
            for update in sorted(file_plan.updates, key=lambda item: item.path)
        ),
        assumptions=change_set.assumptions,
    )


def _validation_view(
    report: domain_contracts.ValidationReport,
    engineer: model_agents.EngineerRun,
    manifest: domain_contracts.MigrationManifest,
    *,
    assessment: model_agents.ValidatorAssessment | None,
) -> ui_contracts.AgentValidationView:
    change_set = engineer.change_set
    if change_set is None:
        raise ui_contracts.AgentUiError("run_unavailable")
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
            raise ui_contracts.AgentUiError("run_unavailable")
        advisory_assessment = raw_advisory_assessment
        concerns = assessment.advisory.concerns
        validator_completed = True
    dependencies = correction_contracts.validation_failure_dependencies(report)
    return ui_contracts.AgentValidationView(
        attempt=report.attempt,
        disposition=report.disposition.value,
        results=tuple(
            ui_contracts.AgentValidationResultView(
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
        final_review_enabled=(
            report.disposition is domain_contracts.ValidationDisposition.READY_FOR_HUMAN_REVIEW
        ),
    )


def _verified_retry_request(
    store: ArtifactStore,
    status: AgentRunStatus,
) -> correction_contracts.CorrectionRequest:
    """Load the exact attempt-one correction without deriving authority from prose."""

    correction = correction_contracts.CorrectionRequest.model_validate(
        store.read_json(_CORRECTION_REQUEST_ATTEMPT_ONE_PATH)
    )
    summary = status.correction
    if (
        status.execution_attempt != 1
        or status.status != "completed"
        or status.terminal_disposition != "recoverable_failure"
        or summary is None
        or correction.action is not correction_contracts.CorrectionAction.RETRY_IMPLEMENTATION
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
        raise ui_contracts.AgentUiError("retry_unavailable")
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
        and status.correction.action is correction_contracts.CorrectionAction.RETRY_IMPLEMENTATION
    )


def _verified_authorized_retry_continuation(
    store: ArtifactStore,
    status: AgentRunStatus,
) -> tuple[correction_contracts.CorrectionRequest, correction_contracts.CorrectionApproval]:
    """Load the immutable correction and approval for the one resumable checkpoint."""

    if not _is_authorized_retry_continuation_status(status):
        raise ui_contracts.AgentUiError("retry_already_recorded")
    correction = correction_contracts.CorrectionRequest.model_validate(
        store.read_json(_CORRECTION_REQUEST_ATTEMPT_ONE_PATH)
    )
    if (
        correction.completed_attempt != 1
        or correction.next_attempt != 2
        or correction.maximum_attempts != 2
        or correction.action is not correction_contracts.CorrectionAction.RETRY_IMPLEMENTATION
        or correction.requires_new_manifest_approval
        or correction.requires_new_manifest_digest
        or not _status_correction_matches(status, correction)
    ):
        raise ui_contracts.AgentUiError("run_unavailable")
    approval = correction_contracts.CorrectionApproval.model_validate(
        store.read_json(_CORRECTION_APPROVAL_ATTEMPT_TWO_PATH)
    )
    expected = correction_contracts.CorrectionController.approve_retry(
        correction,
        presented_correction_id=approval.correction_id,
        reviewer=approval.reviewer,
        comment=approval.comment,
    )
    if approval != expected:
        raise ui_contracts.AgentUiError("run_unavailable")
    return correction, approval


def _correction_view(
    store: ArtifactStore,
    status: AgentRunStatus,
    *,
    model_paths: RunArtifactPaths,
    snapshots: Mapping[int, run_query.VerifiedRunSnapshot],
) -> ui_contracts.AgentCorrectionView | None:
    """Project the latest typed correction while retaining attempt-two approval evidence."""

    try:
        attempt_one_payload = store.read_json(_CORRECTION_REQUEST_ATTEMPT_ONE_PATH)
    except FileNotFoundError:
        if status.execution_attempt == 2:
            raise ui_contracts.AgentUiError("run_unavailable") from None
        return None
    attempt_one = correction_contracts.CorrectionRequest.model_validate(attempt_one_payload)
    if attempt_one.completed_attempt != 1 or attempt_one.maximum_attempts != 2:
        raise ui_contracts.AgentUiError("run_unavailable")

    correction = attempt_one
    if status.execution_attempt == 2:
        if (
            attempt_one.action is not correction_contracts.CorrectionAction.RETRY_IMPLEMENTATION
            or attempt_one.next_attempt != 2
            or attempt_one.requires_new_manifest_approval
            or attempt_one.requires_new_manifest_digest
        ):
            raise ui_contracts.AgentUiError("run_unavailable")
        try:
            attempt_two_payload = store.read_json("control/correction-request-attempt-2.json")
        except FileNotFoundError:
            # A controlled Engineer/Validator failure can stop before attempt two has
            # a domain_contracts.ValidationReport and therefore before a terminal correction exists.
            # In that case the attempt-one authorization remains the latest typed
            # correction evidence, but it can never become available for a third run.
            if status.status not in {"failed", "decision_required"} and not (
                _is_authorized_retry_continuation_status(status)
            ):
                raise ui_contracts.AgentUiError("run_unavailable") from None
        else:
            correction = correction_contracts.CorrectionRequest.model_validate(attempt_two_payload)
            if correction.completed_attempt != 2 or correction.maximum_attempts != 2:
                raise ui_contracts.AgentUiError("run_unavailable")

    if (
        correction.request_id != status.request_id
        or correction.manifest_id != status.manifest_id
        or correction.manifest_digest != status.manifest_digest
    ):
        raise ui_contracts.AgentUiError("run_unavailable")

    if correction.completed_attempt == status.execution_attempt:
        if not _status_correction_matches(status, correction):
            raise ui_contracts.AgentUiError("run_unavailable")
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
        raise ui_contracts.AgentUiError("run_unavailable")

    approval: correction_contracts.CorrectionApproval | None
    try:
        approval_payload = store.read_json(_CORRECTION_APPROVAL_ATTEMPT_TWO_PATH)
    except FileNotFoundError:
        approval = None
    else:
        approval = correction_contracts.CorrectionApproval.model_validate(approval_payload)
        if attempt_one.action is not correction_contracts.CorrectionAction.RETRY_IMPLEMENTATION:
            raise ui_contracts.AgentUiError("run_unavailable")
        expected = correction_contracts.CorrectionController.approve_retry(
            attempt_one,
            presented_correction_id=approval.correction_id,
            reviewer=approval.reviewer,
            comment=approval.comment,
        )
        if approval != expected:
            raise ui_contracts.AgentUiError("run_unavailable")

    if status.execution_attempt == 2 and approval is None:
        raise ui_contracts.AgentUiError("run_unavailable")
    retry_available = (
        status.execution_attempt == 1
        and status.status == "completed"
        and status.terminal_disposition == "recoverable_failure"
        and correction.action is correction_contracts.CorrectionAction.RETRY_IMPLEMENTATION
    ) or _is_authorized_retry_continuation_status(status)
    if retry_available and status.execution_attempt == 1:
        verified = _verified_retry_request(store, status)
        if verified != attempt_one:
            raise ui_contracts.AgentUiError("run_unavailable")
    elif retry_available:
        verified, verified_approval = _verified_authorized_retry_continuation(store, status)
        if verified != attempt_one or verified_approval != approval:
            raise ui_contracts.AgentUiError("run_unavailable")

    snapshot = snapshots.get(correction.completed_attempt)
    if snapshot is not None:
        report = snapshot.report
    else:
        try:
            report = domain_contracts.ValidationReport.model_validate(
                store.read_json(model_paths.report(correction.completed_attempt))
            )
        except FileNotFoundError:
            raise ui_contracts.AgentUiError("run_unavailable") from None
    if correction.report_digest != artifact_digest(report):
        raise ui_contracts.AgentUiError("run_unavailable")
    projected_failure_ids = correction_contracts.correction_failure_ids(report)
    if not set(projected_failure_ids).issubset(correction.failed_check_ids):
        raise ui_contracts.AgentUiError("run_unavailable")

    return ui_contracts.AgentCorrectionView(
        correction_id=correction.correction_id,
        completed_attempt=correction.completed_attempt,
        authorized_attempt=correction.next_attempt,
        action=correction.action,
        requires_graph_regeneration=correction.requires_graph_regeneration,
        failed_check_ids=projected_failure_ids,
        reason=correction.reason,
        retry_available=retry_available,
        approval=(
            None
            if approval is None
            else ui_contracts.AgentCorrectionApprovalView(
                correction_id=approval.correction_id,
                reviewer=approval.reviewer,
                comment=approval.comment,
                authorized_attempt=2,
            )
        ),
    )


def _status_correction_matches(
    status: AgentRunStatus,
    correction: correction_contracts.CorrectionRequest,
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
        and summary.requires_graph_regeneration == correction.requires_graph_regeneration
    )


def _attempt_history(
    store: ArtifactStore,
    model_paths: RunArtifactPaths,
    manifest: domain_contracts.MigrationManifest,
    status: AgentRunStatus,
    snapshots: Mapping[int, run_query.VerifiedRunSnapshot],
) -> tuple[ui_contracts.AgentAttemptView, ...]:
    history: list[ui_contracts.AgentAttemptView] = []
    for attempt in range(1, status.execution_attempt + 1):
        snapshot = snapshots.get(attempt)
        if snapshot is not None:
            engineer = snapshot.engineer
            report = snapshot.report
            change_set = snapshot.change_set
        else:
            try:
                engineer = model_agents.EngineerRun.model_validate(
                    store.read_json(model_paths.engineer(attempt))
                )
                report = domain_contracts.ValidationReport.model_validate(
                    store.read_json(model_paths.report(attempt))
                )
            except FileNotFoundError:
                continue
            candidate_change_set = engineer.change_set
            if candidate_change_set is None or report.attempt != attempt:
                raise ui_contracts.AgentUiError("run_unavailable")
            change_set = candidate_change_set
            validate_report(report, manifest, change_set)
        projected_failure_ids = correction_contracts.correction_failure_ids(report)
        correction_path = f"control/correction-request-attempt-{attempt}.json"
        try:
            correction_payload = store.read_json(correction_path)
        except FileNotFoundError:
            failed_check_ids = projected_failure_ids
        else:
            correction = correction_contracts.CorrectionRequest.model_validate(correction_payload)
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
                raise ui_contracts.AgentUiError("run_unavailable")
            if not set(projected_failure_ids).issubset(correction.failed_check_ids):
                raise ui_contracts.AgentUiError("run_unavailable")
            failed_check_ids = projected_failure_ids
        dependent_check_ids = correction_contracts.validation_failure_dependencies(report).keys()
        diagnostic_ids = tuple(
            dict.fromkeys(
                diagnostic_id
                for result in report.results
                if (result.status.value != "passed" and result.check_id not in dependent_check_ids)
                for diagnostic_id in result.diagnostic_ids
            )
        )
        history.append(
            ui_contracts.AgentAttemptView(
                attempt=attempt,
                disposition=report.disposition.value,
                failed_check_ids=failed_check_ids,
                diagnostic_ids=diagnostic_ids,
            )
        )
    return tuple(history)


def _prior_attempt_model_calls(
    store: ArtifactStore,
    model_paths: RunArtifactPaths,
    manifest: domain_contracts.MigrationManifest,
    status: AgentRunStatus,
    snapshots: Mapping[int, run_query.VerifiedRunSnapshot],
) -> tuple[tuple[ModelCallRecord, Literal[1]], ...]:
    if status.execution_attempt != 2:
        return ()
    snapshot = snapshots.get(1)
    if snapshot is None:
        raise ui_contracts.AgentUiError("run_unavailable")
    engineer = snapshot.engineer
    report = snapshot.report
    assessment = snapshot.validator
    correction = correction_contracts.CorrectionRequest.model_validate(
        store.read_json(_CORRECTION_REQUEST_ATTEMPT_ONE_PATH)
    )
    change_set = snapshot.change_set
    if (
        report.disposition.value != "recoverable_failure"
        or correction.action is not correction_contracts.CorrectionAction.RETRY_IMPLEMENTATION
        or correction.completed_attempt != 1
        or correction.next_attempt != 2
        or correction.manifest_digest != artifact_digest(manifest)
        or correction.report_digest != artifact_digest(report)
        or correction.change_set_digest != artifact_digest(change_set)
    ):
        raise ui_contracts.AgentUiError("run_unavailable")
    if assessment.model_call is None:
        return ((engineer.model_call, 1),)
    return ((engineer.model_call, 1), (assessment.model_call, 1))


def _human_decision_view(
    store: ArtifactStore,
    status: AgentRunStatus,
    manifest: domain_contracts.MigrationManifest,
) -> ui_contracts.AgentHumanDecisionView | None:
    try:
        payload = store.read_json(_MANIFEST_APPROVAL_PATH)
    except FileNotFoundError:
        if status.status == "awaiting_approval":
            return None
        if status.status == "decision_required" and manifest.status.value == "decision_required":
            return None
        raise ui_contracts.AgentUiError("run_unavailable") from None
    approval = ManifestApproval.model_validate(payload)
    if (
        approval.request_id != manifest.request_id
        or approval.manifest_id != manifest.manifest_id
        or approval.manifest_digest != artifact_digest(manifest)
    ):
        raise ui_contracts.AgentUiError("run_unavailable")
    if approval.selection not in {"approve", "reject", "modify"}:
        raise ui_contracts.AgentUiError("run_unavailable")
    return ui_contracts.AgentHumanDecisionView(
        selection=cast(Literal["approve", "reject", "modify"], approval.selection),
        reviewer=approval.reviewer,
        comment=approval.comment,
        manifest_digest=approval.manifest_digest,
    )


def _model_call_view(call: ModelCallRecord, *, attempt: int) -> ui_contracts.AgentModelCallView:
    role = call.agent_version.partition("/")[0]
    if role not in {"architect", "engineer", "validator"}:
        raise ui_contracts.AgentUiError("run_unavailable")
    typed_role = cast(Literal["architect", "engineer", "validator"], role)
    usage = call.usage
    return ui_contracts.AgentModelCallView(
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
    architect: model_agents.ArchitectRun,
    status: AgentRunStatus,
) -> ui_contracts.AgentPlanningDecisionView | None:
    manifest = architect.proposal.manifest
    if manifest.status.value != "decision_required":
        return None
    if (
        status.status != "decision_required"
        or status.terminal_disposition != "decision_required"
        or status.interrupt is not None
    ):
        raise ui_contracts.AgentUiError("run_unavailable")
    return ui_contracts.AgentPlanningDecisionView(
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
    intervention: domain_contracts.ImplementationIntervention,
) -> ui_contracts.AgentInterventionView:
    return ui_contracts.AgentInterventionView(
        intervention_id=intervention.intervention_id,
        requested_action=intervention.requested_action.value,
        reason=intervention.reason,
        affected_paths=intervention.affected_paths,
        public_concerns=intervention.public_concerns,
        recommendation=intervention.recommendation,
    )


def _planning_intervention_view(
    intervention: domain_contracts.PlanningIntervention,
) -> ui_contracts.AgentInterventionView:
    return ui_contracts.AgentInterventionView(
        intervention_id=intervention.intervention_id,
        requested_action=intervention.requested_action.value,
        reason=intervention.reason,
        affected_paths=intervention.affected_paths,
        public_concerns=tuple(item.summary for item in intervention.evidence),
        recommendation=intervention.recommendation,
    )


def _failure_view(status: AgentRunStatus) -> ui_contracts.AgentFailureView | None:
    failure = status.failure
    if failure is None:
        return None
    reason_code = _resolved_failure_reason(failure)
    details = _FAILURE_DETAILS[reason_code]
    return ui_contracts.AgentFailureView(
        operation=failure.operation,
        seam=failure.seam,
        category=failure.category,
        reason_code=reason_code,
        phase=details[0],
        response_received=details[1],
        schema_valid=details[2],
        policy_valid=details[3],
        attempt=failure.attempt,
        summary=failure.summary or details[4],
        guidance=failure.guidance or details[5],
    )


def _resolved_failure_reason(failure: AgentRunFailure) -> ui_contracts.AgentFailureReasonCode:
    if failure.reason_code is not None:
        return failure.reason_code
    if failure.seam == "deterministic_validator":
        return "deterministic_validation_failed"
    legacy_reasons: dict[str, ui_contracts.AgentFailureReasonCode] = {
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
) -> tuple[ui_contracts.AgentStageView, ...]:
    projected_failure = _failure_view(status)
    architect_detail = (
        projected_failure.summary
        if projected_failure is not None
        else "Controller preflight stopped before Architect model invocation and requested a decision."
    )
    return (
        ui_contracts.AgentStageView(
            key="architect",
            label="Controller → Architect",
            state="blocked",
            detail=architect_detail,
        ),
        ui_contracts.AgentStageView(
            key="approval",
            label="Manifest approval",
            state="blocked",
            detail="No migration manifest was available for approval.",
        ),
        ui_contracts.AgentStageView(
            key="engineer",
            label="Engineer",
            state="blocked",
            detail="Engineer was not invoked without an approved manifest.",
        ),
        ui_contracts.AgentStageView(
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
    candidate: ui_contracts.AgentCandidateView | None,
    planning_decision: ui_contracts.AgentPlanningDecisionView | None,
) -> tuple[ui_contracts.AgentStageView, ...]:
    architecture = ui_contracts.AgentStageView(
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
            ui_contracts.AgentStageView(
                key="approval",
                label="Manifest approval",
                state="active",
                detail="Review the exact bounded manifest before candidate creation.",
            ),
            ui_contracts.AgentStageView(
                key="engineer",
                label="Engineer",
                state="pending",
                detail="No Engineer model output exists before approval.",
            ),
            ui_contracts.AgentStageView(
                key="validator",
                label="Validator",
                state="pending",
                detail="Validation waits for an approved migration candidate.",
            ),
        )
    if status.status == "rejected":
        return (
            architecture,
            ui_contracts.AgentStageView(
                key="approval",
                label="Manifest approval",
                state="complete",
                detail="The reviewer rejected this manifest.",
            ),
            ui_contracts.AgentStageView(
                key="engineer",
                label="Engineer",
                state="blocked",
                detail="Rejection stopped the workflow before Engineer invocation.",
            ),
            ui_contracts.AgentStageView(
                key="validator",
                label="Validator",
                state="blocked",
                detail="No candidate existed to validate.",
            ),
        )
    if status.status == "decision_required":
        if planning_decision is not None:
            return (
                ui_contracts.AgentStageView(
                    key="architect",
                    label="Controller → Architect",
                    state="complete",
                    detail=(
                        "Architect returned a semantic recommendation with a material risk; "
                        "Controller expanded it into a digest-bound decision-required manifest."
                    ),
                ),
                ui_contracts.AgentStageView(
                    key="approval",
                    label="Manifest approval",
                    state="blocked",
                    detail="The approve/reject manifest gate did not open for this plan status.",
                ),
                ui_contracts.AgentStageView(
                    key="engineer",
                    label="Engineer",
                    state="blocked",
                    detail="Engineer was not invoked for a decision-required Architect plan.",
                ),
                ui_contracts.AgentStageView(
                    key="validator",
                    label="Validator",
                    state="blocked",
                    detail="Validator was not invoked because no candidate exists.",
                ),
            )
        return (
            architecture,
            ui_contracts.AgentStageView(
                key="approval",
                label="Manifest approval",
                state="complete",
                detail="The reviewer approved the exact persisted manifest.",
            ),
            ui_contracts.AgentStageView(
                key="engineer",
                label="Engineer",
                state="blocked",
                detail=(
                    "Engineer stopped without writing files and requested a new human "
                    "scope or evidence decision."
                ),
            ),
            ui_contracts.AgentStageView(
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
            ui_contracts.AgentStageView(
                key="approval",
                label="Manifest approval",
                state="complete",
                detail="The reviewer approved the exact persisted manifest.",
            ),
            ui_contracts.AgentStageView(
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
            ui_contracts.AgentStageView(
                key="validator",
                label="Validator",
                state="blocked",
                detail=validator_detail,
            ),
        )
    if status.status == "completed" and candidate is not None:
        return (
            architecture,
            ui_contracts.AgentStageView(
                key="approval",
                label="Manifest approval",
                state="complete",
                detail="The reviewer approved the exact persisted manifest.",
            ),
            ui_contracts.AgentStageView(
                key="engineer",
                label="Engineer",
                state="complete",
                detail=(
                    f"Attempt {status.execution_attempt} created "
                    f"{len(candidate.changed_paths)} candidate file(s) in an isolated workspace."
                ),
            ),
            ui_contracts.AgentStageView(
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
        ui_contracts.AgentStageView(
            key="approval",
            label="Manifest approval",
            state="complete",
            detail="The manifest gate has a terminal decision.",
        ),
        ui_contracts.AgentStageView(
            key="engineer",
            label="Engineer",
            state="blocked",
            detail="The workflow did not produce a verified migration candidate.",
        ),
        ui_contracts.AgentStageView(
            key="validator",
            label="Validator",
            state="blocked",
            detail="No completed validation evidence is available.",
        ),
    )


__all__ = ["RunViewProjector", "is_verified_pre_manifest_terminal_without_model_record"]
