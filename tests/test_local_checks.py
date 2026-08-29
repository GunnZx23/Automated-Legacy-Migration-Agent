from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
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
    CASE_LWC_CONTROLLER_TEST_PATH,
    CASE_MANAGEMENT_CONSOLE_UNIT_ID,
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
    tree_fingerprint,
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

    @AuraEnabled(cacheable=true)
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
    assert "inline in the test file" in contract
    assert "stable data-role values" in contract
    assert "Salesforce API 67 supports complex template expressions" in contract
    assert "maintainability convention, not a compiler restriction" in contract
    assert "public static cacheable methods" in contract
    assert (
        "AuraHandledException whose sole argument is a fixed safe, nontechnical string" in contract
    )
    assert "cap each query at 1 through 200 rows" in contract
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


def test_case_management_candidate_passes_static_and_closure_checks() -> None:
    outputs = case_management_candidate_outputs()

    with case_candidate_from_memory(outputs) as workspace:
        result = check_salesforce_candidate(
            workspace.root, unit_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID
        )
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
        != LWC_JEST_TOOLCHAIN_DIGESTS[
            "controller-tests/accountContactExplorer.controller.test.js"
        ]
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
