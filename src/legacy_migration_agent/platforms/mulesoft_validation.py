"""Pure, sanitized evidence adapters for local Maven/MUnit validation.

MUnit integrates with Maven and emits Surefire-compatible ``TEST-*.xml``
reports under ``target/surefire-reports`` by default.  This module accepts the
*contents* of those reports; it performs no filesystem, subprocess, network,
credential, or Mule runtime access.

Completed XML is checked at two levels: each suite's declared aggregate counts
must be internally possible, and those counts must match the actual direct
``testcase`` outcome markers.  Test names, class names, file paths, properties,
console output, failure messages, and stack traces are intentionally ignored.

Queued, running, explicitly not-applicable, and unavailable states cannot be
proven by a completed JUnit document.  Callers represent those states with the
strict :class:`MUnitLocalResult` contract, which accepts controlled reason codes
but no free-form diagnostic text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, cast
from xml.etree import ElementTree

from pydantic import Field, field_validator, model_validator

from legacy_migration_agent.contracts import Identifier, Revision, Sha256Digest, StrictModel

MAX_XML_REPORT_BYTES = 5 * 1024 * 1024
MAX_XML_REPORTS = 1_000
MAX_XML_ELEMENTS = 100_000


class MuleSoftEvidenceError(ValueError):
    """Raised when supplied report data cannot support a trustworthy claim."""


class MuleSoftEvidenceSource(StrEnum):
    SUREFIRE_XML = "surefire_xml"
    LOCAL_RESULT = "local_result"


class MuleSoftValidationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NONTERMINAL = "nonterminal"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"


class MUnitExecutionPhase(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"


class MUnitFailureReason(StrEnum):
    TEST_FAILURES = "test_failures"
    COMMAND_EXIT = "command_exit"
    TEST_FAILURES_AND_COMMAND_EXIT = "test_failures_and_command_exit"
    NO_EXECUTED_TESTS = "no_executed_tests"


class MUnitUnavailableReason(StrEnum):
    MAVEN_TOOL_MISSING = "maven_tool_missing"
    JAVA_RUNTIME_UNAVAILABLE = "java_runtime_unavailable"
    MULE_RUNTIME_UNAVAILABLE = "mule_runtime_unavailable"
    DEPENDENCY_RESOLUTION_UNAVAILABLE = "dependency_resolution_unavailable"
    LICENSE_UNAVAILABLE = "license_unavailable"
    REPORT_UNAVAILABLE = "report_unavailable"


class MUnitNotApplicableReason(StrEnum):
    NO_MUNIT_CHECK_PLANNED = "no_munit_check_planned"
    PLATFORM_SLICE_DEFERRED = "platform_slice_deferred"


class MuleSoftValidationContext(StrictModel):
    """Immutable workflow and artifact identity supplied by the controller."""

    request_id: Identifier
    run_id: Identifier
    base_revision: Revision
    artifact_digest: Sha256Digest


class MUnitLocalResult(StrictModel):
    """Controlled local state used when completed Surefire XML is unavailable.

    ``COMPLETED`` accepts sanitized aggregate counts.  ``QUEUED`` and
    ``RUNNING`` must not claim a process exit or final counts.
    ``NOT_APPLICABLE`` requires an explicit planning reason.  ``UNAVAILABLE``
    requires an environment/tool reason and may record a nonzero process exit
    if the tool started before the boundary failed.
    """

    phase: MUnitExecutionPhase
    command_exit_code: int | None = Field(default=None, ge=-255, le=255)
    suites: int | None = Field(default=None, ge=0)
    tests: int | None = Field(default=None, ge=0)
    failures: int | None = Field(default=None, ge=0)
    errors: int | None = Field(default=None, ge=0)
    skipped: int | None = Field(default=None, ge=0)
    unavailable_reason: MUnitUnavailableReason | None = None
    not_applicable_reason: MUnitNotApplicableReason | None = None

    @field_validator(
        "command_exit_code",
        "suites",
        "tests",
        "failures",
        "errors",
        "skipped",
        mode="before",
    )
    @classmethod
    def require_strict_optional_integers(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("MUnit numeric fields must be integers, not coerced values")
        return value

    @model_validator(mode="after")
    def validate_state_shape(self) -> MUnitLocalResult:
        counts = (self.suites, self.tests, self.failures, self.errors, self.skipped)
        if self.phase is MUnitExecutionPhase.COMPLETED:
            if self.command_exit_code is None or any(count is None for count in counts):
                raise ValueError(
                    "completed MUnit result requires an exit code and every aggregate count"
                )
            assert self.suites is not None
            assert self.tests is not None
            assert self.failures is not None
            assert self.errors is not None
            assert self.skipped is not None
            _validate_aggregate_counts(
                suites=self.suites,
                tests=self.tests,
                failures=self.failures,
                errors=self.errors,
                skipped=self.skipped,
                require_executed_tests=True,
            )
            if self.unavailable_reason is not None or self.not_applicable_reason is not None:
                raise ValueError(
                    "completed MUnit result cannot include availability/applicability reasons"
                )
        elif self.phase in {MUnitExecutionPhase.QUEUED, MUnitExecutionPhase.RUNNING}:
            if self.command_exit_code is not None or any(count is not None for count in counts):
                raise ValueError(
                    "nonterminal MUnit result cannot claim an exit code or final counts"
                )
            if self.unavailable_reason is not None or self.not_applicable_reason is not None:
                raise ValueError("nonterminal MUnit result cannot include final reason codes")
        elif self.phase is MUnitExecutionPhase.NOT_APPLICABLE:
            if self.not_applicable_reason is None:
                raise ValueError("not-applicable MUnit result requires an explicit reason")
            if self.command_exit_code is not None or any(count is not None for count in counts):
                raise ValueError("not-applicable MUnit result cannot claim execution evidence")
            if self.unavailable_reason is not None:
                raise ValueError("not-applicable MUnit result cannot also be unavailable")
        else:
            if self.unavailable_reason is None:
                raise ValueError("unavailable MUnit result requires an explicit reason")
            if self.command_exit_code == 0:
                raise ValueError("unavailable MUnit result cannot use command_exit_code 0")
            if any(count is not None for count in counts):
                raise ValueError("unavailable MUnit result cannot claim completed test counts")
            if self.not_applicable_reason is not None:
                raise ValueError("unavailable MUnit result cannot also be not applicable")
        return self


class MuleSoftValidationEvidence(StrictModel):
    """Strict aggregate evidence with no raw test or environment details."""

    source: MuleSoftEvidenceSource
    request_id: Identifier
    run_id: Identifier
    base_revision: Revision
    artifact_digest: Sha256Digest
    phase: MUnitExecutionPhase
    status: MuleSoftValidationStatus
    applicable: bool
    validation_terminal: bool
    local_process_exited: bool
    command_exit_code: int | None = Field(default=None, ge=-255, le=255)
    report_count: int | None = Field(default=None, ge=1)
    suites: int | None = Field(default=None, ge=0)
    tests: int | None = Field(default=None, ge=0)
    passed: int | None = Field(default=None, ge=0)
    failures: int | None = Field(default=None, ge=0)
    errors: int | None = Field(default=None, ge=0)
    skipped: int | None = Field(default=None, ge=0)
    failure_reason: MUnitFailureReason | None = None
    unavailable_reason: MUnitUnavailableReason | None = None
    not_applicable_reason: MUnitNotApplicableReason | None = None
    evidence_binding: Sha256Digest

    @field_validator(
        "command_exit_code",
        "report_count",
        "suites",
        "tests",
        "passed",
        "failures",
        "errors",
        "skipped",
        mode="before",
    )
    @classmethod
    def require_strict_optional_integers(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(
                "MuleSoft evidence numeric fields must be integers, not coerced values"
            )
        return value

    @field_validator("applicable", "validation_terminal", "local_process_exited", mode="before")
    @classmethod
    def require_strict_booleans(cls, value: Any) -> Any:
        if not isinstance(value, bool):
            raise ValueError("MuleSoft evidence boolean fields must be booleans")
        return value

    @model_validator(mode="after")
    def validate_evidence_state(self) -> MuleSoftValidationEvidence:
        MuleSoftValidationContext(
            request_id=self.request_id,
            run_id=self.run_id,
            base_revision=self.base_revision,
            artifact_digest=self.artifact_digest,
        )
        counts = (self.suites, self.tests, self.passed, self.failures, self.errors, self.skipped)

        if self.status in {MuleSoftValidationStatus.PASSED, MuleSoftValidationStatus.FAILED}:
            if self.phase is not MUnitExecutionPhase.COMPLETED or not self.validation_terminal:
                raise ValueError(
                    "passed/failed evidence requires a completed terminal MUnit result"
                )
            if (
                not self.applicable
                or not self.local_process_exited
                or self.command_exit_code is None
            ):
                raise ValueError("completed evidence requires an applicable exited local process")
            if any(count is None for count in counts):
                raise ValueError("completed evidence requires every aggregate count")
            assert self.suites is not None
            assert self.tests is not None
            assert self.passed is not None
            assert self.failures is not None
            assert self.errors is not None
            assert self.skipped is not None
            _validate_aggregate_counts(
                suites=self.suites,
                tests=self.tests,
                failures=self.failures,
                errors=self.errors,
                skipped=self.skipped,
                require_executed_tests=True,
            )
            if self.passed + self.failures + self.errors + self.skipped != self.tests:
                raise ValueError("completed evidence outcome counts must equal tests")
            if self.status is MuleSoftValidationStatus.PASSED:
                if self.command_exit_code != 0 or self.failures or self.errors or self.passed < 1:
                    raise ValueError(
                        "passed evidence requires exit 0 and at least one passing test"
                    )
                if self.failure_reason is not None:
                    raise ValueError("passed evidence cannot include a failure reason")
            elif self.failure_reason is None:
                raise ValueError("failed evidence requires a controlled failure reason")
        elif self.status is MuleSoftValidationStatus.NONTERMINAL:
            if self.phase not in {MUnitExecutionPhase.QUEUED, MUnitExecutionPhase.RUNNING}:
                raise ValueError("nonterminal evidence requires queued or running phase")
            if self.validation_terminal or not self.applicable or self.local_process_exited:
                raise ValueError("nonterminal evidence cannot claim terminal or exited execution")
            if self.command_exit_code is not None or any(count is not None for count in counts):
                raise ValueError("nonterminal evidence cannot include final counts or exit")
        elif self.status is MuleSoftValidationStatus.NOT_APPLICABLE:
            if self.phase is not MUnitExecutionPhase.NOT_APPLICABLE:
                raise ValueError("not-applicable evidence requires not-applicable phase")
            if self.applicable or not self.validation_terminal or self.local_process_exited:
                raise ValueError(
                    "not-applicable evidence cannot claim an executed applicable check"
                )
            if self.not_applicable_reason is None:
                raise ValueError("not-applicable evidence requires a reason")
            if self.command_exit_code is not None or any(count is not None for count in counts):
                raise ValueError("not-applicable evidence cannot include execution counts")
        else:
            if self.phase is not MUnitExecutionPhase.UNAVAILABLE:
                raise ValueError("unavailable evidence requires unavailable phase")
            if not self.applicable or self.validation_terminal or self.unavailable_reason is None:
                raise ValueError(
                    "unavailable evidence requires a nonterminal applicable boundary reason"
                )
            if self.command_exit_code == 0 or any(count is not None for count in counts):
                raise ValueError(
                    "unavailable evidence cannot claim a successful exit or test counts"
                )
            if self.local_process_exited != (self.command_exit_code is not None):
                raise ValueError(
                    "local_process_exited must match presence of an unavailable process exit"
                )

        if self.source is MuleSoftEvidenceSource.SUREFIRE_XML:
            if self.report_count is None or self.status not in {
                MuleSoftValidationStatus.PASSED,
                MuleSoftValidationStatus.FAILED,
            }:
                raise ValueError("Surefire XML evidence must be completed and include report_count")
        elif self.report_count is not None:
            raise ValueError("local-result evidence cannot claim parsed XML report_count")

        if self.status is not MuleSoftValidationStatus.FAILED and self.failure_reason is not None:
            raise ValueError("failure_reason is allowed only for failed evidence")
        if (
            self.status is not MuleSoftValidationStatus.UNAVAILABLE
            and self.unavailable_reason is not None
        ):
            raise ValueError("unavailable_reason is allowed only for unavailable evidence")
        if (
            self.status is not MuleSoftValidationStatus.NOT_APPLICABLE
            and self.not_applicable_reason is not None
        ):
            raise ValueError("not_applicable_reason is allowed only for not-applicable evidence")

        expected = _evidence_binding(
            source=self.source,
            request_id=self.request_id,
            run_id=self.run_id,
            base_revision=self.base_revision,
            artifact_digest=self.artifact_digest,
            phase=self.phase,
            status=self.status,
            applicable=self.applicable,
            validation_terminal=self.validation_terminal,
            local_process_exited=self.local_process_exited,
            command_exit_code=self.command_exit_code,
            report_count=self.report_count,
            suites=self.suites,
            tests=self.tests,
            passed=self.passed,
            failures=self.failures,
            errors=self.errors,
            skipped=self.skipped,
            failure_reason=self.failure_reason,
            unavailable_reason=self.unavailable_reason,
            not_applicable_reason=self.not_applicable_reason,
        )
        if self.evidence_binding != expected:
            raise ValueError("evidence_binding does not match normalized MuleSoft evidence")
        return self


def parse_munit_surefire_xml(
    reports: str | bytes | Sequence[str | bytes],
    *,
    command_exit_code: int,
    context: MuleSoftValidationContext,
) -> MuleSoftValidationEvidence:
    """Normalize one or more completed in-memory MUnit Surefire XML reports.

    ``reports`` should contain the bytes or text of each selected
    ``target/surefire-reports/TEST-*.xml`` artifact.  The caller is responsible
    for selecting the exact artifact set represented by
    ``context.artifact_digest``; this pure adapter neither discovers nor opens
    filesystem paths.
    """

    _validate_exit_code(command_exit_code)
    payloads = _normalize_reports(reports)
    totals = _Counts()
    seen_payloads: set[str] = set()
    for payload in payloads:
        encoded = payload.encode("utf-8") if isinstance(payload, str) else payload
        if not encoded or len(encoded) > MAX_XML_REPORT_BYTES:
            raise MuleSoftEvidenceError(
                "MUnit XML report must be non-empty and within the size limit"
            )
        digest = hashlib.sha256(encoded).hexdigest()
        if digest in seen_payloads:
            raise MuleSoftEvidenceError("duplicate MUnit XML report payload")
        seen_payloads.add(digest)
        upper_payload = encoded.upper()
        if b"<!DOCTYPE" in upper_payload or b"<!ENTITY" in upper_payload:
            raise MuleSoftEvidenceError(
                "MUnit XML report cannot contain DTD or entity declarations"
            )
        try:
            root = ElementTree.fromstring(payload)
        except (ElementTree.ParseError, UnicodeError):
            raise MuleSoftEvidenceError("MUnit Surefire report is malformed XML") from None
        if sum(1 for _ in root.iter()) > MAX_XML_ELEMENTS:
            raise MuleSoftEvidenceError("MUnit XML report exceeds the element limit")
        totals = totals + _counts_from_document(root)

    _validate_aggregate_counts(
        suites=totals.suites,
        tests=totals.tests,
        failures=totals.failures,
        errors=totals.errors,
        skipped=totals.skipped,
        require_executed_tests=True,
    )
    passed = totals.tests - totals.failures - totals.errors - totals.skipped
    failure_reason = _completed_failure_reason(
        passed=passed,
        failures=totals.failures,
        errors=totals.errors,
        command_exit_code=command_exit_code,
    )
    status = (
        MuleSoftValidationStatus.PASSED
        if failure_reason is None
        else MuleSoftValidationStatus.FAILED
    )
    return _make_evidence(
        context=context,
        source=MuleSoftEvidenceSource.SUREFIRE_XML,
        phase=MUnitExecutionPhase.COMPLETED,
        status=status,
        applicable=True,
        validation_terminal=True,
        local_process_exited=True,
        command_exit_code=command_exit_code,
        report_count=len(payloads),
        suites=totals.suites,
        tests=totals.tests,
        passed=passed,
        failures=totals.failures,
        errors=totals.errors,
        skipped=totals.skipped,
        failure_reason=failure_reason,
    )


def parse_munit_local_result(
    result: MUnitLocalResult,
    *,
    context: MuleSoftValidationContext,
) -> MuleSoftValidationEvidence:
    """Normalize an already sanitized, explicitly classified local result."""

    if result.phase is MUnitExecutionPhase.COMPLETED:
        assert result.command_exit_code is not None
        assert result.suites is not None
        assert result.tests is not None
        assert result.failures is not None
        assert result.errors is not None
        assert result.skipped is not None
        passed = result.tests - result.failures - result.errors - result.skipped
        failure_reason = _completed_failure_reason(
            passed=passed,
            failures=result.failures,
            errors=result.errors,
            command_exit_code=result.command_exit_code,
        )
        return _make_evidence(
            context=context,
            source=MuleSoftEvidenceSource.LOCAL_RESULT,
            phase=result.phase,
            status=(
                MuleSoftValidationStatus.PASSED
                if failure_reason is None
                else MuleSoftValidationStatus.FAILED
            ),
            applicable=True,
            validation_terminal=True,
            local_process_exited=True,
            command_exit_code=result.command_exit_code,
            suites=result.suites,
            tests=result.tests,
            passed=passed,
            failures=result.failures,
            errors=result.errors,
            skipped=result.skipped,
            failure_reason=failure_reason,
        )
    if result.phase in {MUnitExecutionPhase.QUEUED, MUnitExecutionPhase.RUNNING}:
        return _make_evidence(
            context=context,
            source=MuleSoftEvidenceSource.LOCAL_RESULT,
            phase=result.phase,
            status=MuleSoftValidationStatus.NONTERMINAL,
            applicable=True,
            validation_terminal=False,
            local_process_exited=False,
        )
    if result.phase is MUnitExecutionPhase.NOT_APPLICABLE:
        return _make_evidence(
            context=context,
            source=MuleSoftEvidenceSource.LOCAL_RESULT,
            phase=result.phase,
            status=MuleSoftValidationStatus.NOT_APPLICABLE,
            applicable=False,
            validation_terminal=True,
            local_process_exited=False,
            not_applicable_reason=result.not_applicable_reason,
        )
    return _make_evidence(
        context=context,
        source=MuleSoftEvidenceSource.LOCAL_RESULT,
        phase=result.phase,
        status=MuleSoftValidationStatus.UNAVAILABLE,
        applicable=True,
        validation_terminal=False,
        local_process_exited=result.command_exit_code is not None,
        command_exit_code=result.command_exit_code,
        unavailable_reason=result.unavailable_reason,
    )


class _Counts:
    __slots__ = ("suites", "tests", "failures", "errors", "skipped")

    def __init__(
        self,
        suites: int = 0,
        tests: int = 0,
        failures: int = 0,
        errors: int = 0,
        skipped: int = 0,
    ) -> None:
        self.suites = suites
        self.tests = tests
        self.failures = failures
        self.errors = errors
        self.skipped = skipped

    def __add__(self, other: _Counts) -> _Counts:
        return _Counts(
            suites=self.suites + other.suites,
            tests=self.tests + other.tests,
            failures=self.failures + other.failures,
            errors=self.errors + other.errors,
            skipped=self.skipped + other.skipped,
        )


def _normalize_reports(
    reports: str | bytes | Sequence[str | bytes],
) -> tuple[str | bytes, ...]:
    normalized: tuple[str | bytes, ...]
    if isinstance(reports, (str, bytes)):
        normalized = (reports,)
    elif isinstance(reports, Sequence):
        normalized = tuple(cast(Sequence[str | bytes], reports))
    else:
        raise MuleSoftEvidenceError("reports must be XML text/bytes or a sequence of them")
    if not normalized or len(normalized) > MAX_XML_REPORTS:
        raise MuleSoftEvidenceError("reports must contain between 1 and 1000 XML documents")
    if any(not isinstance(report, (str, bytes)) for report in normalized):
        raise MuleSoftEvidenceError("every MUnit report must be XML text or bytes")
    return normalized


def _counts_from_document(root: ElementTree.Element) -> _Counts:
    root_name = _local_name(root.tag)
    if root_name == "testsuite":
        return _counts_from_suite(root)
    if root_name != "testsuites":
        raise MuleSoftEvidenceError("MUnit report root must be testsuite or testsuites")

    suites = tuple(child for child in root if _local_name(child.tag) == "testsuite")
    if not suites:
        raise MuleSoftEvidenceError("testsuites report must contain at least one direct testsuite")
    totals = _Counts()
    for suite in suites:
        totals = totals + _counts_from_suite(suite)

    aggregate_keys = ("tests", "failures", "errors", "skipped")
    present = tuple(key in root.attrib for key in aggregate_keys)
    if any(present):
        if not all(present):
            raise MuleSoftEvidenceError(
                "testsuites root contains only a partial aggregate count set"
            )
        root_counts = _Counts(
            suites=totals.suites,
            tests=_count_attribute(root, "tests"),
            failures=_count_attribute(root, "failures"),
            errors=_count_attribute(root, "errors"),
            skipped=_count_attribute(root, "skipped"),
        )
        if (
            root_counts.tests,
            root_counts.failures,
            root_counts.errors,
            root_counts.skipped,
        ) != (totals.tests, totals.failures, totals.errors, totals.skipped):
            raise MuleSoftEvidenceError("testsuites aggregate counts contradict child suites")
    return totals


def _counts_from_suite(suite: ElementTree.Element) -> _Counts:
    declared = _Counts(
        suites=1,
        tests=_count_attribute(suite, "tests"),
        failures=_count_attribute(suite, "failures"),
        errors=_count_attribute(suite, "errors"),
        skipped=_count_attribute(suite, "skipped"),
    )
    _validate_aggregate_counts(
        suites=1,
        tests=declared.tests,
        failures=declared.failures,
        errors=declared.errors,
        skipped=declared.skipped,
        require_executed_tests=False,
    )
    testcases = tuple(child for child in suite if _local_name(child.tag) == "testcase")
    if len(testcases) != declared.tests:
        raise MuleSoftEvidenceError("testsuite declared test count contradicts testcase elements")

    observed_failures = 0
    observed_errors = 0
    observed_skipped = 0
    for testcase in testcases:
        outcome_markers = [
            _local_name(child.tag)
            for child in testcase
            if _local_name(child.tag) in {"failure", "error", "skipped"}
        ]
        if len(outcome_markers) > 1:
            raise MuleSoftEvidenceError("testcase contains contradictory terminal outcome markers")
        if outcome_markers == ["failure"]:
            observed_failures += 1
        elif outcome_markers == ["error"]:
            observed_errors += 1
        elif outcome_markers == ["skipped"]:
            observed_skipped += 1

    if (
        observed_failures,
        observed_errors,
        observed_skipped,
    ) != (declared.failures, declared.errors, declared.skipped):
        raise MuleSoftEvidenceError("testsuite aggregate outcomes contradict testcase markers")
    return declared


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        raise MuleSoftEvidenceError("MUnit XML contains an unsupported node")
    return tag.rsplit("}", 1)[-1]


def _count_attribute(element: ElementTree.Element, key: str) -> int:
    value = element.attrib.get(key)
    if value is None or re.fullmatch(r"[0-9]+", value.strip()) is None:
        raise MuleSoftEvidenceError("MUnit XML count attributes must be nonnegative integers")
    return int(value)


def _validate_aggregate_counts(
    *,
    suites: int,
    tests: int,
    failures: int,
    errors: int,
    skipped: int,
    require_executed_tests: bool,
) -> None:
    values = (suites, tests, failures, errors, skipped)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise MuleSoftEvidenceError("MUnit aggregate counts must be nonnegative integers")
    if suites < 1:
        raise MuleSoftEvidenceError("MUnit aggregate requires at least one suite")
    if failures + errors + skipped > tests:
        raise MuleSoftEvidenceError("MUnit failure, error, and skipped counts cannot exceed tests")
    if require_executed_tests and tests < 1:
        raise MuleSoftEvidenceError("completed MUnit evidence requires at least one selected test")


def _completed_failure_reason(
    *,
    passed: int,
    failures: int,
    errors: int,
    command_exit_code: int,
) -> MUnitFailureReason | None:
    has_test_failures = failures > 0 or errors > 0
    has_command_failure = command_exit_code != 0
    if passed == 0 and not has_test_failures:
        return MUnitFailureReason.NO_EXECUTED_TESTS
    if has_test_failures and has_command_failure:
        return MUnitFailureReason.TEST_FAILURES_AND_COMMAND_EXIT
    if has_test_failures:
        return MUnitFailureReason.TEST_FAILURES
    if has_command_failure:
        return MUnitFailureReason.COMMAND_EXIT
    return None


def _validate_exit_code(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not -255 <= value <= 255:
        raise MuleSoftEvidenceError("command_exit_code must be an integer from -255 through 255")


def _make_evidence(
    *,
    context: MuleSoftValidationContext,
    source: MuleSoftEvidenceSource,
    phase: MUnitExecutionPhase,
    status: MuleSoftValidationStatus,
    applicable: bool,
    validation_terminal: bool,
    local_process_exited: bool,
    command_exit_code: int | None = None,
    report_count: int | None = None,
    suites: int | None = None,
    tests: int | None = None,
    passed: int | None = None,
    failures: int | None = None,
    errors: int | None = None,
    skipped: int | None = None,
    failure_reason: MUnitFailureReason | None = None,
    unavailable_reason: MUnitUnavailableReason | None = None,
    not_applicable_reason: MUnitNotApplicableReason | None = None,
) -> MuleSoftValidationEvidence:
    binding = _evidence_binding(
        source=source,
        request_id=context.request_id,
        run_id=context.run_id,
        base_revision=context.base_revision,
        artifact_digest=context.artifact_digest,
        phase=phase,
        status=status,
        applicable=applicable,
        validation_terminal=validation_terminal,
        local_process_exited=local_process_exited,
        command_exit_code=command_exit_code,
        report_count=report_count,
        suites=suites,
        tests=tests,
        passed=passed,
        failures=failures,
        errors=errors,
        skipped=skipped,
        failure_reason=failure_reason,
        unavailable_reason=unavailable_reason,
        not_applicable_reason=not_applicable_reason,
    )
    return MuleSoftValidationEvidence(
        source=source,
        request_id=context.request_id,
        run_id=context.run_id,
        base_revision=context.base_revision,
        artifact_digest=context.artifact_digest,
        phase=phase,
        status=status,
        applicable=applicable,
        validation_terminal=validation_terminal,
        local_process_exited=local_process_exited,
        command_exit_code=command_exit_code,
        report_count=report_count,
        suites=suites,
        tests=tests,
        passed=passed,
        failures=failures,
        errors=errors,
        skipped=skipped,
        failure_reason=failure_reason,
        unavailable_reason=unavailable_reason,
        not_applicable_reason=not_applicable_reason,
        evidence_binding=binding,
    )


def _evidence_binding(
    *,
    source: MuleSoftEvidenceSource,
    request_id: str,
    run_id: str,
    base_revision: str,
    artifact_digest: str,
    phase: MUnitExecutionPhase,
    status: MuleSoftValidationStatus,
    applicable: bool,
    validation_terminal: bool,
    local_process_exited: bool,
    command_exit_code: int | None,
    report_count: int | None,
    suites: int | None,
    tests: int | None,
    passed: int | None,
    failures: int | None,
    errors: int | None,
    skipped: int | None,
    failure_reason: MUnitFailureReason | None,
    unavailable_reason: MUnitUnavailableReason | None,
    not_applicable_reason: MUnitNotApplicableReason | None,
) -> str:
    material = json.dumps(
        {
            "applicable": applicable,
            "artifact_digest": artifact_digest,
            "base_revision": base_revision,
            "command_exit_code": command_exit_code,
            "errors": errors,
            "failure_reason": failure_reason.value if failure_reason else None,
            "failures": failures,
            "local_process_exited": local_process_exited,
            "not_applicable_reason": not_applicable_reason.value if not_applicable_reason else None,
            "passed": passed,
            "phase": phase.value,
            "report_count": report_count,
            "request_id": request_id,
            "run_id": run_id,
            "skipped": skipped,
            "source": source.value,
            "status": status.value,
            "suites": suites,
            "tests": tests,
            "unavailable_reason": unavailable_reason.value if unavailable_reason else None,
            "validation_terminal": validation_terminal,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"
