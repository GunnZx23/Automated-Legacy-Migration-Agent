from datetime import UTC, datetime
from pathlib import Path

import pytest

from legacy_migration_agent.contracts import (
    ApprovalAction,
    ChangeSet,
    CheckResult,
    CheckStatus,
    DependencyEvidence,
    EnvironmentKind,
    MigrationManifest,
    MigrationRequest,
    Platform,
    ToolReceipt,
    TransformationStep,
    ValidationCommand,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import (
    CommandRegistry,
    CommandSpec,
    ExecutionApproval,
    PolicyViolation,
    RetryBudget,
    ensure_paths_within_repository,
    require_execution_approval,
    validate_change_set,
    validate_manifest_for_request,
    validate_report,
)

INPUT_DIGEST = f"sha256:{'a' * 64}"


def manifest() -> MigrationManifest:
    return MigrationManifest(
        manifest_id="manifest-1",
        request_id="request-1",
        platform=Platform.SALESFORCE,
        base_revision="abcdef0",
        approved_paths=("force-app/main/default/lwc/accountSummary/accountSummary.js",),
        dependencies=(
            DependencyEvidence(
                path="force-app/main/default/pages/AccountSummary.page",
                relation="migration source",
                source="request",
            ),
        ),
        transformations=(
            TransformationStep(
                step_id="create-lwc",
                description="Create the LWC JavaScript module",
                input_paths=("force-app/main/default/pages/AccountSummary.page",),
                output_paths=("force-app/main/default/lwc/accountSummary/accountSummary.js",),
            ),
        ),
        validation_plan=(
            ValidationCommand(
                check_id="lwc-jest",
                command_id="salesforce-lwc-jest",
                purpose="Run Jest",
            ),
        ),
    )


def change_set(*paths: str) -> ChangeSet:
    return ChangeSet(
        change_set_id="changes-1",
        request_id="request-1",
        manifest_id="manifest-1",
        base_revision="abcdef0",
        changed_paths=paths,
        unified_diff="diff --git a/file b/file",
    )


def receipt(
    *,
    tool_id: str = "salesforce-lwc-jest",
    attempt: int = 1,
    run_id: str = "run-1",
    input_digest: str | None = None,
) -> ToolReceipt:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    return ToolReceipt(
        receipt_id=f"receipt-{tool_id}-{attempt}",
        tool_id=tool_id,
        request_id="request-1",
        run_id=run_id,
        attempt=attempt,
        base_revision="abcdef0",
        environment=EnvironmentKind.LOCAL,
        input_artifact_digest=input_digest
        or artifact_digest(
            change_set("force-app/main/default/lwc/accountSummary/accountSummary.js")
        ),
        operation="allowlisted command",
        working_directory=".",
        started_at=now,
        ended_at=now,
        exit_code=0,
        terminal=True,
    )


def result(
    *,
    check_id: str = "lwc-jest",
    command_id: str = "salesforce-lwc-jest",
    required: bool = True,
    tool_id: str | None = None,
    run_id: str = "run-1",
    input_digest: str | None = None,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        command_id=command_id,
        required=required,
        status=CheckStatus.PASSED,
        receipt=receipt(
            tool_id=tool_id or command_id,
            run_id=run_id,
            input_digest=input_digest,
        ),
        summary="passed",
    )


def report(*results: CheckResult) -> ValidationReport:
    return ValidationReport(
        report_id="report-1",
        request_id="request-1",
        manifest_id="manifest-1",
        change_set_id="changes-1",
        base_revision="abcdef0",
        results=results,
        disposition=ValidationDisposition.READY_FOR_HUMAN_REVIEW,
        attempt=1,
    )


def test_change_set_must_be_subset_of_manifest_paths():
    with pytest.raises(PolicyViolation, match="outside the manifest"):
        validate_change_set(
            change_set("force-app/main/default/classes/Unapproved.cls"),
            manifest(),
        )


def test_execution_approval_requires_every_exact_binding(tmp_path: Path):
    command = CommandSpec(
        command_id="sandbox-validation",
        argv_prefix=("true",),
        allowed_working_directories=(tmp_path,),
        requires_approval=ApprovalAction.SANDBOX_VALIDATION,
    )
    approval = ExecutionApproval(
        decision_id="decision-1",
        request_id="request-1",
        action=ApprovalAction.SANDBOX_VALIDATION,
        artifact_digest=INPUT_DIGEST,
        base_revision="abcdef0",
        command_id="sandbox-validation",
        environment=EnvironmentKind.LOCAL,
        reviewer="reviewer@example.test",
    )
    require_execution_approval(
        command,
        decision_id="decision-1",
        request_id="request-1",
        artifact_digest=INPUT_DIGEST,
        base_revision="abcdef0",
        command_id="sandbox-validation",
        environment=EnvironmentKind.LOCAL,
        approval=approval,
    )
    with pytest.raises(PolicyViolation, match="base_revision does not match"):
        require_execution_approval(
            command,
            decision_id="decision-1",
            request_id="request-1",
            artifact_digest=INPUT_DIGEST,
            base_revision="fffffff",
            command_id="sandbox-validation",
            environment=EnvironmentKind.LOCAL,
            approval=approval,
        )


def test_retry_budget_stops_at_limit():
    budget = RetryBudget(maximum_attempts=2)
    budget.require_remaining(1)
    with pytest.raises(PolicyViolation, match="exhausted"):
        budget.require_remaining(2)


def test_command_registry_rejects_generated_command_ids(tmp_path: Path):
    registry = CommandRegistry(
        [
            CommandSpec(
                command_id="framework-tests",
                argv_prefix=("python", "-m", "pytest"),
                allowed_working_directories=(tmp_path,),
                allowed_argument_vectors=(("tests",),),
                sanitized_environment=(("PYTHONIOENCODING", "utf-8"),),
            )
        ]
    )
    resolved = registry.resolve(
        "framework-tests",
        ("tests",),
        tmp_path,
        EnvironmentKind.LOCAL,
    )
    assert resolved.argv == (
        "python",
        "-m",
        "pytest",
        "tests",
    )
    assert resolved.spec.environment_map() == {"PYTHONIOENCODING": "utf-8"}
    with pytest.raises(PolicyViolation, match="exact allowed argument"):
        registry.resolve("framework-tests", ("tests", "--pdb"), tmp_path, EnvironmentKind.LOCAL)
    with pytest.raises(PolicyViolation, match="unknown command_id"):
        registry.resolve("rm-everything", (), tmp_path, EnvironmentKind.LOCAL)


def test_command_registry_rejects_duplicate_ids(tmp_path: Path):
    spec = CommandSpec(
        command_id="framework-tests",
        argv_prefix=("true",),
        allowed_working_directories=(tmp_path,),
    )
    with pytest.raises(ValueError, match="duplicate command_id"):
        CommandRegistry((spec, spec))


def test_validation_report_requires_exact_plan_result_and_receipt_mapping():
    valid_manifest = manifest()
    valid_change_set = change_set(*valid_manifest.approved_paths)
    validate_report(report(result()), valid_manifest, valid_change_set)

    with pytest.raises(PolicyViolation, match="wrong command_id"):
        validate_report(
            report(result(command_id="different-command")),
            valid_manifest,
            valid_change_set,
        )
    with pytest.raises(PolicyViolation, match="wrong tool"):
        validate_report(
            report(result(tool_id="different-command")),
            valid_manifest,
            valid_change_set,
        )
    with pytest.raises(PolicyViolation, match="unplanned checks"):
        validate_report(
            report(
                result(),
                result(check_id="invented-check", command_id="invented-command", required=False),
            ),
            valid_manifest,
            valid_change_set,
        )


def test_validation_report_binds_receipts_to_change_set_and_one_run():
    valid_manifest = manifest()
    valid_change_set = change_set(*valid_manifest.approved_paths)

    with pytest.raises(PolicyViolation, match="exact change set"):
        validate_report(
            report(result(input_digest=INPUT_DIGEST)),
            valid_manifest,
            valid_change_set,
        )

    second_check = ValidationCommand(
        check_id="security-check",
        command_id="security-tool",
        purpose="Run a second independent check",
    )
    two_check_manifest = valid_manifest.model_copy(
        update={"validation_plan": (*valid_manifest.validation_plan, second_check)}
    )
    with pytest.raises(PolicyViolation, match="different workflow runs"):
        validate_report(
            report(
                result(),
                result(
                    check_id="security-check",
                    command_id="security-tool",
                    run_id="run-2",
                ),
            ),
            two_check_manifest,
            valid_change_set,
        )


def test_manifest_transformations_must_explain_exact_approved_scope():
    valid_manifest = manifest()
    request = MigrationRequest(
        request_id="request-1",
        platform=Platform.SALESFORCE,
        repository="fixture",
        base_revision="abcdef0",
        target={
            "entry_path": "force-app/main/default/pages/AccountSummary.page",
            "target_runtime": "LWC",
            "source_version": "Salesforce API 67.0",
            "target_version": "Salesforce API 67.0",
            "description": "Migrate the fixture",
        },
    )
    validate_manifest_for_request(valid_manifest, request)
    unexplained = valid_manifest.model_copy(
        update={"approved_paths": (*valid_manifest.approved_paths, "extra.txt")}
    )
    with pytest.raises(PolicyViolation, match="not explained"):
        validate_manifest_for_request(unexplained, request)


def test_validation_report_rejects_required_flag_changes():
    valid_change_set = change_set(*manifest().approved_paths)
    optional_check = ValidationCommand(
        check_id="advisory-scan",
        command_id="salesforce-advisory-scan",
        purpose="Run an advisory scan",
        required=False,
    )
    mixed_manifest = manifest().model_copy(
        update={"validation_plan": (*manifest().validation_plan, optional_check)}
    )
    with pytest.raises(PolicyViolation, match="required flag"):
        validate_report(
            report(
                result(),
                result(
                    check_id="advisory-scan",
                    command_id="salesforce-advisory-scan",
                    required=True,
                ),
            ),
            mixed_manifest,
            valid_change_set,
        )


def test_validation_report_rejects_missing_and_duplicate_results():
    second_check = ValidationCommand(
        check_id="apex-tests",
        command_id="salesforce-apex-tests",
        purpose="Run Apex tests",
    )
    two_check_manifest = manifest().model_copy(
        update={"validation_plan": (*manifest().validation_plan, second_check)}
    )
    valid_change_set = change_set(*two_check_manifest.approved_paths)
    with pytest.raises(PolicyViolation, match="omits planned checks"):
        validate_report(report(result()), two_check_manifest, valid_change_set)
    with pytest.raises(PolicyViolation, match="duplicate check results"):
        validate_report(report(result(), result()), manifest(), valid_change_set)


def test_repository_path_guard_rejects_symlink_escape(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    safe_dir = repo / "safe"
    safe_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "escape").symlink_to(outside, target_is_directory=True)

    assert ensure_paths_within_repository(repo, ("safe/new.txt",)) == (safe_dir / "new.txt",)
    with pytest.raises(PolicyViolation, match="escapes repository root"):
        ensure_paths_within_repository(repo, ("escape/new.txt",))
