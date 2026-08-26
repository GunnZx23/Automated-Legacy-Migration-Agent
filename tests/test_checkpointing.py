from datetime import UTC, datetime
from pathlib import Path

import pytest

from legacy_migration_agent.agent_runtime.checkpointing import durable_migration_workflow
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
    ImplementationIntervention,
    ImplementationInterventionEvidence,
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
from legacy_migration_agent.workflow import ApprovalSelection, ManifestApproval, WorkflowStatus


def request() -> MigrationRequest:
    return MigrationRequest(
        request_id="durable-request",
        platform=Platform.SALESFORCE,
        repository="synthetic-fixture",
        base_revision="abcdef0",
        target=MigrationTarget(
            entry_path="force-app/main/default/pages/Legacy.page",
            target_runtime="LWC",
            source_version="Salesforce API 67.0",
            target_version="Salesforce API 67.0",
            description="Exercise durable pause and resume.",
        ),
    )


class Roles:
    def __init__(self):
        self.engineer_calls = 0
        self.validator_calls = 0

    def architect(self, migration_request):
        return MigrationManifest(
            manifest_id="durable-manifest",
            request_id=migration_request.request_id,
            platform=migration_request.platform,
            base_revision=migration_request.base_revision,
            approved_paths=("force-app/main/default/lwc/example/example.js",),
            dependencies=(
                DependencyEvidence(
                    path=migration_request.target.entry_path,
                    relation="migration source",
                    source="fixture",
                ),
            ),
            transformations=(
                TransformationStep(
                    step_id="create-example",
                    description="Create the example LWC.",
                    input_paths=(migration_request.target.entry_path,),
                    output_paths=("force-app/main/default/lwc/example/example.js",),
                ),
            ),
            validation_plan=(
                ValidationCommand(
                    check_id="framework",
                    command_id="pytest",
                    purpose="Run a terminal fixture check.",
                ),
            ),
        )

    def engineer(self, migration_request, manifest):
        self.engineer_calls += 1
        return ChangeSet(
            change_set_id="durable-change",
            request_id=migration_request.request_id,
            manifest_id=manifest.manifest_id,
            base_revision=manifest.base_revision,
            changed_paths=manifest.approved_paths,
            unified_diff="+export default class Example {}",
        )

    def validator(self, migration_request, manifest, change_set):
        self.validator_calls += 1
        now = datetime.now(UTC)
        receipt = ToolReceipt(
            receipt_id="durable-receipt",
            tool_id="pytest",
            request_id=migration_request.request_id,
            run_id="durable-run",
            attempt=1,
            base_revision=manifest.base_revision,
            environment=EnvironmentKind.LOCAL,
            input_artifact_digest=artifact_digest(change_set),
            operation="allowlisted test",
            working_directory=".",
            started_at=now,
            ended_at=now,
            exit_code=0,
            terminal=True,
            redacted=False,
        )
        return ValidationReport(
            report_id="durable-report",
            request_id=migration_request.request_id,
            manifest_id=manifest.manifest_id,
            change_set_id=change_set.change_set_id,
            base_revision=manifest.base_revision,
            results=(
                CheckResult(
                    check_id="framework",
                    command_id="pytest",
                    required=True,
                    status=CheckStatus.PASSED,
                    receipt=receipt,
                    summary="Terminal test passed.",
                ),
            ),
            disposition=ValidationDisposition.READY_FOR_HUMAN_REVIEW,
            attempt=1,
        )


class FailingValidatorRoles(Roles):
    def validator(self, migration_request, manifest, change_set):
        self.validator_calls += 1
        raise RuntimeError("synthetic local validator crash")


class PreflightRoles(Roles):
    def __init__(self):
        super().__init__()
        self.architect_calls = 0

    def architect(self, migration_request):
        self.architect_calls += 1
        entry_path = migration_request.target.entry_path
        return PlanningIntervention(
            intervention_id="durable-preflight",
            request_id=migration_request.request_id,
            request_digest=artifact_digest(migration_request),
            platform=migration_request.platform,
            base_revision=migration_request.base_revision,
            reason="Durable preflight found unresolved dependency evidence.",
            requested_action=ApprovalAction.EXPAND_SCOPE,
            affected_paths=(entry_path,),
            evidence=(
                PlanningInterventionEvidence(
                    category=RiskCategory.DYNAMIC_DEPENDENCY,
                    source="dependency_graph",
                    source_digest=artifact_digest(
                        {"entry_path": entry_path, "unresolved": "dynamic-reference"}
                    ),
                    summary="One dynamic dependency could not be resolved.",
                    affected_paths=(entry_path,),
                    unresolved_items=("dynamic-reference",),
                ),
            ),
            options=(
                PlanningInterventionOption.EXPAND_SCOPE_AND_REPLAN,
                PlanningInterventionOption.STOP_REQUEST,
            ),
            recommendation="Collect evidence and begin a new planning run.",
        )


def implementation_stop(migration_request, manifest) -> ImplementationIntervention:
    output_path = manifest.approved_paths[0]
    input_digest = artifact_digest(
        {
            "request": artifact_digest(migration_request),
            "manifest": artifact_digest(manifest),
            "source": "frozen-engineer-input",
        }
    )
    return ImplementationIntervention(
        intervention_id="durable-implementation-stop",
        request_id=migration_request.request_id,
        request_digest=artifact_digest(migration_request),
        manifest_id=manifest.manifest_id,
        manifest_digest=artifact_digest(manifest),
        base_revision=manifest.base_revision,
        agent_version="engineer/v11",
        agent_definition_digest="sha256:" + "a" * 64,
        input_evidence_digest=input_digest,
        reason="The frozen implementation input lacks a required contract.",
        requested_action=ApprovalAction.EXPAND_SCOPE,
        affected_paths=(output_path,),
        public_concerns=("Implementing now would require guessing.",),
        evidence=(
            ImplementationInterventionEvidence(
                source="engineer_input",
                source_digest=input_digest,
                summary="The complete frozen input is insufficient.",
                affected_paths=(output_path,),
            ),
        ),
        options=(
            PlanningInterventionOption.EXPAND_SCOPE_AND_REPLAN,
            PlanningInterventionOption.STOP_REQUEST,
        ),
        recommendation="Resolve the evidence gap and revise the manifest.",
    )


class ImplementationStopRoles(Roles):
    def engineer(self, migration_request, manifest):
        self.engineer_calls += 1
        return implementation_stop(migration_request, manifest)

    def validator(self, migration_request, manifest, change_set):
        self.validator_calls += 1
        raise AssertionError("Validator must not run after an implementation stop")


class RecoverableThenImplementationStopRoles(Roles):
    def engineer(
        self,
        migration_request,
        manifest,
        *,
        attempt=1,
        correction: CorrectionAttemptEvidence | None = None,
    ):
        if attempt == 1:
            assert correction is None
            return super().engineer(migration_request, manifest)
        assert correction is not None
        self.engineer_calls += 1
        return implementation_stop(migration_request, manifest)

    def validator(
        self,
        migration_request,
        manifest,
        change_set,
        *,
        attempt=1,
        correction: CorrectionAttemptEvidence | None = None,
    ):
        assert correction is None
        report = super().validator(migration_request, manifest, change_set)
        if attempt != 1:
            raise AssertionError("Validator must not run after the attempt-2 stop")
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


def approval_from(paused) -> ManifestApproval:
    payload = paused.interrupts[0].value
    decision = payload["decision"]
    return ManifestApproval(
        decision_id=decision["decision_id"],
        request_id=decision["request_id"],
        manifest_id=payload["manifest_id"],
        manifest_digest=payload["manifest_digest"],
        requested_action=decision["requested_action"],
        selection=ApprovalSelection.APPROVE,
        reviewer="durable-reviewer",
    )


def test_workflow_resumes_from_disk_after_connection_restart(tmp_path: Path):
    database = tmp_path / "checkpoints.sqlite3"
    first_roles = Roles()
    with durable_migration_workflow(
        database,
        first_roles.architect,
        first_roles.engineer,
        first_roles.validator,
    ) as workflow:
        paused = workflow.start(request(), thread_id="durable-thread")
        approval = approval_from(paused)
    assert first_roles.engineer_calls == 0

    resumed_roles = Roles()
    with durable_migration_workflow(
        database,
        resumed_roles.architect,
        resumed_roles.engineer,
        resumed_roles.validator,
    ) as workflow:
        result = workflow.resume(approval, thread_id="durable-thread")
        assert result.value["status"] == WorkflowStatus.COMPLETED
    assert resumed_roles.engineer_calls == 1
    assert resumed_roles.validator_calls == 1


def test_terminal_planning_intervention_survives_checkpoint_restart(tmp_path: Path):
    database = tmp_path / "planning-intervention.sqlite3"
    first_roles = PreflightRoles()
    with durable_migration_workflow(
        database,
        first_roles.architect,
        first_roles.engineer,
        first_roles.validator,
    ) as workflow:
        result = workflow.start(request(), thread_id="durable-planning-stop")
        assert result.interrupts == ()
        assert result.value["status"] == WorkflowStatus.DECISION_REQUIRED
    assert first_roles.architect_calls == 1
    assert first_roles.engineer_calls == 0
    assert first_roles.validator_calls == 0

    restarted_roles = PreflightRoles()
    with durable_migration_workflow(
        database,
        restarted_roles.architect,
        restarted_roles.engineer,
        restarted_roles.validator,
    ) as workflow:
        snapshot = workflow.snapshot(thread_id="durable-planning-stop")
        assert snapshot.next == ()
        assert isinstance(snapshot.values["planning_intervention"], PlanningIntervention)
        assert snapshot.values["status"] == WorkflowStatus.DECISION_REQUIRED
        with pytest.raises(PolicyViolation, match="already has workflow state"):
            workflow.start(request(), thread_id="durable-planning-stop")
    assert restarted_roles.architect_calls == 0
    assert restarted_roles.engineer_calls == 0
    assert restarted_roles.validator_calls == 0


def test_terminal_implementation_intervention_survives_checkpoint_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "implementation-intervention.sqlite3"
    initial_roles = ImplementationStopRoles()
    with durable_migration_workflow(
        database,
        initial_roles.architect,
        initial_roles.engineer,
        initial_roles.validator,
    ) as workflow:
        paused = workflow.start(request(), thread_id="durable-implementation-stop")
        approval = approval_from(paused)
    assert initial_roles.engineer_calls == 0

    resumed_roles = ImplementationStopRoles()
    with durable_migration_workflow(
        database,
        resumed_roles.architect,
        resumed_roles.engineer,
        resumed_roles.validator,
    ) as workflow:
        result = workflow.resume(approval, thread_id="durable-implementation-stop")
        assert result.interrupts == ()
        assert result.value["status"] == WorkflowStatus.DECISION_REQUIRED
        assert isinstance(
            result.value["implementation_intervention"],
            ImplementationIntervention,
        )
        assert "change_set" not in result.value
        assert "validation_report" not in result.value
    assert resumed_roles.engineer_calls == 1
    assert resumed_roles.validator_calls == 0

    restarted_roles = ImplementationStopRoles()
    with durable_migration_workflow(
        database,
        restarted_roles.architect,
        restarted_roles.engineer,
        restarted_roles.validator,
    ) as workflow:
        snapshot = workflow.snapshot(thread_id="durable-implementation-stop")
        assert snapshot.next == ()
        assert snapshot.values["status"] == WorkflowStatus.DECISION_REQUIRED
        assert isinstance(
            snapshot.values["implementation_intervention"],
            ImplementationIntervention,
        )
    assert restarted_roles.engineer_calls == 0
    assert restarted_roles.validator_calls == 0


def test_attempt_2_implementation_intervention_cannot_extend_correction_authority(
    tmp_path: Path,
) -> None:
    database = tmp_path / "attempt-2-implementation-stop.sqlite3"
    roles = RecoverableThenImplementationStopRoles()
    with durable_migration_workflow(
        database,
        roles.architect,
        roles.engineer,
        roles.validator,
    ) as workflow:
        paused = workflow.start(request(), thread_id="attempt-2-implementation-stop")
        first = workflow.resume(
            approval_from(paused),
            thread_id="attempt-2-implementation-stop",
        )
        assert first.value["terminal_disposition"] is ValidationDisposition.RECOVERABLE_FAILURE
        correction = CorrectionRequest.model_validate(first.value["correction_request"])
        approval = CorrectionController.approve_retry(
            correction,
            presented_correction_id=correction.correction_id,
            reviewer="durable-correction-reviewer",
        )

        stopped = workflow.retry_recoverable(
            approval,
            thread_id="attempt-2-implementation-stop",
        )

        assert stopped.interrupts == ()
        assert stopped.value["execution_attempt"] == 2
        assert stopped.value["status"] == WorkflowStatus.DECISION_REQUIRED
        assert stopped.value["terminal_disposition"] is ValidationDisposition.DECISION_REQUIRED
        assert isinstance(
            stopped.value["implementation_intervention"],
            ImplementationIntervention,
        )
        assert "grants no implementation authority" in stopped.value["terminal_reason"]
        assert "newly evidence-bound decision" in stopped.value["decision_request"].recommendation
        with pytest.raises(PolicyViolation, match="already completed"):
            workflow.retry_recoverable(
                approval,
                thread_id="attempt-2-implementation-stop",
            )
        with pytest.raises(PolicyViolation, match="no pending local task"):
            workflow.continue_local_failure(thread_id="attempt-2-implementation-stop")

    assert roles.engineer_calls == 2
    assert roles.validator_calls == 1


def test_checkpoint_path_rejects_symlink_and_wrong_suffix(tmp_path: Path):
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"")
    link = tmp_path / "checkpoints.sqlite3"
    link.symlink_to(outside)
    roles = Roles()
    with pytest.raises(PolicyViolation, match="non-symlink"):
        with durable_migration_workflow(
            link,
            roles.architect,
            roles.engineer,
            roles.validator,
        ):
            pass
    with pytest.raises(ValueError, match=".sqlite3"):
        with durable_migration_workflow(
            tmp_path / "checkpoints.db",
            roles.architect,
            roles.engineer,
            roles.validator,
        ):
            pass


def test_failed_local_validator_can_continue_without_replaying_approval(
    tmp_path: Path,
):
    database = tmp_path / "checkpoints.sqlite3"
    failing_roles = FailingValidatorRoles()
    with pytest.raises(RuntimeError, match="validator crash"):
        with durable_migration_workflow(
            database,
            failing_roles.architect,
            failing_roles.engineer,
            failing_roles.validator,
        ) as workflow:
            paused = workflow.start(request(), thread_id="failure-recovery-thread")
            workflow.resume(
                approval_from(paused),
                thread_id="failure-recovery-thread",
            )
    assert failing_roles.engineer_calls == 1
    assert failing_roles.validator_calls == 1

    recovered_roles = Roles()
    with durable_migration_workflow(
        database,
        recovered_roles.architect,
        recovered_roles.engineer,
        recovered_roles.validator,
    ) as workflow:
        result = workflow.continue_local_failure(thread_id="failure-recovery-thread")
    assert result.value["status"] == WorkflowStatus.COMPLETED
    assert recovered_roles.engineer_calls == 0
    assert recovered_roles.validator_calls == 1
