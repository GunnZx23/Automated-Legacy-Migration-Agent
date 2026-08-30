from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from legacy_migration_agent.agent_runtime.agent_definitions import load_agent_registry
from legacy_migration_agent.agent_runtime.correction import (
    CorrectionAttemptEvidence,
    CorrectionController,
    CorrectionRequest,
)
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectContext,
    ArchitectManifestProposal,
    ArchitectModelContext,
    ArchitectRiskObservation,
    ArchitectSemanticDecision,
    EngineerCorrectionAuthority,
    EngineerCorrectionContext,
    EngineerCorrectionProviderContext,
    EngineerFilePlan,
    EngineerFileUpdate,
    EngineerInterventionOutcome,
    EngineerModelOutcome,
    EngineerRun,
    EngineerWorkspaceContext,
    SourceFileEvidence,
    ValidatorAssessment,
    ValidatorModelAdvisory,
)
from legacy_migration_agent.agent_runtime.model_workflow import (
    ModelAgentWorkflowRoles,
    ModelWorkflowIntegrationError,
    RejectedArchitectModelCallReceipt,
    SanitizedModelPolicyError,
    _safe_role_artifact_persistence_diagnostics,
    _sanitized_role_policy_error,
    filesystem_workspace_factory,
)
from legacy_migration_agent.contracts import (
    ApprovalAction,
    CheckResult,
    CheckStatus,
    EnvironmentKind,
    ImplementationIntervention,
    ImplementationInterventionEvidence,
    ManifestStatus,
    MigrationManifest,
    MigrationRequest,
    MigrationTarget,
    PlanningIntervention,
    PlanningInterventionEvidence,
    PlanningInterventionOption,
    Platform,
    RiskCategory,
    ToolReceipt,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import ArtifactStore, artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.run_session import PortableEvidencePolicyViolation
from legacy_migration_agent.core.scope_policy import MigrationScopePolicy, PlatformAdapter
from legacy_migration_agent.core.workspace import content_revision
from legacy_migration_agent.graphs.dependency_graph import build_salesforce_dependency_graph
from legacy_migration_agent.knowledge.wiki import LlmWiki, RetrievalTrace
from legacy_migration_agent.platforms.local_checks import (
    LWC_CSS_PATH,
    LWC_HTML_PATH,
    LWC_JAVASCRIPT_PATH,
    LWC_TEST_PATH,
    SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID,
    SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID,
)
from legacy_migration_agent.workflow import (
    ApprovalSelection,
    ManifestApproval,
    WorkflowStatus,
)


def test_architect_unresolved_question_failure_has_precise_safe_policy_code() -> None:
    sanitized = _sanitized_role_policy_error(
        "architect",
        PolicyViolation("Architect unresolved questions require a material human-decision risk"),
    )

    assert sanitized.role == "architect"
    assert sanitized.reason_code == "unresolved_question_risk_missing"
    assert str(sanitized) == (
        "model_role_policy_failure:architect:unresolved_question_risk_missing"
    )


def test_role_persistence_diagnostics_expose_only_fixed_classifier_tokens() -> None:
    error = PortableEvidencePolicyViolation(
        "portable evidence contains a local absolute path",
        evidence_category="local_absolute_path",
        field_class="source_bearing",
    )

    assert _safe_role_artifact_persistence_diagnostics(error) == (
        "local_absolute_path",
        "source_bearing",
    )
    assert _safe_role_artifact_persistence_diagnostics(PolicyViolation("arbitrary")) == (
        "unknown",
        "unknown",
    )


PROJECT_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = PROJECT_ROOT / "fixtures/salesforce/account-contact-explorer/input"
VF_ENTRY = "force-app/main/default/pages/LegacyAccountContactExplorer.page"
OUTPUT_PATH = LWC_JAVASCRIPT_PATH
CORRECTION_SIGNAL = "salesforce_lwc_javascript_contract"
CORRECTION_QUERY = f"{CORRECTION_SIGNAL} salesforce correction validation"


def is_engineer_output_type(output_type: type[BaseModel]) -> bool:
    return issubclass(output_type, EngineerModelOutcome)


def engineer_provider_context(
    input_value: BaseModel,
) -> EngineerWorkspaceContext | EngineerCorrectionProviderContext:
    if isinstance(input_value, EngineerCorrectionProviderContext):
        return EngineerCorrectionProviderContext.model_validate(input_value)
    return EngineerWorkspaceContext.model_validate(input_value)


class RoleDispatchModel:
    provider = "offline-structured-test"
    model_id = "three-agent-fixture/v1"
    live_invocation = False
    store_false_sent = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        self.calls.append(output_type.__name__)
        if issubclass(output_type, ArchitectManifestProposal):
            context = ArchitectModelContext.model_validate(input_value)
            return ArchitectManifestProposal(
                semantic_decisions=(
                    ArchitectSemanticDecision(
                        decision_id="additive-lwc-controller",
                        category="target_architecture",
                        summary=("Add one manifest-scoped LWC controller beside the legacy page."),
                        evidence_ids=(
                            context.dependency_graph.nodes[0].node_id,
                            context.wiki_trace.hits[0].page_id,
                        ),
                    ),
                ),
                cited_graph_nodes=(context.dependency_graph.nodes[0].node_id,),
                cited_wiki_pages=(context.wiki_trace.hits[0].page_id,),
            )
        if is_engineer_output_type(output_type):
            context = engineer_provider_context(input_value)
            plan = EngineerFilePlan(
                updates=(
                    EngineerFileUpdate(
                        path=OUTPUT_PATH,
                        content=(
                            "import { LightningElement } from 'lwc';\n"
                            "export default class ModelDemo extends LightningElement "
                            f"{{ static attempt = {context.attempt}; }}\n"
                        ),
                    ),
                ),
                assumptions=("The frozen fixture is the complete approved unit.",),
            )
            return EngineerModelOutcome.for_file_plan(plan)
        if output_type is ValidatorModelAdvisory:
            evidence = input_value.evidence  # type: ignore[attr-defined]
            return ValidatorModelAdvisory(
                manifest_digest=input_value.manifest_digest,  # type: ignore[attr-defined]
                change_set_digest=evidence.change_set_digest,
                report_digest=evidence.report_digest,
                assessment="supports_report",
                summary="The immutable deterministic check passed terminally.",
                cited_check_ids=("bounded-local-check",),
                cited_receipt_digests=(evidence.receipt_bindings[0].receipt_digest,),
                advisory_only=True,
            )
        raise AssertionError(f"unexpected structured output: {output_type}")


class CorrectionCapturingRoleDispatchModel(RoleDispatchModel):
    def __init__(self) -> None:
        super().__init__()
        self.engineer_contexts: list[
            EngineerWorkspaceContext | EngineerCorrectionProviderContext
        ] = []

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        if is_engineer_output_type(output_type):
            self.engineer_contexts.append(engineer_provider_context(input_value))
        return super().parse(
            system_prompt=system_prompt,
            input_value=input_value,
            output_type=output_type,
        )


class MixedJestCorrectionRoleDispatchModel(CorrectionCapturingRoleDispatchModel):
    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        if not is_engineer_output_type(output_type):
            return super().parse(
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )
        context = engineer_provider_context(input_value)
        self.engineer_contexts.append(context)
        self.calls.append(output_type.__name__)
        paths = (
            (LWC_JAVASCRIPT_PATH, LWC_TEST_PATH)
            if context.attempt == 2
            else (LWC_JAVASCRIPT_PATH, LWC_HTML_PATH, LWC_CSS_PATH, LWC_TEST_PATH)
        )
        plan = EngineerFilePlan(
            updates=tuple(
                EngineerFileUpdate(path=path, content=f"attempt-{context.attempt}: {path}\n")
                for path in paths
            ),
            assumptions=("The frozen fixture is the complete approved unit.",),
        )
        return EngineerModelOutcome.for_file_plan(plan)


class AttemptTwoUnapprovedPathModel(RoleDispatchModel):
    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        if is_engineer_output_type(output_type):
            context = engineer_provider_context(input_value)
            if context.attempt == 2:
                self.calls.append(output_type.__name__)
                outcome = EngineerModelOutcome.for_file_plan(
                    EngineerFilePlan(
                        updates=(
                            EngineerFileUpdate(
                                path="outside-approved-scope.js",
                                content="export default 'unauthorized';\n",
                            ),
                        )
                    )
                )
                return outcome
        return super().parse(
            system_prompt=system_prompt,
            input_value=input_value,
            output_type=output_type,
        )


class AttemptTwoInvalidValidatorModel(RoleDispatchModel):
    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        if output_type is ValidatorModelAdvisory:
            evidence = input_value.evidence  # type: ignore[attr-defined]
            if evidence.report.attempt == 2:
                self.calls.append(output_type.__name__)
                return ValidatorModelAdvisory(
                    manifest_digest=input_value.manifest_digest,  # type: ignore[attr-defined]
                    change_set_digest=evidence.change_set_digest,
                    report_digest=evidence.report_digest,
                    assessment="raises_concern",
                    summary="Schema-valid advisory cites a check outside frozen evidence.",
                    concerns=("The cited check is not part of the deterministic report.",),
                    cited_check_ids=("unknown-validator-check",),
                    cited_receipt_digests=(),
                    advisory_only=True,
                )
        return super().parse(
            system_prompt=system_prompt,
            input_value=input_value,
            output_type=output_type,
        )


class DecisionRequiredRoleDispatchModel(RoleDispatchModel):
    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        output = super().parse(
            system_prompt=system_prompt,
            input_value=input_value,
            output_type=output_type,
        )
        if not issubclass(output_type, ArchitectManifestProposal):
            return output
        proposal = ArchitectManifestProposal.model_validate(output)
        context = ArchitectModelContext.model_validate(input_value)
        return proposal.model_copy(
            update={
                "risk_observations": (
                    ArchitectRiskObservation(
                        category=RiskCategory.INCOMPLETE_EVIDENCE,
                        summary="A material planning decision requires human review.",
                        evidence_ids=(context.wiki_trace.hits[0].page_id,),
                        requires_human_decision=True,
                    ),
                ),
                "unresolved_questions": ("Should the bounded risk be accepted?",),
            }
        )


class EngineerStopRoleDispatchModel(RoleDispatchModel):
    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        if not issubclass(output_type, EngineerModelOutcome):
            return super().parse(
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )
        self.calls.append(output_type.__name__)
        context = EngineerWorkspaceContext.model_validate(input_value)
        intervention = ImplementationIntervention(
            intervention_id="implementation-stop-model-workflow",
            request_id=context.request.request_id,
            request_digest=context.request_digest,
            manifest_id=context.manifest.manifest_id,
            manifest_digest=context.manifest_digest,
            base_revision=context.workspace_base_revision,
            agent_version=context.agent_version,
            agent_definition_digest=context.agent_definition_digest,
            input_evidence_digest=context.input_evidence_digest,
            reason="The frozen source omits a required public contract.",
            requested_action=ApprovalAction.EXPAND_SCOPE,
            affected_paths=(OUTPUT_PATH,),
            public_concerns=("Generating the approved output would require an unsupported guess.",),
            evidence=(
                ImplementationInterventionEvidence(
                    source="engineer_input",
                    source_digest=context.input_evidence_digest,
                    summary="The complete frozen implementation input lacks the contract.",
                    affected_paths=(OUTPUT_PATH,),
                ),
            ),
            options=(
                PlanningInterventionOption.EXPAND_SCOPE_AND_REPLAN,
                PlanningInterventionOption.STOP_REQUEST,
            ),
            recommendation="Resolve the evidence gap and produce a revised manifest.",
        )
        return EngineerModelOutcome(
            result=EngineerInterventionOutcome(
                kind="decision_required",
                intervention=intervention,
            )
        )


def request() -> MigrationRequest:
    revision = content_revision(SOURCE_ROOT)
    return MigrationRequest(
        request_id="request-model-workflow",
        platform=Platform.SALESFORCE,
        repository="fixtures/salesforce/account-contact-explorer/input",
        base_revision=revision,
        target=MigrationTarget(
            entry_path=VF_ENTRY,
            target_runtime="Lightning Web Components with Apex",
            source_version="Salesforce API 67.0",
            target_version="Salesforce API 67.0",
            description="Exercise the three model-backed roles in LangGraph.",
        ),
        allowed_environment=EnvironmentKind.LOCAL,
    )


def architect_context(migration_request: MigrationRequest) -> ArchitectContext:
    graph = build_salesforce_dependency_graph(
        SOURCE_ROOT,
        (VF_ENTRY,),
        migration_request.base_revision,
    )
    wiki = LlmWiki.load(PROJECT_ROOT / "knowledge/wiki").search(
        "Visualforce to LWC migration security Jest",
        platform=Platform.SALESFORCE,
        source_version=migration_request.target.source_version,
        target_version=migration_request.target.target_version,
        as_of=date(2026, 8, 29),
    )
    policy = MigrationScopePolicy(
        policy_id="model-workflow-salesforce-policy",
        platform=Platform.SALESFORCE,
        required_source_input_paths=(VF_ENTRY,),
        approved_output_paths=(OUTPUT_PATH,),
        forbidden_paths=(VF_ENTRY, "private/reference-output"),
        allowed_validation_command_ids=("bounded-local-check",),
        required_validation_command_ids=("bounded-local-check",),
        max_changed_files=1,
        required_approval_actions=(ApprovalAction.APPROVE_MANIFEST,),
    )
    source_bytes = (SOURCE_ROOT / VF_ENTRY).read_bytes()
    return ArchitectContext(
        model_context=ArchitectModelContext(
            request=migration_request,
            dependency_graph=graph,
            dependency_graph_digest=artifact_digest(graph),
            source_files=(
                SourceFileEvidence(
                    path=VF_ENTRY,
                    sha256=f"sha256:{hashlib.sha256(source_bytes).hexdigest()}",
                    content=source_bytes.decode("utf-8"),
                ),
            ),
            wiki_trace=wiki,
            wiki_trace_digest=artifact_digest(wiki),
        ),
        platform_adapter=PlatformAdapter.bind(
            adapter_id="model-workflow-salesforce-adapter",
            policy=policy,
        ),
    )


def mixed_jest_architect_context(migration_request: MigrationRequest) -> ArchitectContext:
    context = architect_context(migration_request)
    output_paths = (LWC_JAVASCRIPT_PATH, LWC_HTML_PATH, LWC_CSS_PATH, LWC_TEST_PATH)
    policy = MigrationScopePolicy(
        policy_id="model-workflow-mixed-jest-policy",
        platform=Platform.SALESFORCE,
        required_source_input_paths=(VF_ENTRY,),
        approved_output_paths=output_paths,
        forbidden_paths=(VF_ENTRY, "private/reference-output"),
        allowed_validation_command_ids=(
            "salesforce-lwc-jest",
            "salesforce-lwc-controller-jest",
        ),
        required_validation_command_ids=(
            "salesforce-lwc-jest",
            "salesforce-lwc-controller-jest",
        ),
        max_changed_files=len(output_paths),
        required_approval_actions=(ApprovalAction.APPROVE_MANIFEST,),
    )
    return context.model_copy(
        update={
            "platform_adapter": PlatformAdapter.bind(
                adapter_id="model-workflow-mixed-jest-adapter",
                policy=policy,
            )
        }
    )


class CorrectionWikiTestRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, migration_request: MigrationRequest, query: str):
        self.calls.append((migration_request.request_id, query))
        known_signals = {
            CORRECTION_SIGNAL,
            SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID,
            SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID,
        }
        trace = LlmWiki.load(PROJECT_ROOT / "knowledge/wiki").search(
            query,
            platform=migration_request.platform,
            source_version=migration_request.target.source_version,
            target_version=migration_request.target.target_version,
            as_of=date(2026, 8, 29),
            required_exact_ids=tuple(term for term in query.split() if term in known_signals),
        )
        return trace.model_copy(
            update={
                "query": query,
                "normalized_terms": tuple(sorted(query.lower().split())),
            }
        )


class NoHitCorrectionWikiTestRetriever(CorrectionWikiTestRetriever):
    def __call__(self, migration_request: MigrationRequest, query: str):
        self.calls.append((migration_request.request_id, query))
        trace = LlmWiki.load(PROJECT_ROOT / "knowledge/wiki").search(
            "diagnostic_identifier_that_is_not_curated",
            platform=migration_request.platform,
            source_version=migration_request.target.source_version,
            target_version=migration_request.target.target_version,
            as_of=date(2026, 8, 29),
        )
        return trace.model_copy(
            update={
                "query": query,
                "normalized_terms": tuple(sorted(query.lower().split())),
            }
        )


def unresolved_graph_preflight(
    migration_request: MigrationRequest,
) -> PlanningIntervention:
    graph = build_salesforce_dependency_graph(
        SOURCE_ROOT,
        (VF_ENTRY,),
        migration_request.base_revision,
    )
    unresolved_node = graph.nodes[0].model_copy(update={"resolved": False})
    unresolved_graph = graph.model_copy(update={"nodes": (unresolved_node, *graph.nodes[1:])})
    assert unresolved_graph.has_unresolved is True
    return PlanningIntervention(
        intervention_id="preflight-model-workflow-unresolved-graph",
        request_id=migration_request.request_id,
        request_digest=artifact_digest(migration_request),
        platform=migration_request.platform,
        base_revision=migration_request.base_revision,
        reason="The dependency graph is unresolved, so no Architect prompt may be sent.",
        requested_action=ApprovalAction.EXPAND_SCOPE,
        affected_paths=(migration_request.target.entry_path,),
        evidence=(
            PlanningInterventionEvidence(
                category=RiskCategory.DYNAMIC_DEPENDENCY,
                source="dependency_graph",
                source_digest=artifact_digest(unresolved_graph),
                summary="A graph node remains unresolved after static analysis.",
                affected_paths=(migration_request.target.entry_path,),
                unresolved_items=(unresolved_node.node_id,),
            ),
        ),
        options=(
            PlanningInterventionOption.EXPAND_SCOPE_AND_REPLAN,
            PlanningInterventionOption.STOP_REQUEST,
        ),
        recommendation="Resolve the graph evidence and start a new planning run.",
    )


def approval_from_interrupt(paused: object) -> ManifestApproval:
    interrupt_value = paused.interrupts[0].value  # type: ignore[attr-defined]
    decision = interrupt_value["decision"]
    return ManifestApproval(
        decision_id=decision["decision_id"],
        request_id=decision["request_id"],
        manifest_id=interrupt_value["manifest_id"],
        manifest_digest=interrupt_value["manifest_digest"],
        requested_action=decision["requested_action"],
        selection=ApprovalSelection.APPROVE,
        reviewer="model-workflow-reviewer",
    )


def passing_validation_report(
    current_request,
    manifest,
    change_set,
    workspace,
    attempt,
) -> ValidationReport:
    assert (workspace.root / OUTPUT_PATH).is_file()
    now = datetime(2026, 8, 24, tzinfo=UTC)
    receipt = ToolReceipt(
        receipt_id=f"receipt-model-workflow-{attempt}",
        tool_id="bounded-local-check",
        request_id=current_request.request_id,
        run_id="run-model-workflow",
        attempt=attempt,
        base_revision=manifest.base_revision,
        environment=EnvironmentKind.LOCAL,
        input_artifact_digest=artifact_digest(change_set),
        operation="predeclared deterministic fixture check",
        working_directory=".",
        started_at=now,
        ended_at=now,
        exit_code=0,
        terminal=True,
    )
    return ValidationReport(
        report_id=f"report-model-workflow-{attempt}",
        request_id=current_request.request_id,
        manifest_id=manifest.manifest_id,
        change_set_id=change_set.change_set_id,
        base_revision=manifest.base_revision,
        results=(
            CheckResult(
                check_id="bounded-local-check",
                command_id="bounded-local-check",
                required=True,
                status=CheckStatus.PASSED,
                receipt=receipt,
                summary="The deterministic fixture check passed.",
            ),
        ),
        disposition=ValidationDisposition.READY_FOR_HUMAN_REVIEW,
        attempt=attempt,
    )


def recoverable_then_passing_validation_report(
    current_request,
    manifest,
    change_set,
    workspace,
    attempt,
) -> ValidationReport:
    report = passing_validation_report(
        current_request,
        manifest,
        change_set,
        workspace,
        attempt,
    )
    if attempt == 2:
        return report
    result = report.results[0]
    assert result.receipt is not None
    failed = result.model_copy(
        update={
            "status": CheckStatus.FAILED,
            "receipt": result.receipt.model_copy(update={"exit_code": 1}),
            "summary": "The bounded candidate requires one implementation correction.",
            "diagnostic_ids": (CORRECTION_SIGNAL,),
        }
    )
    return report.model_copy(
        update={
            "results": (failed,),
            "disposition": ValidationDisposition.RECOVERABLE_FAILURE,
        }
    )


def mixed_jest_then_passing_validation_report(
    current_request,
    manifest,
    change_set,
    workspace,
    attempt,
) -> ValidationReport:
    del workspace
    now = datetime(2026, 8, 27, tzinfo=UTC)

    def result(
        check_id: str,
        *,
        status: CheckStatus,
        diagnostic_ids: tuple[str, ...] = (),
    ) -> CheckResult:
        return CheckResult(
            check_id=check_id,
            command_id=check_id,
            required=True,
            status=status,
            receipt=ToolReceipt(
                receipt_id=f"receipt-{check_id}-{attempt}",
                tool_id=check_id,
                request_id=current_request.request_id,
                run_id="run-model-workflow-mixed-jest",
                attempt=attempt,
                base_revision=manifest.base_revision,
                environment=EnvironmentKind.LOCAL,
                input_artifact_digest=artifact_digest(change_set),
                operation=check_id,
                working_directory=".",
                started_at=now,
                ended_at=now,
                exit_code=0 if status is CheckStatus.PASSED else 1,
                terminal=True,
            ),
            summary=(
                f"{check_id} passed terminally."
                if status is CheckStatus.PASSED
                else f"{check_id} failed terminally before behavior assertions ran."
            ),
            diagnostic_ids=diagnostic_ids,
        )

    status = CheckStatus.PASSED if attempt == 2 else CheckStatus.FAILED
    results = (
        result(
            "salesforce-lwc-jest",
            status=status,
            diagnostic_ids=(
                ()
                if status is CheckStatus.PASSED
                else (SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID,)
            ),
        ),
        result(
            "salesforce-lwc-controller-jest",
            status=status,
            diagnostic_ids=(),
        ),
    )
    return ValidationReport(
        report_id=f"report-model-workflow-mixed-jest-{attempt}",
        request_id=current_request.request_id,
        manifest_id=manifest.manifest_id,
        change_set_id=change_set.change_set_id,
        base_revision=manifest.base_revision,
        results=results,
        disposition=(
            ValidationDisposition.READY_FOR_HUMAN_REVIEW
            if attempt == 2
            else ValidationDisposition.RECOVERABLE_FAILURE
        ),
        attempt=attempt,
    )


def recoverable_model_attempt_one(
    tmp_path: Path,
    model: RoleDispatchModel,
    *,
    evidence_name: str,
    thread_id: str,
):
    migration_request = request()
    evidence_root = tmp_path / evidence_name
    correction_wiki = CorrectionWikiTestRetriever()
    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=architect_context,
        correction_wiki_retriever=correction_wiki,
        workspace_factory=filesystem_workspace_factory(SOURCE_ROOT, temp_parent=tmp_path),
        deterministic_validator=recoverable_then_passing_validation_report,
        artifact_store=ArtifactStore(evidence_root),
    )
    workflow = roles.build()
    paused = workflow.start(migration_request, thread_id=thread_id)
    first = workflow.resume(approval_from_interrupt(paused), thread_id=thread_id)
    correction = CorrectionRequest.model_validate(first.value["correction_request"])
    approval = CorrectionController.approve_retry(
        correction,
        presented_correction_id=correction.correction_id,
        reviewer="at-most-once-reviewer",
    )
    return roles, workflow, migration_request, first, approval, evidence_root


def complete_role_handoffs(tmp_path: Path):
    migration_request = request()
    model = CorrectionCapturingRoleDispatchModel()
    evidence_root = tmp_path / "evidence"
    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=architect_context,
        workspace_factory=filesystem_workspace_factory(
            SOURCE_ROOT,
            temp_parent=tmp_path,
        ),
        deterministic_validator=passing_validation_report,
        artifact_store=ArtifactStore(evidence_root),
    )
    manifest = roles.architect(migration_request)
    change_set = roles.engineer(migration_request, manifest)
    report = roles.validator(migration_request, manifest, change_set)
    return roles, model, migration_request, manifest, change_set, report, evidence_root


def test_three_markdown_agents_run_inside_langgraph_and_replay_without_model_calls(
    tmp_path: Path,
) -> None:
    migration_request = request()
    source_before = content_revision(SOURCE_ROOT)
    model = CorrectionCapturingRoleDispatchModel()
    validation_calls: list[int] = []

    def deterministic_validator(
        current_request,
        manifest,
        change_set,
        workspace,
        attempt,
    ) -> ValidationReport:
        validation_calls.append(attempt)
        return passing_validation_report(
            current_request,
            manifest,
            change_set,
            workspace,
            attempt,
        )

    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=architect_context,
        workspace_factory=filesystem_workspace_factory(
            SOURCE_ROOT,
            temp_parent=tmp_path,
        ),
        deterministic_validator=deterministic_validator,
        artifact_store=ArtifactStore(tmp_path / "evidence"),
    )
    workflow = roles.build()

    paused = workflow.start(migration_request, thread_id="model-agent-thread")

    assert paused.value["status"] == WorkflowStatus.AWAITING_APPROVAL
    assert model.calls == ["ArchitectManifestProposal"]
    completed = workflow.resume(
        approval_from_interrupt(paused),
        thread_id="model-agent-thread",
    )

    assert completed.value["status"] == WorkflowStatus.COMPLETED
    assert completed.value["terminal_disposition"] is ValidationDisposition.READY_FOR_HUMAN_REVIEW
    assert model.calls == [
        "ArchitectManifestProposal",
        "EngineerModelOutcome",
        "ValidatorModelAdvisory",
    ]
    assert validation_calls == [1]
    assert content_revision(SOURCE_ROOT) == source_before
    assert not (SOURCE_ROOT / OUTPUT_PATH).exists()
    assert len(model.engineer_contexts) == 1
    attempt_one_context = model.engineer_contexts[0]
    persisted_wiki = RetrievalTrace.model_validate_json(
        (tmp_path / "evidence/model-runs/request-model-workflow/wiki-trace.json").read_text(
            encoding="utf-8"
        )
    )
    assert attempt_one_context.attempt == 1
    assert attempt_one_context.correction is None
    assert attempt_one_context.architect_wiki_trace == persisted_wiki
    assert attempt_one_context.architect_wiki_trace_digest == artifact_digest(persisted_wiki)
    persisted_engineer = EngineerRun.model_validate_json(
        (tmp_path / "evidence/model-runs/request-model-workflow/engineer-attempt-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted_engineer.model_call.input_digest == artifact_digest(attempt_one_context)

    # Replaying completed role handoffs uses immutable artifacts. It performs
    # no additional provider call, validation command, or source mutation.
    manifest = completed.value["manifest"]
    change_set = roles.engineer(migration_request, manifest)
    report = roles.validator(migration_request, manifest, change_set)
    assert report == completed.value["validation_report"]
    assert model.calls == [
        "ArchitectManifestProposal",
        "EngineerModelOutcome",
        "ValidatorModelAdvisory",
    ]
    assert validation_calls == [1]
    assert content_revision(SOURCE_ROOT) == source_before

    assert (tmp_path / "evidence/model-runs/request-model-workflow/architect.json").is_file()
    assert (
        tmp_path / "evidence/model-runs/request-model-workflow/architect-context.json"
    ).is_file()
    assert (tmp_path / "evidence/model-runs/request-model-workflow/dependency-graph.json").is_file()
    assert (tmp_path / "evidence/model-runs/request-model-workflow/wiki-trace.json").is_file()
    assert (
        tmp_path / "evidence/model-runs/request-model-workflow/engineer-attempt-1.json"
    ).is_file()
    assert (
        tmp_path / "evidence/model-runs/request-model-workflow/validator-attempt-1.json"
    ).is_file()


def test_architect_policy_rejection_persists_safe_call_receipt_and_replays_once(
    tmp_path: Path,
) -> None:
    marker = "unbound-authored-evidence-marker"

    class RejectedArchitectModel(RoleDispatchModel):
        def parse(
            self,
            *,
            system_prompt: str,
            input_value: BaseModel,
            output_type: type[BaseModel],
        ) -> BaseModel:
            output = super().parse(
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )
            if issubclass(output_type, ArchitectManifestProposal):
                proposal = ArchitectManifestProposal.model_validate(output)
                return proposal.model_copy(update={"cited_wiki_pages": (marker,)})
            return output

    migration_request = request()
    model = RejectedArchitectModel()
    evidence_root = tmp_path / "rejected-architect-evidence"
    store = ArtifactStore(evidence_root)
    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=architect_context,
        workspace_factory=filesystem_workspace_factory(SOURCE_ROOT, temp_parent=tmp_path),
        deterministic_validator=passing_validation_report,
        artifact_store=store,
    )

    with pytest.raises(
        SanitizedModelPolicyError,
        match="model_role_policy_failure:architect:policy_rejected",
    ):
        roles.architect(migration_request)

    receipt_path = (
        evidence_root / "model-runs/request-model-workflow/architect-policy-rejection.json"
    )
    architect_path = evidence_root / "model-runs/request-model-workflow/architect.json"
    receipt = RejectedArchitectModelCallReceipt.model_validate_json(
        receipt_path.read_text(encoding="utf-8")
    )
    serialized = receipt_path.read_text(encoding="utf-8")
    assert receipt.receipt_kind == "architect_controller_policy_rejection"
    assert receipt.request_digest == artifact_digest(migration_request)
    assert receipt.model_call.input_digest == artifact_digest(
        architect_context(migration_request).model_context
    )
    assert receipt.provider_response_received is True
    assert receipt.structured_output_schema_valid is True
    assert receipt.controller_policy_valid is False
    assert receipt.accepted_role_artifact_persisted is False
    assert receipt.downstream_authority_granted is False
    assert not architect_path.exists()
    assert marker not in serialized
    assert VF_ENTRY not in serialized
    calls_after_rejection = tuple(model.calls)

    with pytest.raises(
        SanitizedModelPolicyError,
        match="model_role_policy_failure:architect:policy_rejected",
    ):
        roles.architect(migration_request)
    assert tuple(model.calls) == calls_after_rejection

    with pytest.raises(PolicyViolation, match="immutable artifact"):
        store.write_json(
            "model-runs/request-model-workflow/architect-policy-rejection.json",
            receipt.model_copy(update={"reason_code": "required_approval_missing"}),
        )

    payload = json.loads(serialized)
    payload["model_call"]["input_digest"] = "sha256:" + "0" * 64
    receipt_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(ModelWorkflowIntegrationError, match="exact model input"):
        roles.architect(migration_request)
    assert tuple(model.calls) == calls_after_rejection


def test_attempt_two_uses_persisted_safe_correction_and_replays_without_model_calls(
    tmp_path: Path,
) -> None:
    migration_request = request()
    source_before = content_revision(SOURCE_ROOT)
    model = CorrectionCapturingRoleDispatchModel()
    correction_wiki = CorrectionWikiTestRetriever()
    evidence_root = tmp_path / "correction-evidence"
    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=architect_context,
        correction_wiki_retriever=correction_wiki,
        workspace_factory=filesystem_workspace_factory(SOURCE_ROOT, temp_parent=tmp_path),
        deterministic_validator=recoverable_then_passing_validation_report,
        artifact_store=ArtifactStore(evidence_root),
    )
    workflow = roles.build()
    paused = workflow.start(migration_request, thread_id="model-correction-thread")
    first = workflow.resume(
        approval_from_interrupt(paused),
        thread_id="model-correction-thread",
    )
    assert first.value["terminal_disposition"] is ValidationDisposition.RECOVERABLE_FAILURE
    first_assessment = ValidatorAssessment.model_validate_json(
        (evidence_root / "model-runs/request-model-workflow/validator-attempt-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert first_assessment.advisory.assessment == "unavailable"
    assert first_assessment.unavailable_receipt is not None
    assert first_assessment.unavailable_receipt.reason_code == "deferred_recoverable_attempt"
    assert first_assessment.unavailable_receipt.attempted is False
    assert model.calls == ["ArchitectManifestProposal", "EngineerModelOutcome"]
    assert not (
        evidence_root
        / "model-runs/request-model-workflow/validator-invocation-lease-attempt-1.json"
    ).exists()
    manifest = first.value["manifest"]
    prior_change_set = first.value["change_set"]
    prior_report = first.value["validation_report"]
    correction_request = CorrectionRequest.model_validate(first.value["correction_request"])
    correction_evidence = CorrectionAttemptEvidence.freeze(
        manifest,
        prior_change_set,
        prior_report,
    )

    calls_before_rejections = tuple(model.calls)
    with pytest.raises(ModelWorkflowIntegrationError, match="requires exact correction"):
        roles.engineer(migration_request, manifest, attempt=2)
    mismatched = correction_evidence.model_copy(update={"evidence_digest": "sha256:" + "0" * 64})
    with pytest.raises((ModelWorkflowIntegrationError, ValueError), match="evidence digest"):
        roles.engineer(
            migration_request,
            manifest,
            attempt=2,
            correction=mismatched,
        )
    assert tuple(model.calls) == calls_before_rejections

    approval = CorrectionController.approve_retry(
        correction_request,
        presented_correction_id=correction_request.correction_id,
        reviewer="private-reviewer",
        comment="This authorization comment must not enter the model prompt.",
    )
    second = workflow.retry_recoverable(
        approval,
        thread_id="model-correction-thread",
    )
    assert second.value["terminal_disposition"] is ValidationDisposition.READY_FOR_HUMAN_REVIEW
    assert correction_wiki.calls == [(migration_request.request_id, CORRECTION_QUERY)]
    assert len(model.engineer_contexts) == 2
    attempt_one, attempt_two = model.engineer_contexts
    assert isinstance(attempt_one, EngineerWorkspaceContext)
    assert isinstance(attempt_two, EngineerCorrectionProviderContext)
    assert attempt_one.attempt == 1
    assert attempt_one.correction is None
    assert attempt_two.attempt == 2
    assert attempt_two.correction is not None
    assert attempt_one.architect_wiki_trace_digest == attempt_two.architect_wiki_trace_digest
    assert attempt_two.correction.correction_wiki_trace.query == CORRECTION_QUERY
    assert attempt_one.input_evidence_digest != attempt_two.controller_input_evidence_digest
    provider_payload = attempt_two.model_dump(mode="json")
    assert "source_files" not in provider_payload
    assert "architect_wiki_trace" not in provider_payload
    assert "prior_file_plan" not in provider_payload["correction"]
    prior_run = EngineerRun.model_validate_json(
        (evidence_root / "model-runs/request-model-workflow/engineer-attempt-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert attempt_two.correction.prior_file_plan_digest == artifact_digest(prior_run.file_plan)
    assert attempt_two.correction.prior_candidate_revision == prior_run.workspace_after_revision
    assert attempt_two.correction.implementation_failure_ids == (
        "bounded-local-check",
        CORRECTION_SIGNAL,
    )

    attempt_two_run_path = (
        evidence_root / "model-runs/request-model-workflow/engineer-attempt-2.json"
    )
    attempt_two_run = EngineerRun.model_validate_json(
        attempt_two_run_path.read_text(encoding="utf-8")
    )
    assert attempt_two_run.correction_delta is not None
    assert attempt_two_run.effective_file_plan is not None
    assert attempt_two_run.file_plan == attempt_two_run.effective_file_plan
    assert "attempt = 2" in attempt_two_run.correction_delta.updates[0].content

    correction_wiki_path = (
        evidence_root / "model-runs/request-model-workflow/correction-wiki-attempt-2.json"
    )
    persisted_wiki = json.loads(correction_wiki_path.read_text(encoding="utf-8"))
    assert persisted_wiki["query"] == CORRECTION_QUERY
    assert persisted_wiki["hits"]

    correction_path = (
        evidence_root / "model-runs/request-model-workflow/engineer-correction-attempt-2.json"
    )
    safe_payload = correction_path.read_text(encoding="utf-8")
    persisted_controller_correction = EngineerCorrectionContext.model_validate_json(safe_payload)
    assert persisted_controller_correction.prior_file_plan == prior_run.file_plan
    persisted_authority = EngineerCorrectionAuthority(
        evidence=correction_evidence,
        model_context=persisted_controller_correction,
    )
    with roles.workspace_factory(migration_request, manifest, 2) as workspace:
        full_attempt_two = roles.engineer_agent.prepare_context(
            migration_request,
            manifest,
            workspace,
            architect_wiki_trace=attempt_one.architect_wiki_trace,
            attempt=2,
            correction_authority=persisted_authority,
        )
    assert attempt_two.controller_input_evidence_digest == full_attempt_two.input_evidence_digest
    for forbidden in (
        "private-reviewer",
        "authorization comment",
        "reviewer",
        "comment",
        "working_directory",
        "started_at",
        "stdout_digest",
        "stderr_digest",
    ):
        assert forbidden not in safe_payload

    engineer_lease = json.loads(
        (
            evidence_root
            / "model-runs/request-model-workflow/engineer-invocation-lease-attempt-2.json"
        ).read_text(encoding="utf-8")
    )
    engineer_attempt_two = json.loads(
        (evidence_root / "model-runs/request-model-workflow/engineer-attempt-2.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        engineer_lease["binding"]["input_evidence_digest"]
        == engineer_attempt_two["model_call"]["input_digest"]
    )
    assert engineer_lease["binding"]["correction_wiki_trace_digest"].startswith("sha256:")
    validator_lease = json.loads(
        (
            evidence_root
            / "model-runs/request-model-workflow/validator-invocation-lease-attempt-2.json"
        ).read_text(encoding="utf-8")
    )
    validator_attempt_two = json.loads(
        (evidence_root / "model-runs/request-model-workflow/validator-attempt-2.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        validator_lease["binding"]["input_evidence_digest"]
        == validator_attempt_two["model_call"]["input_digest"]
    )
    assert validator_lease["binding"]["correction_wiki_trace_digest"].startswith("sha256:")

    exact_evidence = CorrectionAttemptEvidence.model_validate(
        second.value["correction_attempt_evidence"]
    )
    calls_after_attempt_two = tuple(model.calls)
    replayed_change_set = roles.engineer(
        migration_request,
        manifest,
        attempt=2,
        correction=exact_evidence,
    )
    replayed_report = roles.validator(
        migration_request,
        manifest,
        replayed_change_set,
        attempt=2,
        correction=exact_evidence,
    )
    assert replayed_change_set == second.value["change_set"]
    assert replayed_report == second.value["validation_report"]
    assert tuple(model.calls) == calls_after_attempt_two
    assert correction_wiki.calls == [(migration_request.request_id, CORRECTION_QUERY)]

    safe_attempt_two_payload = attempt_two_run_path.read_text(encoding="utf-8")
    tampered_attempt_two = json.loads(safe_attempt_two_payload)
    tampered_attempt_two["effective_file_plan"]["updates"][0]["content"] += "// tampered\n"
    attempt_two_run_path.write_text(json.dumps(tampered_attempt_two) + "\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="effective file plan"):
        roles.engineer(
            migration_request,
            manifest,
            attempt=2,
            correction=exact_evidence,
        )
    attempt_two_run_path.write_text(safe_attempt_two_payload, encoding="utf-8")

    safe_wiki_payload = correction_wiki_path.read_text(encoding="utf-8")
    tampered_wiki = json.loads(safe_wiki_payload)
    tampered_wiki["query"] = "hostile-repair-query"
    correction_wiki_path.write_text(json.dumps(tampered_wiki) + "\n", encoding="utf-8")
    with pytest.raises(ModelWorkflowIntegrationError, match="query differs"):
        roles.engineer(
            migration_request,
            manifest,
            attempt=2,
            correction=exact_evidence,
        )
    correction_wiki_path.write_text(safe_wiki_payload, encoding="utf-8")

    tampered = json.loads(safe_payload)
    tampered["implementation_failure_ids"] = ["hostile-diagnostic"]
    correction_path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    with pytest.raises(ModelWorkflowIntegrationError, match="differs from exact prior evidence"):
        roles.engineer(
            migration_request,
            manifest,
            attempt=2,
            correction=exact_evidence,
        )
    with pytest.raises(ModelWorkflowIntegrationError, match="differs from exact prior evidence"):
        roles.validator(
            migration_request,
            manifest,
            second.value["change_set"],
            attempt=2,
            correction=exact_evidence,
        )
    assert tuple(model.calls) == calls_after_attempt_two
    assert content_revision(SOURCE_ROOT) == source_before


def test_mixed_zero_test_retry_retrieves_wiki_and_dispatches_targeted_delta(
    tmp_path: Path,
) -> None:
    migration_request = request()
    model = MixedJestCorrectionRoleDispatchModel()
    correction_wiki = CorrectionWikiTestRetriever()
    evidence_root = tmp_path / "mixed-jest-correction"
    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=mixed_jest_architect_context,
        correction_wiki_retriever=correction_wiki,
        workspace_factory=filesystem_workspace_factory(SOURCE_ROOT, temp_parent=tmp_path),
        deterministic_validator=mixed_jest_then_passing_validation_report,
        artifact_store=ArtifactStore(evidence_root),
    )
    workflow = roles.build()
    paused = workflow.start(migration_request, thread_id="mixed-jest-correction")
    first = workflow.resume(
        approval_from_interrupt(paused),
        thread_id="mixed-jest-correction",
    )
    assert first.value["terminal_disposition"] is ValidationDisposition.RECOVERABLE_FAILURE
    correction_request = CorrectionRequest.model_validate(first.value["correction_request"])
    approval = CorrectionController.approve_retry(
        correction_request,
        presented_correction_id=correction_request.correction_id,
        reviewer="mixed-jest-reviewer",
    )

    second = workflow.retry_recoverable(
        approval,
        thread_id="mixed-jest-correction",
    )

    expected_signals = (
        SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID,
        SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID,
    )
    expected_query = " ".join((*sorted(expected_signals), "salesforce", "correction", "validation"))
    assert correction_wiki.calls == [(migration_request.request_id, expected_query)]
    assert second.value["terminal_disposition"] is ValidationDisposition.READY_FOR_HUMAN_REVIEW
    attempt_two = model.engineer_contexts[-1]
    assert attempt_two.attempt == 2
    assert attempt_two.correction is not None
    assert attempt_two.correction.repair_signal_ids == expected_signals
    assert attempt_two.correction.allowed_correction_paths == (
        LWC_JAVASCRIPT_PATH,
        LWC_HTML_PATH,
        LWC_CSS_PATH,
        LWC_TEST_PATH,
    )
    controller_directive = next(
        item
        for item in attempt_two.correction.repair_directives
        if item.signal_id == SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID
    )
    assert controller_directive.allowed_paths == (
        LWC_JAVASCRIPT_PATH,
        LWC_HTML_PATH,
        LWC_CSS_PATH,
    )
    assert LWC_TEST_PATH not in controller_directive.allowed_paths
    assert model.calls.count("EngineerModelOutcome") == 2


def test_attempt_two_stops_before_model_dispatch_when_targeted_wiki_has_no_hit(
    tmp_path: Path,
) -> None:
    migration_request = request()
    model = RoleDispatchModel()
    wiki = NoHitCorrectionWikiTestRetriever()
    evidence_root = tmp_path / "no-hit-correction"
    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=architect_context,
        correction_wiki_retriever=wiki,
        workspace_factory=filesystem_workspace_factory(SOURCE_ROOT, temp_parent=tmp_path),
        deterministic_validator=recoverable_then_passing_validation_report,
        artifact_store=ArtifactStore(evidence_root),
    )
    workflow = roles.build()
    paused = workflow.start(migration_request, thread_id="no-hit-correction")
    first = workflow.resume(
        approval_from_interrupt(paused),
        thread_id="no-hit-correction",
    )
    correction_request = CorrectionRequest.model_validate(first.value["correction_request"])
    approval = CorrectionController.approve_retry(
        correction_request,
        presented_correction_id=correction_request.correction_id,
        reviewer="no-hit-reviewer",
    )
    calls_before = tuple(model.calls)

    with pytest.raises(ModelWorkflowIntegrationError, match="no relevant evidence"):
        workflow.retry_recoverable(approval, thread_id="no-hit-correction")

    assert tuple(model.calls) == calls_before
    assert wiki.calls == [(migration_request.request_id, CORRECTION_QUERY)]
    assert not (
        evidence_root / "model-runs/request-model-workflow/engineer-invocation-lease-attempt-2.json"
    ).exists()


def test_attempt_two_engineer_policy_failure_is_dispatched_at_most_once(
    tmp_path: Path,
) -> None:
    model = AttemptTwoUnapprovedPathModel()
    (
        _roles,
        workflow,
        migration_request,
        first,
        approval,
        evidence_root,
    ) = recoverable_model_attempt_one(
        tmp_path,
        model,
        evidence_name="engineer-at-most-once",
        thread_id="engineer-at-most-once",
    )
    assert first.value["terminal_disposition"] is ValidationDisposition.RECOVERABLE_FAILURE

    with pytest.raises(
        SanitizedModelPolicyError,
        match="model_role_policy_failure:engineer:correction_scope_invalid",
    ):
        workflow.retry_recoverable(approval, thread_id="engineer-at-most-once")
    assert model.calls.count("EngineerModelOutcome") == 2
    snapshot = workflow.snapshot(thread_id="engineer-at-most-once")
    assert snapshot.next == ("engineer",)

    lease_path = (
        evidence_root / "model-runs/request-model-workflow/engineer-invocation-lease-attempt-2.json"
    )
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    assert lease["binding"]["role"] == "engineer"
    assert lease["binding"]["attempt"] == 2
    assert lease["binding"]["correction_evidence_digest"].startswith("sha256:")
    assert lease["binding"]["correction_wiki_trace_digest"].startswith("sha256:")
    assert lease["binding"]["change_set_digest"].startswith("sha256:")
    assert lease["binding"]["report_digest"].startswith("sha256:")
    assert not (
        evidence_root / "model-runs/request-model-workflow/engineer-attempt-2.json"
    ).exists()
    assert "outside-approved-scope" not in lease_path.read_text(encoding="utf-8")

    with pytest.raises(ModelWorkflowIntegrationError, match="refusing a duplicate provider call"):
        workflow.continue_local_failure(thread_id="engineer-at-most-once")
    assert model.calls.count("EngineerModelOutcome") == 2
    with pytest.raises(ModelWorkflowIntegrationError, match="refusing a duplicate provider call"):
        workflow.retry_recoverable(approval, thread_id="engineer-at-most-once")
    assert model.calls.count("EngineerModelOutcome") == 2


def test_attempt_two_validator_policy_failure_is_dispatched_at_most_once(
    tmp_path: Path,
) -> None:
    model = AttemptTwoInvalidValidatorModel()
    (
        _roles,
        workflow,
        migration_request,
        first,
        approval,
        evidence_root,
    ) = recoverable_model_attempt_one(
        tmp_path,
        model,
        evidence_name="validator-at-most-once",
        thread_id="validator-at-most-once",
    )
    assert first.value["terminal_disposition"] is ValidationDisposition.RECOVERABLE_FAILURE

    completed = workflow.retry_recoverable(
        approval,
        thread_id="validator-at-most-once",
    )
    assert completed.value["status"] is WorkflowStatus.COMPLETED
    assert completed.value["terminal_disposition"] is ValidationDisposition.READY_FOR_HUMAN_REVIEW
    assert model.calls.count("ValidatorModelAdvisory") == 1
    snapshot = workflow.snapshot(thread_id="validator-at-most-once")
    assert snapshot.next == ()

    lease_path = (
        evidence_root
        / "model-runs/request-model-workflow/validator-invocation-lease-attempt-2.json"
    )
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    assert lease["binding"]["role"] == "validator"
    assert lease["binding"]["attempt"] == 2
    assert lease["binding"]["change_set_digest"].startswith("sha256:")
    assert lease["binding"]["report_digest"].startswith("sha256:")
    assessment_path = evidence_root / "model-runs/request-model-workflow/validator-attempt-2.json"
    assessment = ValidatorAssessment.model_validate_json(assessment_path.read_text())
    assert assessment.advisory.assessment == "unavailable"
    assert assessment.model_call is None
    assert assessment.unavailable_receipt is not None
    assert assessment.unavailable_receipt.reason_code == "model_output_invalid"
    assert assessment.unavailable_receipt.attempted is True
    serialized_lease = lease_path.read_text(encoding="utf-8")
    for forbidden in ("unknown-validator-check", "summary", "concerns", "output"):
        assert forbidden not in serialized_lease

    replayed = _roles.validator(
        migration_request,
        completed.value["manifest"],
        completed.value["change_set"],
        attempt=2,
        correction=CorrectionAttemptEvidence.freeze(
            first.value["manifest"],
            first.value["change_set"],
            first.value["validation_report"],
        ),
    )
    assert replayed == completed.value["validation_report"]
    assert model.calls.count("ValidatorModelAdvisory") == 1


def test_engineer_intervention_is_persisted_replayed_and_skips_both_validators(
    tmp_path: Path,
) -> None:
    migration_request = request()
    source_before = content_revision(SOURCE_ROOT)
    model = EngineerStopRoleDispatchModel()
    deterministic_calls: list[int] = []

    def should_not_validate(*args, **kwargs):
        deterministic_calls.append(1)
        raise AssertionError("deterministic Validator must not run without a candidate")

    evidence_root = tmp_path / "engineer-stop-evidence"
    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=architect_context,
        workspace_factory=filesystem_workspace_factory(
            SOURCE_ROOT,
            temp_parent=tmp_path,
        ),
        deterministic_validator=should_not_validate,
        artifact_store=ArtifactStore(evidence_root),
    )
    workflow = roles.build()
    paused = workflow.start(migration_request, thread_id="engineer-stop")
    manifest_approval = approval_from_interrupt(paused)

    result = workflow.resume(
        manifest_approval,
        thread_id="engineer-stop",
    )

    assert result.interrupts == ()
    assert result.value["status"] == WorkflowStatus.DECISION_REQUIRED
    assert result.value["terminal_disposition"] is ValidationDisposition.DECISION_REQUIRED
    intervention = result.value["implementation_intervention"]
    assert isinstance(intervention, ImplementationIntervention)
    decision = result.value["decision_request"]
    assert decision.requested_action is ApprovalAction.EXPAND_SCOPE
    assert "approve" not in decision.options
    assert "grants no implementation authority" in decision.reason
    assert "new or revised manifest" in decision.recommendation
    assert model.calls == ["ArchitectManifestProposal", "EngineerModelOutcome"]
    assert deterministic_calls == []
    assert "change_set" not in result.value
    assert "validation_report" not in result.value

    manifest = result.value["manifest"]
    replayed = roles.engineer(migration_request, manifest)
    assert replayed == intervention
    assert model.calls == ["ArchitectManifestProposal", "EngineerModelOutcome"]
    assert deterministic_calls == []
    assert content_revision(SOURCE_ROOT) == source_before
    assert not (SOURCE_ROOT / OUTPUT_PATH).exists()
    assert (evidence_root / "model-runs/request-model-workflow/engineer-attempt-1.json").is_file()
    assert not (evidence_root / "model-runs/request-model-workflow/report-attempt-1.json").exists()
    assert not (
        evidence_root / "model-runs/request-model-workflow/validator-attempt-1.json"
    ).exists()
    with pytest.raises(PolicyViolation, match="pending manifest approval"):
        workflow.resume(manifest_approval, thread_id="engineer-stop")


@pytest.mark.parametrize(
    ("field_path", "expected"),
    (
        (("model_outcome", "result", "intervention", "input_evidence_digest"), "input"),
        (("model_outcome", "result", "intervention", "manifest_id"), "manifest"),
        (("model_outcome", "result", "intervention", "manifest_digest"), "manifest digest"),
        (("model_outcome", "result", "intervention", "reason"), "output"),
        (("model_call", "agent_definition_digest"), "agent definition"),
    ),
)
def test_persisted_engineer_intervention_tamper_fails_closed_without_new_model_call(
    tmp_path: Path,
    field_path: tuple[str, ...],
    expected: str,
) -> None:
    migration_request = request()
    model = EngineerStopRoleDispatchModel()
    evidence_root = tmp_path / "tampered-engineer-stop"
    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=architect_context,
        workspace_factory=filesystem_workspace_factory(SOURCE_ROOT, temp_parent=tmp_path),
        deterministic_validator=lambda *args, **kwargs: pytest.fail(
            "deterministic Validator must not run"
        ),
        artifact_store=ArtifactStore(evidence_root),
    )
    manifest = roles.architect(migration_request)
    assert isinstance(manifest, MigrationManifest)
    outcome = roles.engineer(migration_request, manifest)
    assert isinstance(outcome, ImplementationIntervention)
    path = evidence_root / "model-runs/request-model-workflow/engineer-attempt-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    target = payload
    for segment in field_path[:-1]:
        target = target[segment]
    leaf = field_path[-1]
    if leaf == "reason":
        target[leaf] = "Hostile persisted intervention rewrite."
    elif leaf == "manifest_id":
        target[leaf] = "hostile-manifest"
    else:
        target[leaf] = "sha256:" + "0" * 64
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises((ModelWorkflowIntegrationError, PolicyViolation), match=expected):
        roles.engineer(migration_request, manifest)
    assert model.calls == ["ArchitectManifestProposal", "EngineerModelOutcome"]
    assert not (SOURCE_ROOT / OUTPUT_PATH).exists()


@pytest.mark.parametrize(
    ("role", "expected_binding"),
    (
        ("architect", "output"),
        ("engineer", "input"),
        ("validator", "output"),
    ),
)
def test_persisted_role_handoffs_fail_closed_on_model_binding_tamper(
    tmp_path: Path,
    role: str,
    expected_binding: str,
) -> None:
    source_before = content_revision(SOURCE_ROOT)
    (
        roles,
        model,
        migration_request,
        manifest,
        change_set,
        _report,
        evidence_root,
    ) = complete_role_handoffs(tmp_path)
    artifact_paths = {
        "architect": evidence_root / "model-runs/request-model-workflow/architect.json",
        "engineer": evidence_root / "model-runs/request-model-workflow/engineer-attempt-1.json",
        "validator": evidence_root / "model-runs/request-model-workflow/validator-attempt-1.json",
    }
    artifact_path = artifact_paths[role]
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    if role == "architect":
        payload["agent_output"]["semantic_decisions"][0]["summary"] += " Hostile persisted rewrite."
    elif role == "engineer":
        payload["model_call"]["input_digest"] = "sha256:" + "0" * 64
    else:
        payload["advisory"]["summary"] = "Hostile persisted advisory rewrite."
    artifact_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ModelWorkflowIntegrationError, match=expected_binding):
        if role == "architect":
            roles.architect(migration_request)
        elif role == "engineer":
            roles.engineer(migration_request, manifest)
        else:
            roles.validator(migration_request, manifest, change_set)

    assert model.calls == [
        "ArchitectManifestProposal",
        "EngineerModelOutcome",
        "ValidatorModelAdvisory",
    ]
    assert content_revision(SOURCE_ROOT) == source_before
    assert not (SOURCE_ROOT / OUTPUT_PATH).exists()


def test_architect_replay_rejects_tampered_persisted_graph_or_wiki_input(
    tmp_path: Path,
) -> None:
    roles, _model, migration_request, *_rest, evidence_root = complete_role_handoffs(tmp_path)
    context_path = evidence_root / "model-runs/request-model-workflow/architect-context.json"
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    payload["model_context"]["wiki_trace"]["query"] = "tampered query"
    context_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ModelWorkflowIntegrationError, match="structurally invalid"):
        roles.architect(migration_request)


def test_architect_replay_rejects_tampered_digest_bound_source_input(
    tmp_path: Path,
) -> None:
    roles, _model, migration_request, *_rest, evidence_root = complete_role_handoffs(tmp_path)
    context_path = evidence_root / "model-runs/request-model-workflow/architect-context.json"
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    payload["model_context"]["source_files"][0]["content"] += "\n<!-- tampered -->\n"
    context_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ModelWorkflowIntegrationError, match="structurally invalid"):
        roles.architect(migration_request)


def test_architect_replay_rejects_self_consistent_source_rewrite_by_call_digest(
    tmp_path: Path,
) -> None:
    roles, model, migration_request, *_rest, evidence_root = complete_role_handoffs(tmp_path)
    context_path = evidence_root / "model-runs/request-model-workflow/architect-context.json"
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    source = payload["model_context"]["source_files"][0]
    source["content"] += "\n<!-- self-consistent rewrite -->\n"
    source["sha256"] = "sha256:" + hashlib.sha256(source["content"].encode("utf-8")).hexdigest()
    context_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    calls_before = tuple(model.calls)

    with pytest.raises((ModelWorkflowIntegrationError, PolicyViolation)):
        roles.architect(migration_request)
    assert tuple(model.calls) == calls_before


def test_architect_replay_uses_frozen_evidence_without_current_wiki_refresh(
    tmp_path: Path,
) -> None:
    roles, model, migration_request, manifest, *_rest = complete_role_handoffs(tmp_path)
    factory_calls = 0

    def changed_current_wiki(_request: MigrationRequest) -> ArchitectContext:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("historical replay must not derive current Architect evidence")

    roles.architect_context_factory = changed_current_wiki

    replayed = roles.architect(migration_request)

    assert replayed == manifest
    assert factory_calls == 0
    assert model.calls == [
        "ArchitectManifestProposal",
        "EngineerModelOutcome",
        "ValidatorModelAdvisory",
    ]


@pytest.mark.parametrize(
    ("artifact_name", "field_name", "replacement"),
    (
        ("dependency-graph.json", "base_revision", "sha256:" + "0" * 64),
        ("wiki-trace.json", "query", "tampered historical Wiki query"),
    ),
)
def test_architect_replay_fails_closed_on_standalone_evidence_tamper(
    tmp_path: Path,
    artifact_name: str,
    field_name: str,
    replacement: str,
) -> None:
    roles, model, migration_request, *_rest, evidence_root = complete_role_handoffs(tmp_path)
    path = evidence_root / "model-runs/request-model-workflow" / artifact_name
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field_name] = replacement
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises((ModelWorkflowIntegrationError, PolicyViolation)):
        roles.architect(migration_request)
    assert model.calls == [
        "ArchitectManifestProposal",
        "EngineerModelOutcome",
        "ValidatorModelAdvisory",
    ]


@pytest.mark.parametrize(
    ("mutation", "expected_message"),
    (
        ("missing", "missing or structurally invalid"),
        ("wrong_version", "missing or structurally invalid"),
        ("tampered", "differs from frozen context"),
    ),
)
def test_engineer_handoff_fails_closed_on_missing_wrong_or_tampered_architect_wiki(
    tmp_path: Path,
    mutation: str,
    expected_message: str,
) -> None:
    roles, model, migration_request, manifest, *_rest, evidence_root = complete_role_handoffs(
        tmp_path
    )
    model_root = evidence_root / "model-runs/request-model-workflow"
    wiki_path = model_root / "wiki-trace.json"
    context_path = model_root / "architect-context.json"
    if mutation == "missing":
        wiki_path.unlink()
    elif mutation == "tampered":
        wiki_payload = json.loads(wiki_path.read_text(encoding="utf-8"))
        wiki_payload["query"] = "tampered standalone Engineer handoff"
        wiki_path.write_text(json.dumps(wiki_payload) + "\n", encoding="utf-8")
    else:
        wiki_payload = json.loads(wiki_path.read_text(encoding="utf-8"))
        wiki_payload["source_version"] = "wrong-version"
        wrong_trace = RetrievalTrace.model_validate(wiki_payload)
        context_payload = json.loads(context_path.read_text(encoding="utf-8"))
        context_payload["model_context"]["wiki_trace"] = wrong_trace.model_dump(mode="json")
        context_payload["model_context"]["wiki_trace_digest"] = artifact_digest(wrong_trace)
        wiki_path.write_text(
            json.dumps(wrong_trace.model_dump(mode="json")) + "\n",
            encoding="utf-8",
        )
        context_path.write_text(json.dumps(context_payload) + "\n", encoding="utf-8")

    calls_before = tuple(model.calls)
    with pytest.raises(ModelWorkflowIntegrationError, match=expected_message):
        roles.engineer(migration_request, manifest)
    assert tuple(model.calls) == calls_before


def test_unresolved_preflight_persists_and_routes_with_zero_model_calls(
    tmp_path: Path,
) -> None:
    migration_request = request()
    model = RoleDispatchModel()
    evidence_root = tmp_path / "preflight-evidence"
    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=unresolved_graph_preflight,
        workspace_factory=filesystem_workspace_factory(
            SOURCE_ROOT,
            temp_parent=tmp_path,
        ),
        deterministic_validator=passing_validation_report,
        artifact_store=ArtifactStore(evidence_root),
    )

    result = roles.build().start(migration_request, thread_id="unresolved-preflight")

    assert result.interrupts == ()
    assert result.value["status"] == WorkflowStatus.DECISION_REQUIRED
    assert result.value["terminal_disposition"] is ValidationDisposition.DECISION_REQUIRED
    assert result.value["decision_request"].requested_action is ApprovalAction.EXPAND_SCOPE
    assert model.calls == []
    replayed = roles.architect(migration_request)
    assert replayed == result.value["planning_intervention"]
    assert model.calls == []
    assert (evidence_root / "model-runs/request-model-workflow/architect-preflight.json").is_file()
    assert not (evidence_root / "model-runs/request-model-workflow/architect.json").exists()
    assert not (SOURCE_ROOT / OUTPUT_PATH).exists()


def test_persisted_preflight_tamper_fails_closed_without_model_call(tmp_path: Path) -> None:
    migration_request = request()
    model = RoleDispatchModel()
    evidence_root = tmp_path / "tampered-preflight-evidence"
    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=unresolved_graph_preflight,
        workspace_factory=filesystem_workspace_factory(
            SOURCE_ROOT,
            temp_parent=tmp_path,
        ),
        deterministic_validator=passing_validation_report,
        artifact_store=ArtifactStore(evidence_root),
    )
    roles.architect(migration_request)
    path = evidence_root / "model-runs/request-model-workflow/architect-preflight.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["reason"] = "Hostile but structurally valid persisted rewrite."
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ModelWorkflowIntegrationError, match="differs from current preflight"):
        roles.architect(migration_request)
    assert model.calls == []


def test_model_decision_required_manifest_stops_before_approval_and_engineering(
    tmp_path: Path,
) -> None:
    migration_request = request()
    model = DecisionRequiredRoleDispatchModel()
    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=architect_context,
        workspace_factory=filesystem_workspace_factory(
            SOURCE_ROOT,
            temp_parent=tmp_path,
        ),
        deterministic_validator=passing_validation_report,
        artifact_store=ArtifactStore(tmp_path / "decision-required-evidence"),
    )
    workflow = roles.build()

    result = workflow.start(migration_request, thread_id="model-decision-required")

    assert result.interrupts == ()
    assert result.value["status"] == WorkflowStatus.DECISION_REQUIRED
    assert result.value["terminal_disposition"] is ValidationDisposition.DECISION_REQUIRED
    assert result.value["manifest"].status is ManifestStatus.DECISION_REQUIRED
    assert model.calls == ["ArchitectManifestProposal"]
    with pytest.raises(PolicyViolation, match="requires a human decision"):
        roles.engineer(migration_request, result.value["manifest"])
    with pytest.raises(PolicyViolation, match="pending manifest approval"):
        workflow.resume(
            ManifestApproval(
                decision_id="not-authority",
                request_id=migration_request.request_id,
                manifest_id=result.value["manifest"].manifest_id,
                manifest_digest=result.value["manifest_digest"],
                requested_action=ApprovalAction.APPROVE_MANIFEST,
                selection=ApprovalSelection.APPROVE,
                reviewer="model-workflow-reviewer",
            ),
            thread_id="model-decision-required",
        )
    assert model.calls == ["ArchitectManifestProposal"]
    assert not (SOURCE_ROOT / OUTPUT_PATH).exists()
