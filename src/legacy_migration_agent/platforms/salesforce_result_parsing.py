"""Parse bounded Salesforce command evidence without granting authority."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Any, Final, cast

from legacy_migration_agent.contracts import (
    CheckResult,
    CheckStatus,
    ValidationCommand,
    ValidationDisposition,
)
from legacy_migration_agent.core.execution import ExecutionResult
from legacy_migration_agent.core.redaction import SecretRedactor
from legacy_migration_agent.platforms.local_checks import (
    APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,
    APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,
    CASE_LWC_TEST_PATH,
    CASE_MANAGEMENT_CONSOLE_UNIT_ID,
    JEST_GLOBALS_IMPORT_ORDER_DIAGNOSTIC_ID,
    JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID,
    LWC_JEST_SETUP_PATH,
    LWC_JEST_VERSION,
    LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID,
    LWC_TEST_PATH,
    SALESFORCE_ACCOUNT_CONTACT_UNIT_ID,
    SALESFORCE_AGENT_OUTPUT_PATHS,
    SALESFORCE_CANDIDATE_FAILURE_CODES,
    SALESFORCE_CANDIDATE_STATIC_DIAGNOSTIC_IDS,
    SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID,
    resolve_salesforce_controller_jest_spec,
)
from legacy_migration_agent.platforms.local_checks import (
    SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID as SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID,
)

SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID: Final = "salesforce-candidate-contract"
SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID: Final = "salesforce-dependency-closure"
SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID: Final = "salesforce-toolchain-contract"
SALESFORCE_SANDBOX_PROBE_COMMAND_ID: Final = "salesforce-jest-sandbox-probe"
SALESFORCE_LWC_JEST_COMMAND_ID: Final = "salesforce-lwc-jest"
SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID: Final = "salesforce-lwc-controller-jest"
SALESFORCE_WORKSPACE_FINGERPRINT_COMMAND_ID: Final = "salesforce-workspace-fingerprint"

SALESFORCE_MIN_CANDIDATE_LWC_JEST_TESTS: Final = 3
_MAX_MODEL_FACING_JEST_FAILURE_TITLES: Final = 6
_MAX_MODEL_FACING_JEST_TITLE_CHARS: Final = 120
_MAX_JEST_TESTS: Final = 10_000

_SummaryParser = Callable[[ExecutionResult, Path], str]

# The candidate-authored LWC Jest suite path is unit-specific: each Salesforce
# migration unit owns a distinct approved test module, so the account/contact
# and Case Management Console units resolve their own suite from public
# constants rather than a shared hardcoded path.
_CANDIDATE_LWC_TEST_PATH_BY_UNIT: Final[Mapping[str, str]] = {
    SALESFORCE_ACCOUNT_CONTACT_UNIT_ID: LWC_TEST_PATH,
    CASE_MANAGEMENT_CONSOLE_UNIT_ID: CASE_LWC_TEST_PATH,
}


def _candidate_lwc_test_path(unit_id: str) -> str:
    try:
        return _CANDIDATE_LWC_TEST_PATH_BY_UNIT[unit_id]
    except KeyError as exc:
        raise ValueError(f"unsupported Salesforce migration unit: {unit_id}") from exc


def _summary_parser(
    command_id: str,
    *,
    controller_test_path: Path | None = None,
    unit_id: str = SALESFORCE_ACCOUNT_CONTACT_UNIT_ID,
) -> _SummaryParser:
    parsers: Mapping[str, _SummaryParser] = {
        SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID: _candidate_summary,
        SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID: _dependency_summary,
        SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID: partial(
            _toolchain_summary, unit_id=unit_id
        ),
        SALESFORCE_LWC_JEST_COMMAND_ID: partial(
            _jest_summary, lwc_test_path=_candidate_lwc_test_path(unit_id)
        ),
        SALESFORCE_WORKSPACE_FINGERPRINT_COMMAND_ID: _workspace_summary,
    }
    if command_id == SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID:
        if controller_test_path is None:
            raise AssertionError("controller Jest parser requires its immutable suite path")
        return partial(
            _controller_jest_summary,
            controller_test_path=controller_test_path,
            unit_id=unit_id,
        )
    return parsers[command_id]


def _result_from_execution(
    check: ValidationCommand,
    execution: ExecutionResult,
    candidate_root: Path,
    parser: _SummaryParser,
    *,
    controller_test_path: Path | None = None,
    unit_id: str = SALESFORCE_ACCOUNT_CONTACT_UNIT_ID,
) -> CheckResult:
    result = partial(_check_result, check)
    if execution.receipt.exit_code != 0:
        if check.command_id == SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID:
            summary, diagnostic_ids = _candidate_failure_evidence(execution)
            return result(CheckStatus.FAILED, summary, execution, diagnostic_ids)
        if check.command_id == SALESFORCE_SANDBOX_PROBE_COMMAND_ID:
            summary = (
                "The controller-owned OS isolation backend failed its run-specific probe; "
                f"exit={execution.receipt.exit_code}; "
                f"stdout={execution.receipt.stdout_digest}; "
                f"stderr={execution.receipt.stderr_digest}."
            )
            return result(CheckStatus.UNAVAILABLE, summary, execution)
        if check.command_id == SALESFORCE_LWC_JEST_COMMAND_ID:
            try:
                failure_summary = _jest_failure_summary(
                    execution,
                    candidate_root,
                    lwc_test_path=_candidate_lwc_test_path(unit_id),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                summary = (
                    "Jest did not produce complete terminal JSON; its local runtime is "
                    f"unavailable; exit={execution.receipt.exit_code}; "
                    f"stdout={execution.receipt.stdout_digest}; "
                    f"stderr={execution.receipt.stderr_digest}."
                )
                return result(CheckStatus.UNAVAILABLE, summary, execution)
            return result(
                CheckStatus.FAILED,
                failure_summary,
                execution,
                (SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID,),
            )
        if check.command_id == SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID:
            if controller_test_path is None:
                raise AssertionError(
                    "controller Jest failure parser requires its immutable suite path"
                )
            try:
                failure_summary, diagnostic_ids = _controller_jest_failure_evidence(
                    execution,
                    controller_test_path,
                    unit_id=unit_id,
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                summary = (
                    "The controller-owned LWC behavior suite did not produce complete "
                    f"terminal JSON; exit={execution.receipt.exit_code}; "
                    f"stdout={execution.receipt.stdout_digest}; "
                    f"stderr={execution.receipt.stderr_digest}."
                )
                return result(CheckStatus.UNAVAILABLE, summary, execution)
            return result(CheckStatus.FAILED, failure_summary, execution, diagnostic_ids)
        suffix = " after timeout" if execution.timed_out else ""
        summary = (
            f"Local check failed terminally{suffix}; exit={execution.receipt.exit_code}; "
            f"stdout={execution.receipt.stdout_digest}; "
            f"stderr={execution.receipt.stderr_digest}."
        )
        return result(CheckStatus.FAILED, summary, execution)
    try:
        summary = parser(execution, candidate_root)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        summary = (
            "The command exited zero but complete bounded terminal evidence was unavailable; "
            f"stdout={execution.receipt.stdout_digest}."
        )
        return result(CheckStatus.UNAVAILABLE, summary, execution)
    return result(CheckStatus.PASSED, summary, execution)


def _unavailable_result(check: ValidationCommand, reason: str) -> CheckResult:
    summary = f"Required local check is unavailable because {reason}."
    return _check_result(check, CheckStatus.UNAVAILABLE, summary)


def _check_result(
    check: ValidationCommand,
    status: CheckStatus,
    summary: str,
    execution: ExecutionResult | None = None,
    diagnostic_ids: tuple[str, ...] = (),
) -> CheckResult:
    return CheckResult(
        check_id=check.check_id,
        command_id=check.command_id,
        required=check.required,
        status=status,
        receipt=execution.receipt if execution is not None else None,
        summary=summary,
        diagnostic_ids=diagnostic_ids,
    )


def _candidate_summary(execution: ExecutionResult, _candidate_root: Path) -> str:
    value = _json_object(execution.stdout)
    _require_fields(
        value,
        {
            "check": "salesforce-candidate-contract",
            "passed": True,
            "agent_output_files": len(SALESFORCE_AGENT_OUTPUT_PATHS),
            "apex_tests_executed": False,
            "lwc_jest_executed": False,
            "org_validation_performed": False,
            "deployment_claim": False,
        },
    )
    required_files = _bounded_int(value.get("required_files"), minimum=11, maximum=128)
    behavior_states = _bounded_int(value.get("behavior_states"), minimum=1, maximum=64)
    return (
        f"Candidate contract passed files={required_files} states={behavior_states}; "
        f"stdout={execution.receipt.stdout_digest}; org-validation=false deployment=false."
    )


def _candidate_failure_summary(execution: ExecutionResult) -> str:
    summary, _ = _candidate_failure_evidence(execution)
    return summary


def _candidate_failure_evidence(
    execution: ExecutionResult,
) -> tuple[str, tuple[str, ...]]:
    failure_code = "salesforce_candidate_unclassified"
    diagnostic_ids: tuple[str, ...] = ()
    try:
        value = _json_object(execution.stdout)
        if (
            value.get("check") == "candidate-contract"
            and value.get("passed") is False
            and value.get("failure_type") == "LocalCheckFailure"
            and value.get("failure_code") in SALESFORCE_CANDIDATE_FAILURE_CODES
        ):
            failure_code = cast(str, value["failure_code"])
        candidate_diagnostics = value.get("diagnostic_ids")
        if isinstance(candidate_diagnostics, list) and len(candidate_diagnostics) <= 64:
            normalized = tuple(
                dict.fromkeys(
                    diagnostic_id
                    for diagnostic_id in candidate_diagnostics
                    if isinstance(diagnostic_id, str)
                    and diagnostic_id in SALESFORCE_CANDIDATE_STATIC_DIAGNOSTIC_IDS
                )
            )
            normalized_set = set(normalized)
            specific_stage = {
                "lwc_forbidden_runtime_capability": "salesforce_lwc_javascript_contract",
                "jest_forbidden_capability": "salesforce_lwc_jest_contract",
                APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID: (
                    "salesforce_apex_controller_contract"
                ),
                APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID: (
                    "salesforce_apex_controller_contract"
                ),
                JEST_GLOBALS_IMPORT_ORDER_DIAGNOSTIC_ID: ("salesforce_lwc_jest_contract"),
                JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID: ("salesforce_lwc_jest_contract"),
                LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID: ("salesforce_lwc_template_contract"),
            }
            failure_code_matches_diagnostics = failure_code in normalized_set or any(
                specific_stage.get(diagnostic_id) == failure_code
                for diagnostic_id in normalized_set
            )
            if len(normalized) == len(candidate_diagnostics) and failure_code_matches_diagnostics:
                diagnostic_ids = normalized
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass
    if not diagnostic_ids:
        diagnostic_ids = (failure_code,)
    suffix = " after timeout" if execution.timed_out else ""
    return (
        (
            f"Candidate contract failed{suffix}; failure-code={failure_code}; "
            f"diagnostics={','.join(diagnostic_ids)}; "
            f"exit={execution.receipt.exit_code}; stdout={execution.receipt.stdout_digest}; "
            f"stderr={execution.receipt.stderr_digest}."
        ),
        diagnostic_ids,
    )


def _dependency_summary(execution: ExecutionResult, _candidate_root: Path) -> str:
    value = _json_object(execution.stdout)
    _require_fields(
        value,
        {
            "check": "salesforce-target-dependency-closure",
            "passed": True,
            "unresolved": False,
        },
    )
    nodes = _bounded_int(value.get("nodes"), minimum=1, maximum=10_000)
    edges = _bounded_int(value.get("edges"), minimum=1, maximum=50_000)
    warnings = _bounded_int(value.get("warnings"), minimum=0, maximum=10_000)
    return (
        f"Dependency closure passed nodes={nodes} edges={edges} warnings={warnings}; "
        f"stdout={execution.receipt.stdout_digest}."
    )


def _toolchain_summary(
    execution: ExecutionResult,
    _candidate_root: Path,
    *,
    unit_id: str = SALESFORCE_ACCOUNT_CONTACT_UNIT_ID,
) -> str:
    # The pinned toolchain inventory is resolved from the migration unit's
    # controller-Jest spec: the manifest, lock, Jest config, and setup entries
    # are shared byte-for-byte across units, while the controller-owned suite
    # entry is unit-specific. Comparing against the module-level account/contact
    # inventory would reject any second unit's own controller suite.
    spec = resolve_salesforce_controller_jest_spec(unit_id)
    toolchain_digests = spec.toolchain_digests
    value = _json_object(execution.stdout)
    _require_fields(
        value,
        {
            "check": "lwc-jest-toolchain-contract",
            "passed": True,
            "version": LWC_JEST_VERSION,
            "candidate_content_inspected": False,
            "network_accessed": False,
            "install_performed": False,
            "lwc_jest_executed": False,
        },
    )
    expected_keys = {
        "manifest_sha256": "package.json",
        "lock_sha256": "package-lock.json",
        "config_sha256": "jest.config.js",
        "setup_sha256": LWC_JEST_SETUP_PATH,
        "controller_test_sha256": spec.controller_test_path,
    }
    for field, filename in expected_keys.items():
        if value.get(field) != toolchain_digests[filename]:
            raise ValueError("toolchain digest evidence does not match the pinned contract")
    packages = _bounded_int(value.get("locked_packages"), minimum=1, maximum=10_000)
    return (
        f"Pinned LWC Jest {LWC_JEST_VERSION} contract passed packages={packages}; "
        f"manifest={toolchain_digests['package.json']}; "
        f"lock={toolchain_digests['package-lock.json']}; "
        f"config={toolchain_digests['jest.config.js']}; "
        f"setup={toolchain_digests[LWC_JEST_SETUP_PATH]}; "
        f"controller-suite={toolchain_digests[spec.controller_test_path]}."
    )


def _jest_suite(
    value: Mapping[str, Any],
    total_suites: int,
    expected_path: Path,
    suite_label: str,
    inventory: str,
    mismatch_label: str,
) -> Mapping[str, Any]:
    suites = value.get("testResults")
    if not isinstance(suites, list) or len(suites) != total_suites:
        verb = "are" if inventory == "results" else "is"
        raise ValueError(f"{suite_label} suite {inventory} {verb} incomplete")
    suite = _mapping(suites[0])
    suite_name = suite.get("name")
    if not isinstance(suite_name, str) or len(suite_name) > 4096:
        raise ValueError(f"{suite_label} suite path is unavailable")
    try:
        if Path(suite_name).resolve(strict=False) != expected_path.resolve(strict=True):
            raise ValueError(f"{mismatch_label} belongs to another test file")
    except OSError as exc:
        raise ValueError(f"{suite_label} suite path cannot be verified") from exc
    return suite


def _jest_summary(
    execution: ExecutionResult,
    candidate_root: Path,
    *,
    lwc_test_path: str = LWC_TEST_PATH,
) -> str:
    value = _json_object(execution.stdout)
    if value.get("success") is not True or value.get("wasInterrupted") is not False:
        raise ValueError("Jest did not report complete success")
    total_suites = _bounded_int(value.get("numTotalTestSuites"), minimum=1, maximum=100)
    passed_suites = _bounded_int(value.get("numPassedTestSuites"), minimum=1, maximum=100)
    failed_suites = _bounded_int(value.get("numFailedTestSuites"), minimum=0, maximum=100)
    pending_suites = _bounded_int(value.get("numPendingTestSuites"), minimum=0, maximum=100)
    runtime_errors = _bounded_int(value.get("numRuntimeErrorTestSuites"), minimum=0, maximum=100)
    total_tests = _bounded_int(
        value.get("numTotalTests"),
        minimum=SALESFORCE_MIN_CANDIDATE_LWC_JEST_TESTS,
        maximum=_MAX_JEST_TESTS,
    )
    passed_tests = _bounded_int(
        value.get("numPassedTests"),
        minimum=SALESFORCE_MIN_CANDIDATE_LWC_JEST_TESTS,
        maximum=_MAX_JEST_TESTS,
    )
    failed_tests = _bounded_int(value.get("numFailedTests"), minimum=0, maximum=_MAX_JEST_TESTS)
    pending_tests = _bounded_int(value.get("numPendingTests"), minimum=0, maximum=_MAX_JEST_TESTS)
    todo_tests = _bounded_int(value.get("numTodoTests"), minimum=0, maximum=_MAX_JEST_TESTS)
    if (
        total_suites != 1
        or passed_suites != total_suites
        or any(value != 0 for value in (failed_suites, pending_suites, runtime_errors))
        or total_tests != passed_tests
        or any(value != 0 for value in (failed_tests, pending_tests, todo_tests))
    ):
        raise ValueError("Jest terminal counts do not prove complete success")

    suite = _jest_suite(
        value,
        total_suites,
        candidate_root / lwc_test_path,
        "Jest",
        "results",
        "Jest result",
    )
    if suite.get("status") != "passed":
        raise ValueError("Jest suite did not pass")
    failures = suite.get("failureMessages", [])
    if not isinstance(failures, list) or failures:
        raise ValueError("Jest suite contains failures")

    assertions = suite.get("assertionResults")
    if not isinstance(assertions, list) or len(assertions) != total_tests:
        raise ValueError("Jest assertion results are incomplete")
    for raw_assertion in assertions:
        assertion = _mapping(raw_assertion)
        title = assertion.get("title")
        status = assertion.get("status")
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 500
            or status != "passed"
        ):
            raise ValueError("Jest assertion evidence is invalid")
    return (
        "Candidate-authored LWC Jest tests passed "
        f"suites={total_suites} tests={total_tests} evidence-role=supplemental; "
        f"stdout={execution.receipt.stdout_digest}."
    )


def _controller_jest_summary(
    execution: ExecutionResult,
    _candidate_root: Path,
    controller_test_path: Path,
    *,
    unit_id: str = SALESFORCE_ACCOUNT_CONTACT_UNIT_ID,
) -> str:
    spec = resolve_salesforce_controller_jest_spec(unit_id)
    behavior_titles = spec.behavior_titles
    value = _json_object(execution.stdout)
    if value.get("success") is not True or value.get("wasInterrupted") is not False:
        raise ValueError("controller Jest did not report complete success")
    total_suites = _bounded_int(value.get("numTotalTestSuites"), minimum=1, maximum=1)
    passed_suites = _bounded_int(value.get("numPassedTestSuites"), minimum=1, maximum=1)
    failed_suites = _bounded_int(value.get("numFailedTestSuites"), minimum=0, maximum=1)
    pending_suites = _bounded_int(value.get("numPendingTestSuites"), minimum=0, maximum=1)
    runtime_errors = _bounded_int(value.get("numRuntimeErrorTestSuites"), minimum=0, maximum=1)
    required_tests = len(behavior_titles)
    total_tests = _bounded_int(
        value.get("numTotalTests"), minimum=required_tests, maximum=required_tests
    )
    passed_tests = _bounded_int(
        value.get("numPassedTests"), minimum=required_tests, maximum=required_tests
    )
    failed_tests = _bounded_int(value.get("numFailedTests"), minimum=0, maximum=0)
    pending_tests = _bounded_int(value.get("numPendingTests"), minimum=0, maximum=0)
    todo_tests = _bounded_int(value.get("numTodoTests"), minimum=0, maximum=0)
    if (
        total_suites != 1
        or passed_suites != 1
        or any(
            item != 0
            for item in (
                failed_suites,
                pending_suites,
                runtime_errors,
                failed_tests,
                pending_tests,
                todo_tests,
            )
        )
        or total_tests != passed_tests
    ):
        raise ValueError("controller Jest terminal counts do not prove complete success")

    suite = _jest_suite(
        value,
        1,
        controller_test_path,
        "controller Jest",
        "results",
        "controller Jest result",
    )
    if suite.get("status") != "passed":
        raise ValueError("controller Jest suite did not pass")
    failures = suite.get("failureMessages", [])
    if not isinstance(failures, list) or failures:
        raise ValueError("controller Jest suite contains failures")

    assertions = suite.get("assertionResults")
    if not isinstance(assertions, list) or len(assertions) != required_tests:
        raise ValueError("controller Jest assertion results are incomplete")
    titles: set[str] = set()
    for raw_assertion in assertions:
        assertion = _mapping(raw_assertion)
        title = assertion.get("title")
        status = assertion.get("status")
        if not isinstance(title, str) or status != "passed":
            raise ValueError("controller Jest assertion evidence is invalid")
        titles.add(title)
    if titles != set(behavior_titles):
        raise ValueError("controller Jest behavior inventory is incomplete")
    return (
        "Controller-owned LWC behavior Jest passed "
        f"suites=1 tests={required_tests} independent-of-candidate-tests=true; "
        f"stdout={execution.receipt.stdout_digest}."
    )


def _controller_jest_failure_evidence(
    execution: ExecutionResult,
    controller_test_path: Path,
    *,
    unit_id: str = SALESFORCE_ACCOUNT_CONTACT_UNIT_ID,
) -> tuple[str, tuple[str, ...]]:
    spec = resolve_salesforce_controller_jest_spec(unit_id)
    behavior_titles = spec.behavior_titles
    diagnostic_by_title = spec.diagnostic_by_title
    value = _json_object(execution.stdout)
    if value.get("success") is not False or value.get("wasInterrupted") is not False:
        raise ValueError("controller Jest failure evidence is incomplete")
    total_suites = _bounded_int(value.get("numTotalTestSuites"), minimum=1, maximum=1)
    passed_suites = _bounded_int(value.get("numPassedTestSuites"), minimum=0, maximum=1)
    failed_suites = _bounded_int(value.get("numFailedTestSuites"), minimum=0, maximum=1)
    pending_suites = _bounded_int(value.get("numPendingTestSuites"), minimum=0, maximum=1)
    runtime_errors = _bounded_int(value.get("numRuntimeErrorTestSuites"), minimum=0, maximum=1)
    maximum_tests = len(behavior_titles)
    total_tests = _bounded_int(value.get("numTotalTests"), minimum=0, maximum=maximum_tests)
    passed_tests = _bounded_int(value.get("numPassedTests"), minimum=0, maximum=maximum_tests)
    failed_tests = _bounded_int(value.get("numFailedTests"), minimum=0, maximum=maximum_tests)
    pending_tests = _bounded_int(value.get("numPendingTests"), minimum=0, maximum=maximum_tests)
    todo_tests = _bounded_int(value.get("numTodoTests"), minimum=0, maximum=maximum_tests)
    if (
        total_suites != 1
        or total_suites != passed_suites + failed_suites + pending_suites
        or total_tests != passed_tests + failed_tests + pending_tests + todo_tests
        or failed_suites + failed_tests + runtime_errors < 1
    ):
        raise ValueError("controller Jest failure counts are inconsistent")
    suite = _jest_suite(
        value,
        1,
        controller_test_path,
        "controller Jest failure",
        "evidence",
        "controller Jest failure",
    )
    assertions = suite.get("assertionResults")
    if not isinstance(assertions, list) or len(assertions) != total_tests:
        raise ValueError("controller Jest failure assertions are incomplete")
    failed_titles: list[str] = []
    for raw_assertion in assertions:
        assertion = _mapping(raw_assertion)
        title = assertion.get("title")
        status = assertion.get("status")
        if (
            not isinstance(title, str)
            or title not in diagnostic_by_title
            or status not in {"passed", "failed", "pending", "todo"}
        ):
            raise ValueError("controller Jest failure assertion evidence is invalid")
        if status == "failed":
            failed_titles.append(title)
    if len(failed_titles) != failed_tests:
        raise ValueError("controller Jest failed assertion inventory is inconsistent")
    diagnostic_ids = tuple(
        dict.fromkeys(diagnostic_by_title[title] for title in failed_titles)
    )
    if not diagnostic_ids:
        diagnostic_ids = (SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID,)
    return (
        (
            "Controller-owned LWC behavior Jest failed terminally "
            f"suites={total_suites} tests={total_tests} failed-suites={failed_suites} "
            f"failed-tests={failed_tests}; stdout={execution.receipt.stdout_digest}; "
            f"stderr={execution.receipt.stderr_digest}."
        ),
        diagnostic_ids,
    )


def _jest_failure_summary(
    execution: ExecutionResult,
    candidate_root: Path,
    *,
    lwc_test_path: str = LWC_TEST_PATH,
) -> str:
    value = _json_object(execution.stdout)
    if value.get("success") is not False or value.get("wasInterrupted") is not False:
        raise ValueError("Jest failure evidence is incomplete")
    total_suites = _bounded_int(value.get("numTotalTestSuites"), minimum=1, maximum=100)
    passed_suites = _bounded_int(value.get("numPassedTestSuites"), minimum=0, maximum=100)
    failed_suites = _bounded_int(value.get("numFailedTestSuites"), minimum=0, maximum=100)
    pending_suites = _bounded_int(value.get("numPendingTestSuites"), minimum=0, maximum=100)
    runtime_errors = _bounded_int(value.get("numRuntimeErrorTestSuites"), minimum=0, maximum=100)
    total_tests = _bounded_int(value.get("numTotalTests"), minimum=0, maximum=_MAX_JEST_TESTS)
    passed_tests = _bounded_int(value.get("numPassedTests"), minimum=0, maximum=_MAX_JEST_TESTS)
    failed_tests = _bounded_int(value.get("numFailedTests"), minimum=0, maximum=_MAX_JEST_TESTS)
    pending_tests = _bounded_int(value.get("numPendingTests"), minimum=0, maximum=_MAX_JEST_TESTS)
    todo_tests = _bounded_int(value.get("numTodoTests"), minimum=0, maximum=_MAX_JEST_TESTS)
    if (
        total_suites != 1
        or total_suites != passed_suites + failed_suites + pending_suites
        or total_tests != passed_tests + failed_tests + pending_tests + todo_tests
        or failed_suites + failed_tests + runtime_errors < 1
    ):
        raise ValueError("Jest failure counts are inconsistent")
    suite = _jest_suite(
        value,
        total_suites,
        candidate_root / lwc_test_path,
        "Jest failure",
        "evidence",
        "Jest failure",
    )
    assertions = suite.get("assertionResults")
    if not isinstance(assertions, list) or len(assertions) != total_tests:
        raise ValueError("Jest failure assertions are incomplete")
    failed_titles: list[str] = []
    for raw_assertion in assertions:
        assertion = _mapping(raw_assertion)
        title = assertion.get("title")
        status = assertion.get("status")
        if (
            not isinstance(title, str)
            or not title
            or len(title) > 500
            or status not in {"passed", "failed", "pending", "todo"}
        ):
            raise ValueError("Jest failure assertion evidence is invalid")
        if status == "failed":
            failed_titles.append(_model_facing_jest_title(title))
    if len(failed_titles) != failed_tests:
        raise ValueError("Jest failed assertion inventory is inconsistent")
    exposed_titles = failed_titles[:_MAX_MODEL_FACING_JEST_FAILURE_TITLES]
    title_evidence = ""
    if exposed_titles:
        rendered = " | ".join(
            f"{ordinal}:{json.dumps(title, ensure_ascii=True)}"
            for ordinal, title in enumerate(exposed_titles, start=1)
        )
        omitted = len(failed_titles) - len(exposed_titles)
        title_evidence = f" failed-assertions={rendered}"
        if omitted:
            title_evidence += f" additional-failed-tests={omitted}"
        title_evidence += ";"
    return (
        f"LWC Jest failed terminally suites={total_suites} tests={total_tests} "
        f"failed-suites={failed_suites} failed-tests={failed_tests};{title_evidence} "
        f"stdout={execution.receipt.stdout_digest}; stderr={execution.receipt.stderr_digest}."
    )


def _model_facing_jest_title(title: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in title):
        raise ValueError("Jest assertion titles cannot contain control characters")
    normalized = " ".join(title.split())
    if not normalized:
        raise ValueError("Jest assertion title is blank")
    normalized = SecretRedactor().redact(normalized).text
    if len(normalized) > _MAX_MODEL_FACING_JEST_TITLE_CHARS:
        normalized = normalized[: _MAX_MODEL_FACING_JEST_TITLE_CHARS - 3].rstrip() + "..."
    return normalized


def _workspace_summary(execution: ExecutionResult, _candidate_root: Path) -> str:
    value = _json_object(execution.stdout)
    _require_fields(value, {"check": "workspace-read-only", "passed": True})
    expected = value.get("expected_revision")
    actual = value.get("actual_revision")
    if not isinstance(expected, str) or expected != actual or not _is_sha256(expected):
        raise ValueError("workspace fingerprint evidence is invalid")
    return f"Workspace fingerprint remained {actual}; stdout={execution.receipt.stdout_digest}."


def _json_object(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("terminal evidence must be a JSON object")
    return cast(dict[str, Any], value)


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("terminal evidence entry must be an object")
    return cast(Mapping[str, Any], value)


def _require_fields(value: Mapping[str, Any], expected: Mapping[str, object]) -> None:
    for field, expected_value in expected.items():
        if value.get(field) != expected_value:
            raise ValueError(f"terminal evidence field is invalid: {field}")


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("terminal evidence count is invalid")
    return value


def _is_sha256(value: str) -> bool:
    return (
        len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _disposition(results: tuple[CheckResult, ...]) -> ValidationDisposition:
    required = tuple(result for result in results if result.required)
    if any(result.status is CheckStatus.FAILED for result in required):
        return ValidationDisposition.RECOVERABLE_FAILURE
    if any(result.status is CheckStatus.UNAVAILABLE for result in required):
        return ValidationDisposition.ENVIRONMENT_UNAVAILABLE
    if any(result.status is not CheckStatus.PASSED for result in required):
        return ValidationDisposition.RECOVERABLE_FAILURE
    return ValidationDisposition.READY_FOR_HUMAN_REVIEW


def _unmet_runtime_prerequisite(
    command_id: str,
    completed: Mapping[str, CheckResult],
) -> str | None:
    required: tuple[str, ...]
    if command_id == SALESFORCE_SANDBOX_PROBE_COMMAND_ID:
        # The sandbox probe depends only on its toolchain contract.
        required = (SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID,)
    elif command_id in {
        SALESFORCE_LWC_JEST_COMMAND_ID,
        SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
    }:
        required = (
            SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID,
            SALESFORCE_SANDBOX_PROBE_COMMAND_ID,
        )
    else:
        return None
    unmet: list[str] = []
    for prerequisite in required:
        result = completed[prerequisite]
        if result.status is CheckStatus.PASSED:
            continue
        unmet.append(prerequisite)
    if not unmet:
        return None
    return "required prerequisite checks did not pass: " + ", ".join(unmet)
