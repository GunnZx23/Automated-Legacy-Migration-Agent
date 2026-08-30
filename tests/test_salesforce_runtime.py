from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from salesforce_candidate_factory import salesforce_candidate_outputs

import legacy_migration_agent.platforms.salesforce_runtime as salesforce_runtime
from legacy_migration_agent.agent_runtime.agent_definitions import (
    AgentRegistry,
    AgentRole,
    load_agent_registry,
)
from legacy_migration_agent.contracts import (
    ApprovalAction,
    ChangeSet,
    CheckResult,
    CheckStatus,
    EnvironmentKind,
    MigrationManifest,
    MigrationRequest,
    MigrationTarget,
    Platform,
    ToolReceipt,
    TransformationStep,
    ValidationCommand,
    ValidationDisposition,
)
from legacy_migration_agent.core.execution import ExecutionResult
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.run_session import AgentDefinitionDigests, AgentRunSession
from legacy_migration_agent.core.workspace import IsolatedWorkspace, content_revision
from legacy_migration_agent.graphs.dependency_graph import (
    SALESFORCE_ANALYZER_VERSION,
    build_salesforce_dependency_graph,
)
from legacy_migration_agent.platforms.local_checks import (
    APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,
    APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,
    JEST_GLOBALS_IMPORT_ORDER_DIAGNOSTIC_ID,
    JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID,
    LWC_CONTROLLER_TEST_PATH,
    LWC_JEST_TOOLCHAIN_DIGESTS,
    LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID,
    LWC_TEST_PATH,
    SALESFORCE_AGENT_OUTPUT_PATHS,
    SALESFORCE_CONTROLLER_LWC_BEHAVIOR_TITLES,
    SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_BY_TITLE,
    SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID,
    SALESFORCE_IMPLEMENTATION_CONTRACT,
)
from legacy_migration_agent.platforms.salesforce_result_parsing import (
    GRAPH_DEPENDENCY_INCORRECT_DIAGNOSTIC_ID,
    GRAPH_DEPENDENCY_OMISSION_DIAGNOSTIC_ID,
)
from legacy_migration_agent.platforms.salesforce_runtime import (
    SALESFORCE_API_RUNTIME,
    SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID,
    SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID,
    SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
    SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID,
    SALESFORCE_LWC_JEST_COMMAND_ID,
    SALESFORCE_MIN_CANDIDATE_LWC_JEST_TESTS,
    SALESFORCE_PLATFORM_ADAPTER,
    SALESFORCE_RUNTIME_CONFIG,
    SALESFORCE_SANDBOX_PROBE_COMMAND_ID,
    SALESFORCE_SCOPE_POLICY,
    SALESFORCE_SOURCE_ENTRY,
    SALESFORCE_TARGET_RUNTIME,
    SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID,
    SALESFORCE_TRANSFORMATION_INPUT_PATHS,
    SALESFORCE_VALIDATION_COMMAND_IDS,
    SalesforceLocalValidator,
    _candidate_failure_evidence,
    _candidate_failure_summary,
    _capture_node_binding,
    _controller_jest_failure_evidence,
    _controller_jest_summary,
    _discover_supported_node,
    _jest_failure_summary,
    _jest_summary,
    _result_from_execution,
    _sandbox_profile,
    _unmet_runtime_prerequisite,
    build_salesforce_local_validator,
)

REPOSITORY = Path(__file__).parents[1]
FIXTURE = REPOSITORY / "fixtures" / "salesforce" / "account-contact-explorer"
TOOLCHAIN = REPOSITORY / "tooling" / "lwc-jest"
CANDIDATE_BUILDER = Path(__file__).with_name("salesforce_candidate_factory.py")
REGISTRY = load_agent_registry(REPOSITORY / "agents")
AGENT_DIGESTS = AgentDefinitionDigests(
    architect=REGISTRY.get(AgentRole.ARCHITECT).definition_digest,
    engineer=REGISTRY.get(AgentRole.ENGINEER).definition_digest,
    validator=REGISTRY.get(AgentRole.VALIDATOR).definition_digest,
)


@dataclass(frozen=True)
class RuntimeCase:
    project: Path
    session: AgentRunSession
    request: MigrationRequest
    manifest: MigrationManifest
    change_set: ChangeSet
    workspace: IsolatedWorkspace


def _accessible_listbox_card_outputs() -> dict[str, bytes]:
    outputs = salesforce_candidate_outputs()
    html_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.html"
    outputs[html_path] = b"""<template>
    <lightning-card title="Account Contact Explorer" icon-name="standard:account">
        <section class="controls" aria-label="Account contacts">
            <div data-role={selectorHook} role="listbox" aria-label="Account">
                <template for:each={accountOptionsWithSelection} for:item="option">
                    <button
                        key={option.label}
                        type="button"
                        role="option"
                        aria-selected={option.selected}
                        onclick={handleAccountOptionClick}>
                        {option.label}
                    </button>
                </template>
            </div>
            <div
                data-role={loadHook}
                role="button"
                tabindex="0"
                aria-disabled={isLoadDisabled}
                onclick={handleLoad}>
                Load contact details
            </div>
        </section>

        <template lwc:if={warningMessage}>
            <div data-state="warning" role="alert">{warningMessage}</div>
        </template>
        <template lwc:if={errorMessage}>
            <div data-state="error" role="alert">{errorMessage}</div>
        </template>

        <template lwc:if={isLoading}>
            <p data-state={loadingHook} aria-live="polite">Loading contact details</p>
        </template>
        <template lwc:elseif={hasContacts}>
            <section data-role={resultsHook} aria-label="Contact results">
                <template for:each={displayContacts} for:item="contact">
                    <article key={contact.Id} role="article" class="contact-card">
                        <h2>{contact.FirstName} {contact.LastName}</h2>
                        <p>{contact.Email}</p>
                        <p>{contact.Phone}</p>
                    </article>
                </template>
            </section>
        </template>
        <template lwc:elseif={showEmptyState}>
            <p data-state={emptyHook}>No contact details are available for this account.</p>
        </template>
    </lightning-card>
</template>
"""

    javascript_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    javascript = outputs[javascript_path].decode("utf-8")
    original_selection = """    handleAccountChange(event) {
        this.loadRequestGeneration += 1;
        this.selectedAccountId = event.detail.value;
        this.contacts = [];
        this.isLoading = false;
        this.hasLoaded = false;
        this.errorMessage = undefined;
        this.warningMessage = this.selectedAccountId
            ? undefined
            : 'Select an account before loading contacts.';
    }
"""
    alternate_selection = """    handleAccountChange(event) {
        this.applyAccountSelection(event.detail.value);
    }

    handleAccountOptionClick(event) {
        const selectedLabel = event.currentTarget.textContent.trim();
        const selectedOption = this.accountOptions.find(
            (option) => option.label === selectedLabel
        );
        this.applyAccountSelection(selectedOption?.value ?? '');
    }

    applyAccountSelection(accountId) {
        this.loadRequestGeneration += 1;
        this.selectedAccountId = accountId;
        this.contacts = [];
        this.isLoading = false;
        this.hasLoaded = false;
        this.errorMessage = undefined;
        this.warningMessage = this.selectedAccountId
            ? undefined
            : 'Choose an account before loading contact details.';
    }
"""
    assert original_selection in javascript
    javascript = javascript.replace(original_selection, alternate_selection, 1)
    getter_anchor = """    get isLoadDisabled() {
        return !this.selectedAccountId || this.isLoading;
    }
"""
    alternate_getters = """    get selectorHook() { return 'account-selector'; }
    get loadHook() { return 'load-contacts'; }
    get resultsHook() { return 'contact-results'; }
    get loadingHook() { return 'loading'; }
    get emptyHook() { return 'empty'; }

    get accountOptionsWithSelection() {
        return this.accountOptions.map((option) => ({
            ...option,
            selected: option.value === this.selectedAccountId
        }));
    }

    get displayContacts() {
        return this.contacts.map((contact) => ({
            ...contact,
            Email: contact.Email.toUpperCase(),
            Phone: contact.Phone.replace(/-/g, ' ')
        }));
    }

    get isLoadDisabled() {
        return !this.selectedAccountId || this.isLoading;
    }
"""
    assert getter_anchor in javascript
    outputs[javascript_path] = javascript.replace(
        getter_anchor,
        alternate_getters,
        1,
    ).encode("utf-8")
    outputs[
        LWC_TEST_PATH
    ] = b"""import { afterEach, describe, expect, it, jest } from '@jest/globals';
import { createElement } from 'lwc';
import AccountContactExplorer from 'c/accountContactExplorer';
import getAccounts from '@salesforce/apex/AccountContactExplorerController.getAccounts';
import getContacts from '@salesforce/apex/AccountContactExplorerController.getContacts';

jest.mock(
    '@salesforce/apex/AccountContactExplorerController.getAccounts',
    () => {
        const { createApexTestWireAdapter } = require('@salesforce/sfdx-lwc-jest');
        return {
            __esModule: true,
            default: createApexTestWireAdapter(jest.fn())
        };
    },
    { virtual: true }
);

jest.mock(
    '@salesforce/apex/AccountContactExplorerController.getContacts',
    () => ({ __esModule: true, default: jest.fn() }),
    { virtual: true }
);

afterEach(() => {
    while (document.body.firstChild) {
        document.body.removeChild(document.body.firstChild);
    }
});

describe('candidate-selected UI checks', () => {
    it('keeps a bounded account selection surface', () => {
        const element = createElement('c-account-contact-explorer', {
            is: AccountContactExplorer
        });
        document.body.appendChild(element);
        expect(element.shadowRoot).toBeDefined();
        expect(getAccounts).toBeDefined();
        expect(getContacts).toBeDefined();
    });
    it('keeps a bounded contact result surface', () => expect(true).toBe(true));
    it('keeps a bounded pending surface', () => expect(true).toBe(true));
});
"""
    return outputs


def _imperative_accounts_with_duplicate_selector_outputs() -> dict[str, bytes]:
    """Build a candidate with an otherwise working imperative account adapter."""

    outputs = salesforce_candidate_outputs()
    javascript_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    javascript = outputs[javascript_path].decode("utf-8")
    wired_accounts = """    @wire(getAccounts)
    wiredAccounts({ data, error }) {
        if (data) {
            this.accountOptions = [
                BLANK_ACCOUNT_OPTION,
                ...data.map((accountRecord) => ({
                    label: accountRecord.Name,
                    value: accountRecord.Id
                }))
            ];
            this.errorMessage = undefined;
        } else if (error) {
            this.accountOptions = [BLANK_ACCOUNT_OPTION];
            this.errorMessage = 'Accounts could not be loaded.';
        }
    }
"""
    imperative_accounts = """    async connectedCallback() {
        try {
            const data = await getAccounts();
            this.accountOptions = [
                BLANK_ACCOUNT_OPTION,
                ...data.map((accountRecord) => ({
                    label: accountRecord.Name,
                    value: accountRecord.Id
                }))
            ];
            this.errorMessage = undefined;
        } catch (error) {
            this.accountOptions = [BLANK_ACCOUNT_OPTION];
            this.errorMessage = 'Accounts could not be loaded.';
        }
    }
"""
    assert wired_accounts in javascript
    outputs[javascript_path] = javascript.replace(
        wired_accounts,
        imperative_accounts,
        1,
    ).encode("utf-8")

    html_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.html"
    html = outputs[html_path].decode("utf-8")
    selector = """            <lightning-combobox
                data-role="account-selector"
                name="account"
                label="Account"
                value={selectedAccountId}
                options={accountOptions}
                onchange={handleAccountChange}>
            </lightning-combobox>"""
    duplicate_wrapper = f"""            <div data-role="account-selector">
{selector}
            </div>"""
    assert selector in html
    outputs[html_path] = html.replace(selector, duplicate_wrapper, 1).encode("utf-8")
    return outputs


def _hidden_ui_outputs() -> dict[str, bytes]:
    outputs = salesforce_candidate_outputs()
    html_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.html"
    html = outputs[html_path].decode("utf-8")
    assert html.startswith("<template>\n") and html.endswith("</template>\n")
    body = html.removeprefix("<template>\n").removesuffix("</template>\n")
    outputs[html_path] = f"<template>\n<div hidden>\n{body}</div>\n</template>\n".encode()
    return outputs


def _datatable_without_visible_contact_fields_outputs() -> dict[str, bytes]:
    outputs = salesforce_candidate_outputs()
    javascript_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    javascript = outputs[javascript_path].decode("utf-8")
    original_columns = """const CONTACT_COLUMNS = Object.freeze([
    { label: 'First Name', fieldName: 'FirstName', type: 'text' },
    { label: 'Last Name', fieldName: 'LastName', type: 'text' },
    { label: 'Email', fieldName: 'Email', type: 'email' },
    { label: 'Phone', fieldName: 'Phone', type: 'phone' }
]);"""
    wrong_columns = """const CONTACT_COLUMNS = Object.freeze([
    { label: 'Record', fieldName: 'Id', type: 'text' }
]);"""
    assert original_columns in javascript
    outputs[javascript_path] = javascript.replace(
        original_columns,
        wrong_columns,
        1,
    ).encode("utf-8")
    return outputs


@contextmanager
def _runtime_case(
    tmp_path: Path,
    outputs: dict[str, bytes],
    *,
    install_node_modules: bool = True,
    request_repository: str = "source",
    entry_path: str = SALESFORCE_SOURCE_ENTRY,
    target_runtime: str = SALESFORCE_TARGET_RUNTIME,
    source_version: str = SALESFORCE_API_RUNTIME,
    target_version: str = SALESFORCE_API_RUNTIME,
    transformation_inputs: tuple[str, ...] = SALESFORCE_TRANSFORMATION_INPUT_PATHS,
) -> Iterator[RuntimeCase]:
    project = tmp_path / "project"
    source = project / "source"
    shutil.copytree(FIXTURE / "input", source)

    request = MigrationRequest(
        request_id="request-salesforce-runtime",
        platform=Platform.SALESFORCE,
        repository=request_repository,
        base_revision=content_revision(source),
        target=MigrationTarget(
            entry_path=entry_path,
            target_runtime=target_runtime,
            source_version=source_version,
            target_version=target_version,
            description="Migrate the legacy Visualforce explorer to an additive LWC.",
        ),
        allowed_environment=EnvironmentKind.LOCAL,
    )
    session = AgentRunSession.initialize(
        project,
        project / ".runs" / "run-salesforce-runtime",
        run_id="run-salesforce-runtime",
        thread_id="thread-salesforce-runtime",
        slice_id="salesforce-vf-to-lwc",
        source_root="source",
        request_digest=artifact_digest(request),
        agent_definition_digests=AGENT_DIGESTS,
        provider_id="offline-test",
        model_id="structured-agent/v1",
    )
    _copy_toolchain(project)
    if install_node_modules:
        jest_entry = project / "tooling/lwc-jest/node_modules/jest/bin/jest.js"
        jest_entry.parent.mkdir(parents=True)
        (jest_entry.parents[1] / "package.json").write_text(
            json.dumps({"name": "jest", "version": "29.7.0"}),
            encoding="utf-8",
        )
        sfdx_root = project / "tooling/lwc-jest/node_modules/@salesforce/sfdx-lwc-jest"
        sfdx_root.mkdir(parents=True)
        (sfdx_root / "package.json").write_text(
            json.dumps({"name": "@salesforce/sfdx-lwc-jest", "version": "7.9.0"}),
            encoding="utf-8",
        )
        (sfdx_root / "config.js").write_text("module.exports = {};\n", encoding="utf-8")
        jest_cli = project / "tooling/lwc-jest/node_modules/jest-cli/bin/jest.js"
        jest_cli.parent.mkdir(parents=True)
        jest_cli.write_text("// inert fake installed Jest CLI\n", encoding="utf-8")
        jest_entry.write_text(
            "// untrusted candidate runner must never execute directly\n",
            encoding="utf-8",
        )

    workspace = IsolatedWorkspace(
        session.source_root,
        SALESFORCE_AGENT_OUTPUT_PATHS,
        temp_parent=session.workspaces_dir,
        expected_revision=request.base_revision,
    )
    try:
        for path, content in outputs.items():
            workspace.write_bytes(path, content)
        changes = workspace.audit_changes()
        manifest = _manifest(request, transformation_inputs=transformation_inputs)
        change_set = ChangeSet(
            change_set_id="change-set-salesforce-runtime",
            request_id=request.request_id,
            manifest_id=manifest.manifest_id,
            base_revision=request.base_revision,
            changed_paths=changes.changed_paths,
            unified_diff=changes.unified_diff,
        )
        yield RuntimeCase(
            project=project,
            session=session,
            request=request,
            manifest=manifest,
            change_set=change_set,
            workspace=workspace,
        )
    finally:
        workspace.cleanup()


@contextmanager
def _runtime_case_with_real_jest_when_available(
    tmp_path: Path,
    outputs: dict[str, bytes],
) -> Iterator[RuntimeCase]:
    """Use the pinned Jest tree when the host can execute the sandboxed runtime."""

    sandbox_available = _macos_sandbox_available()
    if sandbox_available and not (TOOLCHAIN / "node_modules").is_dir():
        pytest.skip("the controller-pinned Jest installation is unavailable")
    with _runtime_case(
        tmp_path,
        outputs,
        install_node_modules=not sandbox_available,
    ) as case:
        if sandbox_available:
            shutil.copytree(
                TOOLCHAIN / "node_modules",
                case.project / "tooling/lwc-jest/node_modules",
                copy_function=os.link,
                symlinks=True,
            )
        yield case


def _copy_toolchain(project: Path) -> None:
    destination = project / "tooling/lwc-jest"
    destination.mkdir(parents=True)
    for filename in (
        "README.md",
        "package.json",
        "package-lock.json",
        "jest.config.js",
        "jest.setup.js",
    ):
        shutil.copy2(TOOLCHAIN / filename, destination / filename)
    controller_test = destination / LWC_CONTROLLER_TEST_PATH
    controller_test.parent.mkdir(parents=True)
    shutil.copy2(TOOLCHAIN / LWC_CONTROLLER_TEST_PATH, controller_test)


def _manifest(
    request: MigrationRequest,
    *,
    transformation_inputs: tuple[str, ...] = SALESFORCE_TRANSFORMATION_INPUT_PATHS,
) -> MigrationManifest:
    return MigrationManifest(
        manifest_id="manifest-salesforce-runtime",
        request_id=request.request_id,
        platform=Platform.SALESFORCE,
        base_revision=request.base_revision,
        approved_paths=SALESFORCE_AGENT_OUTPUT_PATHS,
        dependencies=(),
        transformations=(
            TransformationStep(
                step_id="add-salesforce-solution",
                description="Add the bounded Apex, LWC, tests, metadata, and manifest solution.",
                input_paths=transformation_inputs,
                output_paths=SALESFORCE_AGENT_OUTPUT_PATHS,
            ),
        ),
        validation_plan=tuple(
            ValidationCommand(
                check_id=f"check-{command_id}",
                command_id=command_id,
                purpose="Run one controller-owned Salesforce local validation command.",
                environment=EnvironmentKind.LOCAL,
                required=True,
            )
            for command_id in SALESFORCE_VALIDATION_COMMAND_IDS
        ),
        implementation_contract=SALESFORCE_IMPLEMENTATION_CONTRACT,
        required_approvals=(ApprovalAction.APPROVE_MANIFEST,),
    )


def _validator(
    case: RuntimeCase,
    *,
    registry: AgentRegistry = REGISTRY,
) -> SalesforceLocalValidator:
    return SalesforceLocalValidator(
        case.session,
        registry,
        timeout_seconds=30,
    )


def _run(case: RuntimeCase):
    return _validator(case)(
        case.request,
        case.manifest,
        case.change_set,
        case.workspace,
        1,
    )


def _result(report, command_id: str):
    return next(result for result in report.results if result.command_id == command_id)


def _assert_jest_not_spawned(report) -> None:
    for command_id in (
        SALESFORCE_LWC_JEST_COMMAND_ID,
        SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
    ):
        jest = _result(report, command_id)
        assert jest.status is CheckStatus.UNAVAILABLE
        assert jest.required is True
        assert jest.receipt is None


def _assert_no_probe_record(case: RuntimeCase) -> None:
    assert not tuple(case.session.scratch_dir.glob("salesforce-sandbox-probe-*.json"))


def _macos_sandbox_available() -> bool:
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        return False
    completed = subprocess.run(
        (
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1) (allow default)",
            "/usr/bin/true",
        ),
        check=False,
        capture_output=True,
    )
    return completed.returncode == 0


def _assert_probe_matches_host(report) -> None:
    probe = _result(report, SALESFORCE_SANDBOX_PROBE_COMMAND_ID)
    if _macos_sandbox_available():
        assert probe.status is CheckStatus.PASSED
        assert probe.receipt is not None and probe.receipt.exit_code == 0
        assert "all nine authority checks" in probe.summary
    else:
        assert probe.status is CheckStatus.UNAVAILABLE
        if probe.receipt is not None:
            assert probe.receipt.exit_code != 0


def _assert_runtime_checks_follow_verified_sandbox(report) -> None:
    """Prove static failures are not runtime prerequisites, while isolation is."""

    _assert_probe_matches_host(report)
    runtime_results = tuple(
        _result(report, command_id)
        for command_id in (
            SALESFORCE_LWC_JEST_COMMAND_ID,
            SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
        )
    )
    if _macos_sandbox_available():
        assert all(result.receipt is not None for result in runtime_results)
    else:
        assert all(result.status is CheckStatus.UNAVAILABLE for result in runtime_results)
        assert all(result.receipt is None for result in runtime_results)
        assert all(
            SALESFORCE_SANDBOX_PROBE_COMMAND_ID in result.summary for result in runtime_results
        )
        assert all(
            SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID not in result.summary
            for result in runtime_results
        )


def _jest_execution(
    stdout: str,
    *,
    exit_code: int = 0,
    tool_id: str = SALESFORCE_LWC_JEST_COMMAND_ID,
) -> ExecutionResult:
    now = datetime.now(UTC)
    digest = "sha256:" + "0" * 64
    return ExecutionResult(
        receipt=ToolReceipt(
            receipt_id="receipt-jest-parser",
            tool_id=tool_id,
            request_id="request-jest-parser",
            run_id="run-jest-parser",
            attempt=1,
            base_revision=digest,
            environment=EnvironmentKind.LOCAL,
            input_artifact_digest=digest,
            operation="bounded Jest parser unit evidence",
            working_directory="candidate",
            started_at=now,
            ended_at=now,
            exit_code=exit_code,
            terminal=True,
            stdout_digest=digest,
            stderr_digest=digest,
        ),
        stdout=stdout,
        stderr="",
        duration_seconds=0.0,
    )


def _jest_payload(
    candidate_root: Path,
    *,
    success: bool = True,
    titles: tuple[str, ...] | None = None,
) -> dict[str, object]:
    if titles is None:
        titles = tuple(
            f"candidate-authored behavior {index}"
            for index in range(1, SALESFORCE_MIN_CANDIDATE_LWC_JEST_TESTS + 1)
        )
    assertions = [{"title": title, "status": "passed"} for title in titles]
    if not success:
        assertions[-1]["status"] = "failed"
    return {
        "success": success,
        "wasInterrupted": False,
        "numTotalTestSuites": 1,
        "numPassedTestSuites": 1 if success else 0,
        "numFailedTestSuites": 0 if success else 1,
        "numPendingTestSuites": 0,
        "numRuntimeErrorTestSuites": 0,
        "numTotalTests": len(titles),
        "numPassedTests": len(titles) if success else len(titles) - 1,
        "numFailedTests": 0 if success else 1,
        "numPendingTests": 0,
        "numTodoTests": 0,
        "testResults": [
            {
                "name": str(candidate_root / LWC_TEST_PATH),
                "status": "passed" if success else "failed",
                "assertionResults": assertions,
                **({"failureMessages": ["controlled candidate failure"]} if not success else {}),
            }
        ],
    }


def _controller_jest_payload(
    controller_test_path: Path,
    *,
    failed_title: str | None = None,
) -> dict[str, object]:
    assertions = [
        {
            "title": title,
            "status": "failed" if title == failed_title else "passed",
        }
        for title in SALESFORCE_CONTROLLER_LWC_BEHAVIOR_TITLES
    ]
    success = failed_title is None
    return {
        "success": success,
        "wasInterrupted": False,
        "numTotalTestSuites": 1,
        "numPassedTestSuites": 1 if success else 0,
        "numFailedTestSuites": 0 if success else 1,
        "numPendingTestSuites": 0,
        "numRuntimeErrorTestSuites": 0,
        "numTotalTests": len(assertions),
        "numPassedTests": len(assertions) if success else len(assertions) - 1,
        "numFailedTests": 0 if success else 1,
        "numPendingTests": 0,
        "numTodoTests": 0,
        "testResults": [
            {
                "name": str(controller_test_path),
                "status": "passed" if success else "failed",
                "assertionResults": assertions,
                **({"failureMessages": ["bounded controller failure"]} if not success else {}),
            }
        ],
    }


def _controller_jest_check() -> ValidationCommand:
    return ValidationCommand(
        check_id="check-salesforce-lwc-controller-jest",
        command_id=SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
        purpose="Run the immutable controller-owned LWC behavior suite.",
        environment=EnvironmentKind.LOCAL,
        required=True,
    )


def _parser_candidate(tmp_path: Path) -> Path:
    candidate = tmp_path / "candidate"
    test_file = candidate / LWC_TEST_PATH
    test_file.parent.mkdir(parents=True)
    test_file.write_text("// parser-only candidate path\n", encoding="utf-8")
    return candidate


def _jest_check() -> ValidationCommand:
    return ValidationCommand(
        check_id="check-salesforce-lwc-jest",
        command_id=SALESFORCE_LWC_JEST_COMMAND_ID,
        purpose="Parse bounded Jest terminal evidence.",
        environment=EnvironmentKind.LOCAL,
        required=True,
    )


def test_salesforce_preset_is_exact_and_excludes_model_owned_tooling() -> None:
    assert SALESFORCE_RUNTIME_CONFIG.platform is Platform.SALESFORCE
    assert SALESFORCE_RUNTIME_CONFIG.analyzer_version == SALESFORCE_ANALYZER_VERSION
    assert SALESFORCE_RUNTIME_CONFIG.graph_builder is build_salesforce_dependency_graph
    assert SALESFORCE_PLATFORM_ADAPTER.scope_policy == SALESFORCE_SCOPE_POLICY
    assert SALESFORCE_PLATFORM_ADAPTER.adapter_id == "salesforce-vf-to-lwc-v11"
    assert SALESFORCE_SCOPE_POLICY.policy_id == "salesforce-vf-to-lwc-v11"
    assert len(SALESFORCE_AGENT_OUTPUT_PATHS) == 11
    assert (
        SALESFORCE_SCOPE_POLICY.required_source_input_paths == SALESFORCE_TRANSFORMATION_INPUT_PATHS
    )
    assert SALESFORCE_SCOPE_POLICY.approved_output_paths == SALESFORCE_AGENT_OUTPUT_PATHS
    assert SALESFORCE_SCOPE_POLICY.max_changed_files == 11
    assert (
        SALESFORCE_SCOPE_POLICY.required_implementation_contract
        == SALESFORCE_IMPLEMENTATION_CONTRACT
    )
    assert (
        SALESFORCE_SCOPE_POLICY.required_validation_command_ids == SALESFORCE_VALIDATION_COMMAND_IDS
    )
    for path in ("package.json", "package-lock.json", "jest.config.js"):
        assert path not in SALESFORCE_AGENT_OUTPUT_PATHS
        assert SALESFORCE_SCOPE_POLICY.allows_output_path(path) is False


def test_salesforce_local_validator_rejects_unknown_migration_unit(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "source"
    shutil.copytree(FIXTURE / "input", source)
    session = AgentRunSession.initialize(
        project,
        project / ".runs" / "run-unknown-unit",
        run_id="run-unknown-unit",
        thread_id="thread-unknown-unit",
        slice_id="bogus-unit",
        source_root="source",
        request_digest="sha256:" + "0" * 64,
        agent_definition_digests=AGENT_DIGESTS,
        provider_id="offline-test",
        model_id="structured-agent/v1",
    )

    with pytest.raises(PolicyViolation, match="unsupported Salesforce migration unit"):
        build_salesforce_local_validator(session, REGISTRY)


def test_runtime_prerequisites_collect_independent_diagnostics_after_static_failures() -> None:
    def result(command_id: str, status: CheckStatus) -> CheckResult:
        return CheckResult(
            check_id=f"check-{command_id}",
            command_id=command_id,
            required=True,
            status=status,
            receipt=_jest_execution(
                "",
                exit_code=1 if status is CheckStatus.FAILED else 0,
                tool_id=command_id,
            ).receipt,
            summary="bounded prerequisite test evidence",
        )

    completed = {
        SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID: result(
            SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID,
            CheckStatus.FAILED,
        ),
        SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID: result(
            SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID,
            CheckStatus.FAILED,
        ),
        SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID: result(
            SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID,
            CheckStatus.PASSED,
        ),
    }

    assert _unmet_runtime_prerequisite(SALESFORCE_SANDBOX_PROBE_COMMAND_ID, completed) is None

    completed[SALESFORCE_SANDBOX_PROBE_COMMAND_ID] = result(
        SALESFORCE_SANDBOX_PROBE_COMMAND_ID,
        CheckStatus.PASSED,
    )
    assert _unmet_runtime_prerequisite(SALESFORCE_LWC_JEST_COMMAND_ID, completed) is None
    assert _unmet_runtime_prerequisite(SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID, completed) is None


def test_macos_profile_allows_bootstrap_but_explicitly_removes_candidate_authority(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    candidate = project / ".runs/run/workspaces/candidate/repository"
    toolchain = project / "tooling/lwc-jest"
    scratch = project / ".runs/run/scratch"
    state = project / ".runs/run/state"
    runtime = tmp_path / "runtime"
    executable = runtime / "bin/runtime"
    package_boundary = candidate.parent / "package.json"

    profile = _sandbox_profile(
        candidate,
        toolchain,
        scratch,
        executable,
        project_root=project,
        private_roots=(project / ".runs/run", state),
        runtime_roots=(runtime,),
        exact_read_files=(package_boundary,),
    )

    assert "(allow default)" in profile
    assert "(deny default)" not in profile
    assert f'(deny file-read* (subpath "/Users") (subpath "{project}")' in profile
    for allowed in (candidate, toolchain, scratch, runtime):
        assert f'(subpath "{allowed}")' in profile
    assert f'(literal "{executable}")' in profile
    assert f'(literal "{package_boundary}")' in profile
    assert f'(subpath "{package_boundary}")' not in profile
    assert profile.index("(deny file-write*)") < profile.index(
        f'(allow file-write* (subpath "{scratch}"))'
    )
    assert "(deny network*)" in profile
    assert "(deny process-fork)" in profile
    assert profile.index("(deny process-exec)") < profile.index(
        f'(allow process-exec (literal "{executable}"))'
    )


def _package_boundary_candidate(tmp_path: Path) -> Path:
    candidate = tmp_path / "legacy-migration-workspace-fixed/repository"
    candidate.mkdir(parents=True)
    candidate.parent.chmod(0o700)
    return candidate


def test_controller_package_boundary_is_adjacent_fixed_and_removable(tmp_path: Path) -> None:
    candidate = _package_boundary_candidate(tmp_path)

    binding = salesforce_runtime._create_package_boundary(candidate)

    assert binding.path == candidate.parent / "package.json"
    assert binding.path.parent == candidate.parent
    assert not (candidate / "package.json").exists()
    assert binding.path.read_bytes() == salesforce_runtime._PACKAGE_BOUNDARY_BYTES
    assert stat.S_IMODE(binding.parent_mode) == 0o700
    assert binding.parent_uid == os.geteuid()
    assert binding.parent_gid == os.getegid()
    assert binding.link_count == 1
    assert json.loads(binding.path.read_text(encoding="utf-8")) == {
        "name": "legacy-migration-candidate-boundary",
        "private": True,
        "browserslist": ["defaults"],
    }
    salesforce_runtime._verify_package_boundary(binding, candidate)

    salesforce_runtime._remove_package_boundary(binding)

    with pytest.raises(OSError):
        os.fstat(binding.descriptor)
    assert not binding.path.exists()
    assert not (candidate.parent / ".legacy-migration-package-boundary.pending").exists()


@pytest.mark.parametrize(
    "drift",
    ("removed", "content", "mode", "replacement", "symlink", "hardlink"),
)
def test_controller_package_boundary_drift_fails_closed_and_cleans_safe_leaf(
    tmp_path: Path,
    drift: str,
) -> None:
    candidate = _package_boundary_candidate(tmp_path)
    binding = salesforce_runtime._create_package_boundary(candidate)
    victim = candidate / "victim.json"
    victim.write_text('{"preserved":true}\n', encoding="utf-8")

    if drift == "removed":
        binding.path.unlink()
    elif drift == "content":
        binding.path.chmod(0o600)
        binding.path.write_bytes(b'{"name":"tampered"}\n')
        binding.path.chmod(0o400)
    elif drift == "mode":
        binding.path.chmod(0o600)
    elif drift == "replacement":
        binding.path.unlink()
        binding.path.write_bytes(salesforce_runtime._PACKAGE_BOUNDARY_BYTES)
        binding.path.chmod(0o400)
    elif drift == "symlink":
        binding.path.unlink()
        binding.path.symlink_to(victim)
    else:
        os.link(binding.path, candidate / "boundary-hardlink.json")

    with pytest.raises(PolicyViolation, match="package boundary"):
        salesforce_runtime._verify_package_boundary(binding, candidate)
    with pytest.raises(PolicyViolation, match="package boundary"):
        salesforce_runtime._remove_package_boundary(binding)
    with pytest.raises(OSError):
        os.fstat(binding.descriptor)

    if drift in {"replacement", "symlink"}:
        assert os.path.lexists(binding.path)
    else:
        assert not binding.path.exists()
    if drift == "hardlink":
        assert (candidate / "boundary-hardlink.json").read_bytes() == (
            salesforce_runtime._PACKAGE_BOUNDARY_BYTES
        )
    assert victim.read_text(encoding="utf-8") == '{"preserved":true}\n'


def test_controller_package_boundary_requires_private_owned_container(tmp_path: Path) -> None:
    candidate = _package_boundary_candidate(tmp_path)
    candidate.parent.chmod(0o755)

    with pytest.raises(PolicyViolation, match="container permissions"):
        salesforce_runtime._create_package_boundary(candidate)

    assert not (candidate.parent / "package.json").exists()
    assert not (candidate.parent / ".legacy-migration-package-boundary.pending").exists()


def test_controller_package_boundary_refuses_reuse(tmp_path: Path) -> None:
    candidate = _package_boundary_candidate(tmp_path)
    binding = salesforce_runtime._create_package_boundary(candidate)
    try:
        with pytest.raises(PolicyViolation, match="already exists"):
            salesforce_runtime._create_package_boundary(candidate)
        salesforce_runtime._verify_package_boundary(binding, candidate)
    finally:
        salesforce_runtime._remove_package_boundary(binding)


def test_sandbox_epoch_anchor_inventory_is_bounded_before_probe_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        validator = _validator(case)
        for sequence in range(salesforce_runtime._MAX_SANDBOX_EPOCHS_PER_ATTEMPT):
            kind = f"salesforce-jest-sandbox-epoch-1-{sequence:024x}"
            case.session.bind_runtime_anchor(kind, {"sequence": sequence})

        with pytest.raises(PolicyViolation, match="recovery budget"):
            validator._require_sandbox_epoch_capacity(
                "salesforce-jest-sandbox-epoch-1-ffffffffffffffffffffffff",
                1,
            )

        monkeypatch.setattr(validator, "_sandbox_unavailable_reason", lambda: None)
        monkeypatch.setattr(validator, "_sandbox_backend", Path("/usr/bin/true"))
        monkeypatch.setattr(
            validator,
            "_probe_python",
            Path(validator._controller_python_binding.resolved_path),
        )
        boundary = salesforce_runtime._create_package_boundary(case.workspace.root)
        try:
            availability, specs, binding = validator._command_specs(
                case.workspace.root,
                salesforce_runtime.tree_fingerprint(case.workspace.root),
                1,
                artifact_digest(case.change_set),
                boundary,
            )
        finally:
            salesforce_runtime._remove_package_boundary(boundary)

        assert binding is None
        assert availability[SALESFORCE_SANDBOX_PROBE_COMMAND_ID] == (
            "the controller-owned sandbox challenge could not be created"
        )
        assert all(spec.command_id != SALESFORCE_SANDBOX_PROBE_COMMAND_ID for spec in specs)
        assert not tuple(case.session.scratch_dir.glob("salesforce-sandbox-probe-*.json"))


def test_crash_left_probe_records_consume_epoch_budget(tmp_path: Path) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        validator = _validator(case)
        for sequence in range(salesforce_runtime._MAX_SANDBOX_EPOCHS_PER_ATTEMPT):
            record = case.session.scratch_dir / (f"salesforce-sandbox-probe-1-{sequence:024x}.json")
            record.write_text("{}", encoding="utf-8")

        with pytest.raises(PolicyViolation, match="recovery budget"):
            validator._require_sandbox_epoch_capacity(
                "salesforce-jest-sandbox-epoch-1-ffffffffffffffffffffffff",
                1,
            )


def _homebrew_node_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    brew = tmp_path / "brew"
    cellar = brew / "Cellar/node"
    resolved = cellar / "1.2.3/bin/node"
    resolved.parent.mkdir(parents=True)
    resolved.write_bytes(b"bounded fake node executable\n")
    resolved.chmod(0o555)
    lexical = brew / "bin/node"
    lexical.parent.mkdir(parents=True)
    lexical.symlink_to(Path("../Cellar/node/1.2.3/bin/node"))
    return lexical, cellar, resolved


def test_validator_does_not_bind_node_without_the_protected_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        monkeypatch.setattr(
            salesforce_runtime,
            "_MACOS_SANDBOX_EXEC",
            tmp_path / "missing-sandbox-exec",
        )

        def unexpected_node_discovery() -> None:
            raise AssertionError("Node must not be discovered without the protected sandbox")

        monkeypatch.setattr(
            salesforce_runtime,
            "_discover_supported_node",
            unexpected_node_discovery,
        )

        validator = _validator(case)

    assert validator._sandbox_backend is None
    assert validator._node_binding is None
    assert validator._node_executable is None


def test_discover_supported_node_treats_regular_usr_local_shape_as_protected_system(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lexical = tmp_path / "usr/local/bin/node"
    lexical.parent.mkdir(parents=True)
    lexical.write_bytes(b"bounded fake setup-node executable\n")
    lexical.chmod(0o555)
    sentinel = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(salesforce_runtime, "_SUPPORTED_NODE_PATHS", (lexical,))
    monkeypatch.setattr(
        salesforce_runtime,
        "_HOMEBREW_NODE_CELLARS",
        {lexical: tmp_path / "usr/local/Cellar/node"},
    )
    monkeypatch.setattr(
        salesforce_runtime,
        "_discover_protected_executable",
        lambda path: path,
    )

    def capture(path: Path, *, cellar_root: Path | None) -> object:
        captured["path"] = path
        captured["cellar_root"] = cellar_root
        return sentinel

    monkeypatch.setattr(salesforce_runtime, "_capture_node_binding", capture)

    assert _discover_supported_node() is sentinel
    assert captured == {"path": lexical, "cellar_root": None}


def test_homebrew_node_symlink_is_bound_to_its_resolved_cellar_leaf(tmp_path: Path) -> None:
    lexical, cellar, resolved = _homebrew_node_fixture(tmp_path)

    binding = _capture_node_binding(lexical, cellar_root=cellar)

    assert binding.lexical_path == str(lexical)
    assert binding.link_target == "../Cellar/node/1.2.3/bin/node"
    assert binding.resolved_path == str(resolved)
    assert binding.resolved_sha256 == (
        "sha256:" + hashlib.sha256(resolved.read_bytes()).hexdigest()
    )
    assert any(component.path == str(lexical.parent) for component in binding.path_components)
    assert any(component.path == str(resolved) for component in binding.path_components)


def test_homebrew_node_unexpected_retarget_is_rejected(tmp_path: Path) -> None:
    lexical, cellar, _resolved = _homebrew_node_fixture(tmp_path)
    outside = tmp_path / "outside/node"
    outside.parent.mkdir()
    outside.write_bytes(b"outside executable\n")
    outside.chmod(0o555)
    lexical.unlink()
    lexical.symlink_to(outside)

    with pytest.raises(PolicyViolation, match="outside its Cellar"):
        _capture_node_binding(lexical, cellar_root=cellar)


@pytest.mark.parametrize("drift", ("retarget", "digest"))
def test_bound_homebrew_node_identity_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    lexical, cellar, resolved = _homebrew_node_fixture(tmp_path)
    monkeypatch.setattr(salesforce_runtime, "_SUPPORTED_NODE_PATHS", (lexical,))
    monkeypatch.setattr(salesforce_runtime, "_HOMEBREW_NODE_CELLARS", {lexical: cellar})
    monkeypatch.setattr(
        salesforce_runtime,
        "_discover_protected_executable",
        lambda path: path,
    )

    with _runtime_case(tmp_path / "case", salesforce_candidate_outputs()) as case:
        validator = _validator(case)
        assert _discover_supported_node() == validator._node_binding
        if drift == "retarget":
            replacement = cellar / "2.0.0/bin/node"
            replacement.parent.mkdir(parents=True)
            replacement.write_bytes(b"replacement node executable\n")
            replacement.chmod(0o555)
            lexical.unlink()
            lexical.symlink_to(Path("../Cellar/node/2.0.0/bin/node"))
        else:
            resolved.chmod(0o755)
            resolved.write_bytes(b"digest drift\n")
            resolved.chmod(0o555)

        with pytest.raises(PolicyViolation, match="Node executable identity changed"):
            validator._verify_node_runtime()


def _salesforce_multi_step_manifest(
    manifest: MigrationManifest,
    *,
    duplicate_output_owner: bool = False,
) -> MigrationManifest:
    split = len(SALESFORCE_AGENT_OUTPUT_PATHS) // 2
    first_outputs = SALESFORCE_AGENT_OUTPUT_PATHS[:split]
    second_outputs = SALESFORCE_AGENT_OUTPUT_PATHS[split:]
    if duplicate_output_owner:
        second_outputs = (first_outputs[0], *second_outputs)
    steps = (
        TransformationStep(
            step_id="add-apex-and-metadata",
            description="Create the first bounded portion of the Salesforce solution.",
            input_paths=SALESFORCE_TRANSFORMATION_INPUT_PATHS,
            output_paths=first_outputs,
        ),
        TransformationStep(
            step_id="add-lwc-and-tests",
            description="Create the remaining bounded Salesforce solution files.",
            input_paths=(SALESFORCE_TRANSFORMATION_INPUT_PATHS[0],),
            output_paths=second_outputs,
        ),
    )
    return manifest.model_copy(update={"transformations": steps})


def test_salesforce_runtime_accepts_bounded_multi_step_manifest(tmp_path: Path) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        manifest = _salesforce_multi_step_manifest(case.manifest)

        report = _validator(case)(
            case.request,
            manifest,
            case.change_set,
            case.workspace,
            1,
        )

        assert tuple(result.command_id for result in report.results) == (
            SALESFORCE_VALIDATION_COMMAND_IDS
        )


def test_salesforce_runtime_rejects_duplicate_output_ownership(tmp_path: Path) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        manifest = _salesforce_multi_step_manifest(
            case.manifest,
            duplicate_output_owner=True,
        )

        with pytest.raises(PolicyViolation, match="owned by exactly one transformation"):
            _validator(case)(
                case.request,
                manifest,
                case.change_set,
                case.workspace,
                1,
            )

        _assert_no_probe_record(case)


def test_builtin_sandbox_probe_enforces_authority_and_fake_toolchain_never_runs_jest(
    tmp_path: Path,
) -> None:
    outputs = salesforce_candidate_outputs()
    with _runtime_case(tmp_path, outputs) as case:
        assert not any(
            part.casefold() in {"expected", "golden", "oracle"}
            for path in case.project.rglob("*")
            for part in path.relative_to(case.project).parts
        )
        candidate_revision = content_revision(case.workspace.root)
        source_revision = content_revision(case.session.source_root)

        report = _run(case)

        assert report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        assert tuple(result.command_id for result in report.results) == (
            SALESFORCE_VALIDATION_COMMAND_IDS
        )
        _assert_probe_matches_host(report)
        if _macos_sandbox_available():
            record_paths = tuple(case.session.scratch_dir.glob("salesforce-sandbox-probe-*.json"))
            assert len(record_paths) == 1
            record = json.loads(record_paths[0].read_text(encoding="utf-8"))
            assert record["backend_id"] == "macos-sandbox-exec-v2"
            assert record["candidate_read_allowed"] is True
            assert record["toolchain_read_allowed"] is True
            assert record["package_boundary_read_allowed"] is True
            assert record["package_boundary_verified"] is True
            assert record["package_boundary_sha256"] == (
                salesforce_runtime._PACKAGE_BOUNDARY_SHA256
            )
            assert record["scratch_write_allowed"] is True
            assert record["external_read_denied"] is True
            assert record["external_write_denied"] is True
            assert record["network_denied"] is True
            assert record["child_process_denied"] is True
        _assert_jest_not_spawned(report)
        assert content_revision(case.workspace.root) == candidate_revision
        assert content_revision(case.session.source_root) == source_revision
        assert not (case.workspace.root / "package.json").exists()
        assert not (case.workspace.root.parent / "package.json").exists()
        assert not (case.session.source_root / "package.json").exists()
        assert not (case.workspace.root / "package-lock.json").exists()
        assert not (case.workspace.root / "jest.config.js").exists()

        serialized = report.model_dump_json()
        assert str(case.project) not in serialized
        assert str(CANDIDATE_BUILDER) not in serialized
        assert all(title not in serialized for title in SALESFORCE_CONTROLLER_LWC_BEHAVIOR_TITLES)
        toolchain = _result(report, SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID)
        assert toolchain.status is CheckStatus.PASSED
        assert all(digest in toolchain.summary for digest in LWC_JEST_TOOLCHAIN_DIGESTS.values())


def test_installed_jest_dependencies_match_pinned_identity() -> None:
    if not _macos_sandbox_available():
        pytest.skip("the controller-pinned Jest runtime is supported only by macOS sandbox-exec")
    if not (TOOLCHAIN / "node_modules").is_dir():
        pytest.skip("the controller-pinned Jest installation is unavailable")
    assert (
        salesforce_runtime._full_tree_fingerprint(TOOLCHAIN / "node_modules")
        == salesforce_runtime._PINNED_NODE_MODULES_TREE_FINGERPRINT
    )


def test_real_pinned_jest_runs_under_resolved_node_sandbox(tmp_path: Path) -> None:
    if not _macos_sandbox_available():
        pytest.skip("macOS sandbox-exec is unavailable in this host boundary")
    if not (TOOLCHAIN / "node_modules").is_dir():
        pytest.skip("the controller-pinned Jest installation is unavailable")

    with _runtime_case(
        tmp_path,
        salesforce_candidate_outputs(),
        install_node_modules=False,
    ) as case:
        shutil.copytree(
            TOOLCHAIN / "node_modules",
            case.project / "tooling/lwc-jest/node_modules",
            copy_function=os.link,
            symlinks=True,
        )

        validator = _validator(case)
        report = validator(
            case.request,
            case.manifest,
            case.change_set,
            case.workspace,
            1,
        )

        assert report.disposition is ValidationDisposition.READY_FOR_HUMAN_REVIEW, tuple(
            (
                result.command_id,
                result.status,
                result.receipt.exit_code if result.receipt is not None else None,
            )
            for result in report.results
            if result.status is not CheckStatus.PASSED
        )
        assert all(result.status is CheckStatus.PASSED for result in report.results)
        probe = _result(report, SALESFORCE_SANDBOX_PROBE_COMMAND_ID)
        jest = _result(report, SALESFORCE_LWC_JEST_COMMAND_ID)
        controller_jest = _result(report, SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID)
        assert probe.receipt is not None and probe.receipt.exit_code == 0
        assert jest.receipt is not None and jest.receipt.exit_code == 0
        assert "tests=10 evidence-role=supplemental" in jest.summary
        assert controller_jest.receipt is not None and controller_jest.receipt.exit_code == 0
        assert "tests=10 independent-of-candidate-tests=true" in controller_jest.summary
        assert validator._probe_python == Path(validator._controller_python_binding.resolved_path)
        assert Path(validator._node_binding.resolved_path) == validator._node_executable
        assert not (case.workspace.root / "package.json").exists()
        assert not (case.workspace.root.parent / "package.json").exists()
        assert not (case.session.source_root / "package.json").exists()


def test_real_pinned_jest_accepts_alternate_private_state_markup_and_safe_copy(
    tmp_path: Path,
) -> None:
    if not _macos_sandbox_available():
        pytest.skip("macOS sandbox-exec is unavailable in this host boundary")
    if not (TOOLCHAIN / "node_modules").is_dir():
        pytest.skip("the controller-pinned Jest installation is unavailable")

    outputs = salesforce_candidate_outputs()
    javascript_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    javascript = outputs[javascript_path].decode("utf-8")
    assert "hasLoaded" in javascript
    outputs[javascript_path] = javascript.replace("hasLoaded", "requestCompleted").encode("utf-8")
    html_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.html"
    html = outputs[html_path].decode("utf-8")
    html = html.replace("lightning-combobox", "lightning-radio-group")
    original_button = """            <lightning-button
                data-role="load-contacts"
                class="load-button"
                label="Load Contacts"
                variant="brand"
                disabled={isLoadDisabled}
                onclick={handleLoad}>
            </lightning-button>"""
    alternate_button = """            <button
                data-role="load-contacts"
                class="load-button"
                type="button"
                disabled={isLoadDisabled}
                onclick={handleLoad}>
                Fetch contacts
            </button>"""
    assert original_button in html
    outputs[html_path] = html.replace(original_button, alternate_button).encode("utf-8")
    replacements = {
        "-- Select an account --": "Choose an account",
        "Accounts could not be loaded.": "Account choices are temporarily unavailable.",
        "Contacts could not be loaded.": "Contact results are temporarily unavailable.",
        "Select an account before loading contacts.": "Choose an account to continue.",
        "Loading contacts": "Working on contact results",
        "No contacts were found for the selected account.": "Nothing matched this account.",
        "empty-state": "no-results",
    }
    for path in (
        javascript_path,
        html_path,
        LWC_TEST_PATH,
        f"{Path(html_path).with_suffix('.css')}",
    ):
        content = outputs[path].decode("utf-8")
        for old, new in replacements.items():
            content = content.replace(old, new)
        if path == LWC_TEST_PATH:
            content = content.replace("'lightning-combobox'", "'lightning-radio-group'")
            content = content.replace("'lightning-button'", "'button'")
        outputs[path] = content.encode("utf-8")

    with _runtime_case(tmp_path, outputs, install_node_modules=False) as case:
        shutil.copytree(
            TOOLCHAIN / "node_modules",
            case.project / "tooling/lwc-jest/node_modules",
            copy_function=os.link,
            symlinks=True,
        )

        report = _run(case)

        candidate = _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID)
        jest = _result(report, SALESFORCE_LWC_JEST_COMMAND_ID)
        controller_jest = _result(report, SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID)
        assert candidate.status is CheckStatus.PASSED
        assert candidate.diagnostic_ids == ()
        assert jest.status is CheckStatus.PASSED
        assert jest.receipt is not None and jest.receipt.exit_code == 0
        assert "tests=10 evidence-role=supplemental" in jest.summary
        assert controller_jest.status is CheckStatus.PASSED
        assert controller_jest.receipt is not None and controller_jest.receipt.exit_code == 0
        assert "tests=10 independent-of-candidate-tests=true" in controller_jest.summary
        assert report.disposition is ValidationDisposition.READY_FOR_HUMAN_REVIEW


def test_real_pinned_jest_accepts_aria_listbox_and_accessible_contact_cards(
    tmp_path: Path,
) -> None:
    if not _macos_sandbox_available():
        pytest.skip("macOS sandbox-exec is unavailable in this host boundary")
    if not (TOOLCHAIN / "node_modules").is_dir():
        pytest.skip("the controller-pinned Jest installation is unavailable")

    with _runtime_case(
        tmp_path,
        _accessible_listbox_card_outputs(),
        install_node_modules=False,
    ) as case:
        shutil.copytree(
            TOOLCHAIN / "node_modules",
            case.project / "tooling/lwc-jest/node_modules",
            copy_function=os.link,
            symlinks=True,
        )

        report = _run(case)

        candidate = _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID)
        candidate_jest = _result(report, SALESFORCE_LWC_JEST_COMMAND_ID)
        controller_jest = _result(report, SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID)
        assert candidate.status is CheckStatus.PASSED
        assert candidate_jest.status is CheckStatus.PASSED
        assert "tests=3 evidence-role=supplemental" in candidate_jest.summary
        assert controller_jest.status is CheckStatus.PASSED
        assert "tests=10 independent-of-candidate-tests=true" in controller_jest.summary
        assert report.disposition is ValidationDisposition.READY_FOR_HUMAN_REVIEW


def test_controller_jest_harness_accepts_aria_listbox_and_accessible_contact_cards(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    jest_entry = TOOLCHAIN / "node_modules/jest/bin/jest.js"
    if node is None or not jest_entry.is_file():
        pytest.skip("the pinned Node/Jest harness is unavailable")

    with _runtime_case(
        tmp_path,
        _accessible_listbox_card_outputs(),
        install_node_modules=False,
    ) as case:
        environment = dict(os.environ)
        environment["NODE_PATH"] = str(TOOLCHAIN / "node_modules")
        candidate_completed = subprocess.run(
            (
                node,
                str(jest_entry),
                "--config",
                str(TOOLCHAIN / "jest.config.js"),
                "--rootDir",
                str(case.workspace.root),
                "--runInBand",
                "--no-cache",
                "--runTestsByPath",
                str(case.workspace.root / LWC_TEST_PATH),
            ),
            cwd=case.workspace.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        controller_completed = subprocess.run(
            (
                node,
                str(jest_entry),
                "--config",
                str(TOOLCHAIN / "jest.config.js"),
                "--rootDir",
                str(TOOLCHAIN),
                "--runInBand",
                "--no-cache",
                "--runTestsByPath",
                str(TOOLCHAIN / LWC_CONTROLLER_TEST_PATH),
            ),
            cwd=case.workspace.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )

    assert candidate_completed.returncode == 0, (
        candidate_completed.stdout + candidate_completed.stderr
    )
    assert "3 passed" in candidate_completed.stderr
    assert controller_completed.returncode == 0, (
        controller_completed.stdout + controller_completed.stderr
    )
    assert "10 passed" in controller_completed.stderr


def _run_controller_jest_harness(
    tmp_path: Path,
    outputs: dict[str, bytes],
) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    jest_entry = TOOLCHAIN / "node_modules/jest/bin/jest.js"
    if node is None or not jest_entry.is_file():
        pytest.skip("the pinned Node/Jest harness is unavailable")

    with _runtime_case(
        tmp_path,
        outputs,
        install_node_modules=False,
    ) as case:
        environment = dict(os.environ)
        environment["NODE_PATH"] = str(TOOLCHAIN / "node_modules")
        completed = subprocess.run(
            (
                node,
                str(jest_entry),
                "--config",
                str(TOOLCHAIN / "jest.config.js"),
                "--rootDir",
                str(TOOLCHAIN),
                "--runInBand",
                "--no-cache",
                "--runTestsByPath",
                str(TOOLCHAIN / LWC_CONTROLLER_TEST_PATH),
            ),
            cwd=case.workspace.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    return completed


def test_controller_jest_harness_accepts_disabled_pending_load_and_unique_row_keys(
    tmp_path: Path,
) -> None:
    completed = _run_controller_jest_harness(tmp_path, salesforce_candidate_outputs())

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "10 passed" in completed.stderr


def test_controller_jest_harness_accepts_enabled_pending_load_with_stale_guard(
    tmp_path: Path,
) -> None:
    outputs = salesforce_candidate_outputs()
    javascript_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[javascript_path].decode("utf-8")
    original = "return !this.selectedAccountId || this.isLoading;"
    assert original in source
    outputs[javascript_path] = source.replace(
        original,
        "return !this.selectedAccountId;",
        1,
    ).encode("utf-8")

    completed = _run_controller_jest_harness(tmp_path, outputs)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "10 passed" in completed.stderr


def test_controller_jest_harness_rejects_duplicate_datatable_row_keys(
    tmp_path: Path,
) -> None:
    outputs = salesforce_candidate_outputs()
    javascript_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[javascript_path].decode("utf-8")
    original = """this.contacts = (result ?? []).map((contactRecord) => ({
                ...contactRecord
            }));"""
    replacement = """this.contacts = (result ?? []).map((contactRecord) => ({
                ...contactRecord,
                Id: 'duplicate-row-key'
            }));"""
    assert original in source
    outputs[javascript_path] = source.replace(original, replacement, 1).encode("utf-8")

    completed = _run_controller_jest_harness(tmp_path, outputs)

    assert completed.returncode != 0
    assert "key-field values must be unique" in completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("case_name", "expected_failure_title"),
    (
        ("hidden-ui", "controller: renders account options from the wire adapter"),
        ("duplicate-selector", "controller: renders account options from the wire adapter"),
        ("missing-contact-columns", "controller: invokes contacts only after the Load action"),
    ),
)
def test_controller_jest_harness_rejects_non_visible_or_ambiguous_ui(
    tmp_path: Path,
    case_name: str,
    expected_failure_title: str,
) -> None:
    outputs_by_case = {
        "hidden-ui": _hidden_ui_outputs,
        "duplicate-selector": _imperative_accounts_with_duplicate_selector_outputs,
        "missing-contact-columns": _datatable_without_visible_contact_fields_outputs,
    }

    completed = _run_controller_jest_harness(tmp_path, outputs_by_case[case_name]())
    terminal_output = completed.stdout + completed.stderr

    assert completed.returncode != 0
    assert expected_failure_title in terminal_output


def test_controller_owned_jest_rejects_noop_candidate_suite_false_green(
    tmp_path: Path,
) -> None:
    if not _macos_sandbox_available():
        pytest.skip("macOS sandbox-exec is unavailable in this host boundary")
    if not (TOOLCHAIN / "node_modules").is_dir():
        pytest.skip("the controller-pinned Jest installation is unavailable")

    outputs = salesforce_candidate_outputs()
    javascript_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    javascript = outputs[javascript_path].decode("utf-8")
    assert "    async handleLoad() {\n" in javascript
    outputs[javascript_path] = javascript.replace(
        "    async handleLoad() {\n",
        "    async handleLoad() {\n        return;\n",
        1,
    ).encode("utf-8")
    candidate_test = outputs[LWC_TEST_PATH].decode("utf-8")
    assert candidate_test.count("async () => {") == 10
    outputs[LWC_TEST_PATH] = candidate_test.replace(
        "async () => {",
        "async () => {\n        return;",
    ).encode("utf-8")

    with _runtime_case(tmp_path, outputs, install_node_modules=False) as case:
        shutil.copytree(
            TOOLCHAIN / "node_modules",
            case.project / "tooling/lwc-jest/node_modules",
            copy_function=os.link,
            symlinks=True,
        )

        report = _run(case)

        candidate = _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID)
        candidate_jest = _result(report, SALESFORCE_LWC_JEST_COMMAND_ID)
        controller_jest = _result(report, SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID)
        assert candidate.status is CheckStatus.PASSED
        assert candidate_jest.status is CheckStatus.PASSED
        assert controller_jest.status is CheckStatus.FAILED
        assert controller_jest.diagnostic_ids
        assert report.disposition is ValidationDisposition.RECOVERABLE_FAILURE


def test_package_boundary_is_cleaned_when_validation_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        validator = _validator(case)

        def fail_after_boundary(*_args: object, **_kwargs: object) -> None:
            assert (case.workspace.root.parent / "package.json").is_file()
            raise RuntimeError("controlled validator failure")

        monkeypatch.setattr(validator, "_validate_with_package_boundary", fail_after_boundary)

        with pytest.raises(RuntimeError, match="controlled validator failure"):
            validator(
                case.request,
                case.manifest,
                case.change_set,
                case.workspace,
                1,
            )

        assert not (case.workspace.root.parent / "package.json").exists()
        assert not (
            case.workspace.root.parent / ".legacy-migration-package-boundary.pending"
        ).exists()


def test_cleanup_drift_is_primary_but_chains_original_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        validator = _validator(case)

        def fail_and_remove_boundary(*args: object, **_kwargs: object) -> None:
            boundary = args[-1]
            assert isinstance(boundary, salesforce_runtime._PackageBoundaryBinding)
            boundary.path.unlink()
            raise RuntimeError("original validator failure")

        monkeypatch.setattr(
            validator,
            "_validate_with_package_boundary",
            fail_and_remove_boundary,
        )

        with pytest.raises(PolicyViolation, match="package boundary drifted") as caught:
            validator(
                case.request,
                case.manifest,
                case.change_set,
                case.workspace,
                1,
            )

        assert isinstance(caught.value.__cause__, RuntimeError)
        assert str(caught.value.__cause__) == "original validator failure"
        assert not (case.workspace.root.parent / "package.json").exists()


def test_real_pinned_jest_passes_from_live_like_private_runs_path() -> None:
    if not _macos_sandbox_available():
        pytest.skip("macOS sandbox-exec is unavailable in this host boundary")
    if not (TOOLCHAIN / "node_modules").is_dir():
        pytest.skip("the controller-pinned Jest installation is unavailable")

    live_runs_root = REPOSITORY / ".runs"
    live_runs_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sandbox-live-like-", dir=live_runs_root) as raw:
        with _runtime_case(
            Path(raw),
            salesforce_candidate_outputs(),
            install_node_modules=False,
        ) as case:
            shutil.copytree(
                TOOLCHAIN / "node_modules",
                case.project / "tooling/lwc-jest/node_modules",
                copy_function=os.link,
                symlinks=True,
            )

            report = _run(case)

            assert report.disposition is ValidationDisposition.READY_FOR_HUMAN_REVIEW
            assert all(result.status is CheckStatus.PASSED for result in report.results)
            jest = _result(report, SALESFORCE_LWC_JEST_COMMAND_ID)
            assert jest.receipt is not None and jest.receipt.exit_code == 0
            assert "tests=10 evidence-role=supplemental" in jest.summary
            assert str(case.workspace.root).startswith(str(REPOSITORY / ".runs"))
            assert not (case.workspace.root / "package.json").exists()
            assert not (case.workspace.root.parent / "package.json").exists()
            assert not (case.session.source_root / "package.json").exists()


def test_real_pinned_jest_same_attempt_replay_preserves_distinct_epochs(
    tmp_path: Path,
) -> None:
    if not _macos_sandbox_available():
        pytest.skip("macOS sandbox-exec is unavailable in this host boundary")
    if not (TOOLCHAIN / "node_modules").is_dir():
        pytest.skip("the controller-pinned Jest installation is unavailable")

    outputs = salesforce_candidate_outputs()
    with _runtime_case(tmp_path, outputs, install_node_modules=False) as case:
        shutil.copytree(
            TOOLCHAIN / "node_modules",
            case.project / "tooling/lwc-jest/node_modules",
            copy_function=os.link,
            symlinks=True,
        )
        validator = _validator(case)

        first = validator(
            case.request,
            case.manifest,
            case.change_set,
            case.workspace,
            1,
        )
        first_records = tuple(case.session.scratch_dir.glob("salesforce-sandbox-probe-*.json"))
        assert first.disposition is ValidationDisposition.READY_FOR_HUMAN_REVIEW
        assert len(first_records) == 1

        replay = IsolatedWorkspace(
            case.session.source_root,
            SALESFORCE_AGENT_OUTPUT_PATHS,
            temp_parent=case.session.workspaces_dir,
            expected_revision=case.request.base_revision,
        )
        try:
            for path, content in outputs.items():
                replay.write_bytes(path, content)
            replay_changes = replay.audit_changes()
            assert replay_changes.changed_paths == case.change_set.changed_paths
            assert replay_changes.unified_diff.strip() == case.change_set.unified_diff.strip()

            second = validator(
                case.request,
                case.manifest,
                case.change_set,
                replay,
                1,
            )

            assert second.disposition is ValidationDisposition.READY_FOR_HUMAN_REVIEW
            assert all(result.status is CheckStatus.PASSED for result in second.results)
            records = tuple(case.session.scratch_dir.glob("salesforce-sandbox-probe-*.json"))
            assert len(records) == 2
            assert set(records) > set(first_records)
            policy_anchor = (
                case.session.runtime_anchors_dir / "salesforce-jest-sandbox-policy-1.json"
            )
            epoch_anchors = tuple(
                case.session.runtime_anchors_dir.glob("salesforce-jest-sandbox-epoch-1-*.json")
            )
            assert policy_anchor.is_file()
            assert len(epoch_anchors) == 2
            assert not (replay.root.parent / "package.json").exists()
        finally:
            replay.cleanup()


def test_forbidden_jest_capability_does_not_suppress_the_builtin_sandbox_probe(
    tmp_path: Path,
) -> None:
    if not _macos_sandbox_available():
        pytest.skip("macOS sandbox-exec is unavailable in this host boundary")
    if not (TOOLCHAIN / "node_modules").is_dir():
        pytest.skip("the controller-pinned Jest installation is unavailable")

    token = hashlib.sha256((str(tmp_path) + "-real-toolchain").encode()).hexdigest()[:20]
    marker = Path("/private/tmp") / f"salesforce-real-toolchain-{token}.marker"
    assert not marker.exists()
    outputs = salesforce_candidate_outputs()
    outputs[LWC_TEST_PATH] += (
        f"\nrequire('fs').writeFileSync({str(marker)!r}, 'escaped');\n".encode()
    )

    with _runtime_case(tmp_path, outputs, install_node_modules=False) as case:
        shutil.copytree(
            TOOLCHAIN / "node_modules",
            case.project / "tooling/lwc-jest/node_modules",
            copy_function=os.link,
            symlinks=True,
        )

        report = _run(case)

        candidate = _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID)
        toolchain = _result(report, SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID)
        assert candidate.status is CheckStatus.FAILED
        assert candidate.diagnostic_ids == (
            JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID,
            "jest_forbidden_capability",
        )
        assert toolchain.status is CheckStatus.PASSED
        _assert_runtime_checks_follow_verified_sandbox(report)
        assert report.disposition is ValidationDisposition.RECOVERABLE_FAILURE
        assert not marker.exists()


def test_jest_29_success_without_failure_messages_is_accepted_by_bounded_parser(
    tmp_path: Path,
) -> None:
    candidate = _parser_candidate(tmp_path)
    payload = _jest_payload(candidate)
    suite = payload["testResults"]
    assert isinstance(suite, list)
    assert "failureMessages" not in suite[0]

    summary = _jest_summary(
        _jest_execution(json.dumps(payload, sort_keys=True)),
        candidate,
    )

    assert "Candidate-authored LWC Jest tests passed" in summary
    assert "suites=1 tests=3 evidence-role=supplemental" in summary
    assert "required-behaviors" not in summary
    assert all(
        f"candidate-authored behavior {index}" not in summary
        for index in range(1, SALESFORCE_MIN_CANDIDATE_LWC_JEST_TESTS + 1)
    )


@pytest.mark.parametrize(
    "titles",
    (
        (
            "renders the candidate's selected state",
            "handles the candidate's empty response",
            "keeps the candidate's loading state bounded",
            "uses an alternative test decomposition",
        ),
        tuple(f"model-authored scenario {index}" for index in range(1, 8)),
    ),
)
def test_candidate_authored_jest_accepts_arbitrary_bounded_titles_and_counts(
    tmp_path: Path,
    titles: tuple[str, ...],
) -> None:
    candidate = _parser_candidate(tmp_path)

    summary = _jest_summary(
        _jest_execution(
            json.dumps(_jest_payload(candidate, titles=titles), sort_keys=True),
        ),
        candidate,
    )

    assert f"tests={len(titles)} evidence-role=supplemental" in summary
    assert all(title not in summary for title in titles)


def test_candidate_authored_jest_requires_a_small_nontrivial_suite(tmp_path: Path) -> None:
    candidate = _parser_candidate(tmp_path)
    titles = tuple(
        f"candidate scenario {index}" for index in range(1, SALESFORCE_MIN_CANDIDATE_LWC_JEST_TESTS)
    )

    with pytest.raises(ValueError, match="count is invalid"):
        _jest_summary(
            _jest_execution(
                json.dumps(_jest_payload(candidate, titles=titles), sort_keys=True),
            ),
            candidate,
        )


@pytest.mark.parametrize("invalid_title", ("", "   ", "x" * 501))
def test_candidate_authored_jest_rejects_invalid_or_oversized_titles(
    tmp_path: Path,
    invalid_title: str,
) -> None:
    candidate = _parser_candidate(tmp_path)
    payload = _jest_payload(
        candidate,
        titles=(invalid_title, "candidate scenario two", "candidate scenario three"),
    )

    with pytest.raises(ValueError, match="assertion evidence"):
        _jest_summary(
            _jest_execution(json.dumps(payload, sort_keys=True)),
            candidate,
        )


def test_candidate_authored_jest_is_bound_to_the_intended_suite(tmp_path: Path) -> None:
    candidate = _parser_candidate(tmp_path)
    payload = _jest_payload(candidate)
    suites = payload["testResults"]
    assert isinstance(suites, list)
    suites[0]["name"] = str(tmp_path / "substitute.test.js")

    with pytest.raises(ValueError, match="another test file"):
        _jest_summary(
            _jest_execution(json.dumps(payload, sort_keys=True)),
            candidate,
        )


def test_controller_owned_jest_success_is_bound_to_immutable_suite(
    tmp_path: Path,
) -> None:
    candidate = _parser_candidate(tmp_path)
    controller_test = tmp_path / LWC_CONTROLLER_TEST_PATH
    controller_test.parent.mkdir(parents=True, exist_ok=True)
    controller_test.write_text("// immutable parser fixture\n", encoding="utf-8")
    execution = _jest_execution(
        json.dumps(_controller_jest_payload(controller_test), sort_keys=True),
        tool_id=SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
    )

    summary = _controller_jest_summary(execution, candidate, controller_test)

    assert "tests=10 independent-of-candidate-tests=true" in summary
    assert all(title not in summary for title in SALESFORCE_CONTROLLER_LWC_BEHAVIOR_TITLES)


def test_controller_owned_jest_failure_projects_only_code_owned_behavior_signal(
    tmp_path: Path,
) -> None:
    candidate = _parser_candidate(tmp_path)
    controller_test = tmp_path / LWC_CONTROLLER_TEST_PATH
    controller_test.parent.mkdir(parents=True, exist_ok=True)
    controller_test.write_text("// immutable parser fixture\n", encoding="utf-8")
    failed_title = "controller: exposes loading state while contacts are pending"
    execution = _jest_execution(
        json.dumps(
            _controller_jest_payload(controller_test, failed_title=failed_title),
            sort_keys=True,
        ),
        exit_code=1,
        tool_id=SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
    )

    summary, diagnostic_ids = _controller_jest_failure_evidence(
        execution,
        controller_test,
    )
    result = _result_from_execution(
        _controller_jest_check(),
        execution,
        candidate,
        lambda value, root: _controller_jest_summary(
            value,
            root,
            controller_test,
        ),
        controller_test_path=controller_test,
    )

    assert "failed-tests=1" in summary
    assert diagnostic_ids == (SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_BY_TITLE[failed_title],)
    assert result.status is CheckStatus.FAILED
    assert result.diagnostic_ids == diagnostic_ids


def test_controller_owned_jest_zero_test_failure_projects_execution_signal(
    tmp_path: Path,
) -> None:
    controller_test = tmp_path / LWC_CONTROLLER_TEST_PATH
    controller_test.parent.mkdir(parents=True, exist_ok=True)
    controller_test.write_text("// immutable parser fixture\n", encoding="utf-8")
    payload = {
        "success": False,
        "wasInterrupted": False,
        "numTotalTestSuites": 1,
        "numPassedTestSuites": 0,
        "numFailedTestSuites": 1,
        "numPendingTestSuites": 0,
        "numRuntimeErrorTestSuites": 1,
        "numTotalTests": 0,
        "numPassedTests": 0,
        "numFailedTests": 0,
        "numPendingTests": 0,
        "numTodoTests": 0,
        "testResults": [
            {
                "name": str(controller_test),
                "status": "failed",
                "assertionResults": [],
                "failureMessages": ["generated component could not be loaded"],
            }
        ],
    }
    execution = _jest_execution(
        json.dumps(payload, sort_keys=True),
        exit_code=1,
        tool_id=SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
    )

    summary, diagnostic_ids = _controller_jest_failure_evidence(execution, controller_test)

    assert "tests=0" in summary
    assert diagnostic_ids == (SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID,)


def test_controller_jest_failure_remains_authoritative_after_candidate_false_green(
    tmp_path: Path,
) -> None:
    candidate = _parser_candidate(tmp_path)
    candidate_result = _result_from_execution(
        _jest_check(),
        _jest_execution(
            json.dumps(
                _jest_payload(
                    candidate,
                    titles=(
                        "candidate smoke test one",
                        "candidate smoke test two",
                        "candidate smoke test three",
                    ),
                ),
                sort_keys=True,
            ),
        ),
        candidate,
        _jest_summary,
    )
    controller_test = tmp_path / LWC_CONTROLLER_TEST_PATH
    controller_test.parent.mkdir(parents=True, exist_ok=True)
    controller_test.write_text("// immutable parser fixture\n", encoding="utf-8")
    failed_title = SALESFORCE_CONTROLLER_LWC_BEHAVIOR_TITLES[0]
    controller_result = _result_from_execution(
        _controller_jest_check(),
        _jest_execution(
            json.dumps(
                _controller_jest_payload(controller_test, failed_title=failed_title),
                sort_keys=True,
            ),
            exit_code=1,
            tool_id=SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
        ),
        candidate,
        lambda value, root: _controller_jest_summary(value, root, controller_test),
        controller_test_path=controller_test,
    )

    assert candidate_result.status is CheckStatus.PASSED
    assert controller_result.status is CheckStatus.FAILED
    assert salesforce_runtime._disposition((candidate_result, controller_result)) is (
        ValidationDisposition.RECOVERABLE_FAILURE
    )


@pytest.mark.parametrize(
    ("command_id", "diagnostic_ids", "expected"),
    (
        (
            SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID,
            (GRAPH_DEPENDENCY_OMISSION_DIAGNOSTIC_ID,),
            ValidationDisposition.PLAN_INVALID,
        ),
        (
            SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID,
            (GRAPH_DEPENDENCY_INCORRECT_DIAGNOSTIC_ID,),
            ValidationDisposition.PLAN_INVALID,
        ),
        (
            SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID,
            (),
            ValidationDisposition.RECOVERABLE_FAILURE,
        ),
        (
            SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
            (SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID,),
            ValidationDisposition.RECOVERABLE_FAILURE,
        ),
    ),
)
def test_salesforce_disposition_reserves_plan_invalid_for_exact_graph_diagnostics(
    command_id: str,
    diagnostic_ids: tuple[str, ...],
    expected: ValidationDisposition,
) -> None:
    execution = _jest_execution("{}", exit_code=1, tool_id=command_id)
    failed = CheckResult(
        check_id=command_id,
        command_id=command_id,
        required=True,
        status=CheckStatus.FAILED,
        receipt=execution.receipt,
        summary="Controller-observed terminal validation failure.",
        diagnostic_ids=diagnostic_ids,
    )

    assert salesforce_runtime._disposition((failed,)) is expected


def test_controller_owned_jest_rejects_candidate_suite_path_substitution(
    tmp_path: Path,
) -> None:
    candidate = _parser_candidate(tmp_path)
    controller_test = tmp_path / LWC_CONTROLLER_TEST_PATH
    controller_test.parent.mkdir(parents=True, exist_ok=True)
    controller_test.write_text("// immutable parser fixture\n", encoding="utf-8")
    payload = _controller_jest_payload(controller_test)
    suites = payload["testResults"]
    assert isinstance(suites, list)
    suites[0]["name"] = str(candidate / LWC_TEST_PATH)

    with pytest.raises(ValueError, match="another test file"):
        _controller_jest_summary(
            _jest_execution(
                json.dumps(payload, sort_keys=True),
                tool_id=SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
            ),
            candidate,
            controller_test,
        )


@pytest.mark.parametrize(
    "stdout",
    (
        "not-json /private/secret planted-secret",
        json.dumps(
            {
                "check": "candidate-contract",
                "passed": False,
                "failure_type": "LocalCheckFailure",
                "failure_code": "../../private/planted-secret",
            }
        ),
    ),
)
def test_candidate_failure_summary_rejects_untrusted_diagnostics(stdout: str) -> None:
    summary = _candidate_failure_summary(_jest_execution(stdout, exit_code=1))

    assert "failure-code=salesforce_candidate_unclassified" in summary
    assert "planted-secret" not in summary
    assert "/private" not in summary


@pytest.mark.parametrize(
    ("failure_code", "diagnostic_id"),
    (
        ("salesforce_lwc_javascript_contract", "salesforce_lwc_javascript_contract"),
        (
            "salesforce_apex_controller_contract",
            APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,
        ),
        (
            "salesforce_apex_controller_contract",
            APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,
        ),
        ("salesforce_lwc_jest_contract", JEST_GLOBALS_IMPORT_ORDER_DIAGNOSTIC_ID),
        ("salesforce_lwc_jest_contract", JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID),
        ("salesforce_lwc_template_contract", LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID),
    ),
)
def test_candidate_failure_preserves_its_precise_static_diagnostic(
    failure_code: str,
    diagnostic_id: str,
) -> None:
    stdout = json.dumps(
        {
            "check": "candidate-contract",
            "passed": False,
            "failure_type": "LocalCheckFailure",
            "failure_code": failure_code,
            "diagnostic_ids": [diagnostic_id],
        }
    )

    summary, diagnostic_ids = _candidate_failure_evidence(_jest_execution(stdout, exit_code=1))

    assert diagnostic_ids == (diagnostic_id,)
    assert f"diagnostics={diagnostic_id}" in summary


def test_candidate_failure_preserves_aggregated_stage_and_security_diagnostics() -> None:
    expected = (
        "lwc_forbidden_runtime_capability",
        "salesforce_permission_set_contract",
    )
    stdout = json.dumps(
        {
            "check": "candidate-contract",
            "passed": False,
            "failure_type": "LocalCheckFailure",
            "failure_code": "salesforce_lwc_javascript_contract",
            "diagnostic_ids": list(expected),
        }
    )

    summary, diagnostic_ids = _candidate_failure_evidence(_jest_execution(stdout, exit_code=1))

    assert diagnostic_ids == expected
    assert f"diagnostics={','.join(expected)}" in summary


def test_candidate_contract_cannot_inject_runtime_jest_diagnostic() -> None:
    stdout = json.dumps(
        {
            "check": "candidate-contract",
            "passed": False,
            "failure_type": "LocalCheckFailure",
            "failure_code": "salesforce_lwc_jest_contract",
            "diagnostic_ids": [SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID],
        }
    )

    summary, diagnostic_ids = _candidate_failure_evidence(_jest_execution(stdout, exit_code=1))

    assert diagnostic_ids == ("salesforce_lwc_jest_contract",)
    assert "diagnostics=salesforce_lwc_jest_contract" in summary


@pytest.mark.parametrize(
    ("failure_code", "mismatched_diagnostic"),
    (
        ("salesforce_apex_controller_contract", "lwc_forbidden_runtime_capability"),
        ("salesforce_lwc_template_contract", "jest_forbidden_capability"),
    ),
)
def test_candidate_failure_rejects_mismatched_security_diagnostic(
    failure_code: str,
    mismatched_diagnostic: str,
) -> None:
    stdout = json.dumps(
        {
            "check": "candidate-contract",
            "passed": False,
            "failure_type": "LocalCheckFailure",
            "failure_code": failure_code,
            "diagnostic_ids": [mismatched_diagnostic],
        }
    )

    summary, diagnostic_ids = _candidate_failure_evidence(_jest_execution(stdout, exit_code=1))

    assert diagnostic_ids == (failure_code,)
    assert f"diagnostics={failure_code}" in summary


@pytest.mark.parametrize("evidence_kind", ("empty", "invalid-title"))
def test_zero_exit_incomplete_jest_json_is_unavailable_without_process_execution(
    tmp_path: Path,
    evidence_kind: str,
) -> None:
    candidate = _parser_candidate(tmp_path)
    payload = _jest_payload(candidate)
    if evidence_kind == "empty":
        stdout = "{}"
    else:
        suites = payload["testResults"]
        assert isinstance(suites, list)
        assertion_results = suites[0]["assertionResults"]
        assert isinstance(assertion_results, list)
        assertion_results[-1]["title"] = "   "
        stdout = json.dumps(payload, sort_keys=True)

    result = _result_from_execution(
        _jest_check(),
        _jest_execution(stdout),
        candidate,
        _jest_summary,
    )

    assert result.status is CheckStatus.UNAVAILABLE
    assert result.receipt is not None and result.receipt.exit_code == 0
    assert "complete bounded terminal evidence was unavailable" in result.summary


@pytest.mark.parametrize(
    ("count_field", "count_value"),
    (
        ("numFailedTests", 1),
        ("numPendingTests", 1),
        ("numTodoTests", 1),
    ),
)
def test_candidate_authored_jest_rejects_nonpassing_terminal_counts(
    tmp_path: Path,
    count_field: str,
    count_value: int,
) -> None:
    candidate = _parser_candidate(tmp_path)
    payload = _jest_payload(candidate)
    payload[count_field] = count_value

    with pytest.raises(ValueError, match="terminal counts"):
        _jest_summary(
            _jest_execution(json.dumps(payload, sort_keys=True)),
            candidate,
        )


def test_bounded_terminal_jest_failure_is_failed_not_environment_unavailable(
    tmp_path: Path,
) -> None:
    candidate = _parser_candidate(tmp_path)
    execution = _jest_execution(
        json.dumps(_jest_payload(candidate, success=False), sort_keys=True),
        exit_code=1,
    )

    summary = _jest_failure_summary(execution, candidate)
    result = _result_from_execution(
        _jest_check(),
        execution,
        candidate,
        _jest_summary,
    )

    assert "failed terminally" in summary
    assert "candidate-authored behavior 3" in summary
    assert result.status is CheckStatus.FAILED
    assert result.receipt is execution.receipt
    assert result.diagnostic_ids == (SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID,)


def test_candidate_jest_failure_preserves_only_bounded_failed_test_titles(
    tmp_path: Path,
) -> None:
    candidate = _parser_candidate(tmp_path)
    titles = (
        "loads accounts",
        "shows contacts after a successful load",
        "shows empty state",
        "shows safe errors",
        "clears selection",
        "keeps A stale across A to B to A",
        "keeps only the newest completion for overlapping loads",
    )
    payload = _jest_payload(candidate, success=False, titles=titles)
    suites = payload["testResults"]
    assert isinstance(suites, list)
    assertions = suites[0]["assertionResults"]
    assert isinstance(assertions, list)
    assertions[-2]["status"] = "failed"
    payload["numPassedTests"] = len(titles) - 2
    payload["numFailedTests"] = 2

    summary = _jest_failure_summary(
        _jest_execution(json.dumps(payload, sort_keys=True), exit_code=1),
        candidate,
    )

    assert "keeps A stale across A to B to A" in summary
    assert "keeps only the newest completion for overlapping loads" in summary
    assert "controlled candidate failure" not in summary
    assert str(candidate) not in summary


def test_candidate_jest_failure_redacts_secret_shaped_test_titles(tmp_path: Path) -> None:
    candidate = _parser_candidate(tmp_path)
    payload = _jest_payload(
        candidate,
        success=False,
        titles=("safe one", "safe two", "fails with sk-1234567890abcdef"),
    )

    summary = _jest_failure_summary(
        _jest_execution(json.dumps(payload, sort_keys=True), exit_code=1),
        candidate,
    )

    assert "sk-1234567890abcdef" not in summary
    assert "REDACTED" in summary


def test_candidate_jest_failure_with_control_character_title_is_unavailable(
    tmp_path: Path,
) -> None:
    candidate = _parser_candidate(tmp_path)
    payload = _jest_payload(
        candidate,
        success=False,
        titles=("safe one", "safe two", "unsafe\nembedded title"),
    )
    execution = _jest_execution(json.dumps(payload, sort_keys=True), exit_code=1)

    result = _result_from_execution(
        _jest_check(),
        execution,
        candidate,
        _jest_summary,
    )

    assert result.status is CheckStatus.UNAVAILABLE
    assert not result.diagnostic_ids


def test_nonzero_without_terminal_jest_json_is_environment_unavailable(
    tmp_path: Path,
) -> None:
    candidate = _parser_candidate(tmp_path)
    result = _result_from_execution(
        _jest_check(),
        _jest_execution("required Node module is unavailable", exit_code=1),
        candidate,
        _jest_summary,
    )

    assert result.status is CheckStatus.UNAVAILABLE
    assert result.receipt is not None and result.receipt.exit_code == 1
    assert "did not produce complete terminal JSON" in result.summary


def test_caller_selected_python_cannot_forge_prerequisites_or_spawn(
    tmp_path: Path,
) -> None:
    token = hashlib.sha256((str(tmp_path) + "-forged-python").encode()).hexdigest()[:20]
    marker = Path("/private/tmp") / f"salesforce-forged-python-{token}.marker"
    assert not marker.exists()

    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        forged_python = case.project / "forged-python"
        forged_payloads = {
            "candidate-contract": {
                "check": "salesforce-candidate-contract",
                "passed": True,
                "agent_output_files": 13,
                "required_files": 13,
                "behavior_states": 1,
                "apex_tests_executed": False,
                "lwc_jest_executed": False,
                "org_validation_performed": False,
                "deployment_claim": False,
            },
            "dependency-closure": {
                "check": "salesforce-target-dependency-closure",
                "passed": True,
                "unresolved": False,
                "nodes": 1,
                "edges": 1,
                "warnings": 0,
            },
            "toolchain-contract": {
                "check": "lwc-jest-toolchain-contract",
                "passed": True,
                "version": "7.9.0",
                "candidate_content_inspected": False,
                "network_accessed": False,
                "install_performed": False,
                "lwc_jest_executed": False,
                "locked_packages": 561,
                "manifest_sha256": LWC_JEST_TOOLCHAIN_DIGESTS["package.json"],
                "lock_sha256": LWC_JEST_TOOLCHAIN_DIGESTS["package-lock.json"],
                "config_sha256": LWC_JEST_TOOLCHAIN_DIGESTS["jest.config.js"],
                "setup_sha256": LWC_JEST_TOOLCHAIN_DIGESTS["jest.setup.js"],
                "controller_test_sha256": LWC_JEST_TOOLCHAIN_DIGESTS[
                    "controller-tests/accountContactExplorer.controller.test.js"
                ],
            },
        }
        forged_python.write_text(
            "#!/usr/bin/python3\n"
            "import json\n"
            "import sys\n"
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('escaped', encoding='utf-8')\n"
            f"payloads = {forged_payloads!r}\n"
            "command = ' '.join(sys.argv)\n"
            "payload = next(value for key, value in payloads.items() if key in command)\n"
            "print(json.dumps(payload, sort_keys=True))\n",
            encoding="utf-8",
        )
        forged_python.chmod(0o700)

        validator_parameters = inspect.signature(SalesforceLocalValidator).parameters
        builder_parameters = inspect.signature(build_salesforce_local_validator).parameters
        assert "python_executable" not in validator_parameters
        assert "python_executable" not in builder_parameters
        with pytest.raises(TypeError, match="python_executable"):
            SalesforceLocalValidator(
                case.session,
                REGISTRY,
                python_executable=forged_python,  # type: ignore[call-arg]
            )
        with pytest.raises(TypeError, match="python_executable"):
            build_salesforce_local_validator(
                case.session,
                REGISTRY,
                python_executable=forged_python,  # type: ignore[call-arg]
            )

        report = _run(case)
        assert (
            _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID).status is CheckStatus.PASSED
        )
        _assert_jest_not_spawned(report)
        assert not marker.exists()


def test_controller_python_runtime_anchor_is_reverified_before_spawn(
    tmp_path: Path,
) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        validator = _validator(case)
        anchor = case.session.runtime_anchors_dir / "salesforce-controller-python-v1.json"
        assert anchor.is_file()
        anchor.write_text('{"tampered":true}\n', encoding="utf-8")

        with pytest.raises(PolicyViolation, match="runtime evidence anchor is malformed"):
            validator(
                case.request,
                case.manifest,
                case.change_set,
                case.workspace,
                1,
            )

        _assert_no_probe_record(case)


def test_arbitrary_self_attested_launcher_is_not_a_runtime_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:20]
    marker = Path("/private/tmp") / f"salesforce-fake-launcher-{token}.marker"
    assert not marker.exists()

    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        fake_launcher = case.project / "fake-isolation-launcher"
        fake_launcher.write_text(
            f"#!/bin/sh\nprintf escaped > {marker}\nprintf '{{\"passed\":true}}'\n",
            encoding="utf-8",
        )
        fake_launcher.chmod(0o700)
        claims = {
            "launcher_path": str(fake_launcher),
            "launcher_sha256": ("sha256:" + hashlib.sha256(fake_launcher.read_bytes()).hexdigest()),
            "all_probe_claims": True,
        }
        assert claims["launcher_sha256"].startswith("sha256:")
        public_parameters = inspect.signature(SalesforceLocalValidator).parameters
        builder_parameters = inspect.signature(build_salesforce_local_validator).parameters
        assert "jest_runtime_attestation" not in public_parameters
        assert "isolation_launcher" not in public_parameters
        assert "node_executable" not in public_parameters
        assert "jest_runtime_attestation" not in builder_parameters

        with pytest.raises(TypeError, match="jest_runtime_attestation"):
            SalesforceLocalValidator(
                case.session,
                REGISTRY,
                jest_runtime_attestation=claims,  # type: ignore[call-arg]
            )

        validator = _validator(case)
        monkeypatch.setattr(validator, "_sandbox_backend", fake_launcher)
        report = validator(
            case.request,
            case.manifest,
            case.change_set,
            case.workspace,
            1,
        )
        probe = _result(report, SALESFORCE_SANDBOX_PROBE_COMMAND_ID)
        assert probe.status is CheckStatus.UNAVAILABLE and probe.receipt is None
        _assert_jest_not_spawned(report)
        assert not marker.exists()


def test_json_emitting_replacement_jest_entry_cannot_return_false_green(
    tmp_path: Path,
) -> None:
    token = hashlib.sha256((str(tmp_path) + "-stub").encode()).hexdigest()[:20]
    marker = Path("/private/tmp") / f"salesforce-jest-stub-{token}.marker"
    assert not marker.exists()

    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        payload = json.dumps(_jest_payload(case.workspace.root), sort_keys=True)
        jest_entry = case.project / "tooling/lwc-jest/node_modules/jest/bin/jest.js"
        jest_entry.write_text(
            "require('fs').writeFileSync("
            f"{json.dumps(str(marker))}, 'escaped');\n"
            f"console.log({json.dumps(payload)});\n",
            encoding="utf-8",
        )

        report = _run(case)

        toolchain = _result(report, SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID)
        assert toolchain.status is CheckStatus.PASSED
        _assert_probe_matches_host(report)
        _assert_jest_not_spawned(report)
        assert report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        assert not marker.exists()


@pytest.mark.parametrize("authority", ("read", "write", "child", "network"))
def test_hostile_candidate_has_no_external_effect_when_sandbox_attempts_it(
    tmp_path: Path,
    authority: str,
) -> None:
    project = tmp_path / "project"
    secret = project / "private-reference/answer.txt"
    outside_marker = tmp_path / f"{authority}-outside-effect.marker"
    scratch_marker = project / ".runs/run-salesforce-runtime/scratch/candidate-started.marker"
    read_result = project / ".runs/run-salesforce-runtime/scratch/read-result.marker"
    operations = {
        "read": (
            f"const value = fs.readFileSync({str(secret)!r}, 'utf8'); "
            f"fs.writeFileSync({str(read_result)!r}, value);"
        ),
        "write": f"fs.writeFileSync({str(outside_marker)!r}, 'escaped');",
        "child": (
            f"require('child_process').execFileSync('/usr/bin/touch', [{str(outside_marker)!r}]);"
        ),
        "network": "require('net').connect({host: '127.0.0.1', port: 9});",
    }
    outputs = salesforce_candidate_outputs()
    outputs[LWC_TEST_PATH] = (
        "const fs = require('fs');\n"
        f"fs.writeFileSync({str(scratch_marker)!r}, 'started');\n"
        f"{operations[authority]}\n"
        "test('hostile candidate', () => expect(true).toBe(true));\n"
    ).encode()

    with _runtime_case_with_real_jest_when_available(tmp_path, outputs) as case:
        secret.parent.mkdir()
        secret.write_text("oracle-secret", encoding="utf-8")

        report = _run(case)

        candidate = _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID)
        assert candidate.status is CheckStatus.FAILED
        assert candidate.receipt is not None and candidate.receipt.exit_code != 0
        assert "failure-code=salesforce_lwc_jest_contract" in candidate.summary
        _assert_runtime_checks_follow_verified_sandbox(report)
        assert report.disposition is ValidationDisposition.RECOVERABLE_FAILURE
        if _macos_sandbox_available():
            assert scratch_marker.read_text(encoding="utf-8") == "started"
        else:
            assert not scratch_marker.exists()
        assert not read_result.exists()
        assert not outside_marker.exists()
        assert secret.read_text(encoding="utf-8") == "oracle-secret"


@pytest.mark.parametrize("missing", ("node_modules", "toolchain"))
def test_missing_jest_runtime_is_required_unavailable_not_a_pass(
    tmp_path: Path,
    missing: str,
) -> None:
    with _runtime_case(
        tmp_path,
        salesforce_candidate_outputs(),
        install_node_modules=missing != "node_modules",
    ) as case:
        if missing == "toolchain":
            (case.project / "tooling/lwc-jest").rename(case.project / "toolchain-unavailable")

        report = _run(case)

        _assert_jest_not_spawned(report)
        assert report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        if missing == "toolchain":
            toolchain = _result(report, SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID)
            probe = _result(report, SALESFORCE_SANDBOX_PROBE_COMMAND_ID)
            assert toolchain.status is CheckStatus.UNAVAILABLE and toolchain.receipt is None
            assert probe.status is CheckStatus.UNAVAILABLE and probe.receipt is None


def test_failed_candidate_contract_still_collects_safe_runtime_evidence(tmp_path: Path) -> None:
    sandbox_available = _macos_sandbox_available()
    if sandbox_available and not (TOOLCHAIN / "node_modules").is_dir():
        pytest.skip("the controller-pinned Jest installation is unavailable")

    outputs = salesforce_candidate_outputs()
    controller_path = "force-app/main/default/classes/AccountContactExplorerController.cls"
    outputs[controller_path] = outputs[controller_path].replace(
        b"        try {\n",
        b"        try {\n            insert new Account(Name = 'unsafe');\n",
        1,
    )

    with _runtime_case(
        tmp_path,
        outputs,
        install_node_modules=not sandbox_available,
    ) as case:
        if sandbox_available:
            shutil.copytree(
                TOOLCHAIN / "node_modules",
                case.project / "tooling/lwc-jest/node_modules",
                copy_function=os.link,
                symlinks=True,
            )

        report = _run(case)

        candidate = _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID)
        assert candidate.status is CheckStatus.FAILED
        assert candidate.receipt is not None and candidate.receipt.exit_code != 0
        assert "failure-code=salesforce_apex_controller_contract" in candidate.summary
        _assert_runtime_checks_follow_verified_sandbox(report)
        if _macos_sandbox_available():
            assert _result(report, SALESFORCE_LWC_JEST_COMMAND_ID).status is CheckStatus.PASSED
            assert (
                _result(report, SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID).status
                is CheckStatus.PASSED
            )
        assert report.disposition is ValidationDisposition.RECOVERABLE_FAILURE


def test_forbidden_lwc_capability_does_not_suppress_independent_runtime_checks(
    tmp_path: Path,
) -> None:
    outputs = salesforce_candidate_outputs()
    javascript_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[javascript_path].decode("utf-8")
    reset_anchor = "        this.isLoading = true;\n        this.hasLoaded = false;\n"
    assert reset_anchor in source
    outputs[javascript_path] = (
        source.replace(reset_anchor, "        this.isLoading = true;\n", 1)
        + "\neval('forbidden');\n"
    ).encode("utf-8")

    with _runtime_case_with_real_jest_when_available(tmp_path, outputs) as case:
        report = _run(case)

        candidate = _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID)
        assert candidate.status is CheckStatus.FAILED
        assert candidate.diagnostic_ids == ("lwc_forbidden_runtime_capability",)
        _assert_runtime_checks_follow_verified_sandbox(report)
        assert report.disposition is ValidationDisposition.RECOVERABLE_FAILURE


def test_component_global_tampering_is_rejected_without_suppressing_safe_checks(
    tmp_path: Path,
) -> None:
    outputs = salesforce_candidate_outputs()
    javascript_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[javascript_path].decode("utf-8")
    outputs[javascript_path] = (
        "const trustedIt = globalThis.it;\n"
        "globalThis.it = (title, body, timeout) => trustedIt(title, () => {}, timeout);\n" + source
    ).encode("utf-8")

    with _runtime_case_with_real_jest_when_available(tmp_path, outputs) as case:
        report = _run(case)

        candidate = _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID)
        assert candidate.status is CheckStatus.FAILED
        assert candidate.diagnostic_ids == ("lwc_forbidden_runtime_capability",)
        _assert_runtime_checks_follow_verified_sandbox(report)
        assert report.disposition is ValidationDisposition.RECOVERABLE_FAILURE


def test_reflective_function_access_is_rejected_without_suppressing_safe_checks(
    tmp_path: Path,
) -> None:
    outputs = salesforce_candidate_outputs()
    javascript_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[javascript_path].decode("utf-8")
    attack = (
        "const callableBase = Object.getPrototypeOf(() => {});\n"
        "const hiddenDescriptor = "
        "Object.getOwnPropertyDescriptor(callableBase, 'constructor');\n"
        "const evaluator = hiddenDescriptor.value;\n"
        "const runtimeObject = evaluator('return this')();\n"
    )
    outputs[javascript_path] = (attack + source).encode("utf-8")

    with _runtime_case_with_real_jest_when_available(tmp_path, outputs) as case:
        report = _run(case)

        candidate = _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID)
        assert candidate.status is CheckStatus.FAILED
        assert candidate.diagnostic_ids == ("lwc_forbidden_runtime_capability",)
        _assert_runtime_checks_follow_verified_sandbox(report)
        assert report.disposition is ValidationDisposition.RECOVERABLE_FAILURE


def test_computed_constructor_extraction_is_rejected_without_suppressing_safe_checks(
    tmp_path: Path,
) -> None:
    outputs = salesforce_candidate_outputs()
    javascript_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[javascript_path].decode("utf-8")
    attack = (
        "const { ['con' + 'structor']: evaluator } = Object.freeze(() => {});\n"
        "const runtimeObject = evaluator('return this')();\n"
    )
    outputs[javascript_path] = (attack + source).encode("utf-8")

    with _runtime_case_with_real_jest_when_available(tmp_path, outputs) as case:
        report = _run(case)

        candidate = _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID)
        assert candidate.status is CheckStatus.FAILED
        assert candidate.diagnostic_ids == ("lwc_forbidden_runtime_capability",)
        _assert_runtime_checks_follow_verified_sandbox(report)
        assert report.disposition is ValidationDisposition.RECOVERABLE_FAILURE


def test_component_module_re_export_is_rejected_without_suppressing_safe_checks(
    tmp_path: Path,
) -> None:
    outputs = salesforce_candidate_outputs()
    javascript_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[javascript_path].decode("utf-8")
    outputs[javascript_path] = (
        "export { readFileSync as runtimeRead } from 'node:fs';\n" + source
    ).encode("utf-8")

    with _runtime_case_with_real_jest_when_available(tmp_path, outputs) as case:
        report = _run(case)

        candidate = _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID)
        assert candidate.status is CheckStatus.FAILED
        assert candidate.diagnostic_ids == ("lwc_forbidden_runtime_capability",)
        _assert_runtime_checks_follow_verified_sandbox(report)
        assert report.disposition is ValidationDisposition.RECOVERABLE_FAILURE


def test_static_preflight_does_not_prescribe_candidate_mock_reset_helper(
    tmp_path: Path,
) -> None:
    outputs = salesforce_candidate_outputs()
    outputs[LWC_TEST_PATH] = outputs[LWC_TEST_PATH].replace(
        b"        getContacts.mockReset();\n",
        b"",
        1,
    )

    with _runtime_case(tmp_path, outputs) as case:
        report = _run(case)

        candidate = _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID)
        assert candidate.status is CheckStatus.PASSED
        assert candidate.diagnostic_ids == ()


def test_static_preflight_accepts_private_state_and_candidate_assertion_variants(
    tmp_path: Path,
) -> None:
    outputs = salesforce_candidate_outputs()
    javascript_path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    javascript = outputs[javascript_path].decode("utf-8")
    direct_field = "    accountOptions = [BLANK_ACCOUNT_OPTION];\n"
    assert direct_field in javascript
    outputs[javascript_path] = javascript.replace(
        direct_field,
        """    get accountOptions() {
        return this._accountOptions || [];
    }

    set accountOptions(options) {
        this._accountOptions = options;
    }
""",
        1,
    ).encode("utf-8")

    jest_source = outputs[LWC_TEST_PATH].decode("utf-8")
    spinner_assertion = "        expect(spinner.alternativeText).toBe('Loading contacts');\n"
    assert spinner_assertion in jest_source
    jest_source = jest_source.replace(
        spinner_assertion,
        "        expect(spinner.getAttribute('alternative-text')).toBe('Loading contacts');\n",
        1,
    )
    outputs[LWC_TEST_PATH] = jest_source.encode("utf-8")

    with _runtime_case(tmp_path, outputs) as case:
        report = _run(case)

        candidate = _result(report, SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID)
        assert candidate.status is CheckStatus.PASSED
        assert candidate.diagnostic_ids == ()


def test_toolchain_digest_drift_fails_and_jest_never_spawns(tmp_path: Path) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        (case.project / "tooling/lwc-jest/jest.config.js").write_text(
            "module.exports = {};\n",
            encoding="utf-8",
        )

        report = _run(case)

        toolchain = _result(report, SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID)
        probe = _result(report, SALESFORCE_SANDBOX_PROBE_COMMAND_ID)
        assert toolchain.status is CheckStatus.FAILED
        assert toolchain.receipt is not None and toolchain.receipt.exit_code == 1
        assert probe.status is CheckStatus.UNAVAILABLE and probe.receipt is None
        _assert_jest_not_spawned(report)
        assert report.disposition is ValidationDisposition.RECOVERABLE_FAILURE
        _assert_no_probe_record(case)


def test_manifest_command_drift_is_rejected_before_any_process_spawns(tmp_path: Path) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        first = case.manifest.validation_plan[0].model_copy(
            update={"command_id": "model-generated-command"}
        )
        drifted = case.manifest.model_copy(
            update={"validation_plan": (first, *case.manifest.validation_plan[1:])}
        )

        with pytest.raises(PolicyViolation, match="outside the scope policy"):
            _validator(case)(
                case.request,
                drifted,
                case.change_set,
                case.workspace,
                1,
            )

        _assert_no_probe_record(case)


def test_foreign_workspace_is_rejected_before_any_process_spawns(tmp_path: Path) -> None:
    outputs = salesforce_candidate_outputs()
    with _runtime_case(tmp_path, outputs) as case:
        foreign_parent = case.project / "foreign-workspaces"
        foreign_parent.mkdir()
        foreign = IsolatedWorkspace(
            case.session.source_root,
            SALESFORCE_AGENT_OUTPUT_PATHS,
            temp_parent=foreign_parent,
            expected_revision=case.request.base_revision,
        )
        try:
            for path, content in outputs.items():
                foreign.write_bytes(path, content)

            with pytest.raises(PolicyViolation, match="not owned by this run session"):
                _validator(case)(
                    case.request,
                    case.manifest,
                    case.change_set,
                    foreign,
                    1,
                )
        finally:
            foreign.cleanup()

        _assert_no_probe_record(case)


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"request_repository": "another-source"}, "repository does not match"),
        (
            {"entry_path": "force-app/main/default/aura/LegacyExplorer/LegacyExplorer.cmp"},
            "fixed Visualforce entry",
        ),
        ({"target_runtime": "Aura Components"}, "Lightning Web Components with Apex"),
        ({"source_version": "Salesforce API 66.0"}, "supported API 67.0"),
        ({"target_version": "Salesforce API 68.0"}, "supported API 67.0"),
        (
            {"transformation_inputs": SALESFORCE_TRANSFORMATION_INPUT_PATHS[:-1]},
            "omit required source inputs",
        ),
    ),
)
def test_request_and_transformation_semantics_are_bound_before_spawn(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs(), **overrides) as case:
        with pytest.raises(PolicyViolation, match=message):
            _run(case)

        _assert_no_probe_record(case)


def test_raw_run_context_tamper_is_rejected_before_spawn(tmp_path: Path) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        validator = _validator(case)
        (case.session.evidence_dir / "run-context.json").write_text(
            '{"tampered":true}\n',
            encoding="utf-8",
        )

        with pytest.raises(PolicyViolation, match="run context"):
            validator(
                case.request,
                case.manifest,
                case.change_set,
                case.workspace,
                1,
            )

        _assert_no_probe_record(case)


def test_every_lifecycle_index_is_reverified_before_spawn(tmp_path: Path) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        case.session.store.write_json("planning.json", {"status": "complete"})
        case.session.write_index("planning", ("run-context.json", "planning.json"))
        validator = _validator(case)
        (case.session.evidence_dir / "planning.json").write_text(
            '{"status":"tampered"}\n',
            encoding="utf-8",
        )

        with pytest.raises(PolicyViolation, match="lifecycle artifact digest mismatch"):
            validator(
                case.request,
                case.manifest,
                case.change_set,
                case.workspace,
                1,
            )

        _assert_no_probe_record(case)


def test_loaded_agent_definition_drift_is_rejected_before_spawn(tmp_path: Path) -> None:
    definitions = {definition.role: definition for definition in REGISTRY.definitions}
    definitions[AgentRole.ENGINEER] = definitions[AgentRole.ENGINEER].model_copy(
        update={"definition_digest": "sha256:" + "0" * 64}
    )
    drifted_registry = AgentRegistry(definitions)

    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        with pytest.raises(PolicyViolation, match="loaded agent definitions"):
            _validator(case, registry=drifted_registry)

        _assert_no_probe_record(case)


def test_model_owned_package_or_lock_output_is_rejected_before_spawn(tmp_path: Path) -> None:
    with _runtime_case(tmp_path, salesforce_candidate_outputs()) as case:
        transformation = case.manifest.transformations[0].model_copy(
            update={"output_paths": (*SALESFORCE_AGENT_OUTPUT_PATHS, "package-lock.json")}
        )
        drifted = case.manifest.model_copy(
            update={
                "approved_paths": (*SALESFORCE_AGENT_OUTPUT_PATHS, "package-lock.json"),
                "transformations": (transformation,),
            }
        )

        with pytest.raises(PolicyViolation, match="max_changed_files|outside the scope policy"):
            _validator(case)(
                case.request,
                drifted,
                case.change_set,
                case.workspace,
                1,
            )

        _assert_no_probe_record(case)
