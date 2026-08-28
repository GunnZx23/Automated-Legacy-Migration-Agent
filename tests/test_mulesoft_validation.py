import pytest
from pydantic import ValidationError

from legacy_migration_agent.platforms.mulesoft_validation import (
    MuleSoftEvidenceError,
    MuleSoftEvidenceSource,
    MuleSoftValidationContext,
    MuleSoftValidationEvidence,
    MuleSoftValidationStatus,
    MUnitExecutionPhase,
    MUnitFailureReason,
    parse_munit_surefire_xml,
)

ARTIFACT_DIGEST = f"sha256:{'a' * 64}"


def context(**overrides) -> MuleSoftValidationContext:
    values = {
        "request_id": "request-1",
        "run_id": "run-1",
        "base_revision": "abcdef0",
        "artifact_digest": ARTIFACT_DIGEST,
    }
    values.update(overrides)
    return MuleSoftValidationContext(**values)


def suite_xml(
    outcomes=("pass", "pass"),
    *,
    suite_name="migration-suite",
    secret_payload="",
) -> str:
    failures = outcomes.count("failure")
    errors = outcomes.count("error")
    skipped = outcomes.count("skipped")
    cases = []
    for index, outcome in enumerate(outcomes):
        marker = ""
        if outcome == "failure":
            marker = f'<failure message="credential={secret_payload}">raw stack {secret_payload}</failure>'
        elif outcome == "error":
            marker = f'<error message="path=/private/{secret_payload}">raw error {secret_payload}</error>'
        elif outcome == "skipped":
            marker = "<skipped/>"
        cases.append(
            f'<testcase name="private-test-{index}" classname="secret.Class" time="0.1">{marker}</testcase>'
        )
    return (
        f'<testsuite name="{suite_name}" tests="{len(outcomes)}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}" time="0.2">'
        f"{''.join(cases)}<system-out>opaque diagnostic sentinel {secret_payload}</system-out></testsuite>"
    )


def test_completed_surefire_report_passes_and_is_bound_without_raw_details():
    secret = "mule-secret-token"
    evidence = parse_munit_surefire_xml(
        suite_xml(secret_payload=secret),
        command_exit_code=0,
        context=context(),
    )

    assert evidence.source is MuleSoftEvidenceSource.SUREFIRE_XML
    assert evidence.status is MuleSoftValidationStatus.PASSED
    assert evidence.phase is MUnitExecutionPhase.COMPLETED
    assert evidence.validation_terminal is True
    assert evidence.local_process_exited is True
    assert evidence.report_count == 1
    assert (evidence.suites, evidence.tests, evidence.passed) == (1, 2, 2)
    assert evidence.request_id == "request-1"
    assert evidence.run_id == "run-1"
    assert evidence.base_revision == "abcdef0"
    assert evidence.artifact_digest == ARTIFACT_DIGEST
    serialized = evidence.model_dump_json()
    assert "private-test" not in serialized
    assert "secret.Class" not in serialized
    assert secret not in serialized
    assert "/private/" not in serialized


@pytest.mark.parametrize(
    "outcomes, expected_failures, expected_errors",
    [
        (("pass", "failure"), 1, 0),
        (("pass", "error"), 0, 1),
        (("failure", "error"), 1, 1),
    ],
)
def test_failure_and_error_markers_map_terminal_failed(
    outcomes, expected_failures, expected_errors
):
    evidence = parse_munit_surefire_xml(
        suite_xml(outcomes, secret_payload="do-not-retain"),
        command_exit_code=1,
        context=context(),
    )

    assert evidence.status is MuleSoftValidationStatus.FAILED
    assert evidence.failure_reason is MUnitFailureReason.TEST_FAILURES_AND_COMMAND_EXIT
    assert evidence.failures == expected_failures
    assert evidence.errors == expected_errors
    assert "do-not-retain" not in evidence.model_dump_json()


def test_test_failure_remains_failed_when_maven_exit_was_ignored():
    evidence = parse_munit_surefire_xml(
        suite_xml(("pass", "failure")),
        command_exit_code=0,
        context=context(),
    )

    assert evidence.status is MuleSoftValidationStatus.FAILED
    assert evidence.failure_reason is MUnitFailureReason.TEST_FAILURES


def test_all_passing_tests_with_nonzero_command_exit_fail_the_validation():
    evidence = parse_munit_surefire_xml(
        suite_xml(),
        command_exit_code=1,
        context=context(),
    )

    assert evidence.status is MuleSoftValidationStatus.FAILED
    assert evidence.failure_reason is MUnitFailureReason.COMMAND_EXIT
    assert evidence.passed == 2


def test_some_skipped_tests_can_pass_but_all_skipped_cannot_be_green():
    partial = parse_munit_surefire_xml(
        suite_xml(("pass", "skipped")),
        command_exit_code=0,
        context=context(),
    )
    all_skipped = parse_munit_surefire_xml(
        suite_xml(("skipped", "skipped")),
        command_exit_code=0,
        context=context(),
    )

    assert partial.status is MuleSoftValidationStatus.PASSED
    assert partial.passed == 1
    assert all_skipped.status is MuleSoftValidationStatus.FAILED
    assert all_skipped.failure_reason is MUnitFailureReason.NO_EXECUTED_TESTS


def test_testsuites_root_and_multiple_documents_aggregate_exactly():
    first = suite_xml(("pass",), suite_name="suite-a")
    second = suite_xml(("pass", "skipped"), suite_name="suite-b")
    aggregate = (
        f'<testsuites tests="3" failures="0" errors="0" skipped="1">{first}{second}</testsuites>'
    )
    from_root = parse_munit_surefire_xml(
        aggregate,
        command_exit_code=0,
        context=context(),
    )
    from_documents = parse_munit_surefire_xml(
        (first, second),
        command_exit_code=0,
        context=context(),
    )

    assert (from_root.report_count, from_root.suites, from_root.tests, from_root.skipped) == (
        1,
        2,
        3,
        1,
    )
    assert (from_documents.report_count, from_documents.suites, from_documents.tests) == (2, 2, 3)


def test_namespaced_surefire_xml_bytes_are_supported_without_retaining_names():
    xml = (
        b'<testsuite xmlns="urn:junit" name="private-suite" tests="1" failures="0" errors="0" skipped="0">'
        b'<testcase name="private-case" classname="private.Class"/></testsuite>'
    )
    evidence = parse_munit_surefire_xml(xml, command_exit_code=0, context=context())

    assert evidence.status is MuleSoftValidationStatus.PASSED
    assert evidence.tests == 1
    assert "private" not in evidence.model_dump_json()


@pytest.mark.parametrize(
    "xml, match",
    [
        ("<testsuite", "malformed XML"),
        ("<project/>", "root must be"),
        ('<testsuite tests="1" failures="0" errors="0" skipped="0"/>', "test count contradicts"),
        (
            '<testsuite tests="1" failures="1" errors="0" skipped="0"><testcase/></testsuite>',
            "outcomes contradict",
        ),
        (
            '<testsuite tests="1" failures="1" errors="1" skipped="0">'
            "<testcase><failure/><error/></testcase></testsuite>",
            "cannot exceed tests",
        ),
        ('<testsuite tests="-1" failures="0" errors="0" skipped="0"/>', "nonnegative integers"),
        ('<testsuite tests="one" failures="0" errors="0" skipped="0"/>', "nonnegative integers"),
        ("<testsuites/>", "at least one direct testsuite"),
    ],
)
def test_malformed_or_contradictory_xml_fails_closed(xml, match):
    with pytest.raises((MuleSoftEvidenceError, ValueError), match=match):
        parse_munit_surefire_xml(xml, command_exit_code=1, context=context())


def test_root_aggregate_counts_must_match_child_suites():
    xml = (
        '<testsuites tests="3" failures="0" errors="0" skipped="0">'
        f"{suite_xml(('pass', 'pass'))}</testsuites>"
    )
    with pytest.raises(MuleSoftEvidenceError, match="aggregate counts contradict"):
        parse_munit_surefire_xml(xml, command_exit_code=0, context=context())


def test_testcase_cannot_have_multiple_terminal_markers_even_if_totals_fit():
    xml = (
        '<testsuite tests="2" failures="1" errors="1" skipped="0">'
        "<testcase><failure/><error/></testcase><testcase/></testsuite>"
    )
    with pytest.raises(MuleSoftEvidenceError, match="contradictory terminal outcome"):
        parse_munit_surefire_xml(xml, command_exit_code=1, context=context())


def test_dtd_entities_and_duplicate_reports_are_rejected():
    dtd = '<!DOCTYPE testsuite [<!ENTITY secret "value">]><testsuite tests="0" failures="0" errors="0" skipped="0"/>'
    with pytest.raises(MuleSoftEvidenceError, match="DTD or entity"):
        parse_munit_surefire_xml(dtd, command_exit_code=0, context=context())
    report = suite_xml(("pass",))
    with pytest.raises(MuleSoftEvidenceError, match="duplicate"):
        parse_munit_surefire_xml((report, report), command_exit_code=0, context=context())


def test_malformed_xml_exception_does_not_chain_secret_payload():
    with pytest.raises(MuleSoftEvidenceError) as captured:
        parse_munit_surefire_xml(
            '<testsuite opaque="sensitive-sentinel">',
            command_exit_code=1,
            context=context(),
        )

    assert captured.value.__cause__ is None
    assert "sensitive-sentinel" not in str(captured.value)


def test_zero_test_report_is_not_implicitly_not_applicable():
    xml = '<testsuite tests="0" failures="0" errors="0" skipped="0"/>'
    with pytest.raises(ValueError, match="at least one selected test"):
        parse_munit_surefire_xml(xml, command_exit_code=0, context=context())


@pytest.mark.parametrize("exit_code", [True, 256, -256, "1"])
def test_xml_parser_rejects_invalid_exit_code_types_or_ranges(exit_code):
    with pytest.raises(MuleSoftEvidenceError, match="command_exit_code"):
        parse_munit_surefire_xml(
            suite_xml(("pass",)), command_exit_code=exit_code, context=context()
        )


def test_evidence_binding_detects_post_parse_mutation():
    evidence = parse_munit_surefire_xml(
        suite_xml(("pass",)),
        command_exit_code=0,
        context=context(),
    )
    tampered = {**evidence.model_dump(), "run_id": "run-tampered"}

    with pytest.raises(ValidationError, match="evidence_binding"):
        MuleSoftValidationEvidence(**tampered)
