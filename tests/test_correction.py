from datetime import UTC, datetime

import pytest

from legacy_migration_agent.agent_runtime.correction import CorrectionAction, CorrectionController
from legacy_migration_agent.contracts import (
    ChangeSet,
    CheckResult,
    CheckStatus,
    EnvironmentKind,
    MigrationManifest,
    Platform,
    ToolReceipt,
    TransformationStep,
    ValidationCommand,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation

REVISION = "sha256:" + "1" * 64


def artifacts():
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    manifest = MigrationManifest(
        manifest_id="manifest-correction-contract",
        request_id="request-correction-contract",
        platform=Platform.SALESFORCE,
        base_revision=REVISION,
        approved_paths=(path,),
        dependencies=(),
        transformations=(
            TransformationStep(
                step_id="implement-approved-lwc",
                description="Implement the exact approved LWC controller change.",
                input_paths=(path,),
                output_paths=(path,),
            ),
        ),
        validation_plan=(
            ValidationCommand(
                check_id="candidate-contract",
                command_id="salesforce-candidate-check",
                purpose="Validate the bounded generated Salesforce candidate.",
            ),
        ),
    )
    change_set = ChangeSet(
        change_set_id="changes-correction-contract",
        request_id=manifest.request_id,
        manifest_id=manifest.manifest_id,
        base_revision=REVISION,
        changed_paths=(path,),
        unified_diff="--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new\n",
    )
    now = datetime(2026, 8, 24, tzinfo=UTC)
    receipt = ToolReceipt(
        receipt_id="receipt-correction-contract",
        tool_id="salesforce-candidate-check",
        request_id=manifest.request_id,
        run_id="run-correction-contract",
        attempt=1,
        base_revision=REVISION,
        environment=EnvironmentKind.LOCAL,
        input_artifact_digest=artifact_digest(change_set),
        operation="allowlisted candidate validation",
        working_directory=".",
        started_at=now,
        ended_at=now,
        exit_code=0,
        terminal=True,
        stdout_digest="sha256:" + "2" * 64,
        stderr_digest="sha256:" + "3" * 64,
        redacted=True,
    )
    report = ValidationReport(
        report_id="report-correction-contract",
        request_id=manifest.request_id,
        manifest_id=manifest.manifest_id,
        change_set_id=change_set.change_set_id,
        base_revision=REVISION,
        results=(
            CheckResult(
                check_id="candidate-contract",
                command_id="salesforce-candidate-check",
                required=True,
                status=CheckStatus.PASSED,
                receipt=receipt,
                summary="The bounded candidate passed its local contract.",
            ),
        ),
        disposition=ValidationDisposition.READY_FOR_HUMAN_REVIEW,
        attempt=1,
        completed_at=now,
    )
    return manifest, change_set, report


def failed_report(
    disposition: ValidationDisposition,
    *,
    attempt: int = 1,
    unavailable: bool = False,
) -> ValidationReport:
    _, _, report = artifacts()
    results = []
    for index, result in enumerate(report.results):
        receipt = result.receipt
        if receipt is not None and attempt != receipt.attempt:
            receipt = receipt.model_copy(update={"attempt": attempt})
        if index == 0:
            if unavailable:
                result = CheckResult(
                    check_id=result.check_id,
                    command_id=result.command_id,
                    required=result.required,
                    status=CheckStatus.UNAVAILABLE,
                    summary="Seeded environment unavailable.",
                )
            else:
                assert receipt is not None
                failed_receipt = receipt.model_copy(update={"exit_code": 1})
                result = CheckResult(
                    check_id=result.check_id,
                    command_id=result.command_id,
                    required=result.required,
                    status=CheckStatus.FAILED,
                    receipt=failed_receipt,
                    summary="Seeded implementation failure.",
                )
        elif receipt is not None:
            result = result.model_copy(update={"receipt": receipt})
        results.append(result)
    return ValidationReport(
        report_id=f"seeded-{disposition.value}-attempt-{attempt}",
        request_id=report.request_id,
        manifest_id=report.manifest_id,
        change_set_id=report.change_set_id,
        base_revision=report.base_revision,
        results=tuple(results),
        disposition=disposition,
        attempt=attempt,
    )


def test_ready_report_is_complete_without_retry():
    manifest, change_set, report = artifacts()
    decision = CorrectionController().evaluate(manifest, change_set, report)

    assert decision.action is CorrectionAction.COMPLETE
    assert decision.failed_check_ids == ()
    assert decision.next_attempt is None


def test_recoverable_failure_gets_one_same_manifest_retry_then_stops():
    manifest, change_set, _ = artifacts()
    controller = CorrectionController(maximum_attempts=2)

    retry = controller.evaluate(
        manifest,
        change_set,
        failed_report(ValidationDisposition.RECOVERABLE_FAILURE),
    )
    exhausted = controller.evaluate(
        manifest,
        change_set,
        failed_report(ValidationDisposition.RECOVERABLE_FAILURE, attempt=2),
    )

    assert retry.action is CorrectionAction.RETRY_IMPLEMENTATION
    assert retry.next_attempt == 2
    assert retry.requires_new_manifest_approval is False
    assert retry.requires_new_manifest_digest is False
    assert retry.manifest_digest == artifact_digest(manifest)
    assert exhausted.action is CorrectionAction.STOP_EXHAUSTED
    assert exhausted.next_attempt is None


def test_recoverable_failure_preserves_ordered_typed_diagnostics() -> None:
    manifest, change_set, _ = artifacts()
    report = failed_report(ValidationDisposition.RECOVERABLE_FAILURE)
    result = report.results[0].model_copy(
        update={
            "diagnostic_ids": (
                "jest_wire_emit_before_subscription",
                "candidate-contract",
                "jest_stale_assertion_vacuous",
            )
        }
    )
    report = report.model_copy(update={"results": (result,)})

    decision = CorrectionController().evaluate(manifest, change_set, report)

    assert decision.failed_check_ids == (
        "candidate-contract",
        "jest_wire_emit_before_subscription",
        "jest_stale_assertion_vacuous",
    )


def test_environment_unavailable_stops_without_consuming_retry():
    manifest, change_set, _ = artifacts()
    decision = CorrectionController().evaluate(
        manifest,
        change_set,
        failed_report(
            ValidationDisposition.ENVIRONMENT_UNAVAILABLE,
            unavailable=True,
        ),
    )

    assert decision.action is CorrectionAction.STOP_ENVIRONMENT
    assert decision.next_attempt is None


def test_controller_toolchain_failure_does_not_offer_engineer_retry() -> None:
    manifest, change_set, report = artifacts()
    toolchain_id = "salesforce-toolchain-contract"
    manifest = manifest.model_copy(
        update={
            "validation_plan": (
                ValidationCommand(
                    check_id=toolchain_id,
                    command_id=toolchain_id,
                    purpose="Verify the pinned controller-owned Jest toolchain.",
                ),
            )
        }
    )
    passed = report.results[0]
    assert passed.receipt is not None
    failed = CheckResult(
        check_id=toolchain_id,
        command_id=toolchain_id,
        required=True,
        status=CheckStatus.FAILED,
        receipt=passed.receipt.model_copy(update={"tool_id": toolchain_id, "exit_code": 1}),
        summary="The controller-owned toolchain attestation failed.",
    )
    report = report.model_copy(
        update={
            "results": (failed,),
            "disposition": ValidationDisposition.RECOVERABLE_FAILURE,
        }
    )

    decision = CorrectionController().evaluate(manifest, change_set, report)

    assert decision.action is CorrectionAction.STOP_ENVIRONMENT
    assert decision.next_attempt is None
    assert decision.failed_check_ids == (toolchain_id,)


def test_invalid_plan_requires_new_manifest_approval():
    manifest, change_set, _ = artifacts()
    decision = CorrectionController().evaluate(
        manifest,
        change_set,
        failed_report(ValidationDisposition.PLAN_INVALID),
    )

    assert decision.action is CorrectionAction.REPLAN_WITH_NEW_APPROVAL
    assert decision.requires_new_manifest_approval is True
    assert decision.requires_new_manifest_digest is True

    with pytest.raises(PolicyViolation, match="new manifest digest and approval"):
        CorrectionController.approve_retry(
            decision,
            presented_correction_id=decision.correction_id,
            reviewer="reviewer-1",
        )


def test_retry_approval_is_bound_to_the_exact_correction_request():
    manifest, change_set, _ = artifacts()
    request = CorrectionController().evaluate(
        manifest,
        change_set,
        failed_report(ValidationDisposition.RECOVERABLE_FAILURE),
    )

    approval = CorrectionController.approve_retry(
        request,
        presented_correction_id=request.correction_id,
        reviewer="reviewer-1",
        comment="Retry only the exact local attempt.",
    )

    assert approval.correction_id == request.correction_id
    assert approval.manifest_digest == artifact_digest(manifest)
    assert approval.report_digest == request.report_digest
    assert approval.change_set_digest == request.change_set_digest
    assert approval.authorized_attempt == 2
    with pytest.raises(PolicyViolation, match="correction_id does not match"):
        CorrectionController.approve_retry(
            request,
            presented_correction_id="correction-tampered",
            reviewer="reviewer-1",
        )
