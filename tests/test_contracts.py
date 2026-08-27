from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from legacy_migration_agent.contracts import (
    ApprovalAction,
    ArtifactDigest,
    ChangeSet,
    CheckResult,
    CheckStatus,
    DecisionRequest,
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
    RiskFinding,
    ToolReceipt,
    TransformationStep,
    ValidationCommand,
    ValidationDisposition,
    ValidationReport,
    validate_relative_path,
)

NOW = datetime(2026, 8, 24, tzinfo=UTC)
INPUT_DIGEST = f"sha256:{'a' * 64}"


def receipt(exit_code: int = 0, *, terminal: bool = True) -> ToolReceipt:
    return ToolReceipt(
        receipt_id=f"receipt-{exit_code}-{terminal}",
        tool_id="pytest",
        request_id="request-1",
        run_id="run-1",
        attempt=1,
        base_revision="abcdef0",
        environment=EnvironmentKind.LOCAL,
        input_artifact_digest=INPUT_DIGEST,
        operation="python -m pytest",
        working_directory="/workspace",
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=1),
        exit_code=exit_code if terminal else None,
        terminal=terminal,
        stdout_digest=f"sha256:{'c' * 64}",
        redacted=True,
    )


def manifest(**overrides) -> MigrationManifest:
    values = {
        "manifest_id": "manifest-1",
        "request_id": "request-1",
        "platform": Platform.SALESFORCE,
        "base_revision": "abcdef0",
        "approved_paths": ("force-app/main/default/lwc/accountSummary/accountSummary.js",),
        "dependencies": (
            DependencyEvidence(
                path="force-app/main/default/classes/AccountSummaryController.cls",
                relation="calls",
                source="static-reference-scan",
            ),
        ),
        "transformations": (
            TransformationStep(
                step_id="create-lwc",
                description="Create LWC bundle",
                input_paths=("force-app/main/default/pages/AccountSummary.page",),
                output_paths=("force-app/main/default/lwc/accountSummary/accountSummary.js",),
            ),
        ),
        "validation_plan": (
            ValidationCommand(
                check_id="lwc-jest",
                command_id="salesforce-lwc-jest",
                purpose="Run LWC Jest tests",
            ),
        ),
    }
    values.update(overrides)
    return MigrationManifest(**values)


def test_relative_paths_reject_traversal_and_absolute_paths():
    for unsafe in (
        "../secret",
        "/etc/passwd",
        "a//b",
        "a/./b",
        "C:/temp/file",
        "artifact\nname.json",
    ):
        with pytest.raises(ValueError):
            validate_relative_path(unsafe)


def test_request_rejects_production_execution_environment():
    with pytest.raises(ValidationError, match="production cannot be selected"):
        MigrationRequest(
            request_id="request-1",
            platform=Platform.SALESFORCE,
            repository="/workspace/repo",
            base_revision="abcdef0",
            target=MigrationTarget(
                entry_path="force-app/main/default/pages/AccountSummary.page",
                target_runtime="LWC",
                source_version="Salesforce API 67.0",
                target_version="Salesforce API 67.0",
                description="Migrate the page",
            ),
            allowed_environment=EnvironmentKind.PRODUCTION,
        )


def test_manifest_requires_decision_for_unresolved_dependency():
    unresolved = DependencyEvidence(
        path="force-app/main/default/classes/UnknownConsumer.cls",
        relation="dynamic reference",
        source="static-reference-scan",
        resolved=False,
    )
    with pytest.raises(ValidationError, match="decision_required"):
        manifest(dependencies=(unresolved,))


def test_manifest_requires_explicit_approval_for_mandatory_risk():
    risk = RiskFinding(
        category=RiskCategory.SECURITY,
        summary="Apex sharing behavior changes",
        evidence=("AccountSummaryController.cls",),
    )
    with pytest.raises(ValidationError, match="approval"):
        manifest(status=ManifestStatus.DECISION_REQUIRED, risks=(risk,))


def test_manifest_rejects_duplicate_validation_check_ids():
    duplicate = ValidationCommand(
        check_id="lwc-jest",
        command_id="salesforce-security-scan",
        purpose="Run a different tool under an accidentally reused ID",
    )
    with pytest.raises(ValidationError, match="check_ids must be unique"):
        manifest(validation_plan=(*manifest().validation_plan, duplicate))


def test_change_set_rejects_empty_changed_paths():
    with pytest.raises(ValidationError, match="changed_paths"):
        ChangeSet(
            change_set_id="changes-1",
            request_id="request-1",
            manifest_id="manifest-1",
            base_revision="abcdef0",
            changed_paths=(),
            unified_diff="diff --git a/a b/a",
        )


def test_ready_report_requires_all_required_checks_to_pass():
    failed = CheckResult(
        check_id="lwc-jest",
        command_id="salesforce-lwc-jest",
        required=True,
        status=CheckStatus.FAILED,
        receipt=receipt(1),
        summary="one Jest test failed",
    )
    with pytest.raises(ValidationError, match="every required check"):
        ValidationReport(
            report_id="report-1",
            request_id="request-1",
            manifest_id="manifest-1",
            change_set_id="changes-1",
            base_revision="abcdef0",
            results=(failed,),
            disposition=ValidationDisposition.READY_FOR_HUMAN_REVIEW,
            attempt=1,
        )


def test_passed_check_requires_terminal_zero_exit_receipt():
    with pytest.raises(ValidationError, match="terminal receipt"):
        CheckResult(
            check_id="lwc-jest",
            command_id="salesforce-lwc-jest",
            required=True,
            status=CheckStatus.PASSED,
            receipt=receipt(terminal=False),
            summary="still running",
        )


def test_check_result_diagnostics_are_optional_unique_identifiers():
    result = CheckResult(
        check_id="lwc-jest",
        command_id="salesforce-lwc-jest",
        required=True,
        status=CheckStatus.FAILED,
        receipt=receipt(1),
        summary="A bounded Jest clause failed.",
        diagnostic_ids=(
            "candidate_jest_execution_failure",
            "controller_jest_stale_response",
        ),
    )

    assert result.diagnostic_ids == (
        "candidate_jest_execution_failure",
        "controller_jest_stale_response",
    )
    assert (
        CheckResult(
            check_id="optional-diagnostics",
            command_id="optional-diagnostics",
            required=False,
            status=CheckStatus.UNAVAILABLE,
            summary="No diagnostic identifier is required for old payloads.",
        ).diagnostic_ids
        == ()
    )

    with pytest.raises(ValidationError, match="diagnostic_ids must be unique"):
        CheckResult.model_validate(
            {
                **result.model_dump(),
                "diagnostic_ids": (
                    "candidate_jest_execution_failure",
                    "candidate_jest_execution_failure",
                ),
            }
        )


def test_receipt_rejects_duplicate_artifact_paths():
    artifact = ArtifactDigest(
        path="artifacts/report.json",
        sha256=f"sha256:{'b' * 64}",
        size_bytes=12,
    )
    with pytest.raises(ValidationError, match="artifact paths must be unique"):
        ToolReceipt(**{**receipt().model_dump(), "artifacts": (artifact, artifact)})


def test_receipt_rejects_malformed_output_digest():
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        ToolReceipt(**{**receipt().model_dump(), "stdout_digest": "sha256:abc"})


def test_decision_request_requires_at_least_two_options():
    with pytest.raises(ValidationError):
        DecisionRequest(
            decision_id="decision-1",
            request_id="request-1",
            reason="A public contract changes",
            requested_action=ApprovalAction.ACCEPT_HIGH_IMPACT_CHANGE,
            affected_paths=("force-app/main/default/classes/PublicApi.cls",),
            options=("approve",),
            recommendation="reject until consumers are reviewed",
        )


def test_planning_intervention_is_evidence_bound_and_non_authorizing():
    evidence = PlanningInterventionEvidence(
        category=RiskCategory.DYNAMIC_DEPENDENCY,
        source="dependency_graph",
        source_digest="sha256:" + "d" * 64,
        summary="A dynamic reference remains unresolved.",
        affected_paths=("force-app/main/default/pages/Legacy.page",),
        unresolved_items=("dynamic-reference",),
    )
    intervention = PlanningIntervention(
        intervention_id="preflight-request-1",
        request_id="request-1",
        request_digest="sha256:" + "a" * 64,
        platform=Platform.SALESFORCE,
        base_revision="abcdef0",
        reason="Planning cannot continue with unresolved evidence.",
        requested_action=ApprovalAction.EXPAND_SCOPE,
        affected_paths=("force-app/main/default/pages/Legacy.page",),
        evidence=(evidence,),
        options=(
            PlanningInterventionOption.EXPAND_SCOPE_AND_REPLAN,
            PlanningInterventionOption.STOP_REQUEST,
        ),
        recommendation="Collect the missing dependency evidence and replan.",
    )

    assert intervention.evidence[0].source_digest == "sha256:" + "d" * 64
    with pytest.raises(ValidationError, match="scope or evidence decisions"):
        PlanningIntervention.model_validate(
            {
                **intervention.model_dump(mode="python"),
                "requested_action": ApprovalAction.APPROVE_MANIFEST,
            }
        )


def test_implementation_intervention_is_digest_bound_and_non_authorizing():
    output_path = "force-app/main/default/lwc/example/example.js"
    input_digest = "sha256:" + "e" * 64
    intervention = ImplementationIntervention(
        intervention_id="implementation-stop-request-1",
        request_id="request-1",
        request_digest="sha256:" + "a" * 64,
        manifest_id="manifest-1",
        manifest_digest="sha256:" + "b" * 64,
        base_revision="abcdef0",
        agent_version="engineer/v12",
        agent_definition_digest="sha256:" + "c" * 64,
        input_evidence_digest=input_digest,
        reason="The frozen implementation evidence is incomplete.",
        requested_action=ApprovalAction.EXPAND_SCOPE,
        affected_paths=(output_path,),
        public_concerns=("Implementation would require guessing.",),
        evidence=(
            ImplementationInterventionEvidence(
                source="engineer_input",
                source_digest=input_digest,
                summary="The complete input omits a required contract.",
                affected_paths=(output_path,),
            ),
        ),
        options=(
            PlanningInterventionOption.EXPAND_SCOPE_AND_REPLAN,
            PlanningInterventionOption.STOP_REQUEST,
        ),
        recommendation="Resolve the evidence gap and replan.",
    )

    assert intervention.status == "decision_required"
    with pytest.raises(ValidationError, match="expand_scope"):
        ImplementationIntervention.model_validate(
            {
                **intervention.model_dump(mode="python"),
                "requested_action": ApprovalAction.APPROVE_MANIFEST,
            }
        )
    with pytest.raises(ValidationError, match="must be included"):
        ImplementationIntervention.model_validate(
            {
                **intervention.model_dump(mode="python"),
                "evidence": (
                    {
                        **intervention.evidence[0].model_dump(mode="python"),
                        "affected_paths": ("outside.txt",),
                    },
                ),
            }
        )
