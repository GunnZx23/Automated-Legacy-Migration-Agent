import json

import pytest
from pydantic import ValidationError

from legacy_migration_agent.contracts import CheckStatus
from legacy_migration_agent.platforms.salesforce_validation import (
    SalesforceAvailabilityReason,
    SalesforceEvidenceError,
    SalesforcePlatformStatus,
    SalesforceTestLevel,
    SalesforceValidationCommand,
    SalesforceValidationContext,
    SalesforceValidationEvidence,
    parse_apex_test_report_json,
    parse_deploy_report_json,
)

MANIFEST_DIGEST = f"sha256:{'a' * 64}"
DEPLOY_ID = "0Af000000000001AAA"
TEST_RUN_ID = "707000000000001AAA"


def deploy_context(**overrides) -> SalesforceValidationContext:
    values = {
        "command": SalesforceValidationCommand.DEPLOY_REPORT,
        "org_alias": "capstone-sandbox",
        "job_id": DEPLOY_ID,
        "manifest_digest": MANIFEST_DIGEST,
        "test_level": SalesforceTestLevel.RUN_SPECIFIED_TESTS,
    }
    values.update(overrides)
    return SalesforceValidationContext(**values)


def apex_context(**overrides) -> SalesforceValidationContext:
    values = {
        "command": SalesforceValidationCommand.APEX_TEST_REPORT,
        "org_alias": "capstone-sandbox",
        "job_id": TEST_RUN_ID,
        "manifest_digest": MANIFEST_DIGEST,
        "test_level": SalesforceTestLevel.RUN_SPECIFIED_TESTS,
    }
    values.update(overrides)
    return SalesforceValidationContext(**values)


def cli_json(result, status=0, **extra) -> str:
    return json.dumps({"status": status, "result": result, "warnings": [], **extra})


def complete_deploy_result(**overrides):
    result = {
        "id": DEPLOY_ID,
        "status": "Succeeded",
        "done": True,
        "success": True,
        "testLevel": "RunSpecifiedTests",
        "numberTestsTotal": 2,
        "numberTestsCompleted": 2,
        "numberTestErrors": 0,
        "numberComponentErrors": 0,
        "files": [],
    }
    result.update(overrides)
    return result


def complete_apex_result(*, outcome="Passed", failing=0, exit_status=0):
    passing = 2 - failing
    rows = [
        {
            "FullName": "AccountSummaryControllerTest.loadsAccount",
            "Outcome": "Pass" if index < passing else "Fail",
            "Message": None if index < passing else "assertion failed",
            "StackTrace": None if index < passing else "Class.SecretTest: line 99",
        }
        for index in range(2)
    ]
    return cli_json(
        {
            "summary": {
                "outcome": outcome,
                "testsRan": 2,
                "passing": passing,
                "failing": failing,
                "skipped": 0,
                "testRunId": TEST_RUN_ID,
                "username": "should-not-be-retained@example.test",
                "hostname": "https://example.my.salesforce.com",
            },
            "tests": rows,
        },
        status=exit_status,
    )


@pytest.mark.parametrize(
    "raw_status", ["Queued", "Pending", "InProgress", "Submitted", "Canceling"]
)
def test_deploy_nonterminal_statuses_never_become_success(raw_status):
    evidence = parse_deploy_report_json(
        cli_json({"id": DEPLOY_ID, "status": raw_status, "done": False}),
        command_exit_code=0,
        context=deploy_context(),
    )

    assert evidence.local_process_exited is True
    assert evidence.platform_terminal is False
    assert evidence.check_status is CheckStatus.NONTERMINAL
    assert evidence.platform_status is not SalesforcePlatformStatus.SUCCEEDED


def test_zero_process_exit_is_not_a_terminal_org_job():
    evidence = parse_deploy_report_json(
        cli_json({"id": DEPLOY_ID, "status": "Queued"}),
        command_exit_code=0,
        context=deploy_context(),
    )

    assert evidence.command_exit_code == 0
    assert evidence.local_process_exited is True
    assert evidence.platform_terminal is False
    assert evidence.check_status is CheckStatus.NONTERMINAL


def test_completed_deploy_with_complete_tests_is_passed_and_context_bound():
    context = deploy_context()
    evidence = parse_deploy_report_json(
        cli_json(complete_deploy_result()),
        command_exit_code=0,
        context=context,
    )

    assert evidence.check_status is CheckStatus.PASSED
    assert evidence.platform_status is SalesforcePlatformStatus.SUCCEEDED
    assert evidence.platform_terminal is True
    assert evidence.org_alias == context.org_alias
    assert evidence.job_id == DEPLOY_ID
    assert evidence.manifest_digest == MANIFEST_DIGEST
    assert evidence.test_level is SalesforceTestLevel.RUN_SPECIFIED_TESTS
    assert (evidence.tests_total, evidence.tests_completed, evidence.tests_failed) == (2, 2, 0)
    assert evidence.evidence_binding.startswith("sha256:")


def test_no_test_run_success_requires_explicit_zero_test_counts():
    context = deploy_context(test_level=SalesforceTestLevel.NO_TEST_RUN)
    evidence = parse_deploy_report_json(
        cli_json(
            complete_deploy_result(
                testLevel="NoTestRun",
                numberTestsTotal="0",
                numberTestsCompleted="0",
            )
        ),
        command_exit_code=0,
        context=context,
    )

    assert evidence.check_status is CheckStatus.PASSED
    assert evidence.tests_total == 0


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"done": False}, "done=true"),
        ({"numberTestsCompleted": 1}, "complete passing"),
        ({"numberTestErrors": 1}, "complete passing"),
        ({"numberComponentErrors": 1}, "complete passing"),
        ({"numberTestsTotal": 0, "numberTestsCompleted": 0}, "no executed tests"),
        ({"numberTestsCompleted": None}, "nonnegative integer"),
    ],
)
def test_deploy_success_with_incomplete_or_contradictory_evidence_fails_closed(overrides, match):
    with pytest.raises(SalesforceEvidenceError, match=match):
        parse_deploy_report_json(
            cli_json(complete_deploy_result(**overrides)),
            command_exit_code=0,
            context=deploy_context(),
        )


@pytest.mark.parametrize("status", ["Failed", "Canceled", "Aborted", "SucceededPartial"])
def test_terminal_deploy_failure_maps_failed(status):
    evidence = parse_deploy_report_json(
        cli_json(
            complete_deploy_result(status=status, success=False, numberComponentErrors=1),
            status=1,
        ),
        command_exit_code=1,
        context=deploy_context(),
    )

    assert evidence.check_status is CheckStatus.FAILED
    assert evidence.platform_terminal is True


def test_failed_deploy_with_zero_cli_exit_is_contradictory():
    with pytest.raises(SalesforceEvidenceError, match="command exit"):
        parse_deploy_report_json(
            cli_json(complete_deploy_result(status="Failed", success=False)),
            command_exit_code=0,
            context=deploy_context(),
        )


@pytest.mark.parametrize("status", ["Queued", "Pending", "Processing", "In Progress", "Submitted"])
def test_apex_async_statuses_map_nonterminal(status):
    evidence = parse_apex_test_report_json(
        cli_json({"testRunId": TEST_RUN_ID, "status": status}),
        command_exit_code=0,
        context=apex_context(),
    )

    assert evidence.check_status is CheckStatus.NONTERMINAL
    assert evidence.platform_terminal is False


def test_apex_async_id_without_status_is_submitted_not_passed():
    evidence = parse_apex_test_report_json(
        cli_json({"testRunId": TEST_RUN_ID}),
        command_exit_code=0,
        context=apex_context(),
    )

    assert evidence.platform_status is SalesforcePlatformStatus.SUBMITTED
    assert evidence.check_status is CheckStatus.NONTERMINAL


def test_complete_apex_pass_maps_passed_without_retaining_raw_identity_or_test_details():
    evidence = parse_apex_test_report_json(
        complete_apex_result(),
        command_exit_code=0,
        context=apex_context(),
    )

    assert evidence.check_status is CheckStatus.PASSED
    assert evidence.platform_status is SalesforcePlatformStatus.PASSED
    assert evidence.platform_terminal is True
    assert evidence.tests_total == evidence.tests_completed == 2
    serialized = evidence.model_dump_json()
    assert "AccountSummaryControllerTest" not in serialized
    assert "example.my.salesforce.com" not in serialized
    assert "should-not-be-retained" not in serialized


def test_complete_apex_failure_maps_failed_without_retaining_stack_or_message():
    evidence = parse_apex_test_report_json(
        complete_apex_result(outcome="Failed", failing=1, exit_status=100),
        command_exit_code=100,
        context=apex_context(),
    )

    assert evidence.check_status is CheckStatus.FAILED
    assert evidence.tests_failed == 1
    serialized = evidence.model_dump_json()
    assert "assertion failed" not in serialized
    assert "Class.SecretTest" not in serialized


@pytest.mark.parametrize("outcome", ["Aborted", "Canceled", "Error"])
def test_terminal_apex_failure_without_test_rows_still_maps_failed(outcome):
    evidence = parse_apex_test_report_json(
        cli_json(
            {
                "summary": {
                    "outcome": outcome,
                    "testRunId": TEST_RUN_ID,
                }
            },
            status=1,
        ),
        command_exit_code=1,
        context=apex_context(),
    )

    assert evidence.check_status is CheckStatus.FAILED
    assert evidence.platform_terminal is True
    assert evidence.tests_total is None


@pytest.mark.parametrize(
    "mutator, match",
    [
        (lambda body: body["result"]["summary"].update(testsRan=3), "account for every"),
        (lambda body: body["result"].update(tests=[]), "array is incomplete"),
        (lambda body: body["result"]["tests"][0].update(Outcome="Fail"), "contradict summary"),
        (lambda body: body["result"]["summary"].update(testRunId="707000000000002AAA"), "job ID"),
    ],
)
def test_apex_contradictions_fail_closed(mutator, match):
    body = json.loads(complete_apex_result())
    mutator(body)
    with pytest.raises(SalesforceEvidenceError, match=match):
        parse_apex_test_report_json(
            json.dumps(body),
            command_exit_code=0,
            context=apex_context(),
        )


def test_auth_error_maps_unavailable_and_does_not_retain_error_or_secret():
    secret = "force://client:super-secret@example.test"
    payload = json.dumps(
        {
            "status": 1,
            "name": "INVALID_SESSION_ID",
            "message": f"Authentication failed for {secret}",
            "stack": "private raw stack",
        }
    )

    evidence = parse_deploy_report_json(
        payload,
        command_exit_code=1,
        context=deploy_context(),
    )

    assert evidence.check_status is CheckStatus.UNAVAILABLE
    assert evidence.availability_reason is SalesforceAvailabilityReason.AUTHENTICATION
    assert evidence.platform_terminal is False
    assert secret not in evidence.model_dump_json()
    assert "private raw stack" not in evidence.model_dump_json()


def test_plain_network_and_missing_tool_failures_map_unavailable():
    network = parse_deploy_report_json(
        "",
        stderr="getaddrinfo ENOTFOUND instance.salesforce.com token=secret",
        command_exit_code=1,
        context=deploy_context(),
    )
    tool = parse_apex_test_report_json(
        "",
        stderr="zsh: command not found: sf",
        command_exit_code=127,
        context=apex_context(),
    )

    assert network.availability_reason is SalesforceAvailabilityReason.NETWORK
    assert tool.availability_reason is SalesforceAvailabilityReason.TOOL_UNAVAILABLE
    assert "secret" not in network.model_dump_json()
    assert "command not found" not in tool.model_dump_json()


def test_nested_network_cause_maps_unavailable_without_retaining_cause():
    payload = json.dumps(
        {
            "status": 1,
            "name": "RequestError",
            "message": "request failed",
            "cause": {"code": "UND_ERR_CONNECT_TIMEOUT", "message": "token=secret"},
        }
    )
    evidence = parse_deploy_report_json(
        payload,
        command_exit_code=1,
        context=deploy_context(),
    )

    assert evidence.availability_reason is SalesforceAvailabilityReason.NETWORK
    assert "token=secret" not in evidence.model_dump_json()


@pytest.mark.parametrize("payload", ["{not-json", "[]", '"value"'])
def test_malformed_or_non_object_json_fails_closed(payload):
    with pytest.raises(SalesforceEvidenceError):
        parse_deploy_report_json(
            payload,
            command_exit_code=1,
            context=deploy_context(),
        )


def test_malformed_json_exception_does_not_chain_raw_secret_payload():
    with pytest.raises(SalesforceEvidenceError) as captured:
        parse_deploy_report_json(
            '{"access_token":"super-secret",',
            command_exit_code=1,
            context=deploy_context(),
        )

    assert captured.value.__cause__ is None
    assert "super-secret" not in str(captured.value)


def test_json_envelope_exit_mismatch_fails_closed():
    with pytest.raises(SalesforceEvidenceError, match="contradicts"):
        parse_deploy_report_json(
            cli_json({"id": DEPLOY_ID, "status": "Queued"}, status=0),
            command_exit_code=1,
            context=deploy_context(),
        )


@pytest.mark.parametrize("exit_code", [True, 256, -256, "1"])
def test_invalid_command_exit_type_or_range_fails_closed(exit_code):
    with pytest.raises(SalesforceEvidenceError, match="command_exit_code"):
        parse_deploy_report_json(
            cli_json({"id": DEPLOY_ID, "status": "Queued"}),
            command_exit_code=exit_code,
            context=deploy_context(),
        )


def test_unknown_platform_status_fails_closed():
    with pytest.raises(SalesforceEvidenceError, match="unknown Salesforce deploy status"):
        parse_deploy_report_json(
            cli_json({"id": DEPLOY_ID, "status": "MaybeDone"}),
            command_exit_code=0,
            context=deploy_context(),
        )


def test_payload_test_level_must_match_bound_context():
    with pytest.raises(SalesforceEvidenceError, match="test level contradicts"):
        parse_deploy_report_json(
            cli_json(complete_deploy_result(testLevel="RunLocalTests")),
            command_exit_code=0,
            context=deploy_context(),
        )


def test_context_enforces_command_specific_job_id_and_apex_test_level():
    with pytest.raises(ValidationError, match="beginning with 0Af"):
        deploy_context(job_id=TEST_RUN_ID)
    with pytest.raises(ValidationError, match="test-run ID"):
        apex_context(job_id=DEPLOY_ID)
    with pytest.raises(ValidationError, match="Apex test reports require"):
        apex_context(test_level=SalesforceTestLevel.NO_TEST_RUN)


def test_evidence_binding_detects_post_parse_mutation():
    evidence = parse_deploy_report_json(
        cli_json(complete_deploy_result()),
        command_exit_code=0,
        context=deploy_context(),
    )
    tampered = {**evidence.model_dump(), "org_alias": "different-sandbox"}

    with pytest.raises(ValidationError, match="evidence_binding"):
        SalesforceValidationEvidence(**tampered)
