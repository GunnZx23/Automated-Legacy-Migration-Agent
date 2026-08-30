from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any

import pytest
from salesforce_candidate_factory import (
    case_management_candidate_outputs,
    salesforce_candidate_outputs,
)

from legacy_migration_agent.core.workspace import IsolatedWorkspace
from legacy_migration_agent.platforms.local_checks import (
    APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,
    APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,
    CASE_AGENT_OUTPUT_PATHS,
    CASE_CONTROLLER_PATH,
    CASE_CONTROLLER_TEST_PATH,
    CASE_IMPLEMENTATION_CONTRACT,
    CASE_LWC_CONTROLLER_TEST_PATH,
    CASE_LWC_HTML_PATH,
    CASE_LWC_JAVASCRIPT_PATH,
    CASE_MANAGEMENT_CONSOLE_UNIT_ID,
    CASE_PERMISSION_SET_PATH,
    CONTROLLER_JEST_ACCOUNT_CHANGE_RESET_DIAGNOSTIC_ID,
    CONTROLLER_JEST_ACCOUNT_ERROR_RESET_DIAGNOSTIC_ID,
    CONTROLLER_JEST_ACCOUNT_ERROR_STALE_RESPONSE_DIAGNOSTIC_ID,
    CONTROLLER_JEST_INITIAL_GUIDANCE_DIAGNOSTIC_ID,
    CONTROLLER_JEST_STATUS_CHANGE_RESET_DIAGNOSTIC_ID,
    CONTROLLER_JEST_STATUS_CHANGE_STALE_RESPONSE_DIAGNOSTIC_ID,
    CONTROLLER_PATH,
    CONTROLLER_TEST_PATH,
    JEST_GLOBALS_IMPORT_ORDER_DIAGNOSTIC_ID,
    JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID,
    LWC_JAVASCRIPT_PATH,
    LWC_JEST_TOOLCHAIN_DIGESTS,
    LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID,
    LWC_TEST_PATH,
    MANIFEST_PATH,
    PERMISSION_SET_PATH,
    SALESFORCE_AGENT_OUTPUT_PATHS,
    SALESFORCE_CANDIDATE_DIAGNOSTIC_IDS,
    SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID,
    SALESFORCE_CANDIDATE_STATIC_DIAGNOSTIC_IDS,
    SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS,
    SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID,
    SALESFORCE_IMPLEMENTATION_CONTRACT,
    LocalCheckFailure,
    check_dependency_closure,
    check_lwc_jest_toolchain,
    check_salesforce_candidate,
    check_workspace_revision,
    main,
    resolve_salesforce_controller_jest_spec,
    tree_fingerprint,
)
from legacy_migration_agent.platforms.salesforce_result_parsing import (
    _controller_jest_failure_evidence,
)

REPOSITORY = Path(__file__).parents[1]
FIXTURE = REPOSITORY / "fixtures" / "salesforce" / "account-contact-explorer"
CASE_FIXTURE = REPOSITORY / "fixtures" / "salesforce" / "case-management-console"
TOOLCHAIN = REPOSITORY / "tooling" / "lwc-jest"
CANDIDATE_BUILDER = Path(__file__).with_name("salesforce_candidate_factory.py")

LWC_TEMPLATE_PATH = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.html"
LWC_STYLES_PATH = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.css"


def candidate_from_memory(outputs: dict[str, bytes]) -> IsolatedWorkspace:
    workspace = IsolatedWorkspace(FIXTURE / "input", SALESFORCE_AGENT_OUTPUT_PATHS)
    for path, content in outputs.items():
        workspace.write_bytes(path, content)
    return workspace


def rejected_candidate(outputs: dict[str, bytes]) -> LocalCheckFailure:
    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)
    return caught.value


def outputs_with_controller_statement(statement: str) -> dict[str, bytes]:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode("utf-8")
    insertion_point = "        try {\n            return [\n                SELECT Id, FirstName"
    assert insertion_point in source
    outputs[CONTROLLER_PATH] = source.replace(
        insertion_point,
        f"        {statement}\n\n{insertion_point}",
        1,
    ).encode("utf-8")
    return outputs


def structurally_distinct_safe_outputs() -> dict[str, bytes]:
    outputs = salesforce_candidate_outputs()
    outputs[CONTROLLER_PATH] = b"""public with sharing class AccountContactExplorerController {
    @AuraEnabled (cacheable = true)
    public static List<Account> getAccounts() {
        try {
            List<Account> visibleAccounts = [
                SELECT Name, Id
                FROM Account
                WITH USER_MODE
                ORDER BY Name
                LIMIT 25
            ];
            return visibleAccounts;
        } catch (QueryException queryError) {
            throw new AuraHandledException('Account choices are unavailable.');
        }
    }

    @AuraEnabled(cacheable=false)
    public static List<Contact> getContacts(Id accountId) {
        if (accountId == null) {
            return new List<Contact>();
        }
        try {
            List<Contact> visibleContacts = [
                SELECT LastName, FirstName, Phone, Email, Id
                FROM Contact
                WHERE AccountId = :accountId
                WITH USER_MODE
                ORDER BY LastName, FirstName
                LIMIT 80
            ];
            return visibleContacts;
        } catch (QueryException queryError) {
            throw new AuraHandledException('Contact results are unavailable.');
        }
    }
}
"""
    outputs[CONTROLLER_TEST_PATH] = b"""@IsTest
private class AccountContactExplorerControllerTest {
    @TestSetup
    static void arrangeSyntheticRecords() {
        List<Account> tenants = new List<Account>{
            new Account(Name = 'Different Empty Tenant'),
            new Account(Name = 'Different Populated Tenant')
        };
        insert tenants;
        insert new Contact(
            AccountId = tenants[1].Id,
            FirstName = 'Model',
            LastName = 'Selected'
        );
    }

    @IsTest
    static void modelSelectedThisScenarioName() {
        List<Account> tenants = [SELECT Id, Name FROM Account ORDER BY Name];

        Test.startTest();
        List<Account> visibleAccounts =
            AccountContactExplorerController.getAccounts();
        List<Contact> emptyContacts =
            AccountContactExplorerController.getContacts(tenants[0].Id);
        List<Contact> populatedContacts =
            AccountContactExplorerController.getContacts(tenants[1].Id);
        Test.stopTest();

        Assert.areEqual(2, visibleAccounts.size());
        Assert.areEqual(0, emptyContacts.size());
        Assert.areEqual(1, populatedContacts.size());
    }

    @IsTest
    static void modelSelectedNullScenarioName() {
        List<Contact> noContacts =
            AccountContactExplorerController.getContacts(null);
        Assert.areEqual(0, noContacts.size());
    }
}
"""
    outputs[
        LWC_JAVASCRIPT_PATH
    ] = b"""import loadContacts from "@salesforce/apex/AccountContactExplorerController.getContacts";
import { wire as observe, LightningElement } from "lwc";
import loadAccounts from "@salesforce/apex/AccountContactExplorerController.getAccounts";

const LABELS = { primary: "Name" };

export default class ModelChosenExplorer extends LightningElement {
    get firstColumnLabel() {
        return `${LABELS["primary"]}`;
    }

    availableOperations() {
        return [observe, loadAccounts, loadContacts];
    }
}
"""
    outputs[LWC_TEST_PATH] = b"""import { describe, expect, it, jest } from '@jest/globals';
import { createElement } from 'lwc';
import ModelChosenExplorer from 'c/accountContactExplorer';
import loadAccounts from '@salesforce/apex/AccountContactExplorerController.getAccounts';
import loadContacts from '@salesforce/apex/AccountContactExplorerController.getContacts';

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

describe('candidate-authored outcomes', () => {
    it('uses a completely different safe title and structure', () => {
        loadAccounts.emit([]);
        loadContacts.mockResolvedValue([]);
        const element = createElement('c-account-contact-explorer', {
            is: ModelChosenExplorer
        });
        document.body.appendChild(element);
        expect(element).not.toBeNull();
    });
});
"""
    outputs[LWC_TEMPLATE_PATH] = b"""<template>
    <section aria-label="Account contacts">
        <slot data-role="account-selector"></slot>
        <button data-role="load-contacts">Load</button>
        <div data-role="contact-results"></div>
        <div data-state="loading"></div>
        <div data-state="empty"></div>
        <div data-state="warning"></div>
        <div data-state="error"></div>
    </section>
</template>
"""
    outputs[LWC_STYLES_PATH] = b"""[data-state='ready'] {
    display: block;
}
"""
    return outputs


def outputs_with_bounded_semantic_bindings() -> dict[str, bytes]:
    outputs = structurally_distinct_safe_outputs()
    template = outputs[LWC_TEMPLATE_PATH].decode("utf-8")
    replacements = {
        'data-role="account-selector"': "data-role={selectorHook}",
        'data-role="load-contacts"': "data-role={loadHook}",
        'data-role="contact-results"': "data-role={resultsHook}",
        'data-state="loading"': "data-state={busyHook}",
        'data-state="empty"': "data-state={emptyHook}",
    }
    for literal, binding in replacements.items():
        assert literal in template
        template = template.replace(literal, binding, 1)
    outputs[LWC_TEMPLATE_PATH] = template.encode("utf-8")

    javascript = outputs[LWC_JAVASCRIPT_PATH].decode("utf-8")
    body, closing_brace = javascript.rsplit("}", 1)
    outputs[LWC_JAVASCRIPT_PATH] = (
        body
        + """
    get selectorHook() { return 'account-selector'; }
    get loadHook() { return 'load-contacts'; }
    get resultsHook() { return 'contact-results'; }
    get busyHook() { return 'loading'; }
    get emptyHook() { return 'empty'; }
"""
        + closing_brace
    ).encode("utf-8")
    return outputs


def test_implementation_contract_assigns_candidate_owned_behavior_to_runtime_tests() -> None:
    contract = "\n".join(SALESFORCE_IMPLEMENTATION_CONTRACT)

    assert "Internal constants, helpers, control flow" in contract
    assert "Test names, helpers, setup, counts" in contract
    assert "Test titles, helpers, assertions, mock implementation" in contract
    assert "validated by the pinned Jest runner" in contract
    assert "at least three consecutive microtask turns" in contract
    assert "never render error.message, error.body.message" in contract
    assert "inline in the test file" in contract
    assert "stable data-role values" in contract
    assert "Salesforce API 67 supports complex template expressions" in contract
    assert "maintainability convention, not a compiler restriction" in contract
    assert "getAccounts() read @AuraEnabled(cacheable=true)" in contract
    assert "getContacts(...) read non-cacheable" in contract
    assert "Project exactly Account Id and Name from the Account query" in contract
    assert (
        "exactly Contact Id, FirstName, LastName, Email and Phone from the Contact query"
        in contract
    )
    assert "Use Contact.AccountId only as the WHERE predicate; do not project it" in contract
    assert (
        "AuraHandledException whose sole argument is a fixed safe, nontechnical string" in contract
    )
    assert "cap the Account query at 1 through 50 rows" in contract
    assert "the Contact query at 1 through 100 rows" in contract
    assert "a selected account with contacts" in contract
    assert "Do not create User records, query Profile, or use System.runAs" in contract
    assert "stale by an account change" in contract
    assert "Do not render the contact-results hook" in contract
    assert "FirstName, LastName, Email and Phone" in contract
    assert "option whose value is the empty string" in contract
    assert "a combobox placeholder is not that option" in contract
    assert "disabled-state getter that returns true for a blank selection" in contract
    assert "do not bind a positive canLoadContacts getter directly" in contract
    assert "do not call non-@api component methods through the host element" in contract
    assert "wired getAccounts method with createApexTestWireAdapter" in contract
    assert "append the component before emitting account data or an error" in contract
    assert "imperative getContacts method with jest.fn" in contract
    assert "before dispatching the user event that triggers the load" in contract
    assert "Arrange initial-load mock outcomes" not in contract

    for obsolete_oracle_literal in (
        "MAX_ACCOUNTS = 50",
        "returnsAccountsInNameOrder",
        "Synthetic Account 000",
        "renders a blank option followed by wired accounts",
        "createDeferredPromise",
        "toHaveBeenNthCalledWith",
        "001000000000001AAA",
        "Skynet",
        "A-to-B-to-A",
        "{ virtual: true }",
        "jest.requireActual",
    ):
        assert obsolete_oracle_literal not in contract


def test_agent_output_inventory_remains_exact_and_excludes_controller_tooling() -> None:
    assert set(SALESFORCE_AGENT_OUTPUT_PATHS) == {
        "manifest/package.xml",
        "force-app/main/default/classes/AccountContactExplorerController.cls",
        "force-app/main/default/classes/AccountContactExplorerController.cls-meta.xml",
        "force-app/main/default/classes/AccountContactExplorerControllerTest.cls",
        "force-app/main/default/classes/AccountContactExplorerControllerTest.cls-meta.xml",
        ("force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.html"),
        ("force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"),
        ("force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.css"),
        ("force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js-meta.xml"),
        (
            "force-app/main/default/lwc/accountContactExplorer/__tests__/"
            "accountContactExplorer.test.js"
        ),
        ("force-app/main/default/permissionsets/AccountContactExplorerUser.permissionset-meta.xml"),
    }
    assert len(SALESFORCE_AGENT_OUTPUT_PATHS) == 11
    assert not {
        "package.json",
        "package-lock.json",
        "jest.config.js",
        "jest.setup.js",
    } & set(SALESFORCE_AGENT_OUTPUT_PATHS)


def test_candidate_jest_execution_failure_is_in_the_public_repair_vocabulary() -> None:
    diagnostic_id = SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID

    assert diagnostic_id == "candidate_jest_execution_failure"
    assert diagnostic_id in SALESFORCE_CANDIDATE_DIAGNOSTIC_IDS
    assert diagnostic_id not in SALESFORCE_CANDIDATE_STATIC_DIAGNOSTIC_IDS


def test_controller_jest_execution_failure_is_a_controller_repair_signal() -> None:
    diagnostic_id = SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID

    assert diagnostic_id == "controller_jest_execution_failure"
    assert diagnostic_id in SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS
    assert diagnostic_id not in SALESFORCE_CANDIDATE_DIAGNOSTIC_IDS


@pytest.mark.parametrize(
    ("unit_id", "title", "expected_diagnostic_id"),
    (
        (
            "salesforce-vf-to-lwc",
            "controller: resets completed and error state on nonblank account change",
            CONTROLLER_JEST_ACCOUNT_CHANGE_RESET_DIAGNOSTIC_ID,
        ),
        (
            CASE_MANAGEMENT_CONSOLE_UNIT_ID,
            "controller: requests closed cases when Closed is selected",
            "controller_jest_status_closed",
        ),
        (
            CASE_MANAGEMENT_CONSOLE_UNIT_ID,
            "controller: requests all cases when All is selected",
            "controller_jest_status_all",
        ),
        (
            CASE_MANAGEMENT_CONSOLE_UNIT_ID,
            "controller: resets completed and error state when status changes",
            CONTROLLER_JEST_STATUS_CHANGE_RESET_DIAGNOSTIC_ID,
        ),
        (
            CASE_MANAGEMENT_CONSOLE_UNIT_ID,
            "controller: ignores a response made stale by status change",
            CONTROLLER_JEST_STATUS_CHANGE_STALE_RESPONSE_DIAGNOSTIC_ID,
        ),
        (
            CASE_MANAGEMENT_CONSOLE_UNIT_ID,
            "controller: renders initial guidance before account selection",
            CONTROLLER_JEST_INITIAL_GUIDANCE_DIAGNOSTIC_ID,
        ),
        (
            CASE_MANAGEMENT_CONSOLE_UNIT_ID,
            "controller: clears completed case state when the account wire later fails",
            CONTROLLER_JEST_ACCOUNT_ERROR_RESET_DIAGNOSTIC_ID,
        ),
        (
            CASE_MANAGEMENT_CONSOLE_UNIT_ID,
            "controller: invalidates pending case work when the account wire fails",
            CONTROLLER_JEST_ACCOUNT_ERROR_STALE_RESPONSE_DIAGNOSTIC_ID,
        ),
    ),
)
def test_controller_jest_failure_parser_returns_precise_state_and_filter_signal(
    unit_id: str,
    title: str,
    expected_diagnostic_id: str,
) -> None:
    spec = resolve_salesforce_controller_jest_spec(unit_id)
    controller_test_path = TOOLCHAIN / spec.controller_test_path
    assertions = [
        {
            "title": behavior_title,
            "status": "failed" if behavior_title == title else "passed",
        }
        for behavior_title in spec.behavior_titles
    ]
    payload = {
        "success": False,
        "wasInterrupted": False,
        "numTotalTestSuites": 1,
        "numPassedTestSuites": 0,
        "numFailedTestSuites": 1,
        "numPendingTestSuites": 0,
        "numRuntimeErrorTestSuites": 0,
        "numTotalTests": len(assertions),
        "numPassedTests": len(assertions) - 1,
        "numFailedTests": 1,
        "numPendingTests": 0,
        "numTodoTests": 0,
        "testResults": [
            {
                "name": str(controller_test_path),
                "status": "failed",
                "failureMessages": ["bounded controller failure"],
                "assertionResults": assertions,
            }
        ],
    }
    execution: Any = SimpleNamespace(
        stdout=json.dumps(payload),
        receipt=SimpleNamespace(
            stdout_digest="sha256:" + "a" * 64,
            stderr_digest="sha256:" + "b" * 64,
        ),
    )

    _, diagnostic_ids = _controller_jest_failure_evidence(
        execution,
        controller_test_path,
        unit_id=unit_id,
    )

    assert diagnostic_ids == (expected_diagnostic_id,)


def test_granular_first_pass_diagnostics_are_in_the_static_repair_vocabulary() -> None:
    assert {
        APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,
        APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,
        JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID,
        LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID,
    } <= SALESFORCE_CANDIDATE_STATIC_DIAGNOSTIC_IDS


def test_candidate_contract_accepts_structurally_distinct_safe_implementation() -> None:
    with candidate_from_memory(structurally_distinct_safe_outputs()) as workspace:
        result = check_salesforce_candidate(workspace.root)

    assert result["passed"] is True
    assert result["apex_tests_prepared"] is True
    assert result["lwc_jest_tests_prepared"] is True
    assert result["apex_tests_executed"] is False
    assert result["lwc_jest_executed"] is False


def test_candidate_contract_accepts_bounded_semantic_hook_bindings() -> None:
    outputs = outputs_with_bounded_semantic_bindings()

    with candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(workspace.root)

    assert result["passed"] is True
    contract = "\n".join(SALESFORCE_IMPLEMENTATION_CONTRACT)
    for candidate_owned_name in ("selectorHook", "resultsHook", "busyHook"):
        assert candidate_owned_name not in contract


def test_candidate_contract_rejects_unbounded_semantic_hook_expression() -> None:
    outputs = structurally_distinct_safe_outputs()
    template = outputs[LWC_TEMPLATE_PATH].decode("utf-8")
    outputs[LWC_TEMPLATE_PATH] = template.replace(
        'data-role="account-selector"',
        "data-role={semanticRoles[currentRole]}",
        1,
    ).encode("utf-8")

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_lwc_template_contract"
    assert failure.diagnostic_ids == (LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID,)


@pytest.mark.parametrize(
    "expression",
    (
        "isLoading || !selectedAccountId",
        "!isLoading",
        "accounts[selectedIndex]",
        "computeDisabled()",
    ),
)
def test_candidate_contract_allows_api_67_lwc_template_expression(
    expression: str,
) -> None:
    outputs = structurally_distinct_safe_outputs()
    template = outputs[LWC_TEMPLATE_PATH].decode("utf-8")
    outputs[LWC_TEMPLATE_PATH] = template.replace(
        '<button data-role="load-contacts"',
        f'<button disabled={{{expression}}} data-role="load-contacts"',
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_accepts_semicolon_free_aliased_lwc_base_import() -> None:
    outputs = structurally_distinct_safe_outputs()
    outputs[LWC_JAVASCRIPT_PATH] = b"""import {
    LightningElement as ComponentBase,
    wire as observe
} from 'lwc'
import loadAccounts from '@salesforce/apex/AccountContactExplorerController.getAccounts'
import loadContacts from '@salesforce/apex/AccountContactExplorerController.getContacts'

export default class extends ComponentBase {
    get availableOperations() {
        return [observe, loadAccounts, loadContacts]
    }
}
"""

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_uses_only_in_memory_outputs_and_candidate_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = salesforce_candidate_outputs()
    original_open = Path.open

    def reject_builder_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> IO[Any]:
        if path.resolve(strict=False) != CANDIDATE_BUILDER.resolve(strict=True):
            return original_open(path, mode, buffering, encoding, errors, newline)
        raise PermissionError("test candidate builder is unavailable during validation")

    with candidate_from_memory(outputs) as workspace:
        monkeypatch.setattr(Path, "open", reject_builder_open)

        result = check_salesforce_candidate(workspace.root)
        dependency_result, graph = check_dependency_closure(workspace.root)
        changes = workspace.audit_changes()
        revision_result = check_workspace_revision(workspace.root, tree_fingerprint(workspace.root))

        assert result["check"] == "salesforce-candidate-contract"
        assert result["agent_output_files"] == 11
        assert result["org_validation_performed"] is False
        assert result["deployment_claim"] is False
        assert changes.changed_paths == SALESFORCE_AGENT_OUTPUT_PATHS
        assert dependency_result["unresolved"] is False
        assert graph.has_unresolved is False
        assert revision_result["passed"] is True


def case_candidate_from_memory(outputs: dict[str, bytes]) -> IsolatedWorkspace:
    workspace = IsolatedWorkspace(CASE_FIXTURE / "input", CASE_AGENT_OUTPUT_PATHS)
    for path, content in outputs.items():
        workspace.write_bytes(path, content)
    return workspace


def rejected_case_candidate(outputs: dict[str, bytes]) -> LocalCheckFailure:
    with case_candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(
                workspace.root,
                unit_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID,
            )
    return caught.value


def run_case_controller_jest(
    outputs: dict[str, bytes],
) -> subprocess.CompletedProcess[str]:
    """Execute the immutable Case controller suite against one isolated candidate."""

    node = shutil.which("node")
    jest_entry = TOOLCHAIN / "node_modules/jest/bin/jest.js"
    if node is None or not jest_entry.is_file():
        pytest.skip("the pinned Node/Jest harness is unavailable")
    with case_candidate_from_memory(outputs) as workspace:
        environment = dict(os.environ)
        environment["NODE_PATH"] = str(TOOLCHAIN / "node_modules")
        return subprocess.run(
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
                str(TOOLCHAIN / CASE_LWC_CONTROLLER_TEST_PATH),
            ),
            cwd=workspace.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )


def case_outputs_with_branch_specific_queries() -> dict[str, bytes]:
    """Return a safe non-golden Case controller with three static query branches."""

    outputs = case_management_candidate_outputs()
    outputs[CASE_CONTROLLER_PATH] = b"""public with sharing class CaseManagementConsoleController {
    @AuraEnabled(cacheable=true)
    public static List<Account> getAccounts() {
        try {
            return [
                SELECT Name, Id
                FROM Account
                WITH USER_MODE
                ORDER BY Name
                LIMIT 25
            ];
        } catch (QueryException queryError) {
            throw new AuraHandledException('Account choices are unavailable.');
        }
    }

    @AuraEnabled
    public static List<Case> getCases(Id accountId, String statusFilter) {
        if (accountId == null) {
            return new List<Case>();
        }
        try {
            if (statusFilter == 'CLOSED') {
                return [
                    SELECT Status, Id, Contact.Name, CaseNumber, Priority, Subject
                    FROM Case
                    WHERE AccountId = :accountId AND IsClosed = true
                    WITH USER_MODE
                    ORDER BY CaseNumber DESC
                    LIMIT 75
                ];
            }
            if (statusFilter == 'ALL') {
                return [
                    SELECT Priority, Subject, CaseNumber, Id, Status, Contact.Name
                    FROM Case
                    WHERE AccountId = :accountId
                    WITH USER_MODE
                    ORDER BY CaseNumber DESC
                    LIMIT 80
                ];
            }
            return [
                SELECT Contact.Name, CaseNumber, Subject, Status, Priority, Id
                FROM Case
                WHERE AccountId = :accountId AND IsClosed = false
                WITH USER_MODE
                ORDER BY CaseNumber DESC
                LIMIT 90
            ];
        } catch (QueryException queryError) {
            throw new AuraHandledException('Case results are unavailable.');
        }
    }
}
"""
    return outputs


def case_outputs_with_two_query_status_strategy() -> dict[str, bytes]:
    """Return a valid non-golden Case controller with ALL plus a bound Boolean query."""

    outputs = case_management_candidate_outputs()
    outputs[CASE_CONTROLLER_PATH] = b"""public with sharing class CaseManagementConsoleController {
    @AuraEnabled(cacheable=true)
    public static List<Account> getAccounts() {
        try {
            return [
                SELECT Id, Name
                FROM Account
                WITH USER_MODE
                ORDER BY Name
                LIMIT 30
            ];
        } catch (QueryException queryError) {
            throw new AuraHandledException('Account choices are unavailable.');
        }
    }

    @AuraEnabled(cacheable=false)
    public static List<Case> getCases(Id accountId, String statusFilter) {
        if (accountId == null) {
            return new List<Case>();
        }
        try {
            if (statusFilter == 'ALL') {
                return [
                    SELECT Id, CaseNumber, Subject, Status, Priority, Contact.Name
                    FROM Case
                    WHERE AccountId = :accountId
                    WITH USER_MODE
                    ORDER BY CaseNumber DESC
                    LIMIT 70
                ];
            }
            Boolean closedOnly = statusFilter == 'CLOSED';
            return [
                SELECT Id, CaseNumber, Subject, Status, Priority, Contact.Name
                FROM Case
                WHERE AccountId = :accountId AND IsClosed = :closedOnly
                WITH USER_MODE
                ORDER BY CaseNumber DESC
                LIMIT 85
            ];
        } catch (QueryException queryError) {
            throw new AuraHandledException('Case results are unavailable.');
        }
    }
}
"""
    return outputs


def replace_nth(source: str, old: str, new: str, occurrence: int) -> str:
    """Replace one one-indexed occurrence in a test candidate."""

    offsets = [match.start() for match in re.finditer(re.escape(old), source)]
    assert 1 <= occurrence <= len(offsets)
    offset = offsets[occurrence - 1]
    return source[:offset] + new + source[offset + len(old) :]


def case_outputs_for_query_count(query_count: int) -> dict[str, bytes]:
    """Return one accepted candidate-owned Case query strategy."""

    builders = {
        1: case_management_candidate_outputs,
        2: case_outputs_with_two_query_status_strategy,
        3: case_outputs_with_branch_specific_queries,
    }
    return builders[query_count]()


def case_outputs_with_direct_status_constants(query_count: int = 3) -> dict[str, bytes]:
    """Return one Case strategy routed through literal-valued class constants."""

    outputs = case_outputs_for_query_count(query_count)
    controller = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    declaration = "public with sharing class CaseManagementConsoleController {"
    assert declaration in controller
    controller = controller.replace(
        declaration,
        declaration
        + """
    public static final String STATUS_OPEN = 'OPEN';
    public static final String STATUS_CLOSED = 'CLOSED';
    public static final String STATUS_ALL = 'ALL';""",
        1,
    )
    for value in ("CLOSED", "ALL"):
        controller = controller.replace(
            f"statusFilter == '{value}'",
            f"statusFilter == STATUS_{value}",
        )
    outputs[CASE_CONTROLLER_PATH] = controller.encode("utf-8")

    test_source = outputs[CASE_CONTROLLER_TEST_PATH].decode("utf-8")
    for value in ("OPEN", "CLOSED", "ALL"):
        test_source = test_source.replace(
            f", '{value}')",
            f", CaseManagementConsoleController.STATUS_{value})",
        )
    outputs[CASE_CONTROLLER_TEST_PATH] = test_source.encode("utf-8")
    return outputs


def case_outputs_with_live_status_normalizer() -> dict[str, bytes]:
    """Return the status routing and Apex-test call shape from live handle 1612f4b0."""

    outputs = case_outputs_with_direct_status_constants()
    controller = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    null_guard = """        if (accountId == null) {
            return new List<Case>();
        }
"""
    assert null_guard in controller
    controller = controller.replace(
        null_guard,
        null_guard
        + """
        String normalizedStatus = normalizeStatusFilter(statusFilter);
""",
        1,
    )
    controller = controller.replace(
        "statusFilter == STATUS_CLOSED",
        "normalizedStatus == STATUS_CLOSED",
    ).replace(
        "statusFilter == STATUS_ALL",
        "normalizedStatus == STATUS_ALL",
    )
    body, closing_brace = controller.rsplit("}", 1)
    outputs[CASE_CONTROLLER_PATH] = (
        body
        + """
    private static String normalizeStatusFilter(String statusFilter) {
        if (statusFilter == STATUS_CLOSED || statusFilter == STATUS_ALL) {
            return statusFilter;
        }
        return STATUS_OPEN;
    }
"""
        + closing_brace
    ).encode("utf-8")
    return outputs


def test_case_management_candidate_passes_static_and_closure_checks() -> None:
    outputs = case_management_candidate_outputs()

    with case_candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(workspace.root, unit_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID)
        dependency_result, graph = check_dependency_closure(
            workspace.root, unit_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID
        )
        changes = workspace.audit_changes()

        assert result["check"] == "salesforce-candidate-contract"
        assert result["passed"] is True
        assert result["agent_output_files"] == 11
        assert result["org_validation_performed"] is False
        assert result["deployment_claim"] is False
        assert changes.changed_paths == CASE_AGENT_OUTPUT_PATHS
        assert dependency_result["unresolved"] is False
        assert graph.has_unresolved is False


def test_case_controller_jest_accepts_known_good_account_error_boundaries() -> None:
    completed = run_case_controller_jest(case_management_candidate_outputs())

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "19 passed" in completed.stderr


def test_case_controller_jest_accepts_accessible_guidance_without_private_data_state() -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_LWC_HTML_PATH].decode("utf-8")
    assert 'data-state="guidance"' not in source
    assert '<p role="alert" class="guidance">' in source

    completed = run_case_controller_jest(outputs)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "19 passed" in completed.stderr


def test_case_controller_jest_rejects_missing_account_error_reset_and_invalidation() -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_LWC_JAVASCRIPT_PATH].decode("utf-8")
    safe_error_branch = """        } else if (error) {
            this.loadRequestGeneration += 1;
            this.accountOptions = [BLANK_ACCOUNT_OPTION];
            this.selectedAccountId = '';
            this.cases = [];
            this.isLoading = false;
            this.hasLoaded = false;
            this.warningMessage = undefined;
            this.errorMessage = 'Accounts could not be loaded.';
        }"""
    incomplete_error_branch = """        } else if (error) {
            this.accountOptions = [BLANK_ACCOUNT_OPTION];
            this.errorMessage = 'Accounts could not be loaded.';
        }"""
    assert safe_error_branch in source
    outputs[CASE_LWC_JAVASCRIPT_PATH] = source.replace(
        safe_error_branch,
        incomplete_error_branch,
        1,
    ).encode("utf-8")

    completed = run_case_controller_jest(outputs)
    terminal_output = completed.stdout + completed.stderr

    assert completed.returncode != 0
    assert (
        "controller: clears completed case state when the account wire later fails"
        in terminal_output
    )
    assert (
        "controller: invalidates pending case work when the account wire fails" in terminal_output
    )


def test_case_implementation_contract_declares_scenario_specific_query_caps() -> None:
    contract = "\n".join(CASE_IMPLEMENTATION_CONTRACT)

    assert "cap the Account query at 1 through 50 rows" in contract
    assert "one to three branch-specific static Case queries" in contract
    assert "each capped at 1 through 100 rows" in contract
    assert "ordering by CaseNumber DESC" in contract
    assert "map OPEN to nonclosed cases, CLOSED to closed cases, and ALL to both" in contract
    assert "getAccounts() read @AuraEnabled(cacheable=true)" in contract
    assert "getCases(...) read non-cacheable" in contract
    assert "Project exactly Account Id and Name from the Account query" in contract
    assert (
        "exactly Case Id, CaseNumber, Subject, Status, Priority and Contact.Name from every Case "
        "query" in contract
    )
    assert "Use Case.AccountId only as the WHERE predicate; do not project it" in contract
    assert "wired getAccounts method with createApexTestWireAdapter" in contract
    assert "append the component before emitting account data or an error" in contract
    assert "imperative getCases method with jest.fn" in contract
    assert "at least three consecutive microtask turns" in contract
    assert "never render error.message, error.body.message" in contract
    assert "before dispatching the user event that triggers the load" in contract
    assert "Arrange initial-load mock outcomes" not in contract
    assert "Account wire transitions to error" in contract
    assert "late Case success or failure must not reappear" in contract


@pytest.mark.parametrize("unit_id", ("salesforce-vf-to-lwc", CASE_MANAGEMENT_CONSOLE_UNIT_ID))
@pytest.mark.parametrize("annotation", ("@AuraEnabled", "@AuraEnabled(cacheable=false)"))
def test_explicit_dependent_read_accepts_noncacheable_annotation(
    unit_id: str,
    annotation: str,
) -> None:
    if unit_id == CASE_MANAGEMENT_CONSOLE_UNIT_ID:
        outputs = case_management_candidate_outputs()
        path = CASE_CONTROLLER_PATH
        signature = "public static List<Case> getCases"
        workspace_factory = case_candidate_from_memory
    else:
        outputs = salesforce_candidate_outputs()
        path = CONTROLLER_PATH
        signature = "public static List<Contact> getContacts"
        workspace_factory = candidate_from_memory
    source = outputs[path].decode("utf-8")
    marker = "@AuraEnabled\n    " + signature
    assert marker in source
    outputs[path] = source.replace(marker, annotation + "\n    " + signature, 1).encode("utf-8")

    with workspace_factory(outputs) as workspace:
        result = check_salesforce_candidate(workspace.root, unit_id=unit_id)

    assert result["passed"] is True


@pytest.mark.parametrize("unit_id", ("salesforce-vf-to-lwc", CASE_MANAGEMENT_CONSOLE_UNIT_ID))
def test_explicit_dependent_read_rejects_cacheable_true(unit_id: str) -> None:
    if unit_id == CASE_MANAGEMENT_CONSOLE_UNIT_ID:
        outputs = case_management_candidate_outputs()
        path = CASE_CONTROLLER_PATH
        signature = "public static List<Case> getCases"
        reject = rejected_case_candidate
    else:
        outputs = salesforce_candidate_outputs()
        path = CONTROLLER_PATH
        signature = "public static List<Contact> getContacts"
        reject = rejected_candidate
    source = outputs[path].decode("utf-8")
    marker = "@AuraEnabled\n    " + signature
    assert marker in source
    outputs[path] = source.replace(
        marker,
        "@AuraEnabled(cacheable=true)\n    " + signature,
        1,
    ).encode("utf-8")

    failure = reject(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,)


def test_case_wired_account_read_rejects_noncacheable_annotation() -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    marker = "@AuraEnabled(cacheable=true)\n    public static List<Account> getAccounts"
    assert marker in source
    outputs[CASE_CONTROLLER_PATH] = source.replace(
        marker,
        "@AuraEnabled(cacheable=false)\n    public static List<Account> getAccounts",
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,)


def test_case_target_and_preserved_source_permission_fields_are_intentional() -> None:
    outputs = case_management_candidate_outputs()
    target = outputs[CASE_PERMISSION_SET_PATH].decode("utf-8")
    source = (
        CASE_FIXTURE
        / "input/force-app/main/default/permissionsets/CaseManagementConsoleUser.permissionset-meta.xml"
    ).read_text(encoding="utf-8")
    target_fields = frozenset(re.findall(r"<field>([^<]+)</field>", target))
    source_fields = frozenset(re.findall(r"<field>([^<]+)</field>", source))

    assert source_fields == {
        "Case.AccountId",
        "Case.ContactId",
        "Case.Description",
        "Case.Priority",
        "Case.Subject",
    }
    assert target_fields == source_fields


@pytest.mark.parametrize("unapproved_field", ("Case.CaseNumber", "Case.IsClosed", "Case.Status"))
def test_case_target_permission_contract_rejects_wrong_field_inventory(
    unapproved_field: str,
) -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_PERMISSION_SET_PATH].decode("utf-8")
    assert "Case.AccountId" in source
    outputs[CASE_PERMISSION_SET_PATH] = source.replace(
        "Case.AccountId",
        unapproved_field,
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_permission_set_contract"


@pytest.mark.parametrize("unapproved_field", ("Case.CaseNumber", "Case.IsClosed", "Case.Status"))
def test_case_target_permission_contract_rejects_extra_nonpermissionable_field(
    unapproved_field: str,
) -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_PERMISSION_SET_PATH].decode("utf-8")
    marker = "    <hasActivationRequired>false</hasActivationRequired>"
    assert marker in source
    extra_permission = (
        "    <fieldPermissions>\n"
        "        <editable>false</editable>\n"
        f"        <field>{unapproved_field}</field>\n"
        "        <readable>true</readable>\n"
        "    </fieldPermissions>\n"
    )
    outputs[CASE_PERMISSION_SET_PATH] = source.replace(
        marker,
        extra_permission + marker,
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_permission_set_contract"


def test_case_target_permission_contract_requires_preserved_description() -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_PERMISSION_SET_PATH].decode("utf-8")
    description_permission = (
        "    <fieldPermissions>\n"
        "        <editable>false</editable>\n"
        "        <field>Case.Description</field>\n"
        "        <readable>true</readable>\n"
        "    </fieldPermissions>\n"
    )
    assert source.count(description_permission) == 1
    outputs[CASE_PERMISSION_SET_PATH] = source.replace(description_permission, "", 1).encode(
        "utf-8"
    )

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_permission_set_contract"


def test_case_contract_accepts_three_non_golden_branch_specific_queries() -> None:
    outputs = case_outputs_with_branch_specific_queries()

    with case_candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(
            workspace.root,
            unit_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID,
        )

    assert result["passed"] is True


def test_case_contract_accepts_two_query_all_plus_bound_boolean_strategy() -> None:
    outputs = case_outputs_with_two_query_status_strategy()

    with case_candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(
            workspace.root,
            unit_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID,
        )

    assert result["passed"] is True


@pytest.mark.parametrize("query_count", (1, 2, 3))
def test_case_contract_accepts_literal_valued_status_constants_for_every_query_strategy(
    query_count: int,
) -> None:
    outputs = case_outputs_with_direct_status_constants(query_count)

    with case_candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(
            workspace.root,
            unit_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID,
        )

    assert result["passed"] is True


def test_case_accepts_live_status_normalizer_and_qualified_test_constants() -> None:
    outputs = case_outputs_with_live_status_normalizer()

    with case_candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(
            workspace.root,
            unit_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID,
        )

    assert result["passed"] is True


def test_case_contract_resolves_status_constants_by_value_not_name() -> None:
    outputs = case_outputs_with_live_status_normalizer()
    for path in (CASE_CONTROLLER_PATH, CASE_CONTROLLER_TEST_PATH):
        source = outputs[path].decode("utf-8")
        source = source.replace("STATUS_OPEN", "FILTER_ALPHA")
        source = source.replace("STATUS_CLOSED", "FILTER_BETA")
        source = source.replace("STATUS_ALL", "FILTER_GAMMA")
        outputs[path] = source.encode("utf-8")

    with case_candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(
            workspace.root,
            unit_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID,
        )

    assert result["passed"] is True


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("STATUS_CLOSED = 'CLOSED'", "STATUS_CLOSED = 'OPEN'"),
        (
            "statusFilter == STATUS_CLOSED || statusFilter == STATUS_ALL",
            "STATUS_CLOSED == STATUS_CLOSED || STATUS_ALL == STATUS_ALL",
        ),
        ("return statusFilter;", "return STATUS_CLOSED;"),
        ("return STATUS_OPEN;", "return STATUS_ALL;"),
    ),
)
def test_case_contract_rejects_unproven_or_incorrect_status_normalizer(
    old: str,
    new: str,
) -> None:
    outputs = case_outputs_with_live_status_normalizer()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    assert old in source
    outputs[CASE_CONTROLLER_PATH] = source.replace(old, new, 1).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_case_contract_rejects_normalized_status_alias_reassignment() -> None:
    outputs = case_outputs_with_live_status_normalizer()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    declaration = "String normalizedStatus = normalizeStatusFilter(statusFilter);"
    assert declaration in source
    outputs[CASE_CONTROLLER_PATH] = source.replace(
        declaration,
        declaration + "\n        normalizedStatus = STATUS_OPEN;",
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_case_rejects_qualified_constants_without_complete_status_coverage() -> None:
    outputs = case_outputs_with_live_status_normalizer()
    source = outputs[CASE_CONTROLLER_TEST_PATH].decode("utf-8")
    all_argument = "CaseManagementConsoleController.STATUS_ALL)"
    assert source.count(all_argument) == 1
    outputs[CASE_CONTROLLER_TEST_PATH] = source.replace(
        all_argument,
        "CaseManagementConsoleController.STATUS_OPEN)",
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_test_contract"


def test_case_contract_accepts_locally_assigned_query_result_that_is_returned() -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    query_start = """        try {
            return [
                SELECT Id, CaseNumber"""
    query_end = """                LIMIT :MAX_CASES
            ];
        } catch (QueryException queryError) {"""
    assert query_start in source
    assert query_end in source
    source = source.replace(
        query_start,
        """        try {
            List<Case> visibleCases = [
                SELECT Id, CaseNumber""",
        1,
    )
    outputs[CASE_CONTROLLER_PATH] = source.replace(
        query_end,
        """                LIMIT :MAX_CASES
            ];
            return visibleCases;
        } catch (QueryException queryError) {""",
        1,
    ).encode("utf-8")

    with case_candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(
            workspace.root,
            unit_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID,
        )

    assert result["passed"] is True


@pytest.mark.parametrize("query_count", (1, 2, 3))
@pytest.mark.parametrize(
    "unauthorized_statement",
    (
        "statusFilter = 'OPEN';",
        "String statusFilter = 'OPEN';",
    ),
)
def test_case_contract_rejects_status_filter_authority_replacement_for_every_query_strategy(
    query_count: int,
    unauthorized_statement: str,
) -> None:
    outputs = case_outputs_for_query_count(query_count)
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    null_guard = """        if (accountId == null) {
            return new List<Case>();
        }
"""
    assert null_guard in source
    outputs[CASE_CONTROLLER_PATH] = source.replace(
        null_guard,
        null_guard + f"        {unauthorized_statement}\n",
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_case_contract_rejects_query_result_discarded_before_public_return() -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    query_start = """        try {
            return [
                SELECT Id, CaseNumber"""
    query_end = """                LIMIT :MAX_CASES
            ];
        } catch (QueryException queryError) {"""
    assert query_start in source
    assert query_end in source
    source = source.replace(
        query_start,
        """        try {
            List<Case> ignoredCases = [
                SELECT Id, CaseNumber""",
        1,
    )
    outputs[CASE_CONTROLLER_PATH] = source.replace(
        query_end,
        """                LIMIT :MAX_CASES
            ];
            return new List<Case>();
        } catch (QueryException queryError) {""",
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_account_contact_contract_rejects_account_id_reassignment() -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode("utf-8")
    null_guard = """        if (accountId == null) {
            return new List<Contact>();
        }
"""
    assert null_guard in source
    outputs[CONTROLLER_PATH] = source.replace(
        null_guard,
        null_guard + "        accountId = '001000000000001AAA';\n",
        1,
    ).encode("utf-8")

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_account_query_contract_rejects_unconditional_empty_return_before_query() -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode("utf-8")
    query_start = """    public static List<Account> getAccounts() {
        try {"""
    assert query_start in source
    outputs[CONTROLLER_PATH] = source.replace(
        query_start,
        """    public static List<Account> getAccounts() {
        if (true) {
            return new List<Account>();
        }
        try {""",
        1,
    ).encode("utf-8")

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_contact_query_contract_rejects_nonnull_account_empty_return() -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode("utf-8")
    null_guard = """        if (accountId == null) {
            return new List<Contact>();
        }
"""
    assert null_guard in source
    outputs[CONTROLLER_PATH] = source.replace(
        null_guard,
        null_guard
        + """        if (accountId != null) {
            return new List<Contact>();
        }
""",
        1,
    ).encode("utf-8")

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


@pytest.mark.parametrize("query_count", (1, 2, 3))
@pytest.mark.parametrize(
    "early_condition",
    (
        "true",
        "accountId != null",
        "statusFilter != null",
    ),
)
def test_case_contract_rejects_early_empty_return_for_every_query_strategy(
    query_count: int,
    early_condition: str,
) -> None:
    outputs = case_outputs_for_query_count(query_count)
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    null_guard = """        if (accountId == null) {
            return new List<Case>();
        }
"""
    assert null_guard in source
    outputs[CASE_CONTROLLER_PATH] = source.replace(
        null_guard,
        null_guard
        + f"""        if ({early_condition}) {{
            return new List<Case>();
        }}
""",
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


@pytest.mark.parametrize(
    ("query_start", "query_end", "assignment", "empty_return"),
    (
        (
            """        try {
            return [
                SELECT Id, Name""",
            """                LIMIT :MAX_ACCOUNTS
            ];
        } catch (QueryException queryError) {""",
            "List<Account> ignoredAccounts = [",
            "return new List<Account>();",
        ),
        (
            """        try {
            return [
                SELECT Id, FirstName""",
            """                LIMIT :MAX_CONTACTS
            ];
        } catch (QueryException queryError) {""",
            "List<Contact> ignoredContacts = [",
            "return new List<Contact>();",
        ),
    ),
)
def test_account_contact_contract_rejects_discarded_query_result(
    query_start: str,
    query_end: str,
    assignment: str,
    empty_return: str,
) -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode("utf-8")
    assert query_start in source
    assert query_end in source
    source = source.replace(
        query_start,
        query_start.replace("return [", assignment),
        1,
    )
    outputs[CONTROLLER_PATH] = source.replace(
        query_end,
        query_end.replace(
            "];\n        } catch",
            f"];\n            {empty_return}\n        }} catch",
        ),
        1,
    ).encode("utf-8")

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_case_contract_accepts_only_publicly_required_semantic_states() -> None:
    outputs = case_management_candidate_outputs()
    template = outputs[CASE_LWC_HTML_PATH].decode("utf-8")
    assert 'data-state="guidance"' not in template
    for state in ("warning", "error", "results"):
        marker = f' data-state="{state}"'
        assert marker in template
        template = template.replace(marker, "", 1)
    outputs[CASE_LWC_HTML_PATH] = template.encode("utf-8")

    with case_candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(
            workspace.root,
            unit_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID,
        )

    assert result["passed"] is True


def test_case_contract_rejects_more_than_three_branch_specific_queries() -> None:
    outputs = case_outputs_with_branch_specific_queries()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    insertion = "        try {\n            if (statusFilter == 'CLOSED') {"
    assert insertion in source
    fourth_query = """        try {
            if (statusFilter == 'UNKNOWN') {
                return [
                    SELECT Id, CaseNumber, Subject, Status, Priority, Contact.Name
                    FROM Case
                    WHERE AccountId = :accountId
                    WITH USER_MODE
                    ORDER BY CaseNumber DESC
                    LIMIT 10
                ];
            }
            if (statusFilter == 'CLOSED') {"""
    outputs[CASE_CONTROLLER_PATH] = source.replace(insertion, fourth_query, 1).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_case_contract_rejects_excess_field_in_any_branch_query() -> None:
    outputs = case_outputs_with_branch_specific_queries()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    selected_fields = "SELECT Status, Id, Contact.Name, CaseNumber, Priority, Subject"
    assert selected_fields in source
    outputs[CASE_CONTROLLER_PATH] = source.replace(
        selected_fields,
        "SELECT Status, Id, Description, Contact.Name, CaseNumber, Priority, Subject",
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_case_contract_rejects_one_query_that_ignores_status_filter() -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    predicate = "WHERE AccountId = :accountId AND IsClosed IN :closedValues"
    assert predicate in source
    outputs[CASE_CONTROLLER_PATH] = source.replace(
        predicate,
        "WHERE AccountId = :accountId",
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("closedValues = new List<Boolean>{ true };", "closedValues = new List<Boolean>{ false };"),
        (
            "closedValues = new List<Boolean>{ true, false };",
            "closedValues = new List<Boolean>{ true };",
        ),
    ),
)
def test_case_contract_rejects_incorrect_one_query_status_mapping(old: str, new: str) -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    assert old in source
    outputs[CASE_CONTROLLER_PATH] = source.replace(old, new, 1).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_case_contract_rejects_status_collection_overwrite_before_query() -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    query_start = """        try {
            return [
                SELECT Id, CaseNumber"""
    assert query_start in source
    outputs[CASE_CONTROLLER_PATH] = source.replace(
        query_start,
        """        closedValues = new List<Boolean>{ false };

        try {
            return [
                SELECT Id, CaseNumber""",
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_case_contract_rejects_bound_boolean_overwrite_before_query() -> None:
    outputs = case_outputs_with_two_query_status_strategy()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    mapping = "Boolean closedOnly = statusFilter == 'CLOSED';"
    assert mapping in source
    outputs[CASE_CONTROLLER_PATH] = source.replace(
        mapping,
        mapping + "\n            closedOnly = false;",
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


@pytest.mark.parametrize("occurrence", (2, 3))
def test_case_contract_rejects_ascending_order_in_later_query_branch(
    occurrence: int,
) -> None:
    outputs = case_outputs_with_branch_specific_queries()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    outputs[CASE_CONTROLLER_PATH] = replace_nth(
        source,
        "ORDER BY CaseNumber DESC",
        "ORDER BY CaseNumber ASC",
        occurrence,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


@pytest.mark.parametrize(
    ("selected_fields", "replacement"),
    (
        (
            "SELECT Priority, Subject, CaseNumber, Id, Status, Contact.Name",
            "SELECT Priority, Subject, Description, CaseNumber, Id, Status, Contact.Name",
        ),
        (
            "SELECT Contact.Name, CaseNumber, Subject, Status, Priority, Id",
            "SELECT Contact.Name, CaseNumber, Description, Subject, Status, Priority, Id",
        ),
    ),
)
def test_case_contract_rejects_excess_field_in_later_query_branch(
    selected_fields: str,
    replacement: str,
) -> None:
    outputs = case_outputs_with_branch_specific_queries()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    assert selected_fields in source
    outputs[CASE_CONTROLLER_PATH] = source.replace(
        selected_fields,
        replacement,
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


@pytest.mark.parametrize(
    ("old", "new"),
    (
        (
            "WHERE AccountId = :accountId\n                    WITH USER_MODE",
            "WHERE AccountId = :accountId AND IsClosed = true\n                    WITH USER_MODE",
        ),
        ("AND IsClosed = false", "AND IsClosed = true"),
    ),
)
def test_case_contract_rejects_wrong_filter_in_second_or_third_query_branch(
    old: str,
    new: str,
) -> None:
    outputs = case_outputs_with_branch_specific_queries()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    assert old in source
    outputs[CASE_CONTROLLER_PATH] = source.replace(old, new, 1).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_case_contract_rejects_unguarded_query_before_status_branches() -> None:
    outputs = case_outputs_with_branch_specific_queries()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    open_query = """            return [
                SELECT Contact.Name, CaseNumber, Subject, Status, Priority, Id
                FROM Case
                WHERE AccountId = :accountId AND IsClosed = false
                WITH USER_MODE
                ORDER BY CaseNumber DESC
                LIMIT 90
            ];
"""
    assert source.count(open_query) == 1
    without_default = source.replace(open_query, "", 1)
    try_start = """        if (accountId == null) {
            return new List<Case>();
        }
        try {
"""
    assert try_start in without_default
    outputs[CASE_CONTROLLER_PATH] = without_default.replace(
        try_start,
        try_start + open_query,
        1,
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_case_contract_rejects_static_query_hidden_in_private_helper() -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_CONTROLLER_PATH].decode("utf-8")
    body, closing_brace = source.rsplit("}", 1)
    outputs[CASE_CONTROLLER_PATH] = (
        body
        + """
    private static List<Account> hiddenQuery() {
        return [
            SELECT Id, Name
            FROM Account
            WITH USER_MODE
            ORDER BY Name
            LIMIT 1
        ];
    }
"""
        + closing_brace
    ).encode("utf-8")

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_check_dependency_closure_rejects_unknown_unit() -> None:
    outputs = salesforce_candidate_outputs()
    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure, match="unsupported Salesforce migration unit"):
            check_dependency_closure(workspace.root, unit_id="bogus")


def test_check_salesforce_candidate_rejects_unknown_unit() -> None:
    outputs = salesforce_candidate_outputs()
    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure, match="unsupported Salesforce migration unit"):
            check_salesforce_candidate(workspace.root, unit_id="does-not-exist")


@pytest.mark.parametrize(
    "declaration",
    (
        "public without sharing class AccountContactExplorerController",
        "public inherited sharing class AccountContactExplorerController",
        "public class AccountContactExplorerController",
    ),
)
def test_candidate_contract_rejects_non_with_sharing_controller(
    declaration: str,
) -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    outputs[CONTROLLER_PATH] = source.replace(
        "public with sharing class AccountContactExplorerController",
        declaration,
        1,
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_candidate_contract_rejects_extra_aura_enabled_surface() -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    body, closing_brace = source.rsplit("}", 1)
    outputs[CONTROLLER_PATH] = (
        body
        + """
    @AuraEnabled(cacheable=true)
    public static List<Account> getEveryAccount() {
        return [SELECT Id FROM Account WITH USER_MODE LIMIT 1];
    }
"""
        + closing_brace
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,)


@pytest.mark.parametrize(
    "replacement",
    (
        "@AuraEnabled",
        "@AuraEnabled(cacheable=false)",
        "@AuraEnabled(cacheable=true)\n    private static List<Account>",
    ),
)
def test_candidate_contract_reports_exact_apex_public_interface_mismatch(
    replacement: str,
) -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    if replacement.startswith("@AuraEnabled(cacheable=true)\n"):
        source = source.replace(
            "@AuraEnabled(cacheable=true)\n    public static List<Account>",
            replacement,
            1,
        )
    else:
        source = source.replace("@AuraEnabled(cacheable=true)", replacement, 1)
    outputs[CONTROLLER_PATH] = source.encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,)


def test_candidate_contract_rejects_queries_without_user_mode() -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    outputs[CONTROLLER_PATH] = source.replace("WITH USER_MODE", "WITH SYSTEM_MODE").encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("MAX_ACCOUNTS = 50", "MAX_ACCOUNTS = 201"),
        ("LIMIT :MAX_ACCOUNTS", "LIMIT 201"),
        ("LIMIT :MAX_ACCOUNTS", "LIMIT :runtimeLimit"),
    ),
)
def test_candidate_contract_rejects_missing_or_excessive_query_caps(
    old: str,
    new: str,
) -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    assert old in source
    outputs[CONTROLLER_PATH] = source.replace(old, new, 1).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("MAX_ACCOUNTS = 50", "MAX_ACCOUNTS = 51"),
        ("LIMIT :MAX_ACCOUNTS", "LIMIT 51"),
        ("MAX_CONTACTS = 100", "MAX_CONTACTS = 101"),
        ("LIMIT :MAX_CONTACTS", "LIMIT 101"),
    ),
)
def test_account_contact_contract_enforces_per_object_query_caps(
    old: str,
    new: str,
) -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    assert old in source
    outputs[CONTROLLER_PATH] = source.replace(old, new, 1).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_account_contact_contract_still_rejects_a_second_valid_method_query() -> None:
    outputs = structurally_distinct_safe_outputs()
    source = outputs[CONTROLLER_PATH].decode("utf-8")
    insertion = "        try {\n            List<Account> visibleAccounts = ["
    assert insertion in source
    two_queries = """        try {
            List<Account> alternateAccounts = [
                SELECT Id, Name
                FROM Account
                WITH USER_MODE
                ORDER BY Name
                LIMIT 10
            ];
            List<Account> visibleAccounts = ["""
    outputs[CONTROLLER_PATH] = source.replace(insertion, two_queries, 1).encode("utf-8")

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("MAX_ACCOUNTS = 50", "MAX_ACCOUNTS = 51"),
        ("LIMIT :MAX_ACCOUNTS", "LIMIT 51"),
        ("MAX_CASES = 100", "MAX_CASES = 101"),
        ("LIMIT :MAX_CASES", "LIMIT 101"),
    ),
)
def test_case_contract_enforces_per_object_query_caps(old: str, new: str) -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_CONTROLLER_PATH].decode()
    assert old in source
    outputs[CASE_CONTROLLER_PATH] = source.replace(old, new, 1).encode()

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_case_contract_rejects_one_argument_get_cases_test_call() -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_CONTROLLER_TEST_PATH].decode()
    valid_call = "CaseManagementConsoleController.getCases(null, 'OPEN')"
    assert valid_call in source
    outputs[CASE_CONTROLLER_TEST_PATH] = source.replace(
        valid_call,
        "CaseManagementConsoleController.getCases(null)",
        1,
    ).encode()

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_test_contract"


def test_case_contract_rejects_apex_test_without_all_status_filter_values() -> None:
    outputs = case_management_candidate_outputs()
    source = outputs[CASE_CONTROLLER_TEST_PATH].decode()
    all_call = "CaseManagementConsoleController.getCases(skynetAccount.Id, 'ALL')"
    assert all_call in source
    outputs[CASE_CONTROLLER_TEST_PATH] = source.replace(
        all_call,
        "CaseManagementConsoleController.getCases(skynetAccount.Id, 'OPEN')",
        1,
    ).encode()

    failure = rejected_case_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_test_contract"


def test_candidate_contract_rejects_extra_non_user_mode_query() -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    body, closing_brace = source.rsplit("}", 1)
    outputs[CONTROLLER_PATH] = (
        body
        + """
    private static List<Account> unsafeHelper() {
        return [SELECT Id FROM Account LIMIT 1];
    }
"""
        + closing_brace
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_candidate_contract_rejects_clear_invalid_apex_statement() -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    outputs[CONTROLLER_PATH] = source.replace(
        "public with sharing class AccountContactExplorerController {",
        "public with sharing class AccountContactExplorerController {\n    THIS IS NOT VALID APEX;",
        1,
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


@pytest.mark.parametrize(
    ("unsafe_fallback", "safe_exception"),
    (
        (
            "return new List<Account>();",
            "throw new AuraHandledException('Accounts could not be read.');",
        ),
        (
            "return new List<Contact>();",
            "throw new AuraHandledException('Contacts could not be read.');",
        ),
    ),
)
def test_candidate_contract_rejects_uncontrolled_query_failure(
    unsafe_fallback: str,
    safe_exception: str,
) -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    assert safe_exception in source
    outputs[CONTROLLER_PATH] = source.replace(safe_exception, unsafe_fallback, 1).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,)


@pytest.mark.parametrize(
    "unsafe_exception",
    (
        "throw new AuraHandledException(queryError.getMessage());",
        "throw new AuraHandledException('Query exception while selecting from Account.');",
    ),
)
def test_candidate_contract_rejects_technical_or_dynamic_error_disclosure(
    unsafe_exception: str,
) -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    outputs[CONTROLLER_PATH] = source.replace(
        "throw new AuraHandledException('Accounts could not be read.');",
        unsafe_exception,
        1,
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,)


def _constant_bound_safe_error_source(account_message: str, contact_message: str) -> bytes:
    """Rewrite the controller to the constant + ``setMessage`` + throw-variable
    safe-error shape, declaring class-level ``final String`` message constants.

    This mirrors the idiomatic form a live model (Claude/Sonnet) emits for a
    controlled query failure: ``AuraHandledException e = new AuraHandledException(CONST);
    e.setMessage(CONST); throw e;`` — semantically equivalent to the reference
    ``throw new AuraHandledException('literal');`` the factory ships.
    """

    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    source = source.replace(
        "public with sharing class AccountContactExplorerController {",
        "public with sharing class AccountContactExplorerController {\n"
        f"    private static final String ACCOUNTS_ERROR = '{account_message}';\n"
        f"    private static final String CONTACTS_ERROR = '{contact_message}';",
        1,
    )
    for constant, inline_throw in (
        ("ACCOUNTS_ERROR", "throw new AuraHandledException('Accounts could not be read.');"),
        ("CONTACTS_ERROR", "throw new AuraHandledException('Contacts could not be read.');"),
    ):
        assert inline_throw in source
        source = source.replace(
            inline_throw,
            f"AuraHandledException handled = new AuraHandledException({constant});\n"
            f"                        handled.setMessage({constant});\n"
            f"                        throw handled;",
            1,
        )
    return source.encode()


_SAFE_EXCEPTION_HELPER = """    private static AuraHandledException buildSafeException(
        String safeMessage
    ) {
        AuraHandledException safeException = new AuraHandledException(safeMessage);
        safeException.setMessage(safeMessage);
        return safeException;
    }
"""


def _helper_bound_safe_error_outputs() -> dict[str, bytes]:
    """Reproduce the safe controller shape emitted in run 07be attempt 2."""

    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    source = source.replace(
        "public with sharing class AccountContactExplorerController {",
        "public with sharing class AccountContactExplorerController {\n"
        "    private static final String ACCOUNTS_LOAD_ERROR =\n"
        "        'Unable to load accounts right now. Please try again later.';\n"
        "    private static final String CONTACTS_LOAD_ERROR =\n"
        "        'Unable to load contacts right now. Please try again later.';",
        1,
    )
    source = source.replace(
        "throw new AuraHandledException('Accounts could not be read.');",
        "throw buildSafeException(ACCOUNTS_LOAD_ERROR);",
        1,
    )
    source = source.replace(
        "throw new AuraHandledException('Contacts could not be read.');",
        "throw buildSafeException(CONTACTS_LOAD_ERROR);",
        1,
    )
    source = source.replace("ORDER BY Name", "ORDER BY Name ASC", 1)
    source = source.replace(
        "ORDER BY LastName, FirstName",
        "ORDER BY LastName ASC, FirstName ASC",
        1,
    )
    source = source.replace(
        "@AuraEnabled\n    public static List<Contact>",
        "@AuraEnabled(cacheable=false)\n    public static List<Contact>",
        1,
    )
    body, closing_brace = source.rsplit("}", 1)
    outputs[CONTROLLER_PATH] = (body + _SAFE_EXCEPTION_HELPER + "}" + closing_brace).encode()
    return outputs


def test_candidate_contract_accepts_attempt_two_safe_exception_helper_shape() -> None:
    outputs = _helper_bound_safe_error_outputs()

    with candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(workspace.root)

    assert result["passed"] is True
    assert result["apex_tests_prepared"] is True


@pytest.mark.parametrize(
    "contact_order",
    (
        "LastName ASC, FirstName",
        "LastName, FirstName ASC",
        "LastName ASC, FirstName ASC",
    ),
)
def test_candidate_contract_accepts_equivalent_explicit_ascending_order(
    contact_order: str,
) -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    source = source.replace("ORDER BY Name", "ORDER BY Name ASC", 1)
    outputs[CONTROLLER_PATH] = source.replace(
        "ORDER BY LastName, FirstName",
        f"ORDER BY {contact_order}",
        1,
    ).encode()

    with candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(workspace.root)

    assert result["passed"] is True


@pytest.mark.parametrize(
    ("valid_order", "invalid_order"),
    (
        ("LastName, FirstName", "LastName DESC, FirstName ASC"),
        ("LastName, FirstName", "FirstName ASC, LastName ASC"),
        ("LastName, FirstName", "LastName ASC, FirstName ASC, Id ASC"),
        ("LastName, FirstName", "LOWER(LastName), FirstName ASC"),
        ("LastName, FirstName", "LastName ASC NULLS LAST, FirstName ASC"),
        ("Name", "Name, CreatedDate"),
    ),
)
def test_candidate_contract_rejects_noncanonical_or_extended_order_clause(
    valid_order: str,
    invalid_order: str,
) -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    assert f"ORDER BY {valid_order}" in source
    outputs[CONTROLLER_PATH] = source.replace(
        f"ORDER BY {valid_order}",
        f"ORDER BY {invalid_order}",
        1,
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_candidate_contract_accepts_constant_bound_safe_error_via_setmessage() -> None:
    outputs = salesforce_candidate_outputs()
    outputs[CONTROLLER_PATH] = _constant_bound_safe_error_source(
        "Accounts could not be read.",
        "Contacts could not be read.",
    )

    with candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(workspace.root)

    assert result["passed"] is True
    assert result["apex_tests_prepared"] is True


@pytest.mark.parametrize(
    ("old", "new"),
    (
        (
            "private static AuraHandledException buildSafeException(",
            "public static AuraHandledException buildSafeException(",
        ),
        (
            "private static AuraHandledException buildSafeException(",
            "private AuraHandledException buildSafeException(",
        ),
        (
            "    ) {\n        AuraHandledException safeException =",
            "    ) {\n        safeMessage = 'Changed safe message.';\n"
            "        AuraHandledException safeException =",
        ),
        (
            "    ) {\n        AuraHandledException safeException =",
            "    ) {\n        System.debug(safeMessage);\n"
            "        AuraHandledException safeException =",
        ),
        (
            "        AuraHandledException safeException = new AuraHandledException(safeMessage);",
            "        if (String.isBlank(safeMessage)) {\n"
            "            return new AuraHandledException('Safe fallback.');\n"
            "        }\n"
            "        AuraHandledException safeException = new AuraHandledException(safeMessage);",
        ),
        (
            "safeException.setMessage(safeMessage);",
            "safeException.setMessage('Different safe message.');",
        ),
        (
            "return safeException;",
            "return new AuraHandledException(safeMessage);",
        ),
        (
            "AuraHandledException safeException = new AuraHandledException(safeMessage);\n"
            "        safeException.setMessage(safeMessage);\n"
            "        return safeException;",
            "AuraHandledException safeMessage = new AuraHandledException(safeMessage);\n"
            "        safeMessage.setMessage(safeMessage);\n"
            "        return safeMessage;",
        ),
    ),
)
def test_candidate_contract_rejects_unbounded_safe_exception_helper_body(
    old: str,
    new: str,
) -> None:
    outputs = _helper_bound_safe_error_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    assert old in source
    outputs[CONTROLLER_PATH] = source.replace(old, new, 1).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,)


@pytest.mark.parametrize(
    "unsafe_argument",
    (
        "queryError",
        "queryError.getMessage()",
        "'Query exception while selecting from Account.'",
        "ACCOUNTS_LOAD_ERROR + ' Retry.'",
    ),
)
def test_candidate_contract_rejects_unsafe_safe_helper_argument(
    unsafe_argument: str,
) -> None:
    outputs = _helper_bound_safe_error_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    outputs[CONTROLLER_PATH] = source.replace(
        "buildSafeException(ACCOUNTS_LOAD_ERROR)",
        f"buildSafeException({unsafe_argument})",
        1,
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,)


def test_candidate_contract_rejects_technical_safe_helper_constant() -> None:
    outputs = _helper_bound_safe_error_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    outputs[CONTROLLER_PATH] = source.replace(
        "Unable to load accounts right now. Please try again later.",
        "Account query exception while selecting from Account.",
        1,
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,)


def test_candidate_contract_rejects_shadowed_safe_helper_constant() -> None:
    outputs = _helper_bound_safe_error_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    outputs[CONTROLLER_PATH] = source.replace(
        "    public static List<Account> getAccounts() {\n        try {",
        "    public static List<Account> getAccounts() {\n"
        "        String ACCOUNTS_LOAD_ERROR = 'A different local message.';\n"
        "        try {",
        1,
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,)


def test_candidate_contract_rejects_safe_helper_overload() -> None:
    outputs = _helper_bound_safe_error_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    body, closing_brace = source.rsplit("}", 1)
    outputs[CONTROLLER_PATH] = (
        body
        + """    private static AuraHandledException buildSafeException(
        String firstMessage,
        String secondMessage
    ) {
        return new AuraHandledException(firstMessage);
    }
"""
        + "}"
        + closing_brace
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,)


def test_candidate_contract_rejects_unrelated_safe_helper_call_site() -> None:
    outputs = _helper_bound_safe_error_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    outputs[CONTROLLER_PATH] = source.replace(
        "    public static List<Account> getAccounts() {\n        try {",
        "    public static List<Account> getAccounts() {\n"
        "        buildSafeException(ACCOUNTS_LOAD_ERROR);\n"
        "        try {",
        1,
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,)


def test_candidate_contract_rejects_constant_bound_technical_error_message() -> None:
    # A leaky class-level constant must not launder a technical disclosure past
    # the widened check: the referenced name never enters the safe-constant set.
    outputs = salesforce_candidate_outputs()
    outputs[CONTROLLER_PATH] = _constant_bound_safe_error_source(
        "Reading accounts raised an exception.",
        "Contacts could not be read.",
    )

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,)


def test_candidate_contract_rejects_dynamic_setmessage_in_constructed_exception() -> None:
    # The construction form still forbids a dynamic message on any span — here a
    # setMessage that echoes the caught exception's text.
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    source = source.replace(
        "public with sharing class AccountContactExplorerController {",
        "public with sharing class AccountContactExplorerController {\n"
        "    private static final String ACCOUNTS_ERROR = 'Accounts could not be read.';",
        1,
    )
    source = source.replace(
        "throw new AuraHandledException('Accounts could not be read.');",
        "AuraHandledException handled = new AuraHandledException(ACCOUNTS_ERROR);\n"
        "                        handled.setMessage(queryError.getMessage());\n"
        "                        throw handled;",
        1,
    )
    outputs[CONTROLLER_PATH] = source.encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,)


def test_candidate_contract_rejects_reference_to_undeclared_error_constant() -> None:
    # A bare identifier that resolves to no known-safe constant is rejected, so a
    # typo or out-of-scope reference cannot slip an unaudited message through.
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    source = source.replace(
        "throw new AuraHandledException('Accounts could not be read.');",
        "throw new AuraHandledException(UNDECLARED_ERROR);",
        1,
    )
    outputs[CONTROLLER_PATH] = source.encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"
    assert failure.diagnostic_ids == (APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,)


@pytest.mark.parametrize(
    "removed_clause",
    (
        "WHERE AccountId = :accountId",
        "ORDER BY LastName, FirstName",
        "LIMIT :MAX_CONTACTS",
    ),
)
def test_candidate_contract_rejects_incomplete_method_bound_contact_query(
    removed_clause: str,
) -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    assert removed_clause in source
    outputs[CONTROLLER_PATH] = source.replace(removed_clause, "", 1).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_candidate_contract_rejects_contact_query_missing_visible_field() -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    outputs[CONTROLLER_PATH] = source.replace(
        "SELECT Id, FirstName, LastName, Email, Phone",
        "SELECT Id, FirstName, LastName, Email",
        1,
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


@pytest.mark.parametrize(
    "invalid_guard",
    (
        "if (String.isBlank(accountId))",
        "if (accountId != null)",
    ),
)
def test_candidate_contract_rejects_invalid_contact_null_guard(invalid_guard: str) -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_PATH].decode()
    outputs[CONTROLLER_PATH] = source.replace("if (accountId == null)", invalid_guard, 1).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_controller_contract"


@pytest.mark.parametrize(
    "statement",
    (
        "if (accountId != null) { insert new Account(); }",
        "if (accountId != null) { update new Account(); }",
        "if (accountId != null) { upsert new Account(); }",
        "if (accountId != null) { delete new Account(); }",
        "if (accountId != null) { undelete new Account(); }",
        "if (accountId != null) { merge new Account(), new Account(); }",
        (
            "Database.queryWithBinds('SELECT Id FROM Account', "
            "new Map<String, Object>(), AccessLevel.USER_MODE);"
        ),
        "Database.query('SELECT Id FROM Account');",
        "HttpRequest request = new HttpRequest();",
        "String access_token = 'synthetic-but-forbidden';",
        "String endpoint = 'https://example.invalid/service';",
    ),
)
def test_candidate_contract_rejects_controller_capabilities(statement: str) -> None:
    failure = rejected_candidate(outputs_with_controller_statement(statement))

    assert failure.failure_code == "salesforce_apex_controller_contract"


def test_candidate_contract_ignores_harmless_controller_comments_and_strings() -> None:
    outputs = outputs_with_controller_statement(
        "String harmless = 'insert update Database.queryWithBinds HttpRequest'; "
        "// delete Database.merge(new Account())"
    )

    with candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(workspace.root)

    assert result["security"] == "with-sharing-user-mode-read-only"


@pytest.mark.parametrize(
    "mutation",
    (
        "@IsTest(SeeAllData=true)",
        "Database.query('SELECT Id FROM Account')",
        "new HttpRequest()",
        "String authorization = 'Bearer fake'",
        "String endpoint = 'https://example.invalid/test'",
    ),
)
def test_candidate_contract_rejects_unsafe_apex_test_source(mutation: str) -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_TEST_PATH].decode()
    if mutation.startswith("@IsTest"):
        source = source.replace("@IsTest\nprivate class", f"{mutation}\nprivate class", 1)
    else:
        body, closing_brace = source.rsplit("}", 1)
        source = (
            body
            + f"""
    @IsTest
    static void unsafeCapabilityProbe() {{
        Object unsafeValue = {mutation};
        Assert.isNotNull(unsafeValue);
    }}
"""
            + closing_brace
        )
    outputs[CONTROLLER_TEST_PATH] = source.encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_test_contract"


@pytest.mark.parametrize("method_name", ("getAccounts", "getContacts"))
def test_candidate_contract_requires_generated_tests_to_call_public_methods(
    method_name: str,
) -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_TEST_PATH].decode()
    outputs[CONTROLLER_TEST_PATH] = source.replace(
        f"AccountContactExplorerController.{method_name}",
        f"AccountContactExplorerController.unrelated{method_name}",
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_test_contract"


def test_candidate_contract_requires_distinct_populated_empty_and_null_apex_scenarios() -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_TEST_PATH].decode()
    outputs[CONTROLLER_TEST_PATH] = source.replace(
        "AccountContactExplorerController.getContacts(null)",
        "AccountContactExplorerController.getContacts(emptyAccount.Id)",
        1,
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_apex_test_contract"


@pytest.mark.parametrize(
    "injection",
    (
        'import extra from "unapproved-module";\n',
        "export { ModelChosenExplorer };\n",
        "const execute = eval;\n",
        "window.alert('unsafe');\n",
        "const prototype = Object.getPrototypeOf({});\n",
        (
            "const { ['con' + 'structor']: evaluator } = Object.freeze(() => {});\n"
            "const runtimeObject = evaluator('return this')();\n"
        ),
        "const promise = import('unapproved-module');\n",
        "const hidden = `${eval('1')}`;\n",
        "fetch('/external');\n",
    ),
)
def test_candidate_contract_rejects_unapproved_lwc_capabilities(injection: str) -> None:
    outputs = structurally_distinct_safe_outputs()
    outputs[LWC_JAVASCRIPT_PATH] = (injection + outputs[LWC_JAVASCRIPT_PATH].decode()).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_lwc_javascript_contract"
    assert failure.diagnostic_ids == (
        ("salesforce_lwc_javascript_contract",)
        if injection.startswith("export")
        else ("lwc_forbidden_runtime_capability",)
    )


def test_candidate_contract_allows_capability_words_in_lwc_comments_and_strings() -> None:
    outputs = structurally_distinct_safe_outputs()
    source = outputs[LWC_JAVASCRIPT_PATH].decode()
    outputs[LWC_JAVASCRIPT_PATH] = (
        "const harmlessText = 'window eval process fetch';\n// globalThis.fetch()\n" + source
    ).encode()

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


@pytest.mark.parametrize(
    "mutate",
    (
        lambda source: "private requestGeneration = 0;\n" + source,
        lambda source: "@api requestGeneration = 0;\n" + source,
        lambda source: source.replace(
            "[observe, loadAccounts, loadContacts]",
            "[observe, unusedAccountLoader, loadContacts]",
            1,
        ),
    ),
)
def test_candidate_contract_rejects_non_executable_lwc_javascript(
    mutate: Any,
) -> None:
    outputs = structurally_distinct_safe_outputs()
    source = outputs[LWC_JAVASCRIPT_PATH].decode()
    mutated = mutate(source)
    assert mutated != source
    outputs[LWC_JAVASCRIPT_PATH] = mutated.encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_lwc_javascript_contract"


@pytest.mark.parametrize(
    "test_source",
    (
        "it.skip('skipped', () => {});",
        "it.todo('todo');",
        "xit('disabled', () => {});",
        "pending(); it('present', () => {});",
        "describe.only('focused', () => { it('present', () => {}); });",
        "test.only('focused', () => {});",
    ),
)
def test_candidate_contract_rejects_skipped_todo_pending_or_focused_jest(
    test_source: str,
) -> None:
    outputs = structurally_distinct_safe_outputs()
    outputs[LWC_TEST_PATH] = test_source.encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_lwc_jest_contract"


@pytest.mark.parametrize(
    ("injection", "expected_diagnostics"),
    (
        (
            "const files = require('node:fs');\n",
            (JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID, "jest_forbidden_capability"),
        ),
        (
            "import { readFileSync } from 'fs';\n",
            (JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID, "jest_forbidden_capability"),
        ),
        (
            "import {\n    readFileSync\n} from 'node:fs';\n",
            (JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID, "jest_forbidden_capability"),
        ),
        (
            "const files = jest.requireActual('node:fs');\n",
            ("jest_forbidden_capability",),
        ),
        (
            "const files = jest.requireMock('node:fs');\n",
            ("jest_forbidden_capability",),
        ),
        (
            "const files = jest.createMockFromModule('node:fs');\n",
            ("jest_forbidden_capability",),
        ),
        (
            "jest.setMock('node:fs', {});\n",
            ("jest_forbidden_capability",),
        ),
        (
            "jest.unstable_mockModule('node:fs', () => ({}));\n",
            ("jest_forbidden_capability",),
        ),
        ("const stop = process.exit;\n", ("jest_forbidden_capability",)),
        ("globalThis.it = () => {};\n", ("jest_forbidden_capability",)),
        ("const execute = eval;\n", ("jest_forbidden_capability",)),
        ("fetch('/external');\n", ("jest_forbidden_capability",)),
        ("const loader = require;\n", ("jest_forbidden_capability",)),
    ),
)
def test_candidate_contract_rejects_dangerous_jest_capabilities(
    injection: str,
    expected_diagnostics: tuple[str, ...],
) -> None:
    outputs = structurally_distinct_safe_outputs()
    jest_globals_import, remaining_source = outputs[LWC_TEST_PATH].decode().split("\n", 1)
    outputs[LWC_TEST_PATH] = (jest_globals_import + "\n" + injection + remaining_source).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_lwc_jest_contract"
    assert failure.diagnostic_ids == expected_diagnostics


@pytest.mark.parametrize(
    "injection",
    (
        "import helper from 'candidate-helper';\n",
        (
            "const getAccounts = require("
            "'@salesforce/apex/AccountContactExplorerController.getAccounts');\n"
        ),
    ),
)
def test_candidate_contract_reports_unapproved_jest_module_target(injection: str) -> None:
    outputs = structurally_distinct_safe_outputs()
    jest_globals_import, remaining_source = outputs[LWC_TEST_PATH].decode().split("\n", 1)
    outputs[LWC_TEST_PATH] = (jest_globals_import + "\n" + injection + remaining_source).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_lwc_jest_contract"
    assert failure.diagnostic_ids == (JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID,)


def test_candidate_contract_leaves_jest_globals_compatibility_to_runtime() -> None:
    outputs = structurally_distinct_safe_outputs()
    source = outputs[LWC_TEST_PATH].decode()
    outputs[LWC_TEST_PATH] = source.replace(
        "describe, expect, it, jest",
        "expect, it, jest",
        1,
    ).encode()

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_allows_arbitrary_jest_titles_and_helper_names() -> None:
    outputs = structurally_distinct_safe_outputs()
    source = outputs[LWC_TEST_PATH].decode()
    outputs[LWC_TEST_PATH] = (
        source.replace(
            "candidate-authored outcomes",
            "model chose this organization",
            1,
        )
        .replace(
            "uses a completely different safe title and structure",
            "proves a user outcome under an arbitrary title",
            1,
        )
        .replace("element", "modelChosenElement")
        .encode()
    )

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_accepts_parameterized_jest_with_inline_data() -> None:
    outputs = structurally_distinct_safe_outputs()
    source = (
        outputs[LWC_TEST_PATH]
        .decode()
        .replace(
            "describe, expect, it, jest",
            "describe, expect, it, jest, test",
            1,
        )
    )
    source += """

const requiredAccounts = [{ Id: '001000000000001AAA', Name: 'Inline Account' }];
test.each(requiredAccounts)('accepts candidate row %s', (account) => {
    expect(account.Id).toMatch(/^001/);
});
"""
    outputs[LWC_TEST_PATH] = source.encode()

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_accepts_static_sfdx_lwc_jest_import() -> None:
    outputs = structurally_distinct_safe_outputs()
    original = outputs[LWC_TEST_PATH].decode()
    jest_globals_import, remaining_source = original.split("\n", 1)
    source = (
        jest_globals_import
        + "\nimport { createApexTestWireAdapter } from '@salesforce/sfdx-lwc-jest';\n"
        + remaining_source
        + """

it('uses the public wire adapter helper', () => {
    expect(typeof createApexTestWireAdapter).toBe('function');
});
"""
    )
    outputs[LWC_TEST_PATH] = source.encode()

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_rejects_jest_globals_import_after_component_loading() -> None:
    outputs = structurally_distinct_safe_outputs()
    source = outputs[LWC_TEST_PATH].decode()
    import_block, remainder = source.split("\n\n", 1)
    import_lines = import_block.splitlines()
    assert "from '@jest/globals'" in import_lines[0]
    outputs[LWC_TEST_PATH] = (
        "\n".join((*import_lines[1:], import_lines[0])) + "\n\n" + remainder
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_lwc_jest_contract"
    assert failure.diagnostic_ids == (JEST_GLOBALS_IMPORT_ORDER_DIAGNOSTIC_ID,)


def test_candidate_contract_ignores_skip_and_capability_words_in_jest_text() -> None:
    outputs = structurally_distinct_safe_outputs()
    source = (
        outputs[LWC_TEST_PATH].decode()
        + """

// it.skip('commented out', () => {});
describe('safe words', () => {
    it('mentions todo, pending, process and eval in its title', () => {
        const explanation = 'process, eval, jest.requireActual("fs") and import x from "fs" are only words';
        expect(explanation).toContain('words');
    });
});
"""
    )
    outputs[LWC_TEST_PATH] = source.encode()

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_rejects_empty_jest_source() -> None:
    outputs = structurally_distinct_safe_outputs()
    outputs[LWC_TEST_PATH] = b"describe('empty', () => {});\n"

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_lwc_jest_contract"


@pytest.mark.parametrize(
    "mutate",
    (
        lambda source: source.replace("createElement(", "document.createElement(", 1),
        lambda source: source.replace(
            "import ModelChosenExplorer from 'c/accountContactExplorer';\n",
            "",
            1,
        ),
    ),
)
def test_candidate_contract_leaves_lwc_jest_harness_shape_to_runtime(mutate: Any) -> None:
    outputs = structurally_distinct_safe_outputs()
    source = outputs[LWC_TEST_PATH].decode()
    mutated = mutate(source)
    assert mutated != source
    outputs[LWC_TEST_PATH] = mutated.encode()

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


@pytest.mark.parametrize(
    "mutate",
    (
        lambda source: source.replace("__esModule: true,", "", 1),
        lambda source: source.replace("default: jest.fn()", "named: jest.fn()", 1),
        lambda source: source.replace("{ virtual: true }", "{}", 1),
    ),
)
def test_candidate_contract_leaves_apex_mock_shape_to_runtime(mutate: Any) -> None:
    outputs = structurally_distinct_safe_outputs()
    source = outputs[LWC_TEST_PATH].decode()
    mutated = mutate(source)
    assert mutated != source
    outputs[LWC_TEST_PATH] = mutated.encode()

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_accepts_inline_synthetic_jest_data() -> None:
    outputs = structurally_distinct_safe_outputs()
    source = outputs[LWC_TEST_PATH].decode()
    outputs[LWC_TEST_PATH] = (
        "const inlineAccounts = [{ Id: '001000000000030AAA', Name: 'Inline' }];\n" + source
    ).encode()

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


@pytest.mark.parametrize(
    ("path", "injection"),
    (
        (LWC_TEMPLATE_PATH, "<script>unsafe()</script>"),
        (LWC_TEMPLATE_PATH, '<iframe src="local"></iframe>'),
        (LWC_TEMPLATE_PATH, '<div lwc:dom="manual"></div>'),
        (LWC_STYLES_PATH, "@import 'theme.css';"),
        (LWC_STYLES_PATH, "section { background: url(asset.png); }"),
    ),
)
def test_candidate_contract_rejects_template_and_css_capabilities(
    path: str,
    injection: str,
) -> None:
    outputs = structurally_distinct_safe_outputs()
    source = outputs[path].decode()
    if path == LWC_TEMPLATE_PATH:
        source = source.replace("</template>", f"{injection}\n</template>")
    else:
        source += f"\n{injection}\n"
    outputs[path] = source.encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code in {
        "salesforce_lwc_template_contract",
        "salesforce_lwc_styles_contract",
    }


def test_candidate_contract_rejects_unapproved_permission_capability() -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[PERMISSION_SET_PATH].decode()
    outputs[PERMISSION_SET_PATH] = source.replace(
        "</PermissionSet>",
        """    <userPermissions>
        <enabled>true</enabled>
        <name>ModifyAllData</name>
    </userPermissions>
</PermissionSet>""",
        1,
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_permission_set_contract"


def test_candidate_contract_rejects_manifest_outside_exact_dependency_closure() -> None:
    outputs = salesforce_candidate_outputs()
    source = outputs[MANIFEST_PATH].decode()
    outputs[MANIFEST_PATH] = source.replace(
        "<members>AccountContactExplorerController</members>",
        (
            "<members>AccountContactExplorerController</members>\n"
            "        <members>UnapprovedController</members>"
        ),
        1,
    ).encode()

    failure = rejected_candidate(outputs)

    assert failure.failure_code == "salesforce_manifest_contract"


@pytest.mark.parametrize("suffix", ("/* unterminated", "String value = 'unterminated"))
def test_candidate_contract_fails_closed_on_unterminated_apex_lexemes(suffix: str) -> None:
    outputs = salesforce_candidate_outputs()
    outputs[CONTROLLER_PATH] = (outputs[CONTROLLER_PATH].decode() + f"\n{suffix}").encode()

    assert rejected_candidate(outputs).failure_code == "salesforce_apex_controller_contract"


def test_candidate_contract_rejects_xml_entities_with_controlled_failure() -> None:
    outputs = salesforce_candidate_outputs()
    outputs[MANIFEST_PATH] = (
        b'<?xml version="1.0"?><!DOCTYPE Package [<!ENTITY leak SYSTEM "file:///etc/passwd">]>'
        b'<Package xmlns="http://soap.sforce.com/2006/04/metadata">&leak;</Package>'
    )

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure, match="local contract assertion failed"):
            check_salesforce_candidate(workspace.root)


def test_candidate_contract_cli_emits_only_controlled_stage_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "planted-secret-that-must-not-cross-the-validator-boundary"
    outputs = salesforce_candidate_outputs()
    source = outputs[CONTROLLER_TEST_PATH].decode()
    body, closing_brace = source.rsplit("}", 1)
    outputs[CONTROLLER_TEST_PATH] = (
        body
        + f"""
    private static String access_token = '{secret}';
"""
        + closing_brace
    ).encode()

    with candidate_from_memory(outputs) as workspace:
        workspace_root = str(workspace.root)
        monkeypatch.chdir(workspace.root)
        assert main(["candidate-contract"]) == 1
        serialized = capsys.readouterr().out

    assert json.loads(serialized) == {
        "check": "candidate-contract",
        "diagnostic_ids": ["salesforce_apex_test_contract"],
        "failure_code": "salesforce_apex_test_contract",
        "failure_type": "LocalCheckFailure",
        "passed": False,
    }
    assert secret not in serialized
    assert workspace_root not in serialized


def test_toolchain_contract_returns_pinned_digests_without_candidate_inspection(
    tmp_path: Path,
) -> None:
    toolchain = tmp_path / "tooling" / "lwc-jest"
    shutil.copytree(TOOLCHAIN, toolchain)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "package.json").write_text(
        json.dumps({"devDependencies": {"@salesforce/sfdx-lwc-jest": "latest"}}),
        encoding="utf-8",
    )
    (candidate / "package-lock.json").write_text("not json\n", encoding="utf-8")
    (candidate / "secret.txt").write_text("do not inspect\n", encoding="utf-8")

    result = check_lwc_jest_toolchain(toolchain)

    assert result["version"] == "7.9.0"
    assert result["locked_packages"] == 561
    assert result["candidate_content_inspected"] is False
    assert result["network_accessed"] is False
    assert result["install_performed"] is False
    assert result["lwc_jest_executed"] is False
    assert result["manifest_sha256"] == LWC_JEST_TOOLCHAIN_DIGESTS["package.json"]
    assert result["lock_sha256"] == LWC_JEST_TOOLCHAIN_DIGESTS["package-lock.json"]
    assert result["config_sha256"] == LWC_JEST_TOOLCHAIN_DIGESTS["jest.config.js"]
    assert result["setup_sha256"] == LWC_JEST_TOOLCHAIN_DIGESTS["jest.setup.js"]
    assert (
        result["controller_test_sha256"]
        == LWC_JEST_TOOLCHAIN_DIGESTS["controller-tests/accountContactExplorer.controller.test.js"]
    )
    for path, key in (
        (toolchain / "package.json", "manifest_sha256"),
        (toolchain / "package-lock.json", "lock_sha256"),
        (toolchain / "jest.config.js", "config_sha256"),
        (toolchain / "jest.setup.js", "setup_sha256"),
        (
            toolchain / "controller-tests/accountContactExplorer.controller.test.js",
            "controller_test_sha256",
        ),
    ):
        expected = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        assert result[key] == expected


def test_case_toolchain_contract_returns_pinned_case_controller_digest(
    tmp_path: Path,
) -> None:
    toolchain = tmp_path / "tooling" / "lwc-jest"
    shutil.copytree(TOOLCHAIN, toolchain)

    result = check_lwc_jest_toolchain(
        toolchain,
        unit_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID,
    )

    assert result["passed"] is True
    assert result["version"] == "7.9.0"
    assert result["locked_packages"] == 561
    assert result["candidate_content_inspected"] is False
    assert result["network_accessed"] is False
    assert result["install_performed"] is False
    assert result["lwc_jest_executed"] is False
    # The Case unit shares the pinned toolchain manifest, lock, config, and setup
    # with the account/contact unit and pins its own controller-owned suite.
    assert result["manifest_sha256"] == LWC_JEST_TOOLCHAIN_DIGESTS["package.json"]
    assert result["lock_sha256"] == LWC_JEST_TOOLCHAIN_DIGESTS["package-lock.json"]
    assert result["config_sha256"] == LWC_JEST_TOOLCHAIN_DIGESTS["jest.config.js"]
    assert result["setup_sha256"] == LWC_JEST_TOOLCHAIN_DIGESTS["jest.setup.js"]
    case_suite = toolchain / CASE_LWC_CONTROLLER_TEST_PATH
    expected_case_digest = f"sha256:{hashlib.sha256(case_suite.read_bytes()).hexdigest()}"
    assert result["controller_test_sha256"] == expected_case_digest
    assert (
        result["controller_test_sha256"]
        != LWC_JEST_TOOLCHAIN_DIGESTS["controller-tests/accountContactExplorer.controller.test.js"]
    )


def test_toolchain_contract_rejects_lock_tampering(tmp_path: Path) -> None:
    toolchain = tmp_path / "lwc-jest"
    shutil.copytree(TOOLCHAIN, toolchain)
    lock_path = toolchain / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["packages"]["node_modules/@salesforce/sfdx-lwc-jest"]["version"] = "7.9.1"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(LocalCheckFailure):
        check_lwc_jest_toolchain(toolchain)


def test_toolchain_contract_rejects_controller_suite_tampering(tmp_path: Path) -> None:
    toolchain = tmp_path / "lwc-jest"
    shutil.copytree(TOOLCHAIN, toolchain)
    controller_test = toolchain / "controller-tests/accountContactExplorer.controller.test.js"
    controller_test.write_text("it('forged', () => {});\n", encoding="utf-8")

    with pytest.raises(LocalCheckFailure):
        check_lwc_jest_toolchain(toolchain)


def test_toolchain_contract_rejects_symlinked_config(tmp_path: Path) -> None:
    toolchain = tmp_path / "lwc-jest"
    shutil.copytree(TOOLCHAIN, toolchain)
    config = toolchain / "jest.config.js"
    config.unlink()
    config.symlink_to(TOOLCHAIN / "jest.config.js")

    with pytest.raises(LocalCheckFailure, match="symlink"):
        check_lwc_jest_toolchain(toolchain)


def test_workspace_revision_rejects_mutation() -> None:
    with candidate_from_memory(salesforce_candidate_outputs()) as workspace:
        before = tree_fingerprint(workspace.root)
        source = (workspace.root / CONTROLLER_PATH).read_text(encoding="utf-8")
        workspace.write_text(CONTROLLER_PATH, f"{source}\n")

        with pytest.raises(LocalCheckFailure):
            check_workspace_revision(workspace.root, before)
