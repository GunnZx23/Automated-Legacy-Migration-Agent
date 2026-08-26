from __future__ import annotations

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
    EngineerFilePlan,
    EngineerFileUpdate,
    EngineerInterventionOutcome,
    EngineerModelOutcome,
    EngineerRun,
    EngineerWorkspaceContext,
    ValidatorAdvisory,
)
from legacy_migration_agent.agent_runtime.model_workflow import (
    ModelAgentWorkflowRoles,
    ModelWorkflowIntegrationError,
    SanitizedModelPolicyError,
    filesystem_workspace_factory,
)
from legacy_migration_agent.contracts import (
    ApprovalAction,
    CheckResult,
    CheckStatus,
    DependencyEvidence,
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
    TransformationStep,
    ValidationCommand,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import ArtifactStore, artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.scope_policy import MigrationScopePolicy, PlatformAdapter
from legacy_migration_agent.core.workspace import content_revision
from legacy_migration_agent.graphs.dependency_graph import build_salesforce_dependency_graph
from legacy_migration_agent.knowledge.wiki import LlmWiki
from legacy_migration_agent.workflow import (
    ApprovalSelection,
    ManifestApproval,
    WorkflowStatus,
)

PROJECT_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = PROJECT_ROOT / "fixtures/salesforce/account-contact-explorer/input"
VF_ENTRY = "force-app/main/default/pages/LegacyAccountContactExplorer.page"
OUTPUT_PATH = "force-app/main/default/lwc/modelDemo/modelDemo.js"


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
        if output_type is ArchitectManifestProposal:
            context = ArchitectContext.model_validate(input_value)
            manifest = MigrationManifest(
                manifest_id="manifest-model-workflow",
                request_id=context.request.request_id,
                platform=context.request.platform,
                base_revision=context.request.base_revision,
                approved_paths=(OUTPUT_PATH,),
                dependencies=(
                    DependencyEvidence(
                        path=VF_ENTRY,
                        relation="Visualforce migration source",
                        source="frozen dependency graph",
                    ),
                ),
                transformations=(
                    TransformationStep(
                        step_id="create-model-demo-lwc-controller",
                        description="Create the approved additive LWC controller.",
                        input_paths=(VF_ENTRY,),
                        output_paths=(OUTPUT_PATH,),
                    ),
                ),
                validation_plan=(
                    ValidationCommand(
                        check_id="bounded-local-check",
                        command_id="bounded-local-check",
                        purpose="Run the deterministic model-workflow fixture check.",
                    ),
                ),
                required_approvals=(ApprovalAction.APPROVE_MANIFEST,),
            )
            return ArchitectManifestProposal(
                manifest=manifest,
                scope_policy_digest=context.platform_adapter.scope_policy_digest,
                public_decisions=(
                    "Add one manifest-scoped LWC controller beside the legacy page.",
                ),
                cited_graph_nodes=(context.dependency_graph.nodes[0].node_id,),
                cited_wiki_pages=(context.wiki_trace.hits[0].page_id,),
            )
        if output_type is EngineerModelOutcome:
            return EngineerModelOutcome.for_file_plan(
                EngineerFilePlan(
                    updates=(
                        EngineerFileUpdate(
                            path=OUTPUT_PATH,
                            content=(
                                "import { LightningElement } from 'lwc';\n"
                                "export default class ModelDemo extends LightningElement {}\n"
                            ),
                        ),
                    ),
                    assumptions=("The frozen fixture is the complete approved unit.",),
                )
            )
        if output_type is ValidatorAdvisory:
            evidence = input_value.evidence  # type: ignore[attr-defined]
            return ValidatorAdvisory(
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
        self.engineer_contexts: list[EngineerWorkspaceContext] = []

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        if output_type is EngineerModelOutcome:
            self.engineer_contexts.append(EngineerWorkspaceContext.model_validate(input_value))
        return super().parse(
            system_prompt=system_prompt,
            input_value=input_value,
            output_type=output_type,
        )


class AttemptTwoUnapprovedPathModel(RoleDispatchModel):
    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        if output_type is EngineerModelOutcome:
            context = EngineerWorkspaceContext.model_validate(input_value)
            if context.attempt == 2:
                self.calls.append(output_type.__name__)
                return EngineerModelOutcome.for_file_plan(
                    EngineerFilePlan(
                        updates=(
                            EngineerFileUpdate(
                                path="outside-approved-scope.js",
                                content="export default 'unauthorized';\n",
                            ),
                        )
                    )
                )
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
        if output_type is ValidatorAdvisory:
            evidence = input_value.evidence  # type: ignore[attr-defined]
            if evidence.report.attempt == 2:
                self.calls.append(output_type.__name__)
                return ValidatorAdvisory(
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
        if output_type is not ArchitectManifestProposal:
            return output
        proposal = ArchitectManifestProposal.model_validate(output)
        unresolved = proposal.manifest.dependencies[0].model_copy(update={"resolved": False})
        return proposal.model_copy(
            update={
                "manifest": proposal.manifest.model_copy(
                    update={
                        "dependencies": (unresolved,),
                        "status": ManifestStatus.DECISION_REQUIRED,
                    }
                )
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
        if output_type is not EngineerModelOutcome:
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
        as_of=date(2026, 8, 26),
    )
    policy = MigrationScopePolicy(
        policy_id="model-workflow-salesforce-policy",
        platform=Platform.SALESFORCE,
        required_source_input_paths=(VF_ENTRY,),
        approved_output_paths=(OUTPUT_PATH,),
        forbidden_paths=(VF_ENTRY, "fixtures/salesforce/account-contact-explorer/expected"),
        allowed_validation_command_ids=("bounded-local-check",),
        required_validation_command_ids=("bounded-local-check",),
        max_changed_files=1,
    )
    return ArchitectContext(
        request=migration_request,
        dependency_graph=graph,
        dependency_graph_digest=artifact_digest(graph),
        wiki_trace=wiki,
        wiki_trace_digest=artifact_digest(wiki),
        platform_adapter=PlatformAdapter.bind(
            adapter_id="model-workflow-salesforce-adapter",
            policy=policy,
        ),
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
        }
    )
    return report.model_copy(
        update={
            "results": (failed,),
            "disposition": ValidationDisposition.RECOVERABLE_FAILURE,
        }
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
    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=architect_context,
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
    model = RoleDispatchModel()
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
    model = RoleDispatchModel()
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
        "ValidatorAdvisory",
    ]
    assert validation_calls == [1]
    assert content_revision(SOURCE_ROOT) == source_before
    assert not (SOURCE_ROOT / OUTPUT_PATH).exists()

    # Replaying completed role handoffs uses immutable artifacts. It performs
    # no additional provider call, validation command, or source mutation.
    manifest = completed.value["manifest"]
    change_set = roles.engineer(migration_request, manifest)
    report = roles.validator(migration_request, manifest, change_set)
    assert report == completed.value["validation_report"]
    assert model.calls == [
        "ArchitectManifestProposal",
        "EngineerModelOutcome",
        "ValidatorAdvisory",
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


def test_attempt_two_uses_persisted_safe_correction_and_replays_without_model_calls(
    tmp_path: Path,
) -> None:
    migration_request = request()
    source_before = content_revision(SOURCE_ROOT)
    model = CorrectionCapturingRoleDispatchModel()
    evidence_root = tmp_path / "correction-evidence"
    roles = ModelAgentWorkflowRoles(
        load_agent_registry(PROJECT_ROOT / "agents"),
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=architect_context,
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
    assert len(model.engineer_contexts) == 2
    attempt_one, attempt_two = model.engineer_contexts
    assert attempt_one.attempt == 1
    assert attempt_one.correction is None
    assert attempt_two.attempt == 2
    assert attempt_two.correction is not None
    assert attempt_one.input_evidence_digest != attempt_two.input_evidence_digest
    prior_run = EngineerRun.model_validate_json(
        (evidence_root / "model-runs/request-model-workflow/engineer-attempt-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert attempt_two.correction.prior_file_plan == prior_run.file_plan
    assert attempt_two.correction.implementation_failure_ids == ("bounded-local-check",)

    correction_path = (
        evidence_root / "model-runs/request-model-workflow/engineer-correction-attempt-2.json"
    )
    safe_payload = correction_path.read_text(encoding="utf-8")
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


def test_attempt_two_engineer_policy_failure_is_dispatched_at_most_once(
    tmp_path: Path,
) -> None:
    model = AttemptTwoUnapprovedPathModel()
    (
        _roles,
        workflow,
        _migration_request,
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
        match="model_role_policy_failure:engineer:policy_rejected",
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
        _migration_request,
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

    with pytest.raises(
        SanitizedModelPolicyError,
        match="model_role_policy_failure:validator:policy_rejected",
    ):
        workflow.retry_recoverable(approval, thread_id="validator-at-most-once")
    assert model.calls.count("ValidatorAdvisory") == 2
    snapshot = workflow.snapshot(thread_id="validator-at-most-once")
    assert snapshot.next == ("validator",)

    lease_path = (
        evidence_root
        / "model-runs/request-model-workflow/validator-invocation-lease-attempt-2.json"
    )
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    assert lease["binding"]["role"] == "validator"
    assert lease["binding"]["attempt"] == 2
    assert lease["binding"]["change_set_digest"].startswith("sha256:")
    assert lease["binding"]["report_digest"].startswith("sha256:")
    assert not (
        evidence_root / "model-runs/request-model-workflow/validator-attempt-2.json"
    ).exists()
    serialized_lease = lease_path.read_text(encoding="utf-8")
    for forbidden in ("unknown-validator-check", "summary", "concerns", "output"):
        assert forbidden not in serialized_lease

    with pytest.raises(ModelWorkflowIntegrationError, match="refusing a duplicate provider call"):
        workflow.continue_local_failure(thread_id="validator-at-most-once")
    assert model.calls.count("ValidatorAdvisory") == 2
    with pytest.raises(ModelWorkflowIntegrationError, match="refusing a duplicate provider call"):
        workflow.retry_recoverable(approval, thread_id="validator-at-most-once")
    assert model.calls.count("ValidatorAdvisory") == 2


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
        payload["proposal"]["public_decisions"][0] += " Hostile persisted rewrite."
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
        "ValidatorAdvisory",
    ]
    assert content_revision(SOURCE_ROOT) == source_before
    assert not (SOURCE_ROOT / OUTPUT_PATH).exists()


def test_architect_replay_rejects_tampered_persisted_graph_or_wiki_input(
    tmp_path: Path,
) -> None:
    roles, _model, migration_request, *_rest, evidence_root = complete_role_handoffs(tmp_path)
    context_path = evidence_root / "model-runs/request-model-workflow/architect-context.json"
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    payload["wiki_trace"]["query"] = "tampered query"
    context_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(PolicyViolation, match="immutable artifact"):
        roles.architect(migration_request)


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
