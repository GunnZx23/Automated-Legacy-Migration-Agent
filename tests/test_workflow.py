from __future__ import annotations

from datetime import UTC, datetime

import pytest

from legacy_migration_agent.agent_runtime.correction import (
    CorrectionAttemptEvidence,
    CorrectionController,
    CorrectionRequest,
)
from legacy_migration_agent.contracts import (
    ApprovalAction,
    ChangeSet,
    CheckResult,
    CheckStatus,
    DependencyEvidence,
    EnvironmentKind,
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
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.workflow import (
    ApprovalSelection,
    ManifestApproval,
    WorkflowStatus,
    build_workflow,
)


def request(request_id: str = "request-1") -> MigrationRequest:
    return MigrationRequest(
        request_id=request_id,
        platform=Platform.SALESFORCE,
        repository="synthetic-salesforce-fixture",
        base_revision="abcdef1",
        target=MigrationTarget(
            entry_path="force-app/main/default/pages/LegacyAccount.page",
            target_runtime="LWC",
            source_version="Salesforce API 67.0",
            target_version="Salesforce API 67.0",
            description="Migrate a synthetic Visualforce page to LWC.",
        ),
        allowed_environment=EnvironmentKind.LOCAL,
    )


def planning_intervention(
    migration_request: MigrationRequest,
    *,
    request_digest: str | None = None,
) -> PlanningIntervention:
    entry_path = migration_request.target.entry_path
    return PlanningIntervention(
        intervention_id=f"preflight-{migration_request.request_id}",
        request_id=migration_request.request_id,
        request_digest=request_digest or artifact_digest(migration_request),
        platform=migration_request.platform,
        base_revision=migration_request.base_revision,
        reason="The dependency graph contains an unresolved dynamic reference.",
        requested_action=ApprovalAction.EXPAND_SCOPE,
        affected_paths=(entry_path,),
        evidence=(
            PlanningInterventionEvidence(
                category=RiskCategory.DYNAMIC_DEPENDENCY,
                source="dependency_graph",
                source_digest=artifact_digest(
                    {"entry_path": entry_path, "unresolved": "dynamic-reference"}
                ),
                summary="Static analysis could not resolve one dynamic dependency.",
                affected_paths=(entry_path,),
                unresolved_items=("dynamic-reference",),
            ),
        ),
        options=(
            PlanningInterventionOption.EXPAND_SCOPE_AND_REPLAN,
            PlanningInterventionOption.STOP_REQUEST,
        ),
        recommendation="Expand evidence collection and begin a new planning run.",
    )


class DeterministicRoles:
    def __init__(self) -> None:
        self.architect_calls: list[str] = []
        self.engineer_calls: list[str] = []
        self.validator_calls: list[str] = []
        self.engineer_corrections: list[CorrectionAttemptEvidence | None] = []
        self.validator_corrections: list[CorrectionAttemptEvidence | None] = []

    def architect(self, migration_request: MigrationRequest) -> MigrationManifest:
        self.architect_calls.append(migration_request.request_id)
        return MigrationManifest(
            manifest_id=f"manifest-{migration_request.request_id}",
            request_id=migration_request.request_id,
            platform=migration_request.platform,
            base_revision=migration_request.base_revision,
            approved_paths=("force-app/main/default/lwc/accountSummary/accountSummary.js",),
            dependencies=(
                DependencyEvidence(
                    path="force-app/main/default/classes/AccountController.cls",
                    relation="controller",
                    source="Visualforce controller attribute",
                ),
            ),
            transformations=(
                TransformationStep(
                    step_id="create-lwc",
                    description="Create the LWC JavaScript controller.",
                    input_paths=("force-app/main/default/pages/LegacyAccount.page",),
                    output_paths=("force-app/main/default/lwc/accountSummary/accountSummary.js",),
                ),
            ),
            validation_plan=(
                ValidationCommand(
                    check_id="unit-test",
                    command_id="npm-test",
                    purpose="Run the synthetic LWC unit test.",
                ),
            ),
        )

    def engineer(
        self,
        migration_request: MigrationRequest,
        manifest: MigrationManifest,
        *,
        attempt: int = 1,
        correction: CorrectionAttemptEvidence | None = None,
    ) -> ChangeSet:
        self.engineer_calls.append(migration_request.request_id)
        self.engineer_corrections.append(correction)
        return ChangeSet(
            change_set_id=f"changes-{migration_request.request_id}",
            request_id=migration_request.request_id,
            manifest_id=manifest.manifest_id,
            base_revision=manifest.base_revision,
            changed_paths=manifest.approved_paths,
            unified_diff="+export default class AccountSummary {}",
        )

    def validator(
        self,
        migration_request: MigrationRequest,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        *,
        attempt: int = 1,
        correction: CorrectionAttemptEvidence | None = None,
    ) -> ValidationReport:
        self.validator_calls.append(migration_request.request_id)
        self.validator_corrections.append(correction)
        now = datetime.now(UTC)
        receipt = ToolReceipt(
            receipt_id=f"receipt-{migration_request.request_id}",
            tool_id="npm-test",
            request_id=migration_request.request_id,
            run_id=f"run-{migration_request.request_id}",
            attempt=attempt,
            base_revision=manifest.base_revision,
            environment=EnvironmentKind.LOCAL,
            input_artifact_digest=artifact_digest(change_set),
            operation="Run the synthetic LWC unit test",
            working_directory="/isolated/worktree",
            started_at=now,
            ended_at=now,
            exit_code=0,
            terminal=True,
        )
        return ValidationReport(
            report_id=f"report-{migration_request.request_id}-attempt-{attempt}",
            request_id=migration_request.request_id,
            manifest_id=manifest.manifest_id,
            change_set_id=change_set.change_set_id,
            base_revision=manifest.base_revision,
            results=(
                CheckResult(
                    check_id="unit-test",
                    command_id="npm-test",
                    required=True,
                    status=CheckStatus.PASSED,
                    receipt=receipt,
                    summary="Synthetic LWC test passed.",
                ),
            ),
            disposition=ValidationDisposition.READY_FOR_HUMAN_REVIEW,
            attempt=attempt,
        )


class RecoverableThenReadyRoles(DeterministicRoles):
    def validator(
        self,
        migration_request: MigrationRequest,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        *,
        attempt: int = 1,
        correction: CorrectionAttemptEvidence | None = None,
    ) -> ValidationReport:
        report = super().validator(
            migration_request,
            manifest,
            change_set,
            attempt=attempt,
            correction=correction,
        )
        if attempt != 1:
            return report
        result = report.results[0]
        assert result.receipt is not None
        failed = result.model_copy(
            update={
                "status": CheckStatus.FAILED,
                "receipt": result.receipt.model_copy(update={"exit_code": 1}),
                "summary": "Seeded recoverable attempt-1 failure.",
            }
        )
        return report.model_copy(
            update={
                "results": (failed,),
                "disposition": ValidationDisposition.RECOVERABLE_FAILURE,
            }
        )


class ScopeExpandingRetryRoles(RecoverableThenReadyRoles):
    def engineer(
        self,
        migration_request: MigrationRequest,
        manifest: MigrationManifest,
        *,
        attempt: int = 1,
        correction: CorrectionAttemptEvidence | None = None,
    ) -> ChangeSet:
        change_set = super().engineer(
            migration_request,
            manifest,
            attempt=attempt,
            correction=correction,
        )
        if attempt == 2:
            return change_set.model_copy(
                update={
                    "changed_paths": (*change_set.changed_paths, "outside-scope.txt"),
                }
            )
        return change_set


def approval_from_interrupt(
    run_result: object,
    selection: str = ApprovalSelection.APPROVE,
    *,
    reviewer: str = "reviewer-1",
    comment: str = "",
) -> ManifestApproval:
    interrupt_value = run_result.interrupts[0].value  # type: ignore[attr-defined]
    decision = interrupt_value["decision"]
    return ManifestApproval(
        decision_id=decision["decision_id"],
        request_id=decision["request_id"],
        manifest_id=interrupt_value["manifest_id"],
        manifest_digest=interrupt_value["manifest_digest"],
        requested_action=decision["requested_action"],
        selection=selection,
        reviewer=reviewer,
        comment=comment,
    )


def test_pauses_after_architect_before_engineer() -> None:
    roles = DeterministicRoles()
    workflow = build_workflow(roles.architect, roles.engineer, roles.validator)

    result = workflow.start(request(), thread_id="pause-thread")

    assert len(result.interrupts) == 1
    assert result.interrupts[0].value["kind"] == "manifest_approval"
    assert result.value["status"] == WorkflowStatus.AWAITING_APPROVAL
    assert result.interrupts[0].value["manifest_digest"].startswith("sha256:")
    assert roles.architect_calls == ["request-1"]
    assert roles.engineer_calls == []
    assert roles.validator_calls == []


def test_preflight_intervention_routes_terminal_without_approval_or_implementation() -> None:
    roles = DeterministicRoles()
    architect_calls: list[str] = []

    def preflight(migration_request: MigrationRequest) -> PlanningIntervention:
        architect_calls.append(migration_request.request_id)
        return planning_intervention(migration_request)

    workflow = build_workflow(preflight, roles.engineer, roles.validator)
    result = workflow.start(request(), thread_id="preflight-intervention")

    assert result.interrupts == ()
    assert result.value["status"] == WorkflowStatus.DECISION_REQUIRED
    assert result.value["terminal_disposition"] is ValidationDisposition.DECISION_REQUIRED
    assert result.value["decision_request"].options == (
        PlanningInterventionOption.EXPAND_SCOPE_AND_REPLAN.value,
        PlanningInterventionOption.STOP_REQUEST.value,
    )
    assert architect_calls == ["request-1"]
    assert roles.engineer_calls == []
    assert roles.validator_calls == []
    with pytest.raises(PolicyViolation, match="pending manifest approval"):
        workflow.resume(
            ManifestApproval(
                decision_id="not-authority",
                request_id="request-1",
                manifest_id="not-a-manifest",
                manifest_digest="sha256:" + "0" * 64,
                requested_action=ApprovalAction.APPROVE_MANIFEST,
                selection=ApprovalSelection.APPROVE,
                reviewer="reviewer-1",
            ),
            thread_id="preflight-intervention",
        )


def test_preflight_intervention_rejects_tampered_request_binding() -> None:
    roles = DeterministicRoles()

    def preflight(migration_request: MigrationRequest) -> PlanningIntervention:
        return planning_intervention(
            migration_request,
            request_digest="sha256:" + "0" * 64,
        )

    workflow = build_workflow(preflight, roles.engineer, roles.validator)
    with pytest.raises(PolicyViolation, match="request digest does not match"):
        workflow.start(request(), thread_id="tampered-preflight")
    assert roles.engineer_calls == []
    assert roles.validator_calls == []


def test_decision_required_manifest_routes_terminal_without_approval() -> None:
    roles = DeterministicRoles()

    def stopped_architect(migration_request: MigrationRequest) -> MigrationManifest:
        manifest = roles.architect(migration_request)
        unresolved = manifest.dependencies[0].model_copy(update={"resolved": False})
        return manifest.model_copy(
            update={
                "dependencies": (unresolved,),
                "status": ManifestStatus.DECISION_REQUIRED,
            }
        )

    workflow = build_workflow(stopped_architect, roles.engineer, roles.validator)
    result = workflow.start(request(), thread_id="manifest-decision-required")

    assert result.interrupts == ()
    assert result.value["status"] == WorkflowStatus.DECISION_REQUIRED
    assert result.value["terminal_disposition"] is ValidationDisposition.DECISION_REQUIRED
    assert result.value["decision_request"].requested_action is ApprovalAction.EXPAND_SCOPE
    assert "cannot authorize implementation" in result.value["terminal_reason"]
    assert roles.engineer_calls == []
    assert roles.validator_calls == []


def test_approve_runs_engineer_and_validator_to_terminal_disposition() -> None:
    roles = DeterministicRoles()
    workflow = build_workflow(roles.architect, roles.engineer, roles.validator)
    paused = workflow.start(request(), thread_id="approve-thread")

    result = workflow.resume(
        approval_from_interrupt(paused),
        thread_id="approve-thread",
    )

    assert result.interrupts == ()
    assert result.value["status"] == WorkflowStatus.COMPLETED
    assert result.value["terminal_disposition"] == ValidationDisposition.READY_FOR_HUMAN_REVIEW
    assert roles.engineer_calls == ["request-1"]
    assert roles.validator_calls == ["request-1"]


@pytest.mark.parametrize(
    ("selection", "expected_status", "expected_disposition"),
    [
        (
            ApprovalSelection.REJECT,
            WorkflowStatus.REJECTED,
            ValidationDisposition.PLAN_INVALID,
        ),
        (
            ApprovalSelection.MODIFY,
            WorkflowStatus.MODIFICATION_REQUESTED,
            ValidationDisposition.DECISION_REQUIRED,
        ),
    ],
)
def test_reject_and_modify_stop_without_engineering(
    selection: str,
    expected_status: str,
    expected_disposition: ValidationDisposition,
) -> None:
    roles = DeterministicRoles()
    workflow = build_workflow(roles.architect, roles.engineer, roles.validator)
    paused = workflow.start(request(), thread_id=f"{selection}-thread")

    result = workflow.resume(
        approval_from_interrupt(
            paused,
            selection,
            comment=f"Reviewer selected {selection}.",
        ),
        thread_id=f"{selection}-thread",
    )

    assert result.interrupts == ()
    assert result.value["status"] == expected_status
    assert result.value["terminal_disposition"] == expected_disposition
    assert result.value["terminal_reason"] == f"Reviewer selected {selection}."
    assert roles.engineer_calls == []
    assert roles.validator_calls == []


def test_tampered_manifest_digest_is_rejected_before_engineering() -> None:
    roles = DeterministicRoles()
    workflow = build_workflow(roles.architect, roles.engineer, roles.validator)
    paused = workflow.start(request(), thread_id="tamper-thread")
    approval = approval_from_interrupt(paused)
    tampered = approval.model_copy(update={"manifest_digest": "sha256:" + "0" * 64})

    with pytest.raises(PolicyViolation, match="manifest digest does not match"):
        workflow.resume(tampered, thread_id="tamper-thread")

    assert roles.engineer_calls == []
    assert roles.validator_calls == []
    snapshot = workflow.snapshot(thread_id="tamper-thread")
    assert snapshot.next == ("approval_gate",)


def test_checkpoints_are_isolated_and_resume_requires_same_thread() -> None:
    roles = DeterministicRoles()
    workflow = build_workflow(roles.architect, roles.engineer, roles.validator)
    first = workflow.start(request("request-a"), thread_id="thread-a")
    workflow.start(request("request-b"), thread_id="thread-b")

    with pytest.raises(PolicyViolation, match="pending manifest approval"):
        workflow.resume(
            approval_from_interrupt(first),
            thread_id="never-started-thread",
        )

    result = workflow.resume(
        approval_from_interrupt(first),
        thread_id="thread-a",
    )

    assert result.value["request"].request_id == "request-a"
    assert workflow.snapshot(thread_id="thread-b").values["request"].request_id == "request-b"
    assert workflow.snapshot(thread_id="thread-b").next == ("approval_gate",)
    assert roles.engineer_calls == ["request-a"]


def test_thread_id_cannot_be_reused() -> None:
    roles = DeterministicRoles()
    workflow = build_workflow(roles.architect, roles.engineer, roles.validator)
    workflow.start(request(), thread_id="one-run-only")

    with pytest.raises(PolicyViolation, match="already has workflow state"):
        workflow.start(request("request-2"), thread_id="one-run-only")


def correction_approval_for(result: object, reviewer: str = "reviewer-2"):
    request = CorrectionRequest.model_validate(  # type: ignore[attr-defined]
        result.value["correction_request"]
    )
    return CorrectionController.approve_retry(
        request,
        presented_correction_id=request.correction_id,
        reviewer=reviewer,
        comment="Authorize only bounded local attempt 2.",
    )


def test_recoverable_report_requires_explicit_bound_attempt_2_and_cannot_replay() -> None:
    roles = RecoverableThenReadyRoles()
    workflow = build_workflow(roles.architect, roles.engineer, roles.validator)
    paused = workflow.start(request(), thread_id="bounded-retry")
    first = workflow.resume(
        approval_from_interrupt(paused),
        thread_id="bounded-retry",
    )
    assert first.value["terminal_disposition"] == ValidationDisposition.RECOVERABLE_FAILURE
    assert first.value["validation_report"].attempt == 1
    correction_approval = correction_approval_for(first)

    second = workflow.retry_recoverable(
        correction_approval,
        thread_id="bounded-retry",
    )

    assert second.value["terminal_disposition"] == ValidationDisposition.READY_FOR_HUMAN_REVIEW
    assert second.value["validation_report"].attempt == 2
    assert all(
        result.receipt and result.receipt.attempt == 2
        for result in second.value["validation_report"].results
    )
    assert roles.engineer_calls == ["request-1", "request-1"]
    assert roles.validator_calls == ["request-1", "request-1"]
    assert roles.engineer_corrections[0] is None
    assert roles.validator_corrections[0] is None
    assert roles.engineer_corrections[1] == roles.validator_corrections[1]
    retry_evidence = roles.engineer_corrections[1]
    assert retry_evidence is not None
    assert retry_evidence.prior_validation_report == first.value["validation_report"]
    assert "reviewer" not in retry_evidence.model_dump(mode="json")
    assert "comment" not in retry_evidence.model_dump(mode="json")
    with pytest.raises(PolicyViolation, match="does not authorize"):
        workflow.retry_recoverable(
            correction_approval,
            thread_id="bounded-retry",
        )


def test_retry_rejects_tampered_evidence_binding_before_execution() -> None:
    roles = RecoverableThenReadyRoles()
    workflow = build_workflow(roles.architect, roles.engineer, roles.validator)
    paused = workflow.start(request(), thread_id="retry-binding")
    first = workflow.resume(
        approval_from_interrupt(paused),
        thread_id="retry-binding",
    )
    approval = correction_approval_for(first).model_copy(
        update={"report_digest": "sha256:" + "0" * 64}
    )

    with pytest.raises(PolicyViolation, match="report_digest does not match"):
        workflow.retry_recoverable(approval, thread_id="retry-binding")
    assert roles.engineer_calls == ["request-1"]
    assert roles.validator_calls == ["request-1"]


def test_retry_scope_expansion_fails_closed_on_checkpointed_attempt_2() -> None:
    roles = ScopeExpandingRetryRoles()
    workflow = build_workflow(roles.architect, roles.engineer, roles.validator)
    paused = workflow.start(request(), thread_id="retry-scope")
    first = workflow.resume(
        approval_from_interrupt(paused),
        thread_id="retry-scope",
    )

    with pytest.raises(PolicyViolation, match="outside the manifest"):
        workflow.retry_recoverable(
            correction_approval_for(first),
            thread_id="retry-scope",
        )
    snapshot = workflow.snapshot(thread_id="retry-scope")
    assert snapshot.next == ("engineer",)
    assert snapshot.values["execution_attempt"] == 2
    assert roles.validator_calls == ["request-1"]
