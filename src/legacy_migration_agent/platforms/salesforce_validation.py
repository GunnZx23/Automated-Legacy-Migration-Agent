"""Normalize Salesforce CLI deploy and Apex-test JSON into safe evidence.

This module is deliberately a *pure adapter*: it never invokes ``sf`` and it
does not read Salesforce credentials or org state.  Callers execute an exact,
approved command elsewhere and pass the completed process output here.

Salesforce operations have two different notions of completion.  The local
CLI process can exit after submitting or polling a server-side job that is
still queued.  Consequently, ``command_exit_code`` is recorded independently
from ``platform_terminal`` and a zero local exit never proves that an org job
completed successfully.

Only normalized counts and controlled status values survive parsing.  Raw
messages, usernames, hostnames, test names, stack traces, and stderr are never
stored in :class:`SalesforceValidationEvidence`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from legacy_migration_agent.contracts import CheckStatus, Sha256Digest, StrictModel

_SALESFORCE_ID = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")


class SalesforceEvidenceError(ValueError):
    """Raised when CLI output cannot support a trustworthy normalized claim."""


class SalesforceValidationCommand(StrEnum):
    """The two report commands whose JSON contracts this adapter understands."""

    DEPLOY_REPORT = "sf_project_deploy_report"
    APEX_TEST_REPORT = "sf_apex_get_test"


class SalesforceTestLevel(StrEnum):
    """Salesforce Metadata API test levels accepted by current CLI commands."""

    NO_TEST_RUN = "NoTestRun"
    RUN_SPECIFIED_TESTS = "RunSpecifiedTests"
    RUN_LOCAL_TESTS = "RunLocalTests"
    RUN_ALL_TESTS_IN_ORG = "RunAllTestsInOrg"
    RUN_RELEVANT_TESTS = "RunRelevantTests"


class SalesforcePlatformStatus(StrEnum):
    QUEUED = "queued"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    CANCELING = "canceling"
    SUCCEEDED = "succeeded"
    PASSED = "passed"
    FAILED = "failed"
    CANCELED = "canceled"
    ABORTED = "aborted"
    UNAVAILABLE = "unavailable"


class SalesforceAvailabilityReason(StrEnum):
    AUTHENTICATION = "authentication"
    NETWORK = "network"
    TOOL_UNAVAILABLE = "tool_unavailable"


class SalesforceValidationContext(StrictModel):
    """Caller-owned identity and artifact binding for one report command."""

    command: SalesforceValidationCommand
    org_alias: str = Field(min_length=1, max_length=255)
    job_id: str = Field(min_length=15, max_length=18)
    manifest_digest: Sha256Digest
    test_level: SalesforceTestLevel

    @field_validator("org_alias")
    @classmethod
    def validate_org_alias(cls, value: str) -> str:
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("org_alias cannot contain whitespace or control characters")
        return value

    @field_validator("job_id")
    @classmethod
    def validate_job_id_shape(cls, value: str) -> str:
        if _SALESFORCE_ID.fullmatch(value) is None:
            raise ValueError("job_id must be a 15- or 18-character Salesforce ID")
        return value

    @model_validator(mode="after")
    def validate_command_binding(self) -> SalesforceValidationContext:
        if self.command is SalesforceValidationCommand.DEPLOY_REPORT:
            if not self.job_id.startswith("0Af"):
                raise ValueError(
                    "deploy report job_id must be a Metadata API deploy ID beginning with 0Af"
                )
        else:
            if not self.job_id.startswith("707"):
                raise ValueError("Apex test report job_id must be a test-run ID beginning with 707")
            if self.test_level in {
                SalesforceTestLevel.NO_TEST_RUN,
                SalesforceTestLevel.RUN_RELEVANT_TESTS,
            }:
                raise ValueError(
                    "Apex test reports require RunSpecifiedTests, RunLocalTests, or RunAllTestsInOrg"
                )
        return self


class SalesforceValidationEvidence(StrictModel):
    """Strict, sanitized evidence derived from one completed local process."""

    command: SalesforceValidationCommand
    org_alias: str
    job_id: str
    manifest_digest: Sha256Digest
    test_level: SalesforceTestLevel
    command_exit_code: int = Field(ge=-255, le=255)
    local_process_exited: Literal[True] = True
    platform_status: SalesforcePlatformStatus
    check_status: CheckStatus
    platform_terminal: bool
    tests_total: int | None = Field(default=None, ge=0)
    tests_completed: int | None = Field(default=None, ge=0)
    tests_failed: int | None = Field(default=None, ge=0)
    component_failures: int | None = Field(default=None, ge=0)
    availability_reason: SalesforceAvailabilityReason | None = None
    evidence_binding: Sha256Digest

    @model_validator(mode="after")
    def validate_normalized_state(self) -> SalesforceValidationEvidence:
        SalesforceValidationContext(
            command=self.command,
            org_alias=self.org_alias,
            job_id=self.job_id,
            manifest_digest=self.manifest_digest,
            test_level=self.test_level,
        )
        terminal_successes = {
            SalesforcePlatformStatus.SUCCEEDED,
            SalesforcePlatformStatus.PASSED,
        }
        terminal_failures = {
            SalesforcePlatformStatus.FAILED,
            SalesforcePlatformStatus.CANCELED,
            SalesforcePlatformStatus.ABORTED,
        }
        nonterminal = {
            SalesforcePlatformStatus.QUEUED,
            SalesforcePlatformStatus.PENDING,
            SalesforcePlatformStatus.IN_PROGRESS,
            SalesforcePlatformStatus.SUBMITTED,
            SalesforcePlatformStatus.CANCELING,
        }

        if self.check_status is CheckStatus.PASSED:
            if not self.platform_terminal or self.platform_status not in terminal_successes:
                raise ValueError("PASSED requires a terminal Salesforce success status")
            if self.command_exit_code != 0:
                raise ValueError("PASSED requires command_exit_code 0")
        elif self.check_status is CheckStatus.FAILED:
            if not self.platform_terminal or self.platform_status not in terminal_failures:
                raise ValueError("FAILED requires a terminal Salesforce failure status")
            if self.command_exit_code == 0:
                raise ValueError("FAILED requires a nonzero command_exit_code")
        elif self.check_status is CheckStatus.NONTERMINAL:
            if self.platform_terminal or self.platform_status not in nonterminal:
                raise ValueError("NONTERMINAL requires a nonterminal Salesforce status")
        elif self.check_status is CheckStatus.UNAVAILABLE:
            if (
                self.platform_terminal
                or self.platform_status is not SalesforcePlatformStatus.UNAVAILABLE
            ):
                raise ValueError("UNAVAILABLE cannot claim a terminal Salesforce status")
            if self.command_exit_code == 0 or self.availability_reason is None:
                raise ValueError("UNAVAILABLE requires a nonzero exit and a classified reason")

        if (
            self.check_status is not CheckStatus.UNAVAILABLE
            and self.availability_reason is not None
        ):
            raise ValueError("availability_reason is allowed only for UNAVAILABLE evidence")
        if self.tests_total is not None and self.tests_completed is not None:
            if self.tests_completed > self.tests_total:
                raise ValueError("tests_completed cannot exceed tests_total")
        if self.tests_total is not None and self.tests_failed is not None:
            if self.tests_failed > self.tests_total:
                raise ValueError("tests_failed cannot exceed tests_total")

        expected_binding = _evidence_binding(
            command=self.command,
            org_alias=self.org_alias,
            job_id=self.job_id,
            manifest_digest=self.manifest_digest,
            test_level=self.test_level,
            command_exit_code=self.command_exit_code,
            platform_status=self.platform_status,
            check_status=self.check_status,
            platform_terminal=self.platform_terminal,
            tests_total=self.tests_total,
            tests_completed=self.tests_completed,
            tests_failed=self.tests_failed,
            component_failures=self.component_failures,
            availability_reason=self.availability_reason,
        )
        if self.evidence_binding != expected_binding:
            raise ValueError("evidence_binding does not match the normalized Salesforce evidence")
        return self


def parse_deploy_report_json(
    stdout: str,
    *,
    stderr: str = "",
    command_exit_code: int,
    context: SalesforceValidationContext,
) -> SalesforceValidationEvidence:
    """Parse ``sf project deploy report --json`` output.

    ``stdout`` must be the JSON envelope emitted by the CLI.  Plain stderr is
    consulted only to classify an unavailable CLI/network/auth boundary; it is
    never copied into returned evidence.
    """

    _validate_command_exit_code(command_exit_code)
    _require_command(context, SalesforceValidationCommand.DEPLOY_REPORT)
    envelope = _load_envelope(stdout, stderr, command_exit_code, context)
    if isinstance(envelope, SalesforceValidationEvidence):
        return envelope
    result = _require_result(envelope, stderr, command_exit_code, context)
    if isinstance(result, SalesforceValidationEvidence):
        return result

    job_id = _required_string(result, "id")
    _require_job_id(job_id, context)
    _verify_test_level(result, context)
    platform_status = _deploy_status(_required_string(result, "status"))
    done = _optional_bool(result, "done")
    success = _optional_bool(result, "success")
    tests_total = _optional_count(result, "numberTestsTotal")
    tests_completed = _optional_count(result, "numberTestsCompleted")
    tests_failed = _optional_count(result, "numberTestErrors")
    component_failures = _optional_count(result, "numberComponentErrors")
    _validate_partial_counts(tests_total, tests_completed, tests_failed)

    if platform_status in {
        SalesforcePlatformStatus.QUEUED,
        SalesforcePlatformStatus.PENDING,
        SalesforcePlatformStatus.IN_PROGRESS,
        SalesforcePlatformStatus.SUBMITTED,
        SalesforcePlatformStatus.CANCELING,
    }:
        if done is True or success is True:
            raise SalesforceEvidenceError(
                "nonterminal deploy status contradicts done/success fields"
            )
        return _make_evidence(
            context,
            command_exit_code,
            platform_status,
            CheckStatus.NONTERMINAL,
            platform_terminal=False,
            tests_total=tests_total,
            tests_completed=tests_completed,
            tests_failed=tests_failed,
            component_failures=component_failures,
        )

    if done is not True:
        raise SalesforceEvidenceError("terminal deploy status requires done=true")

    if platform_status is SalesforcePlatformStatus.SUCCEEDED:
        if success is not True or command_exit_code != 0:
            raise SalesforceEvidenceError("successful deploy contradicts success or command exit")
        _require_complete_deploy_tests(
            context.test_level,
            tests_total,
            tests_completed,
            tests_failed,
            component_failures,
        )
        return _make_evidence(
            context,
            command_exit_code,
            platform_status,
            CheckStatus.PASSED,
            platform_terminal=True,
            tests_total=tests_total,
            tests_completed=tests_completed,
            tests_failed=tests_failed,
            component_failures=component_failures,
        )

    if success is True or command_exit_code == 0:
        raise SalesforceEvidenceError("failed deploy contradicts success or command exit")
    return _make_evidence(
        context,
        command_exit_code,
        platform_status,
        CheckStatus.FAILED,
        platform_terminal=True,
        tests_total=tests_total,
        tests_completed=tests_completed,
        tests_failed=tests_failed,
        component_failures=component_failures,
    )


def parse_apex_test_report_json(
    stdout: str,
    *,
    stderr: str = "",
    command_exit_code: int,
    context: SalesforceValidationContext,
) -> SalesforceValidationEvidence:
    """Parse ``sf apex get test --json`` output or its async ID result."""

    _validate_command_exit_code(command_exit_code)
    _require_command(context, SalesforceValidationCommand.APEX_TEST_REPORT)
    envelope = _load_envelope(stdout, stderr, command_exit_code, context)
    if isinstance(envelope, SalesforceValidationEvidence):
        return envelope
    result = _require_result(envelope, stderr, command_exit_code, context)
    if isinstance(result, SalesforceValidationEvidence):
        return result
    _verify_test_level(result, context)

    summary = result.get("summary")
    if summary is None:
        job_id = _required_string(result, "testRunId")
        _require_job_id(job_id, context)
        raw_status = result.get("status", "Submitted")
        if not isinstance(raw_status, str):
            raise SalesforceEvidenceError("Apex async status must be a string")
        platform_status = _apex_status(raw_status)
        if platform_status not in {
            SalesforcePlatformStatus.QUEUED,
            SalesforcePlatformStatus.PENDING,
            SalesforcePlatformStatus.IN_PROGRESS,
            SalesforcePlatformStatus.SUBMITTED,
        }:
            raise SalesforceEvidenceError(
                "Apex terminal output requires a summary with complete counts"
            )
        return _make_evidence(
            context,
            command_exit_code,
            platform_status,
            CheckStatus.NONTERMINAL,
            platform_terminal=False,
        )

    if not isinstance(summary, Mapping):
        raise SalesforceEvidenceError("Apex result.summary must be an object")
    job_id = _required_string(summary, "testRunId")
    _require_job_id(job_id, context)
    _verify_test_level(summary, context)
    platform_status = _apex_status(_required_string(summary, "outcome"))

    tests_total: int | None
    tests_completed: int | None
    tests_failed: int | None

    if platform_status in {
        SalesforcePlatformStatus.QUEUED,
        SalesforcePlatformStatus.PENDING,
        SalesforcePlatformStatus.IN_PROGRESS,
        SalesforcePlatformStatus.SUBMITTED,
    }:
        return _make_evidence(
            context,
            command_exit_code,
            platform_status,
            CheckStatus.NONTERMINAL,
            platform_terminal=False,
        )

    if platform_status is SalesforcePlatformStatus.PASSED:
        tests_total = _required_count(summary, "testsRan")
        passing = _required_count(summary, "passing")
        tests_failed = _required_count(summary, "failing")
        skipped = _required_count(summary, "skipped")
        tests_completed = passing + tests_failed + skipped
        if tests_total < 1:
            raise SalesforceEvidenceError(
                "terminal Apex evidence must include at least one executed test"
            )
        if tests_completed != tests_total:
            raise SalesforceEvidenceError(
                "Apex summary counts do not account for every executed test"
            )
        _validate_apex_test_rows(result.get("tests"), passing, tests_failed, skipped, tests_total)
        if tests_failed != 0 or command_exit_code != 0:
            raise SalesforceEvidenceError(
                "passed Apex outcome contradicts failures or command exit"
            )
        check_status = CheckStatus.PASSED
    else:
        if command_exit_code == 0:
            raise SalesforceEvidenceError("failed Apex outcome contradicts command exit 0")
        tests_total, tests_completed, tests_failed = _optional_apex_failure_counts(
            summary, result.get("tests")
        )
        check_status = CheckStatus.FAILED

    return _make_evidence(
        context,
        command_exit_code,
        platform_status,
        check_status,
        platform_terminal=True,
        tests_total=tests_total,
        tests_completed=tests_completed,
        tests_failed=tests_failed,
    )


def _load_envelope(
    stdout: str,
    stderr: str,
    command_exit_code: int,
    context: SalesforceValidationContext,
) -> Mapping[str, Any] | SalesforceValidationEvidence:
    if stdout.strip():
        try:
            decoded = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeError):
            # JSONDecodeError retains its entire input document.  Suppress the
            # cause so malformed output containing a credential is not attached
            # to the safe adapter exception.
            raise SalesforceEvidenceError("Salesforce CLI stdout is malformed JSON") from None
        if not isinstance(decoded, Mapping):
            raise SalesforceEvidenceError("Salesforce CLI JSON envelope must be an object")
        envelope_status = decoded.get("status")
        if isinstance(envelope_status, bool) or not isinstance(envelope_status, int):
            raise SalesforceEvidenceError("Salesforce CLI JSON envelope requires an integer status")
        if envelope_status != command_exit_code:
            raise SalesforceEvidenceError("JSON status contradicts the observed command exit code")
        return decoded

    reason = _classify_unavailability(None, stderr, command_exit_code)
    if reason is None:
        raise SalesforceEvidenceError("Salesforce CLI produced no JSON evidence")
    return _make_unavailable(context, command_exit_code, reason)


def _require_result(
    envelope: Mapping[str, Any],
    stderr: str,
    command_exit_code: int,
    context: SalesforceValidationContext,
) -> Mapping[str, Any] | SalesforceValidationEvidence:
    result = envelope.get("result")
    if isinstance(result, Mapping):
        if any(key in envelope for key in ("stack", "cause")):
            raise SalesforceEvidenceError("CLI envelope contains both result and error-only fields")
        return result
    reason = _classify_unavailability(envelope, stderr, command_exit_code)
    if reason is not None:
        return _make_unavailable(context, command_exit_code, reason)
    raise SalesforceEvidenceError("Salesforce CLI JSON does not contain a result object")


def _classify_unavailability(
    envelope: Mapping[str, Any] | None,
    stderr: str,
    command_exit_code: int,
) -> SalesforceAvailabilityReason | None:
    if command_exit_code == 0:
        return None
    selected: list[str] = [stderr[:4096]]
    if envelope is not None:
        selected.extend(_selected_error_strings(envelope))
    text = " ".join(selected).casefold()
    auth_markers = (
        "invalid_session_id",
        "authentication failed",
        "authorization failed",
        "not authenticated",
        "expired access token",
        "no authorization information",
        "authinfo",
        "cannot refresh token",
        "org authorization",
        "invalid grant",
        "namedorgnotfound",
        "no org configuration found",
        "not authorized",
    )
    network_markers = (
        "enotfound",
        "econnrefused",
        "econnreset",
        "econnaborted",
        "etimedout",
        "und_err_connect_timeout",
        "network error",
        "socket hang up",
        "unable to resolve host",
        "getaddrinfo",
        "fetch failed",
        "request timed out",
    )
    tool_markers = (
        "command not found",
        "no such file or directory: 'sf'",
        'no such file or directory: "sf"',
        "sf is not recognized",
        "unknown command",
    )
    if any(marker in text for marker in auth_markers):
        return SalesforceAvailabilityReason.AUTHENTICATION
    if any(marker in text for marker in network_markers):
        return SalesforceAvailabilityReason.NETWORK
    if command_exit_code in {126, 127} or any(marker in text for marker in tool_markers):
        return SalesforceAvailabilityReason.TOOL_UNAVAILABLE
    return None


def _selected_error_strings(payload: Mapping[str, Any], *, depth: int = 0) -> tuple[str, ...]:
    """Return bounded classifier text while deliberately excluding raw stacks."""

    selected: list[str] = []
    for key in ("name", "code", "message", "context", "commandName"):
        value = payload.get(key)
        if isinstance(value, str):
            selected.append(value[:4096])
    cause = payload.get("cause")
    if depth < 2 and isinstance(cause, Mapping):
        selected.extend(_selected_error_strings(cause, depth=depth + 1))
    return tuple(selected)


def _make_unavailable(
    context: SalesforceValidationContext,
    command_exit_code: int,
    reason: SalesforceAvailabilityReason,
) -> SalesforceValidationEvidence:
    return _make_evidence(
        context,
        command_exit_code,
        SalesforcePlatformStatus.UNAVAILABLE,
        CheckStatus.UNAVAILABLE,
        platform_terminal=False,
        availability_reason=reason,
    )


def _require_command(
    context: SalesforceValidationContext,
    expected: SalesforceValidationCommand,
) -> None:
    if context.command is not expected:
        raise SalesforceEvidenceError(f"context is not bound to {expected.value}")


def _validate_command_exit_code(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not -255 <= value <= 255:
        raise SalesforceEvidenceError("command_exit_code must be an integer from -255 through 255")


def _require_job_id(observed: str, context: SalesforceValidationContext) -> None:
    if observed != context.job_id:
        raise SalesforceEvidenceError(
            "Salesforce result job ID does not match the requested job ID"
        )


def _verify_test_level(payload: Mapping[str, Any], context: SalesforceValidationContext) -> None:
    for key in ("testLevel", "test_level"):
        if key not in payload:
            continue
        observed = payload[key]
        if not isinstance(observed, str):
            raise SalesforceEvidenceError("Salesforce result test level must be a string")
        if observed != context.test_level.value:
            raise SalesforceEvidenceError(
                "Salesforce result test level contradicts the requested test level"
            )


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SalesforceEvidenceError(f"Salesforce result requires a non-empty {key} string")
    return value.strip()


def _optional_bool(payload: Mapping[str, Any], key: str) -> bool | None:
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, bool):
        raise SalesforceEvidenceError(f"Salesforce result {key} must be a boolean")
    return value


def _optional_count(payload: Mapping[str, Any], key: str) -> int | None:
    if key not in payload:
        return None
    return _coerce_count(payload[key], key)


def _required_count(payload: Mapping[str, Any], key: str) -> int:
    if key not in payload:
        raise SalesforceEvidenceError(f"Salesforce result requires {key}")
    return _coerce_count(payload[key], key)


def _coerce_count(value: Any, key: str) -> int:
    if isinstance(value, bool):
        raise SalesforceEvidenceError(f"Salesforce result {key} must be a nonnegative integer")
    if isinstance(value, int):
        count = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        count = int(value)
    else:
        raise SalesforceEvidenceError(f"Salesforce result {key} must be a nonnegative integer")
    if count < 0:
        raise SalesforceEvidenceError(f"Salesforce result {key} cannot be negative")
    return count


def _deploy_status(raw_status: str) -> SalesforcePlatformStatus:
    canonical = _canonical_status(raw_status)
    mapping = {
        "queued": SalesforcePlatformStatus.QUEUED,
        "pending": SalesforcePlatformStatus.PENDING,
        "inprogress": SalesforcePlatformStatus.IN_PROGRESS,
        "submitted": SalesforcePlatformStatus.SUBMITTED,
        "canceling": SalesforcePlatformStatus.CANCELING,
        "cancelrequested": SalesforcePlatformStatus.CANCELING,
        "succeeded": SalesforcePlatformStatus.SUCCEEDED,
        "success": SalesforcePlatformStatus.SUCCEEDED,
        "failed": SalesforcePlatformStatus.FAILED,
        "error": SalesforcePlatformStatus.FAILED,
        "canceled": SalesforcePlatformStatus.CANCELED,
        "cancelled": SalesforcePlatformStatus.CANCELED,
        "aborted": SalesforcePlatformStatus.ABORTED,
        "succeededpartial": SalesforcePlatformStatus.FAILED,
    }
    try:
        return mapping[canonical]
    except KeyError:
        raise SalesforceEvidenceError("unknown Salesforce deploy status") from None


def _apex_status(raw_status: str) -> SalesforcePlatformStatus:
    canonical = _canonical_status(raw_status)
    mapping = {
        "queued": SalesforcePlatformStatus.QUEUED,
        "holding": SalesforcePlatformStatus.PENDING,
        "pending": SalesforcePlatformStatus.PENDING,
        "preparing": SalesforcePlatformStatus.IN_PROGRESS,
        "processing": SalesforcePlatformStatus.IN_PROGRESS,
        "inprogress": SalesforcePlatformStatus.IN_PROGRESS,
        "submitted": SalesforcePlatformStatus.SUBMITTED,
        "passed": SalesforcePlatformStatus.PASSED,
        "success": SalesforcePlatformStatus.PASSED,
        "succeeded": SalesforcePlatformStatus.PASSED,
        "failed": SalesforcePlatformStatus.FAILED,
        "error": SalesforcePlatformStatus.FAILED,
        "canceled": SalesforcePlatformStatus.CANCELED,
        "cancelled": SalesforcePlatformStatus.CANCELED,
        "aborted": SalesforcePlatformStatus.ABORTED,
    }
    try:
        return mapping[canonical]
    except KeyError:
        raise SalesforceEvidenceError("unknown Salesforce Apex test status") from None


def _canonical_status(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value.strip().casefold())


def _validate_partial_counts(
    tests_total: int | None,
    tests_completed: int | None,
    tests_failed: int | None,
) -> None:
    if tests_total is not None and tests_completed is not None and tests_completed > tests_total:
        raise SalesforceEvidenceError("deploy completed-test count exceeds total-test count")
    if tests_total is not None and tests_failed is not None and tests_failed > tests_total:
        raise SalesforceEvidenceError("deploy failed-test count exceeds total-test count")


def _require_complete_deploy_tests(
    test_level: SalesforceTestLevel,
    tests_total: int | None,
    tests_completed: int | None,
    tests_failed: int | None,
    component_failures: int | None,
) -> None:
    if None in (tests_total, tests_completed, tests_failed, component_failures):
        raise SalesforceEvidenceError(
            "successful deploy requires complete component and test counts"
        )
    assert tests_total is not None
    assert tests_completed is not None
    assert tests_failed is not None
    assert component_failures is not None
    if tests_completed != tests_total or tests_failed != 0 or component_failures != 0:
        raise SalesforceEvidenceError(
            "successful deploy does not contain complete passing tests/components"
        )
    if test_level is SalesforceTestLevel.NO_TEST_RUN:
        if tests_total != 0:
            raise SalesforceEvidenceError("NoTestRun contradicts nonzero executed tests")
    elif tests_total == 0:
        raise SalesforceEvidenceError("selected deploy test level produced no executed tests")


def _validate_apex_test_rows(
    rows: Any,
    expected_passing: int,
    expected_failing: int,
    expected_skipped: int,
    expected_total: int,
) -> None:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise SalesforceEvidenceError("terminal Apex result requires a tests array")
    if len(rows) != expected_total:
        raise SalesforceEvidenceError("Apex tests array is incomplete")
    observed = {"pass": 0, "fail": 0, "skip": 0}
    for row in rows:
        if not isinstance(row, Mapping):
            raise SalesforceEvidenceError("each Apex test result must be an object")
        outcome = row.get("Outcome", row.get("outcome"))
        if not isinstance(outcome, str):
            raise SalesforceEvidenceError("each Apex test result requires an outcome")
        canonical = _canonical_status(outcome)
        if canonical in {"pass", "passed", "success", "succeeded"}:
            observed["pass"] += 1
        elif canonical in {"fail", "failed", "error"}:
            observed["fail"] += 1
        elif canonical in {"skip", "skipped"}:
            observed["skip"] += 1
        else:
            raise SalesforceEvidenceError("unknown Apex test-row outcome")
    if observed != {
        "pass": expected_passing,
        "fail": expected_failing,
        "skip": expected_skipped,
    }:
        raise SalesforceEvidenceError("Apex test rows contradict summary counts")


def _optional_apex_failure_counts(
    summary: Mapping[str, Any],
    rows: Any,
) -> tuple[int | None, int | None, int | None]:
    count_keys = ("testsRan", "passing", "failing", "skipped")
    present = tuple(key in summary for key in count_keys)
    if not any(present):
        return None, None, None
    if not all(present):
        raise SalesforceEvidenceError(
            "Apex failure summary contains only a partial set of test counts"
        )
    tests_total = _required_count(summary, "testsRan")
    passing = _required_count(summary, "passing")
    tests_failed = _required_count(summary, "failing")
    skipped = _required_count(summary, "skipped")
    tests_completed = passing + tests_failed + skipped
    if tests_completed != tests_total:
        raise SalesforceEvidenceError("Apex summary counts do not account for every executed test")
    if rows is not None:
        _validate_apex_test_rows(rows, passing, tests_failed, skipped, tests_total)
    return tests_total, tests_completed, tests_failed


def _make_evidence(
    context: SalesforceValidationContext,
    command_exit_code: int,
    platform_status: SalesforcePlatformStatus,
    check_status: CheckStatus,
    *,
    platform_terminal: bool,
    tests_total: int | None = None,
    tests_completed: int | None = None,
    tests_failed: int | None = None,
    component_failures: int | None = None,
    availability_reason: SalesforceAvailabilityReason | None = None,
) -> SalesforceValidationEvidence:
    binding = _evidence_binding(
        command=context.command,
        org_alias=context.org_alias,
        job_id=context.job_id,
        manifest_digest=context.manifest_digest,
        test_level=context.test_level,
        command_exit_code=command_exit_code,
        platform_status=platform_status,
        check_status=check_status,
        platform_terminal=platform_terminal,
        tests_total=tests_total,
        tests_completed=tests_completed,
        tests_failed=tests_failed,
        component_failures=component_failures,
        availability_reason=availability_reason,
    )
    return SalesforceValidationEvidence(
        command=context.command,
        org_alias=context.org_alias,
        job_id=context.job_id,
        manifest_digest=context.manifest_digest,
        test_level=context.test_level,
        command_exit_code=command_exit_code,
        platform_status=platform_status,
        check_status=check_status,
        platform_terminal=platform_terminal,
        tests_total=tests_total,
        tests_completed=tests_completed,
        tests_failed=tests_failed,
        component_failures=component_failures,
        availability_reason=availability_reason,
        evidence_binding=binding,
    )


def _evidence_binding(
    *,
    command: SalesforceValidationCommand,
    org_alias: str,
    job_id: str,
    manifest_digest: str,
    test_level: SalesforceTestLevel,
    command_exit_code: int,
    platform_status: SalesforcePlatformStatus,
    check_status: CheckStatus,
    platform_terminal: bool,
    tests_total: int | None,
    tests_completed: int | None,
    tests_failed: int | None,
    component_failures: int | None,
    availability_reason: SalesforceAvailabilityReason | None,
) -> str:
    material = json.dumps(
        {
            "availability_reason": availability_reason.value if availability_reason else None,
            "check_status": check_status.value,
            "command": command.value,
            "command_exit_code": command_exit_code,
            "component_failures": component_failures,
            "job_id": job_id,
            "manifest_digest": manifest_digest,
            "org_alias": org_alias,
            "platform_status": platform_status.value,
            "platform_terminal": platform_terminal,
            "test_level": test_level.value,
            "tests_completed": tests_completed,
            "tests_failed": tests_failed,
            "tests_total": tests_total,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"
