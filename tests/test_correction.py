from datetime import UTC, datetime

import pytest

from legacy_migration_agent.agent_runtime.correction import (
    CorrectionAction,
    CorrectionController,
    CorrectionRequest,
    correction_failure_ids,
    implementation_failure_ids,
    validation_failure_dependencies,
)
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


def lwc_load_failure_artifacts() -> tuple[MigrationManifest, ChangeSet, ValidationReport]:
    manifest, change_set, report = artifacts()
    commands = (
        ValidationCommand(
            check_id="salesforce-candidate-contract",
            command_id="salesforce-candidate-contract",
            purpose="Validate the bounded generated Salesforce candidate.",
        ),
        ValidationCommand(
            check_id="salesforce-lwc-jest",
            command_id="salesforce-lwc-jest",
            purpose="Run candidate-authored LWC Jest.",
        ),
        ValidationCommand(
            check_id="salesforce-lwc-controller-jest",
            command_id="salesforce-lwc-controller-jest",
            purpose="Run controller-owned LWC Jest.",
        ),
    )
    manifest = manifest.model_copy(update={"validation_plan": commands})
    base_receipt = report.results[0].receipt
    assert base_receipt is not None
    summaries = (
        (
            "Candidate contract failed; "
            "failure-code=salesforce_lwc_javascript_contract; "
            "diagnostics=salesforce_lwc_javascript_contract; exit=1."
        ),
        (
            "LWC Jest failed terminally suites=1 tests=0 failed-suites=1 "
            "failed-tests=0; stdout=sha256:receipt."
        ),
        (
            "Controller-owned LWC behavior Jest failed terminally suites=1 tests=0 "
            "failed-suites=1 failed-tests=0; stdout=sha256:receipt."
        ),
    )
    diagnostics = (
        ("salesforce_lwc_javascript_contract",),
        ("candidate_jest_execution_failure",),
        ("controller_jest_execution_failure",),
    )
    results = tuple(
        CheckResult(
            check_id=command.check_id,
            command_id=command.command_id,
            required=True,
            status=CheckStatus.FAILED,
            receipt=base_receipt.model_copy(
                update={
                    "receipt_id": f"receipt-{command.check_id}",
                    "tool_id": command.command_id,
                    "exit_code": 1,
                }
            ),
            summary=summary,
            diagnostic_ids=diagnostic_ids,
        )
        for command, summary, diagnostic_ids in zip(
            commands,
            summaries,
            diagnostics,
            strict=True,
        )
    )
    return (
        manifest,
        change_set,
        report.model_copy(
            update={
                "results": results,
                "disposition": ValidationDisposition.RECOVERABLE_FAILURE,
            }
        ),
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


def test_lwc_load_failure_clusters_exact_zero_test_cascade_without_mutating_report() -> None:
    manifest, change_set, report = lwc_load_failure_artifacts()
    raw_report = report.model_dump(mode="json")
    raw_digest = artifact_digest(report)

    dependencies = validation_failure_dependencies(report)
    decision = CorrectionController().evaluate(manifest, change_set, report)

    assert dependencies == {
        "salesforce-lwc-jest": "salesforce-candidate-contract",
        "salesforce-lwc-controller-jest": "salesforce-candidate-contract",
    }
    assert correction_failure_ids(report) == (
        "salesforce-candidate-contract",
        "salesforce_lwc_javascript_contract",
    )
    assert implementation_failure_ids(report) == (
        "salesforce-candidate-contract",
        "salesforce_lwc_javascript_contract",
    )
    assert decision.failed_check_ids == correction_failure_ids(report)
    assert report.model_dump(mode="json") == raw_report
    assert artifact_digest(report) == raw_digest
    assert tuple(result.status for result in report.results) == (
        CheckStatus.FAILED,
        CheckStatus.FAILED,
        CheckStatus.FAILED,
    )


def test_lwc_load_failure_does_not_cluster_partial_or_nonzero_test_evidence() -> None:
    _manifest, _change_set, report = lwc_load_failure_artifacts()
    candidate_jest = report.results[1].model_copy(
        update={
            "summary": (
                "LWC Jest failed terminally suites=1 tests=1 failed-suites=1 "
                "failed-tests=1; stdout=sha256:receipt."
            )
        }
    )
    report = report.model_copy(
        update={"results": (report.results[0], candidate_jest, report.results[2])}
    )

    assert validation_failure_dependencies(report) == {}
    assert correction_failure_ids(report) == (
        "salesforce-candidate-contract",
        "salesforce_lwc_javascript_contract",
        "salesforce-lwc-jest",
        "candidate_jest_execution_failure",
        "salesforce-lwc-controller-jest",
        "controller_jest_execution_failure",
    )


def test_zero_test_results_remain_independent_without_javascript_or_template_root() -> None:
    _manifest, _change_set, report = lwc_load_failure_artifacts()
    unrelated_root = report.results[0].model_copy(
        update={
            "summary": (
                "Candidate contract failed; failure-code=salesforce_lwc_styles_contract; "
                "diagnostics=salesforce_lwc_styles_contract; exit=1."
            ),
            "diagnostic_ids": ("salesforce_lwc_styles_contract",),
        }
    )
    report = report.model_copy(
        update={"results": (unrelated_root, report.results[1], report.results[2])}
    )

    assert validation_failure_dependencies(report) == {}
    assert correction_failure_ids(report) == (
        "salesforce-candidate-contract",
        "salesforce_lwc_styles_contract",
        "salesforce-lwc-jest",
        "candidate_jest_execution_failure",
        "salesforce-lwc-controller-jest",
        "controller_jest_execution_failure",
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


@pytest.mark.parametrize(
    "diagnostic_id",
    ("salesforce_candidate_inventory", "salesforce_candidate_unclassified"),
)
def test_structurally_invalid_candidate_does_not_offer_impossible_engineer_retry(
    diagnostic_id: str,
) -> None:
    manifest, change_set, report = artifacts()
    command_id = "salesforce-candidate-contract"
    manifest = manifest.model_copy(
        update={
            "validation_plan": (
                ValidationCommand(
                    check_id=command_id,
                    command_id=command_id,
                    purpose="Validate the bounded generated Salesforce candidate.",
                ),
            )
        }
    )
    original = report.results[0]
    assert original.receipt is not None
    failed = CheckResult(
        check_id=command_id,
        command_id=command_id,
        required=True,
        status=CheckStatus.FAILED,
        receipt=original.receipt.model_copy(update={"tool_id": command_id, "exit_code": 1}),
        summary=f"Candidate contract failed; failure-code={diagnostic_id}; exit=1.",
        diagnostic_ids=(diagnostic_id,),
    )
    report = report.model_copy(
        update={
            "results": (failed,),
            "disposition": ValidationDisposition.RECOVERABLE_FAILURE,
        }
    )

    assert implementation_failure_ids(report) == ()
    decision = CorrectionController().evaluate(manifest, change_set, report)
    assert decision.action is CorrectionAction.REPLAN_WITH_NEW_APPROVAL
    assert decision.next_attempt is None
    assert decision.requires_new_manifest_approval is True
    assert decision.requires_new_manifest_digest is True
    assert decision.requires_graph_regeneration is False


def test_invalid_plan_requires_new_manifest_approval():
    manifest, change_set, _ = artifacts()
    decision = CorrectionController().evaluate(
        manifest,
        change_set,
        failed_report(ValidationDisposition.PLAN_INVALID),
    )

    assert decision.action is CorrectionAction.REPLAN_WITH_NEW_APPROVAL
    assert decision.next_attempt is None
    assert decision.requires_new_manifest_approval is True
    assert decision.requires_new_manifest_digest is True
    assert decision.requires_graph_regeneration is True

    with pytest.raises(
        PolicyViolation, match="graph regeneration, a new manifest digest, and approval"
    ):
        CorrectionController.approve_retry(
            decision,
            presented_correction_id=decision.correction_id,
            reviewer="reviewer-1",
        )


def test_graph_regeneration_cannot_be_attached_to_an_engineer_retry() -> None:
    manifest, change_set, _ = artifacts()
    retry = CorrectionController().evaluate(
        manifest,
        change_set,
        failed_report(ValidationDisposition.RECOVERABLE_FAILURE),
    )

    with pytest.raises(ValueError, match="graph regeneration requires replanning"):
        CorrectionRequest.model_validate(
            retry.model_dump(mode="json") | {"requires_graph_regeneration": True}
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
