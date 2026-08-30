"""Read-only, candidate-specific Salesforce validation commands.

The supported Salesforce contract validates generated solution artifacts only.
The separately pinned LWC Jest toolchain has its own integrity contract, so a
model never needs to generate a dependency manifest or lockfile.

These checks never contact a Salesforce org and never write to either tree.
Their small JSON summaries are controlled output: raw source text, absolute
paths, parser messages, and secrets are not emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast
from xml.etree import ElementTree

from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.workspace import content_revision, snapshot_tree
from legacy_migration_agent.graphs.dependency_graph import (
    NodeKind,
    build_salesforce_dependency_graph,
)

API_VERSION = "67.0"
METADATA_NAMESPACE = "http://soap.sforce.com/2006/04/metadata"
TARGET_ENTRY = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
SALESFORCE_ACCOUNT_CONTACT_UNIT_ID: Final = "salesforce-vf-to-lwc"

LWC_JEST_PACKAGE = "@salesforce/sfdx-lwc-jest"
LWC_JEST_VERSION = "7.9.0"
LWC_JEST_TOOLCHAIN_NAME = "legacy-migration-agent-lwc-jest-toolchain"
LWC_CONTROLLER_TEST_PATH = "controller-tests/accountContactExplorer.controller.test.js"
LWC_JEST_SETUP_PATH = "jest.setup.js"
LWC_JEST_TOOLCHAIN_DIGESTS = {
    "package.json": "sha256:61bb618b82bd43432a7027abe57c66bd28c485b09e844272e25b122353fbc6db",
    "package-lock.json": (
        "sha256:8ad213914b4fb568c7a1de2bb22ce81d6ce804adc098776d47940a07cda6758c"
    ),
    "jest.config.js": "sha256:e936811245d9d04f6db2157d85ea0cb5b4a62433be47ac7a86bf99cc277ecfdb",
    LWC_JEST_SETUP_PATH: "sha256:55ccbb4ec76acbfca5ee8e18d26ef5dcfa1eaa812a31648b38f2c2443e38e745",
    LWC_CONTROLLER_TEST_PATH: (
        "sha256:cf984b2daacd99366aacee5583d7d4e2e6343bd8159e97a31a996636abc69bb3"
    ),
}

MANIFEST_PATH = "manifest/package.xml"
CONTROLLER_PATH = "force-app/main/default/classes/AccountContactExplorerController.cls"
CONTROLLER_METADATA_PATH = f"{CONTROLLER_PATH}-meta.xml"
CONTROLLER_TEST_PATH = "force-app/main/default/classes/AccountContactExplorerControllerTest.cls"
CONTROLLER_TEST_METADATA_PATH = f"{CONTROLLER_TEST_PATH}-meta.xml"
LWC_ROOT = "force-app/main/default/lwc/accountContactExplorer"
LWC_HTML_PATH = f"{LWC_ROOT}/accountContactExplorer.html"
LWC_JAVASCRIPT_PATH = f"{LWC_ROOT}/accountContactExplorer.js"
LWC_CSS_PATH = f"{LWC_ROOT}/accountContactExplorer.css"
LWC_METADATA_PATH = f"{LWC_ROOT}/accountContactExplorer.js-meta.xml"
LWC_TEST_PATH = f"{LWC_ROOT}/__tests__/accountContactExplorer.test.js"
PERMISSION_SET_PATH = (
    "force-app/main/default/permissionsets/AccountContactExplorerUser.permissionset-meta.xml"
)
LWC_SEMANTIC_DATA_ROLES: Final[frozenset[str]] = frozenset(
    {"account-selector", "load-contacts", "contact-results"}
)
LWC_SEMANTIC_DATA_STATES: Final[frozenset[str]] = frozenset({"loading", "empty"})
_LWC_SIMPLE_BINDING_BODY_PATTERN: Final = (
    r"[A-Za-z_$][A-Za-z0-9_$]{0,63}"
    r"(?:\.[A-Za-z_$][A-Za-z0-9_$]{0,63}){0,7}"
)
_LWC_SEMANTIC_BINDING_PATTERN: Final = rf"\{{\s*{_LWC_SIMPLE_BINDING_BODY_PATTERN}\s*\}}"

APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID: Final = "apex_public_interface_annotation_mismatch"
APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID: Final = "apex_controlled_query_error_missing"
JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID: Final = "jest_unapproved_module_target"
JEST_GLOBALS_IMPORT_ORDER_DIAGNOSTIC_ID: Final = "jest_globals_import_order"
LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID: Final = "lwc_template_binding_invalid"
CONTROLLER_JEST_ACCOUNT_CHANGE_RESET_DIAGNOSTIC_ID: Final = "controller_jest_account_change_reset"
CONTROLLER_JEST_INITIAL_GUIDANCE_DIAGNOSTIC_ID: Final = "controller_jest_initial_guidance"
CONTROLLER_JEST_ACCOUNT_ERROR_RESET_DIAGNOSTIC_ID: Final = "controller_jest_account_error_reset"
CONTROLLER_JEST_ACCOUNT_ERROR_STALE_RESPONSE_DIAGNOSTIC_ID: Final = (
    "controller_jest_account_error_stale_response"
)
CONTROLLER_JEST_STATUS_CHANGE_RESET_DIAGNOSTIC_ID: Final = "controller_jest_status_change_reset"
CONTROLLER_JEST_STATUS_CHANGE_STALE_RESPONSE_DIAGNOSTIC_ID: Final = (
    "controller_jest_status_change_stale_response"
)

# This is the complete model-writable Salesforce scope. Dependency manifests,
# dependency locks, and Jest configuration are deliberately absent.
SALESFORCE_AGENT_OUTPUT_PATHS = tuple(
    sorted(
        (
            MANIFEST_PATH,
            CONTROLLER_PATH,
            CONTROLLER_METADATA_PATH,
            CONTROLLER_TEST_PATH,
            CONTROLLER_TEST_METADATA_PATH,
            LWC_HTML_PATH,
            LWC_JAVASCRIPT_PATH,
            LWC_CSS_PATH,
            LWC_METADATA_PATH,
            LWC_TEST_PATH,
            PERMISSION_SET_PATH,
        )
    )
)

# --- Case Management Console migration unit ---------------------------------
# A second Salesforce migration unit resolved entirely as data from the
# per-unit registries below. Its writable inventory, semantic template surface,
# and closure target mirror the account/contact shapes with Case-specific names.
CASE_MANAGEMENT_CONSOLE_UNIT_ID: Final = "case-management-console"
CASE_TARGET_ENTRY = "force-app/main/default/lwc/caseManagementConsole/caseManagementConsole.js"
CASE_MANIFEST_PATH = MANIFEST_PATH
CASE_CONTROLLER_PATH = "force-app/main/default/classes/CaseManagementConsoleController.cls"
CASE_CONTROLLER_METADATA_PATH = f"{CASE_CONTROLLER_PATH}-meta.xml"
CASE_CONTROLLER_TEST_PATH = "force-app/main/default/classes/CaseManagementConsoleControllerTest.cls"
CASE_CONTROLLER_TEST_METADATA_PATH = f"{CASE_CONTROLLER_TEST_PATH}-meta.xml"
CASE_LWC_ROOT = "force-app/main/default/lwc/caseManagementConsole"
CASE_LWC_HTML_PATH = f"{CASE_LWC_ROOT}/caseManagementConsole.html"
CASE_LWC_JAVASCRIPT_PATH = f"{CASE_LWC_ROOT}/caseManagementConsole.js"
CASE_LWC_CSS_PATH = f"{CASE_LWC_ROOT}/caseManagementConsole.css"
CASE_LWC_METADATA_PATH = f"{CASE_LWC_ROOT}/caseManagementConsole.js-meta.xml"
CASE_LWC_TEST_PATH = f"{CASE_LWC_ROOT}/__tests__/caseManagementConsole.test.js"
CASE_PERMISSION_SET_PATH = (
    "force-app/main/default/permissionsets/CaseManagementConsoleUser.permissionset-meta.xml"
)
CASE_SEMANTIC_DATA_ROLES: Final[frozenset[str]] = frozenset(
    {"account-selector", "status-filter", "load-cases", "clear-selection", "case-results"}
)
CASE_SEMANTIC_DATA_STATES: Final[frozenset[str]] = frozenset({"loading", "empty"})
CASE_AGENT_OUTPUT_PATHS = tuple(
    sorted(
        (
            CASE_MANIFEST_PATH,
            CASE_CONTROLLER_PATH,
            CASE_CONTROLLER_METADATA_PATH,
            CASE_CONTROLLER_TEST_PATH,
            CASE_CONTROLLER_TEST_METADATA_PATH,
            CASE_LWC_HTML_PATH,
            CASE_LWC_JAVASCRIPT_PATH,
            CASE_LWC_CSS_PATH,
            CASE_LWC_METADATA_PATH,
            CASE_LWC_TEST_PATH,
            CASE_PERMISSION_SET_PATH,
        )
    )
)

# These names belong only to the controller-owned independent behavior suite.
# Candidate-authored test titles are intentionally unconstrained.
SALESFORCE_CONTROLLER_LWC_BEHAVIOR_TITLES: Final[tuple[str, ...]] = (
    "controller: renders account options from the wire adapter",
    "controller: renders a safe account-wire failure",
    "controller: enables Load only after account selection",
    "controller: invokes contacts only after the Load action",
    "controller: exposes loading state while contacts are pending",
    "controller: ignores a response made stale by account change",
    "controller: resets completed and error state on nonblank account change",
    "controller: clears results and disables Load for blank selection",
    "controller: renders empty state only after an empty success",
    "controller: renders a safe contacts failure",
)
SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_BY_TITLE: Final[dict[str, str]] = {
    title: diagnostic_id
    for title, diagnostic_id in zip(
        SALESFORCE_CONTROLLER_LWC_BEHAVIOR_TITLES,
        (
            "controller_jest_account_options",
            "controller_jest_account_error",
            "controller_jest_selection_gate",
            "controller_jest_explicit_load",
            "controller_jest_loading_state",
            "controller_jest_stale_response",
            CONTROLLER_JEST_ACCOUNT_CHANGE_RESET_DIAGNOSTIC_ID,
            "controller_jest_blank_selection",
            "controller_jest_empty_state",
            "controller_jest_contacts_error",
        ),
        strict=True,
    )
}
SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID: Final = (
    "controller_jest_execution_failure"
)

# --- Case Management Console controller-owned Jest suite --------------------
# The Case unit pins its own controller-owned behavior suite. It reuses the
# shared, immutable toolchain files (package manifests, Jest config, and setup)
# byte-for-byte; only the pinned controller-suite entry is unit-specific.
CASE_LWC_CONTROLLER_TEST_PATH = "controller-tests/caseManagementConsole.controller.test.js"
CASE_LWC_JEST_TOOLCHAIN_DIGESTS = {
    "package.json": LWC_JEST_TOOLCHAIN_DIGESTS["package.json"],
    "package-lock.json": LWC_JEST_TOOLCHAIN_DIGESTS["package-lock.json"],
    "jest.config.js": LWC_JEST_TOOLCHAIN_DIGESTS["jest.config.js"],
    LWC_JEST_SETUP_PATH: LWC_JEST_TOOLCHAIN_DIGESTS[LWC_JEST_SETUP_PATH],
    CASE_LWC_CONTROLLER_TEST_PATH: (
        "sha256:b0cf57b8a7263edcdc34407721f327ed9dc33720c2a14a39088e566c5e8b22b6"
    ),
}
CASE_SALESFORCE_CONTROLLER_LWC_BEHAVIOR_TITLES: Final[tuple[str, ...]] = (
    "controller: renders initial guidance before account selection",
    "controller: lists account options with a blank choice from the wire adapter",
    "controller: defaults the status filter to Open with all choices",
    "controller: requests closed cases when Closed is selected",
    "controller: requests all cases when All is selected",
    "controller: renders scoped case results with stable keys",
    "controller: warns and issues no query for a blank account",
    "controller: renders empty state only after an empty success",
    "controller: exposes loading state while cases are pending",
    "controller: renders a safe case-load failure",
    "controller: ignores a response made stale by account change",
    "controller: resets completed and error state when status changes",
    "controller: ignores a response made stale by status change",
    "controller: clears results and prompts to reselect on clear",
    "controller: enables Load only after account selection",
    "controller: requests cases only after the Load action",
    "controller: renders a safe account-wire failure",
    "controller: clears completed case state when the account wire later fails",
    "controller: invalidates pending case work when the account wire fails",
)
CASE_SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_BY_TITLE: Final[dict[str, str]] = {
    title: diagnostic_id
    for title, diagnostic_id in zip(
        CASE_SALESFORCE_CONTROLLER_LWC_BEHAVIOR_TITLES,
        (
            CONTROLLER_JEST_INITIAL_GUIDANCE_DIAGNOSTIC_ID,
            "controller_jest_account_options",
            "controller_jest_status_default",
            "controller_jest_status_closed",
            "controller_jest_status_all",
            "controller_jest_case_results",
            "controller_jest_blank_selection",
            "controller_jest_empty_state",
            "controller_jest_loading_state",
            "controller_jest_cases_error",
            "controller_jest_stale_response",
            CONTROLLER_JEST_STATUS_CHANGE_RESET_DIAGNOSTIC_ID,
            CONTROLLER_JEST_STATUS_CHANGE_STALE_RESPONSE_DIAGNOSTIC_ID,
            "controller_jest_clear_selection",
            "controller_jest_selection_gate",
            "controller_jest_explicit_load",
            "controller_jest_account_error",
            CONTROLLER_JEST_ACCOUNT_ERROR_RESET_DIAGNOSTIC_ID,
            CONTROLLER_JEST_ACCOUNT_ERROR_STALE_RESPONSE_DIAGNOSTIC_ID,
        ),
        strict=True,
    )
}


@dataclass(frozen=True)
class _SalesforceControllerJestSpec:
    """One migration unit's controller-owned Jest suite identity.

    The controller-owned behavior suite is trusted, immutable tooling the model
    never authors. Each unit pins the checked-in suite path, the exact behavior
    titles the suite must report, the behavior-title to diagnostic-id map, and
    the full pinned toolchain digest inventory (whose controller-suite entry is
    unit-specific). The account/contact entry references the module-level
    constants byte-for-byte so a second unit can be added as pure data.
    """

    controller_test_path: str
    behavior_titles: tuple[str, ...]
    diagnostic_by_title: Mapping[str, str]
    toolchain_digests: Mapping[str, str]


_SALESFORCE_CONTROLLER_JEST_BY_UNIT: Final[dict[str, _SalesforceControllerJestSpec]] = {
    SALESFORCE_ACCOUNT_CONTACT_UNIT_ID: _SalesforceControllerJestSpec(
        controller_test_path=LWC_CONTROLLER_TEST_PATH,
        behavior_titles=SALESFORCE_CONTROLLER_LWC_BEHAVIOR_TITLES,
        diagnostic_by_title=SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_BY_TITLE,
        toolchain_digests=LWC_JEST_TOOLCHAIN_DIGESTS,
    ),
    CASE_MANAGEMENT_CONSOLE_UNIT_ID: _SalesforceControllerJestSpec(
        controller_test_path=CASE_LWC_CONTROLLER_TEST_PATH,
        behavior_titles=CASE_SALESFORCE_CONTROLLER_LWC_BEHAVIOR_TITLES,
        diagnostic_by_title=CASE_SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_BY_TITLE,
        toolchain_digests=CASE_LWC_JEST_TOOLCHAIN_DIGESTS,
    ),
}


def resolve_salesforce_controller_jest_spec(
    unit_id: str = SALESFORCE_ACCOUNT_CONTACT_UNIT_ID,
) -> _SalesforceControllerJestSpec:
    """Resolve one unit's controller-owned Jest suite spec, failing closed."""

    spec = _SALESFORCE_CONTROLLER_JEST_BY_UNIT.get(unit_id)
    if spec is None:
        raise LocalCheckFailure("unsupported Salesforce migration unit")
    return spec


# The complete controller-owned diagnostic vocabulary is the union across every
# registered unit plus the shared execution-failure signal. With account/contact
# as the sole registered unit this equals its title-derived id set exactly, and a
# second unit's diagnostic ids extend the union as pure data.
SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS: Final[frozenset[str]] = frozenset(
    {
        SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID,
        *(
            diagnostic_id
            for spec in _SALESFORCE_CONTROLLER_JEST_BY_UNIT.values()
            for diagnostic_id in spec.diagnostic_by_title.values()
        ),
    }
)

# Per-unit controller-owned behavior diagnostic ids, keyed by migration unit id.
# Correction path mapping is unit-aware: a unit's behavior diagnostics bind that
# unit's LWC bundle. The union above stays the complete controller vocabulary.
SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS_BY_UNIT: Final[dict[str, frozenset[str]]] = {
    unit_id: frozenset(spec.diagnostic_by_title.values())
    for unit_id, spec in _SALESFORCE_CONTROLLER_JEST_BY_UNIT.items()
}

SALESFORCE_IMPLEMENTATION_CONTRACT = (
    (
        "Write only the eleven approved Salesforce candidate artifacts: manifest/package.xml; "
        "the AccountContactExplorerController class, generated Apex test and their metadata; the "
        "accountContactExplorer HTML, JavaScript, CSS, metadata and Jest test; and the "
        "AccountContactExplorerUser permission set. Keep synthetic Jest data inline in the test "
        "file rather than generating separate data files. Preserve all legacy Apex, Visualforce "
        "and project files. Do not generate or modify controller-owned Jest tooling."
    ),
    (
        "Use Salesforce API 67.0. Keep the deployment manifest dependency-closed for the new and "
        "preserved legacy artifacts, declaring each metadata type in exactly one <types> block "
        "whose single <name> lists every member of that type. Apex metadata must be Active; expose "
        "the LWC only on lightning__AppPage and lightning__Tab."
    ),
    (
        "Expose public with sharing class AccountContactExplorerController with exactly the two "
        "public static methods getAccounts() and getContacts(Id accountId). Mark the wired "
        "getAccounts() read @AuraEnabled(cacheable=true). Keep the explicit, user-triggered "
        "getContacts(...) read non-cacheable with bare @AuraEnabled or "
        "@AuraEnabled(cacheable=false); never mark it cacheable=true. Project exactly Account Id "
        "and Name from the Account query and exactly Contact Id, FirstName, LastName, Email and "
        "Phone from the Contact query. Use Contact.AccountId only as the WHERE predicate; do not "
        "project it. Use static SOQL WITH USER_MODE, filter contacts by the selected account, "
        "preserve the specified ordering, cap the Account query at 1 "
        "through 50 rows and the Contact query at 1 through 100 rows with either a literal LIMIT "
        "or a positive compile-time Integer constant. Return "
        "an empty list for a null selection before querying. Catch each query failure and translate "
        "it to an AuraHandledException whose sole argument is a fixed safe, nontechnical string; "
        "never pass through exception details. Include no DML, dynamic query, callout, "
        "external endpoint, credential, authorization value or secret. Internal constants, helpers, "
        "control flow, query layout and safe wording are candidate-owned."
    ),
    (
        "Generate an @IsTest Apex class that exercises both public controller methods with "
        "isolated synthetic Account and Contact data and meaningful assertions for account results, "
        "a selected account with contacts, a selected account without contacts, and a null selection. "
        "Do not create User records, query Profile, or use System.runAs to fabricate a "
        "permission failure; those tests are org-configuration-dependent, while the controller's "
        "safe exception translation is checked separately. Test names, helpers, setup, counts, "
        "record values and assertion forms are candidate-owned. Do not use SeeAllData=true, "
        "dynamic query, callouts, external endpoints, credentials or secrets."
    ),
    (
        "Export a standard-JavaScript LightningElement component whose only static dependencies "
        "are lwc and the two controller methods. Let the user select an account and explicitly "
        "load contacts. Render an actual account option whose value is the empty string; a combobox "
        "placeholder is not that option. Render returned account options, contact results, loading, "
        "empty and controlled error states. Treat Account-wire and Contact-load error objects as "
        "untrusted: never render error.message, error.body.message, exception text, query text, or "
        "another payload-derived detail; map each failure to a fixed candidate-owned nontechnical "
        "literal before it reaches visible DOM. Bind disabled to a disabled-state getter that returns "
        "true for a blank selection; do not bind a positive canLoadContacts getter directly because "
        "that reverses the Load gate. Whenever the account selection changes, immediately "
        "invalidate pending work and clear prior contact rows, loaded or empty state, loading, and "
        "contact-load errors; a new nonblank account must wait for the next explicit Load, while a "
        "blank selection also shows safe guidance. Prevent a response made stale by an account "
        "change from overwriting current state. Do not render the contact-results hook in empty, guidance, or "
        "controlled-error states. Expose FirstName, LastName, Email and Phone for each result and "
        "retain a unique key value for structured rows. State fields, helpers, async guards, "
        "control flow and wording are candidate-owned. Include no dynamic module loading, host or "
        "test globals, network primitives, reflective execution, external URLs or secrets."
    ),
    (
        "Provide an accessible local UI with stable data-role values account-selector, "
        "load-contacts and contact-results, data-state values loading and empty, and role=alert for "
        "controlled guidance and errors. Put the account-selector hook on the interactive control. "
        "Hooks may be literal values or simple property bindings so their rendered values remain "
        "stable for the public semantic test surface. Salesforce API 67 supports complex template "
        "expressions elsewhere in the component; using a JavaScript getter for a nontrivial value "
        "is a maintainability convention, not a compiler restriction. The pinned LWC compiler and "
        "Jest runner remain authoritative for expression syntax. "
        "Do not use external scripts, frames, imports or URL-backed CSS."
    ),
    (
        "Generate executable LWC Jest tests for important outcomes, including successful loading, "
        "controlled failures and stale-response handling. Keep bounded synthetic Account and "
        "Contact data inline. Put the named import of every used Jest API from @jest/globals "
        "before every other static import so the pinned transform initializes jest before loading "
        "the component and its hoisted virtual Apex mock factories. Test titles, helpers, "
        "assertions, mock implementation and all remaining source order are candidate-owned and "
        "validated by the pinned Jest runner. Mock the wired getAccounts method with "
        "createApexTestWireAdapter; append the component before emitting account data or an error "
        "from that adapter. Mock the imperative getContacts method with jest.fn and install its "
        "resolved or rejected outcome before dispatching the user event that triggers the load; "
        "after either outcome settles, drain at least three consecutive microtask turns directly "
        "or through a local helper before asserting rendered DOM because a rejection handler "
        "consumes a turn before LWC schedules its rerender; "
        "do not call non-@api component methods through the host element. Do not include skipped "
        "or focused tests, dangerous Node capabilities, network access, dynamic code execution, "
        "external endpoints, credentials, authorization values or secrets."
    ),
    (
        "Keep AccountContactExplorerUser least-privileged and read-only: Account and Contact read "
        "access only, Contact Email and Phone readable but not editable, only the legacy and new "
        "controller class accesses, and only the legacy Visualforce page access. Do not grant "
        "create, edit, delete, modify-all, view-all, view-all-fields, user or administrative "
        "capabilities."
    ),
)

# Case Management Console implementation contract. It mirrors the account/contact
# contract structure with the Case unit's controller, LWC bundle, status-filter
# behavior, keyed case datatable, explicit clear action, and permission surface.
CASE_IMPLEMENTATION_CONTRACT = (
    (
        "Write only the eleven approved Salesforce candidate artifacts: manifest/package.xml; "
        "the CaseManagementConsoleController class, generated Apex test and their metadata; the "
        "caseManagementConsole HTML, JavaScript, CSS, metadata and Jest test; and the "
        "CaseManagementConsoleUser permission set. Keep synthetic Jest data inline in the test "
        "file rather than generating separate data files. Preserve all legacy Apex (including "
        "LegacyCaseManagementConsoleController and LegacyCaseQueryService), Visualforce and "
        "project files. Do not generate or modify controller-owned Jest tooling."
    ),
    (
        "Use Salesforce API 67.0. Keep the deployment manifest dependency-closed for the new and "
        "preserved legacy artifacts, declaring each metadata type in exactly one <types> block "
        "whose single <name> lists every member of that type. Apex metadata must be Active; expose "
        "the LWC only on lightning__AppPage and lightning__Tab."
    ),
    (
        "Expose public with sharing class CaseManagementConsoleController with exactly the two "
        "public static methods getAccounts() and getCases(Id accountId, String statusFilter). "
        "Mark the wired getAccounts() read @AuraEnabled(cacheable=true). Keep the explicit, "
        "user-triggered getCases(...) read non-cacheable with bare @AuraEnabled or "
        "@AuraEnabled(cacheable=false); never mark it cacheable=true. Project exactly Account Id "
        "and Name from the Account query and exactly Case Id, CaseNumber, Subject, Status, Priority "
        "and Contact.Name from every Case query. Use Case.AccountId only as the WHERE predicate; "
        "do not project it. Use static SOQL WITH "
        "USER_MODE, filter cases by the selected account, preserve ordering by CaseNumber DESC, "
        "and cap "
        "the Account query at 1 through 50 rows. Use one to three branch-specific static Case "
        "queries, each capped at 1 through 100 rows with either a literal LIMIT or a positive "
        "compile-time Integer constant. Offer the OPEN, "
        "CLOSED and ALL status choices, default to Open, and map OPEN to nonclosed cases, CLOSED "
        "to closed cases, and ALL to both through candidate-owned bounded filter state or branch "
        "control flow. Return an empty list for a null "
        "selection before querying. Catch each query failure and translate it to an "
        "AuraHandledException whose sole argument is a fixed safe, nontechnical string; never pass "
        "through exception details. Include no DML, dynamic query, callout, external endpoint, "
        "credential, authorization value or secret. Internal constants, helpers, control flow, "
        "query layout and safe wording are candidate-owned."
    ),
    (
        "Generate an @IsTest Apex class that exercises both public controller methods with "
        "isolated synthetic Account and Case data and meaningful assertions for account results, "
        "a selected account with cases scoped by status, a selected account without cases, and a "
        "null selection. Do not create User records, query Profile, or use System.runAs to "
        "fabricate a permission failure; those tests are org-configuration-dependent, while the "
        "controller's safe exception translation is checked separately. Test names, helpers, "
        "setup, counts, record values and assertion forms are candidate-owned. Do not use "
        "SeeAllData=true, dynamic query, callouts, external endpoints, credentials or secrets."
    ),
    (
        "Export a standard-JavaScript LightningElement component whose only static dependencies "
        "are lwc and the two controller methods. Let the user select an account, choose a status "
        "filter, explicitly load cases, and clear the selection. Render an actual account option "
        "whose value is the empty string; a combobox placeholder is not that option. Render "
        "returned account options, the status choices defaulting to Open, keyed case results, "
        "loading, empty and controlled error states. Before any Account is selected, show safe "
        "initial guidance and render no Case results. Bind disabled to a disabled-state getter that "
        "returns true for a blank selection; do not bind a positive canLoadCases getter directly "
        "because that reverses the Load gate. Whenever the account or status selection changes, "
        "immediately invalidate pending work and clear prior case rows, loaded or empty state, "
        "loading, and case-load errors; wait for the next explicit Load before querying again. On "
        "clear, also disable Load and show safe reselect guidance. If the Account wire transitions "
        "to error after data was available, invalidate pending Case work, clear the selected "
        "Account, Case rows, loading and empty state, disable Load, and show only a fixed safe "
        "alert; a late Case success or failure must not reappear. Prevent a response made stale "
        "by an account, status, or Account-wire error from overwriting current state. Do not render "
        "the case-results hook in empty, guidance, or controlled-error states. Expose each case's "
        "CaseNumber, Subject, Status, Priority and contact name and retain a unique key value for "
        "structured rows. State fields, helpers, async guards, control flow and wording are "
        "candidate-owned. Include no dynamic module loading, host or test globals, network "
        "primitives, reflective execution, external URLs or secrets."
    ),
    (
        "Treat Account-wire and Case-load error objects as untrusted input; never render "
        "error.message, error.body.message, exception text, query text or another payload-derived "
        "detail. Map each failure to a fixed candidate-owned nontechnical literal before it reaches "
        "visible DOM."
    ),
    (
        "Provide an accessible local UI with stable data-role values account-selector, "
        "status-filter, load-cases, clear-selection and case-results, data-state values loading "
        "and empty, and role=alert for controlled guidance and errors. Put the account-selector "
        "hook on the interactive control. Hooks may be literal values or simple property bindings "
        "so their rendered values remain stable for the public semantic test surface. Salesforce "
        "API 67 supports complex template expressions elsewhere in the component; using a "
        "JavaScript getter for a nontrivial value is a maintainability convention, not a compiler "
        "restriction. The pinned LWC compiler and Jest runner remain authoritative for expression "
        "syntax. Do not use external scripts, frames, imports or URL-backed CSS."
    ),
    (
        "Generate executable LWC Jest tests for important outcomes, including successful loading, "
        "status-filter handling, controlled failures, clear-selection and stale-response handling. "
        "Keep bounded synthetic Account and Case data inline. Put the named import of every used "
        "Jest API from @jest/globals before every other static import so the pinned transform "
        "initializes jest before loading the component and its hoisted virtual Apex mock "
        "factories. Test titles, helpers, assertions, mock implementation and all remaining source "
        "order are candidate-owned and validated by the pinned Jest runner. Mock the wired "
        "getAccounts method with createApexTestWireAdapter; append the component before emitting "
        "account data or an error from that adapter. Mock the imperative getCases method with "
        "jest.fn and install its resolved or rejected outcome before dispatching the user event "
        "that triggers the load; after either outcome settles, drain at least three consecutive "
        "microtask turns directly or through a local helper before asserting rendered DOM because "
        "a rejection handler consumes a turn before LWC schedules its rerender; do not call "
        "non-@api component methods through the host element. "
        "Do not include skipped or focused tests, dangerous "
        "Node capabilities, network access, dynamic code execution, external endpoints, "
        "credentials, authorization values or secrets."
    ),
    (
        "Keep CaseManagementConsoleUser least-privileged and read-only: Account, Contact and Case "
        "read access only; preserve the existing readable, noneditable Case.AccountId, "
        "Case.ContactId, Case.Description, Case.Priority and Case.Subject field grants exactly. "
        "Do not add fieldPermissions for Case.CaseNumber, Case.Status or Case.IsClosed because "
        "those standard fields are not permissionable in the pinned API contract. Grant only the "
        "legacy and new controller class accesses (including LegacyCaseQueryService) and only the "
        "legacy Visualforce page access. Do not grant create, edit, delete, modify-all, view-all, "
        "view-all-fields, user or administrative capabilities."
    ),
)

SALESFORCE_CANDIDATE_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {
        "salesforce_candidate_inventory",
        "salesforce_manifest_contract",
        "salesforce_apex_controller_metadata_contract",
        "salesforce_apex_test_metadata_contract",
        "salesforce_apex_controller_contract",
        "salesforce_apex_test_contract",
        "salesforce_lwc_javascript_contract",
        "salesforce_lwc_template_contract",
        "salesforce_lwc_styles_contract",
        "salesforce_lwc_metadata_contract",
        "salesforce_lwc_jest_contract",
        "salesforce_permission_set_contract",
        "salesforce_candidate_unclassified",
    }
)

SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID: Final = (
    "candidate_jest_execution_failure"
)
# Candidate static checks emit their failed stage or a bounded contract/security
# diagnostic. Runtime test execution owns the separate Jest execution signal.
SALESFORCE_CANDIDATE_STATIC_DIAGNOSTIC_IDS: Final[frozenset[str]] = frozenset(
    {
        *SALESFORCE_CANDIDATE_FAILURE_CODES,
        APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,
        APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,
        JEST_GLOBALS_IMPORT_ORDER_DIAGNOSTIC_ID,
        JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID,
        LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID,
        "lwc_forbidden_runtime_capability",
        "jest_forbidden_capability",
    }
)
SALESFORCE_CANDIDATE_DIAGNOSTIC_IDS: Final[frozenset[str]] = frozenset(
    {
        *SALESFORCE_CANDIDATE_STATIC_DIAGNOSTIC_IDS,
        SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID,
    }
)


class LocalCheckFailure(RuntimeError):
    """A controlled local validation failure."""

    def __init__(
        self,
        message: str,
        *,
        failure_code: str | None = None,
        diagnostic_ids: Sequence[str] = (),
    ) -> None:
        self.failure_code = failure_code
        normalized = tuple(dict.fromkeys(diagnostic_ids))
        if not set(normalized).issubset(SALESFORCE_CANDIDATE_STATIC_DIAGNOSTIC_IDS):
            raise AssertionError("local check uses an unknown diagnostic identifier")
        self.diagnostic_ids = normalized
        super().__init__(message)


def _candidate_stage(failure_code: str, action: Callable[[], None]) -> None:
    """Collapse detailed assertion failures to one code-owned public stage."""

    if failure_code not in SALESFORCE_CANDIDATE_FAILURE_CODES:
        raise AssertionError("candidate stage uses an unknown failure code")
    try:
        action()
    except LocalCheckFailure as exc:
        diagnostic_ids = exc.diagnostic_ids or (failure_code,)
        raise LocalCheckFailure(
            "local contract assertion failed",
            failure_code=failure_code,
            diagnostic_ids=diagnostic_ids,
        ) from exc


def check_salesforce_candidate(
    root: Path,
    *,
    unit_id: str = SALESFORCE_ACCOUNT_CONTACT_UNIT_ID,
) -> dict[str, Any]:
    """Validate a candidate solution without consulting a fixture or lockfile.

    This is a static, fail-closed preflight. It validates Salesforce metadata,
    source-level security constraints, and the presence of focused Apex and LWC
    tests. It intentionally makes no claim that Apex, Jest, or deployment ran.

    Every migration-unit-specific expectation is resolved from the requested
    unit's :class:`_SalesforceUnitProfile`; an unknown unit fails closed.
    """

    profile = _SALESFORCE_PROFILE_BY_UNIT.get(unit_id)
    if profile is None:
        raise LocalCheckFailure("unsupported Salesforce migration unit")

    root = _root(root)
    required = profile.agent_output_paths
    try:
        paths = {relative: _file(root, relative) for relative in required}
    except LocalCheckFailure as exc:
        raise LocalCheckFailure(
            "local contract assertion failed",
            failure_code="salesforce_candidate_inventory",
            diagnostic_ids=("salesforce_candidate_inventory",),
        ) from exc

    controller_source = _text(paths[profile.controller_path])
    controller_status_constants: dict[str, str] | None = None

    def resolved_controller_status_constants() -> Mapping[str, str]:
        nonlocal controller_status_constants
        if controller_status_constants is None:
            controller_status_constants = _apex_literal_status_constants(controller_source)
        return controller_status_constants

    stages: tuple[tuple[str, Callable[[], None]], ...] = (
        (
            "salesforce_manifest_contract",
            lambda: _check_manifest(_xml_root(paths[profile.manifest_path]), profile),
        ),
        (
            "salesforce_apex_controller_metadata_contract",
            lambda: _check_apex_metadata(_xml_root(paths[profile.controller_metadata_path])),
        ),
        (
            "salesforce_apex_test_metadata_contract",
            lambda: _check_apex_metadata(_xml_root(paths[profile.controller_test_metadata_path])),
        ),
        (
            "salesforce_apex_controller_contract",
            lambda: _check_controller(
                controller_source,
                profile,
                status_constants=resolved_controller_status_constants(),
            ),
        ),
        (
            "salesforce_apex_test_contract",
            lambda: _check_controller_test(
                _text(paths[profile.controller_test_path]),
                profile,
                controller_status_constants=resolved_controller_status_constants(),
            ),
        ),
        (
            "salesforce_lwc_javascript_contract",
            lambda: _check_lwc_javascript(_text(paths[profile.lwc_javascript_path]), profile),
        ),
        (
            "salesforce_lwc_template_contract",
            lambda: _check_lwc_template(_text(paths[profile.lwc_html_path]), profile),
        ),
        (
            "salesforce_lwc_styles_contract",
            lambda: _check_lwc_styles(_text(paths[profile.lwc_css_path])),
        ),
        (
            "salesforce_lwc_metadata_contract",
            lambda: _check_lwc_metadata(_xml_root(paths[profile.lwc_metadata_path])),
        ),
        (
            "salesforce_lwc_jest_contract",
            lambda: _check_lwc_test(_text(paths[profile.lwc_test_path]), profile),
        ),
        (
            "salesforce_permission_set_contract",
            lambda: _check_permission_set(_xml_root(paths[profile.permission_set_path]), profile),
        ),
    )
    failures: list[LocalCheckFailure] = []
    for failure_code, action in stages:
        try:
            _candidate_stage(failure_code, action)
        except LocalCheckFailure as exc:
            failures.append(exc)
    if failures:
        diagnostic_ids = tuple(
            dict.fromkeys(
                diagnostic_id for failure in failures for diagnostic_id in failure.diagnostic_ids
            )
        )
        raise LocalCheckFailure(
            "local contract assertion failed",
            failure_code=failures[0].failure_code,
            diagnostic_ids=diagnostic_ids,
        ) from failures[0]

    return {
        "check": "salesforce-candidate-contract",
        "passed": True,
        "api_version": API_VERSION,
        "required_files": len(required),
        "agent_output_files": len(SALESFORCE_AGENT_OUTPUT_PATHS),
        "security": "with-sharing-user-mode-read-only",
        "behavior_states": 6,
        "apex_tests_prepared": True,
        "lwc_jest_tests_prepared": True,
        "apex_tests_executed": False,
        "lwc_jest_executed": False,
        "org_validation_performed": False,
        "deployment_claim": False,
    }


def check_lwc_jest_toolchain(
    toolchain_root: Path,
    *,
    unit_id: str = SALESFORCE_ACCOUNT_CONTACT_UNIT_ID,
) -> dict[str, Any]:
    """Validate the immutable Jest harness without inspecting a candidate.

    The controller-owned suite path and the pinned toolchain digest inventory are
    resolved from the requested unit's controller-Jest spec; an unknown unit fails
    closed.
    """

    spec = resolve_salesforce_controller_jest_spec(unit_id)
    toolchain_digests = spec.toolchain_digests
    root = _root(toolchain_root)
    paths = {relative: _file(root, relative) for relative in toolchain_digests}
    contents = {
        relative: _bytes(
            path,
            max_bytes=2_000_000 if relative == "package-lock.json" else 512_000,
        )
        for relative, path in paths.items()
    }
    digests = {
        name: f"sha256:{hashlib.sha256(content).hexdigest()}" for name, content in contents.items()
    }
    _require(digests == toolchain_digests, "toolchain content digests")

    package = _json_object_bytes(contents["package.json"])
    lock = _json_object_bytes(contents["package-lock.json"])
    config = _decode(contents["jest.config.js"])
    _require(
        package
        == {
            "name": LWC_JEST_TOOLCHAIN_NAME,
            "version": "1.0.0",
            "private": True,
            "scripts": {"test:unit": "sfdx-lwc-jest"},
            "devDependencies": {LWC_JEST_PACKAGE: LWC_JEST_VERSION},
        },
        "toolchain manifest",
    )
    _require(config == _lwc_jest_config(), "toolchain Jest configuration")
    _check_lwc_jest_lock(lock)

    lock_packages = cast(dict[str, Any], lock["packages"])
    return {
        "check": "lwc-jest-toolchain-contract",
        "passed": True,
        "package": LWC_JEST_PACKAGE,
        "version": LWC_JEST_VERSION,
        "locked_packages": len(lock_packages),
        "manifest_sha256": digests["package.json"],
        "lock_sha256": digests["package-lock.json"],
        "config_sha256": digests["jest.config.js"],
        "setup_sha256": digests[LWC_JEST_SETUP_PATH],
        "controller_test_sha256": digests[spec.controller_test_path],
        "candidate_content_inspected": False,
        "network_accessed": False,
        "install_performed": False,
        "lwc_jest_executed": False,
    }


@dataclass(frozen=True)
class _SalesforceClosureSpec:
    """One migration unit's fixed dependency-closure target and expected nodes."""

    target_entry: str
    expected_nodes: frozenset[tuple[NodeKind, str]]


_SALESFORCE_CLOSURE_BY_UNIT: Final[dict[str, _SalesforceClosureSpec]] = {
    SALESFORCE_ACCOUNT_CONTACT_UNIT_ID: _SalesforceClosureSpec(
        target_entry=TARGET_ENTRY,
        expected_nodes=frozenset(
            {
                (NodeKind.LWC_COMPONENT, "accountContactExplorer"),
                (NodeKind.APEX_CLASS, "AccountContactExplorerController"),
                (NodeKind.APEX_TEST, "AccountContactExplorerControllerTest"),
                (NodeKind.PERMISSION_SET, "AccountContactExplorerUser"),
                (NodeKind.VISUALFORCE_PAGE, "LegacyAccountContactExplorer"),
            }
        ),
    ),
    CASE_MANAGEMENT_CONSOLE_UNIT_ID: _SalesforceClosureSpec(
        target_entry=CASE_TARGET_ENTRY,
        expected_nodes=frozenset(
            {
                (NodeKind.LWC_COMPONENT, "caseManagementConsole"),
                (NodeKind.APEX_CLASS, "CaseManagementConsoleController"),
                (NodeKind.APEX_TEST, "CaseManagementConsoleControllerTest"),
                (NodeKind.PERMISSION_SET, "CaseManagementConsoleUser"),
                (NodeKind.VISUALFORCE_PAGE, "LegacyCaseManagementConsole"),
            }
        ),
    ),
}


@dataclass(frozen=True)
class _ApexControllerMethodSpec:
    """One controller method's public interface and bounded static-SOQL contract."""

    return_type: str  # SObject list element type in the public method signature
    method_name: str  # public @AuraEnabled method name
    cacheable: bool  # true requires cacheable=true; false permits bare or cacheable=false only
    arguments: str  # regex fragment matching the method's parameter list
    argument_count: int  # exact public-method arity required in generated Apex calls
    max_static_queries: int  # inclusive method-bound static-query count cap
    query_object: str  # FROM object required for every method-bound static query
    query_fields: frozenset[str]  # exact selected-field set required in every query
    query_order_by: tuple[str, ...]  # ORDER BY field sequence required in every query
    query_order_direction: str | None  # explicit shared direction, or SQL's default ascending
    query_max_rows: int  # inclusive LIMIT cap applied independently to every query
    selection_filter: str | None  # required bound WHERE filter in every query, or None
    null_selection_guard: str | None  # regex requiring an early empty-list return, or None
    immutable_parameters: tuple[str, ...] = ()  # caller-owned selection authority
    required_status_filter_values: tuple[str, ...] = ()  # exact public filter modes, when any


@dataclass(frozen=True)
class _SalesforceUnitProfile:
    """Every migration-unit-specific literal the candidate validator resolves per unit.

    The account/contact profile below reproduces the previously hard-coded
    account/contact expectations byte-for-byte. Shared prose/title constants stay
    at module scope (external modules import them); this profile only references
    them so a second unit can be added by supplying data alone.
    """

    # Candidate inventory: the model-writable output scope plus the individual
    # relative path each validation stage consumes.
    agent_output_paths: tuple[str, ...]
    manifest_path: str
    controller_path: str
    controller_metadata_path: str
    controller_test_path: str
    controller_test_metadata_path: str
    lwc_javascript_path: str
    lwc_html_path: str
    lwc_css_path: str
    lwc_metadata_path: str
    lwc_test_path: str
    permission_set_path: str
    # Deployment manifest: exact per-type member inventory.
    manifest_members: Mapping[str, frozenset[str]]
    # Apex controller + generated test.
    controller_class_name: str
    controller_test_class_name: str
    controller_methods: tuple[_ApexControllerMethodSpec, ...]
    # LWC: the wire/imperative Apex module targets, the component's own Jest
    # module, and the semantic template data-role / data-state vocabularies.
    lwc_apex_modules: tuple[str, ...]
    lwc_component_module: str
    lwc_template_data_roles: frozenset[str]
    lwc_template_data_states: frozenset[str]
    # Permission set: the objects, class accesses, field permissions, and the
    # single legacy page access the least-privileged profile grants.
    permission_objects: frozenset[str]
    permission_class_accesses: frozenset[str]
    permission_field_permissions: frozenset[str]
    permission_page_access: str


_SALESFORCE_PROFILE_BY_UNIT: Final[dict[str, _SalesforceUnitProfile]] = {
    SALESFORCE_ACCOUNT_CONTACT_UNIT_ID: _SalesforceUnitProfile(
        agent_output_paths=SALESFORCE_AGENT_OUTPUT_PATHS,
        manifest_path=MANIFEST_PATH,
        controller_path=CONTROLLER_PATH,
        controller_metadata_path=CONTROLLER_METADATA_PATH,
        controller_test_path=CONTROLLER_TEST_PATH,
        controller_test_metadata_path=CONTROLLER_TEST_METADATA_PATH,
        lwc_javascript_path=LWC_JAVASCRIPT_PATH,
        lwc_html_path=LWC_HTML_PATH,
        lwc_css_path=LWC_CSS_PATH,
        lwc_metadata_path=LWC_METADATA_PATH,
        lwc_test_path=LWC_TEST_PATH,
        permission_set_path=PERMISSION_SET_PATH,
        manifest_members={
            "ApexClass": frozenset(
                {
                    "AccountContactExplorerController",
                    "AccountContactExplorerControllerTest",
                    "LegacyAccountContactExplorerController",
                    "LegacyAcctContactExplorerCtrlTest",
                }
            ),
            "ApexPage": frozenset({"LegacyAccountContactExplorer"}),
            "LightningComponentBundle": frozenset({"accountContactExplorer"}),
            "PermissionSet": frozenset({"AccountContactExplorerUser"}),
        },
        controller_class_name="AccountContactExplorerController",
        controller_test_class_name="AccountContactExplorerControllerTest",
        controller_methods=(
            _ApexControllerMethodSpec(
                return_type="Account",
                method_name="getAccounts",
                cacheable=True,
                arguments=r"\(\s*\)",
                argument_count=0,
                max_static_queries=1,
                query_object="Account",
                query_fields=frozenset({"Id", "Name"}),
                query_order_by=("Name",),
                query_order_direction=None,
                query_max_rows=50,
                selection_filter=None,
                null_selection_guard=None,
            ),
            _ApexControllerMethodSpec(
                return_type="Contact",
                method_name="getContacts",
                cacheable=False,
                arguments=r"\(\s*Id\s+accountId\s*\)",
                argument_count=1,
                max_static_queries=1,
                query_object="Contact",
                query_fields=frozenset({"Id", "FirstName", "LastName", "Email", "Phone"}),
                query_order_by=("LastName", "FirstName"),
                query_order_direction=None,
                query_max_rows=100,
                selection_filter=r"\bWHERE\s+AccountId\s*=\s*:accountId\b",
                null_selection_guard=(
                    r"\bif\s*\(\s*(?:accountId\s*==\s*null|null\s*==\s*accountId)\s*\)\s*"
                    r"\{\s*return\s+new\s+List\s*<\s*Contact\s*>\s*\(\s*\)\s*;\s*\}"
                ),
                immutable_parameters=("accountId",),
            ),
        ),
        lwc_apex_modules=(
            "@salesforce/apex/AccountContactExplorerController.getAccounts",
            "@salesforce/apex/AccountContactExplorerController.getContacts",
        ),
        lwc_component_module="c/accountContactExplorer",
        lwc_template_data_roles=LWC_SEMANTIC_DATA_ROLES,
        lwc_template_data_states=LWC_SEMANTIC_DATA_STATES,
        permission_objects=frozenset({"Account", "Contact"}),
        permission_class_accesses=frozenset(
            {
                "LegacyAccountContactExplorerController",
                "AccountContactExplorerController",
            }
        ),
        permission_field_permissions=frozenset({"Contact.Email", "Contact.Phone"}),
        permission_page_access="LegacyAccountContactExplorer",
    ),
    CASE_MANAGEMENT_CONSOLE_UNIT_ID: _SalesforceUnitProfile(
        agent_output_paths=CASE_AGENT_OUTPUT_PATHS,
        manifest_path=CASE_MANIFEST_PATH,
        controller_path=CASE_CONTROLLER_PATH,
        controller_metadata_path=CASE_CONTROLLER_METADATA_PATH,
        controller_test_path=CASE_CONTROLLER_TEST_PATH,
        controller_test_metadata_path=CASE_CONTROLLER_TEST_METADATA_PATH,
        lwc_javascript_path=CASE_LWC_JAVASCRIPT_PATH,
        lwc_html_path=CASE_LWC_HTML_PATH,
        lwc_css_path=CASE_LWC_CSS_PATH,
        lwc_metadata_path=CASE_LWC_METADATA_PATH,
        lwc_test_path=CASE_LWC_TEST_PATH,
        permission_set_path=CASE_PERMISSION_SET_PATH,
        manifest_members={
            "ApexClass": frozenset(
                {
                    "CaseManagementConsoleController",
                    "CaseManagementConsoleControllerTest",
                    "LegacyCaseManagementConsoleController",
                    "LegacyCaseQueryService",
                    "LegacyCaseConsoleCtrlTest",
                }
            ),
            "ApexPage": frozenset({"LegacyCaseManagementConsole"}),
            "LightningComponentBundle": frozenset({"caseManagementConsole"}),
            "PermissionSet": frozenset({"CaseManagementConsoleUser"}),
        },
        controller_class_name="CaseManagementConsoleController",
        controller_test_class_name="CaseManagementConsoleControllerTest",
        controller_methods=(
            _ApexControllerMethodSpec(
                return_type="Account",
                method_name="getAccounts",
                cacheable=True,
                arguments=r"\(\s*\)",
                argument_count=0,
                max_static_queries=1,
                query_object="Account",
                query_fields=frozenset({"Id", "Name"}),
                query_order_by=("Name",),
                query_order_direction=None,
                query_max_rows=50,
                selection_filter=None,
                null_selection_guard=None,
            ),
            _ApexControllerMethodSpec(
                return_type="Case",
                method_name="getCases",
                cacheable=False,
                arguments=r"\(\s*Id\s+accountId\s*,\s*String\s+statusFilter\s*\)",
                argument_count=2,
                max_static_queries=3,
                query_object="Case",
                query_fields=frozenset(
                    {"Id", "CaseNumber", "Subject", "Status", "Priority", "Contact.Name"}
                ),
                query_order_by=("CaseNumber",),
                query_order_direction="DESC",
                query_max_rows=100,
                selection_filter=r"\bWHERE\s+AccountId\s*=\s*:accountId\b",
                null_selection_guard=(
                    r"\bif\s*\(\s*(?:accountId\s*==\s*null|null\s*==\s*accountId)\s*\)\s*"
                    r"\{\s*return\s+new\s+List\s*<\s*Case\s*>\s*\(\s*\)\s*;\s*\}"
                ),
                immutable_parameters=("accountId", "statusFilter"),
                required_status_filter_values=("OPEN", "CLOSED", "ALL"),
            ),
        ),
        lwc_apex_modules=(
            "@salesforce/apex/CaseManagementConsoleController.getAccounts",
            "@salesforce/apex/CaseManagementConsoleController.getCases",
        ),
        lwc_component_module="c/caseManagementConsole",
        lwc_template_data_roles=CASE_SEMANTIC_DATA_ROLES,
        lwc_template_data_states=CASE_SEMANTIC_DATA_STATES,
        permission_objects=frozenset({"Account", "Contact", "Case"}),
        permission_class_accesses=frozenset(
            {
                "CaseManagementConsoleController",
                "LegacyCaseManagementConsoleController",
                "LegacyCaseQueryService",
            }
        ),
        permission_field_permissions=frozenset(
            {
                "Case.AccountId",
                "Case.ContactId",
                "Case.Description",
                "Case.Priority",
                "Case.Subject",
            }
        ),
        permission_page_access="LegacyCaseManagementConsole",
    ),
}


def check_dependency_closure(
    root: Path,
    *,
    unit_id: str = SALESFORCE_ACCOUNT_CONTACT_UNIT_ID,
) -> tuple[dict[str, Any], Any]:
    """Require a resolved target LWC/Apex/test/permission dependency closure."""

    spec = _SALESFORCE_CLOSURE_BY_UNIT.get(unit_id)
    if spec is None:
        raise LocalCheckFailure("unsupported Salesforce migration unit")

    root = _root(root)
    revision = content_revision(root)
    graph = build_salesforce_dependency_graph(root, (spec.target_entry,), revision)
    _require(not graph.has_unresolved, "resolved dependency closure")
    expected = set(spec.expected_nodes)
    observed = {(node.kind, node.name) for node in graph.nodes if node.resolved}
    _require(expected <= observed, "required dependency nodes")
    return (
        {
            "check": "salesforce-target-dependency-closure",
            "passed": True,
            "base_revision": revision,
            "nodes": len(graph.nodes),
            "edges": len(graph.edges),
            "warnings": len(graph.warnings),
            "unresolved": False,
        },
        graph,
    )


def tree_fingerprint(root: Path) -> str:
    """Hash paths, bytes, modes, and directories for a read-only tree audit."""

    snapshot = snapshot_tree(_root(root))
    return artifact_digest(
        {
            "directories": snapshot.directories,
            "entries": [
                {
                    "path": entry.path,
                    "mode": entry.mode,
                    "content_sha256": hashlib.sha256(entry.content).hexdigest(),
                }
                for entry in snapshot.entries
            ],
        }
    )


def check_workspace_revision(root: Path, expected_revision: str) -> dict[str, Any]:
    """Prove prior validation commands did not mutate bytes, modes, or layout."""

    resolved = _root(root)
    actual = tree_fingerprint(resolved)
    _require(actual == expected_revision, "workspace revision")
    return {
        "check": "workspace-read-only",
        "passed": True,
        "expected_revision": expected_revision,
        "actual_revision": actual,
        "content_revision": content_revision(resolved),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="legacy-migration-local-check")
    subparsers = parser.add_subparsers(dest="command", required=True)
    candidate = subparsers.add_parser("candidate-contract")
    candidate.add_argument("--unit", default=SALESFORCE_ACCOUNT_CONTACT_UNIT_ID)
    dependency = subparsers.add_parser("dependency-closure")
    dependency.add_argument("--unit", default=SALESFORCE_ACCOUNT_CONTACT_UNIT_ID)
    toolchain = subparsers.add_parser("toolchain-contract")
    toolchain.add_argument("--toolchain-root", type=Path, required=True)
    toolchain.add_argument("--unit", default=SALESFORCE_ACCOUNT_CONTACT_UNIT_ID)
    revision = subparsers.add_parser("workspace-revision")
    revision.add_argument("--expected", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "candidate-contract":
            result = check_salesforce_candidate(Path.cwd(), unit_id=args.unit)
        elif args.command == "dependency-closure":
            result, _ = check_dependency_closure(Path.cwd(), unit_id=args.unit)
        elif args.command == "toolchain-contract":
            result = check_lwc_jest_toolchain(args.toolchain_root, unit_id=args.unit)
        elif args.command == "workspace-revision":
            result = check_workspace_revision(Path.cwd(), args.expected)
        else:  # pragma: no cover - argparse makes this unreachable
            raise AssertionError(f"unhandled command: {args.command}")
    except Exception as exc:
        failure_code = (
            exc.failure_code
            if isinstance(exc, LocalCheckFailure)
            and exc.failure_code in SALESFORCE_CANDIDATE_FAILURE_CODES
            else "salesforce_candidate_unclassified"
        )
        diagnostic_ids = exc.diagnostic_ids if isinstance(exc, LocalCheckFailure) else ()
        diagnostic_ids = tuple(
            diagnostic_id
            for diagnostic_id in diagnostic_ids
            if diagnostic_id in SALESFORCE_CANDIDATE_STATIC_DIAGNOSTIC_IDS
        )
        print(
            json.dumps(
                {
                    "check": args.command,
                    "passed": False,
                    "failure_type": type(exc).__name__,
                    "failure_code": failure_code,
                    "diagnostic_ids": diagnostic_ids,
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def _check_manifest(package_root: ElementTree.Element, profile: _SalesforceUnitProfile) -> None:
    _require(
        package_root.tag == f"{{{METADATA_NAMESPACE}}}Package",
        "package metadata root",
    )
    _require(
        package_root.findtext(f"{{{METADATA_NAMESPACE}}}version") == API_VERSION,
        "package API version",
    )
    observed: dict[str, set[str]] = {}
    for type_element in package_root.findall(f"{{{METADATA_NAMESPACE}}}types"):
        name = type_element.findtext(f"{{{METADATA_NAMESPACE}}}name")
        members = {
            member.text
            for member in type_element.findall(f"{{{METADATA_NAMESPACE}}}members")
            if member.text is not None
        }
        _require(name is not None and name not in observed and bool(members), "package type")
        observed[cast(str, name)] = members
    _require(
        observed == profile.manifest_members,
        "package members",
    )


def _check_apex_metadata(root: ElementTree.Element) -> None:
    _require(root.tag == f"{{{METADATA_NAMESPACE}}}ApexClass", "Apex metadata root")
    _require(
        root.findtext(f"{{{METADATA_NAMESPACE}}}apiVersion") == API_VERSION,
        "Apex API version",
    )
    _require(
        root.findtext(f"{{{METADATA_NAMESPACE}}}status") == "Active",
        "Apex status",
    )


def _apex_method_bounds(
    code: str,
    *,
    return_type: str,
    method_name: str,
    arguments: str,
) -> tuple[int, int]:
    """Return one public method-body span from the comment/string-free Apex view."""

    matches = tuple(
        re.finditer(
            rf"public\s+static\s+List\s*<\s*{re.escape(return_type)}\s*>\s+"
            rf"{re.escape(method_name)}\s*{arguments}\s*\{{",
            code,
            re.I,
        )
    )
    _require(len(matches) == 1, f"{method_name} Apex method body")
    opening = matches[0].end() - 1
    depth = 0
    for index in range(opening, len(code)):
        character = code[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return opening + 1, index
            _require(depth >= 0, f"{method_name} Apex method braces")
    raise LocalCheckFailure(f"{method_name} Apex method braces")


def _apex_static_queries(body: str) -> tuple[str, ...]:
    return tuple(
        match.group(0) for match in re.finditer(r"\[\s*SELECT\b.*?\]", body, re.I | re.DOTALL)
    )


def _require_soql_contract(
    query: str,
    *,
    object_name: str,
    fields: frozenset[str],
    order_by: tuple[str, ...],
    order_direction: str | None,
    max_rows: int,
    integer_constants: Mapping[str, int],
) -> None:
    select = re.search(
        rf"\bSELECT\b(?P<fields>.*?)\bFROM\s+{re.escape(object_name)}\b",
        query,
        re.I | re.DOTALL,
    )
    _require(select is not None, f"{object_name} method-bound query")
    assert select is not None
    observed_fields = frozenset(
        re.sub(r"\s+", "", field).casefold()
        for field in select.group("fields").split(",")
        if field.strip()
    )
    _require(
        observed_fields == frozenset(field.casefold() for field in fields),
        f"{object_name} query fields",
    )
    order_clauses = tuple(
        re.finditer(
            r"\bORDER\s+BY\s+(?P<clause>.*?)"
            r"(?=\b(?:LIMIT|WITH\s+USER_MODE)\b)",
            query,
            re.I | re.DOTALL,
        )
    )
    _require(len(order_clauses) == 1, f"{object_name} query order")
    order_items = tuple(order_clauses[0].group("clause").split(","))
    _require(len(order_items) == len(order_by), f"{object_name} query order")
    expected_direction = (order_direction or "ASC").casefold()
    observed_order: list[tuple[str, str]] = []
    for item in order_items:
        parsed = re.fullmatch(
            r"\s*(?P<field>[A-Za-z_][A-Za-z0-9_.]*)"
            r"(?:\s+(?P<direction>ASC|DESC))?\s*",
            item,
            re.I,
        )
        _require(parsed is not None, f"{object_name} query order")
        assert parsed is not None
        observed_order.append(
            (
                parsed.group("field").casefold(),
                (parsed.group("direction") or "ASC").casefold(),
            )
        )
    _require(
        tuple(observed_order)
        == tuple((field.casefold(), expected_direction) for field in order_by),
        f"{object_name} query order",
    )
    limit = re.search(
        r"\bLIMIT\s+(?:(?P<binding>:[A-Za-z_][A-Za-z0-9_]*)|(?P<literal>[1-9][0-9]*))\b",
        query,
        re.I,
    )
    _require(limit is not None, f"{object_name} bounded query")
    assert limit is not None
    if limit.group("literal") is not None:
        limit_value = int(limit.group("literal"))
    else:
        constant_name = limit.group("binding")[1:].casefold()
        _require(
            constant_name in integer_constants,
            f"{object_name} compile-time query limit",
        )
        limit_value = integer_constants[constant_name]
    _require(
        1 <= limit_value <= max_rows,
        f"{object_name} query row cap",
    )
    _require(
        re.search(r"\bWITH\s+USER_MODE\b", query, re.I) is not None,
        f"{object_name} user-mode query",
    )


def _apex_integer_constants(code: str) -> dict[str, int]:
    constants: dict[str, int] = {}
    for match in re.finditer(
        r"\b(?:public|private|protected|global)?\s*"
        r"(?:(?:static\s+final)|(?:final\s+static))\s+Integer\s+"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>[0-9]+)\s*;",
        code,
        re.I,
    ):
        constants[match.group("name").casefold()] = int(match.group("value"))
    return constants


def _matching_block_end(code: str, opening: int, label: str) -> int:
    _require(0 <= opening < len(code) and code[opening] == "{", f"{label} opening brace")
    depth = 0
    for index in range(opening, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                return index
            _require(depth >= 0, f"{label} braces")
    raise LocalCheckFailure(f"{label} braces")


_TECHNICAL_APEX_MESSAGE_MARKERS: Final = (
    "exception",
    "stack trace",
    "select ",
    " from ",
    "accountid",
    "getmessage",
)


def _is_nontechnical_apex_message(message: str) -> bool:
    """Return whether a fixed user-facing message leaks no technical detail."""

    normalized = message.casefold()
    return not any(marker in normalized for marker in _TECHNICAL_APEX_MESSAGE_MARKERS)


def _apex_brace_depth(code: str, offset: int) -> int:
    """Return lexical brace depth immediately before ``offset``."""

    return code[:offset].count("{") - code[:offset].count("}")


def _safe_apex_message_constants(code: str, source: str) -> frozenset[str]:
    """Collect class ``static final String`` constants holding safe fixed messages.

    The declaration is located in the lexical view (so a ``final String`` phrase
    inside a comment or string literal cannot masquerade as one) and its
    initializer must be a single string literal, read back from the original
    source and screened for technical-disclosure markers. Only safe names are
    returned, so a catch that references an unknown or leaky constant still fails.
    """

    safe: set[str] = set()
    for match in re.finditer(
        r"\b(?:public|private|protected|global)?\s*"
        r"(?:(?:static\s+final)|(?:final\s+static))\s+String\s+"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=",
        code,
        re.I,
    ):
        if _apex_brace_depth(code, match.start()) != 1:
            continue
        # In the lexical view a string literal (quotes included) is blanked to
        # spaces, so the initializer span cannot be captured by a regex group —
        # a greedy pattern would swallow it as whitespace. Anchor on ``=`` and
        # take the initializer as the source text up to the next statement
        # terminator (``;`` never appears inside a blanked literal).
        value_start = match.end()
        semicolon = code.find(";", value_start)
        if semicolon == -1:
            continue
        # A blank lexical-view initializer means it holds only a string literal;
        # any operator, identifier, or call would leave visible code characters.
        if code[value_start:semicolon].strip():
            continue
        literal = re.fullmatch(
            r"\s*'(?P<message>(?:\\.|[^'\\\r\n]){1,200})'\s*",
            source[value_start:semicolon],
        )
        if literal is None:
            continue
        if _is_nontechnical_apex_message(literal.group("message")):
            safe.add(match.group("name").casefold())
    return frozenset(safe)


def _safe_aura_handled_exception_helpers(
    code: str,
    safe_message_constants: frozenset[str],
) -> frozenset[str]:
    """Collect tightly bounded class-level safe-exception helper methods.

    A helper is accepted only when it is ``private static``, takes one ``String``,
    and has a straight-line body that constructs an ``AuraHandledException``
    from that parameter, optionally calls ``setMessage`` with the same parameter,
    and returns the same local instance. The lexical view makes comments and
    string literals inert, while an exact body match rejects reassignment,
    branches, extra calls or statements, and alternate returns.
    """

    safe: set[str] = set()
    for declaration in re.finditer(
        r"\bprivate\s+static\s+AuraHandledException\s+"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*"
        r"\(\s*String\s+(?P<parameter>[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*\{",
        code,
        re.I,
    ):
        if _apex_brace_depth(code, declaration.start()) != 1:
            continue
        opening = declaration.end() - 1
        closing = _matching_block_end(code, opening, "safe exception helper")
        body = code[opening + 1 : closing]
        parameter = declaration.group("parameter")
        parsed = re.fullmatch(
            rf"\s*AuraHandledException\s+"
            rf"(?P<variable>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
            rf"new\s+AuraHandledException\s*\(\s*{re.escape(parameter)}\s*\)\s*;"
            rf"(?:\s*(?P=variable)\s*\.\s*setMessage\s*"
            rf"\(\s*{re.escape(parameter)}\s*\)\s*;)?"
            rf"\s*return\s+(?P=variable)\s*;\s*",
            body,
            re.I | re.DOTALL,
        )
        if parsed is None:
            continue
        variable = parsed.group("variable")
        if (
            variable.casefold() == parameter.casefold()
            or variable.casefold() in safe_message_constants
            or parameter.casefold() in safe_message_constants
        ):
            continue
        safe.add(declaration.group("name").casefold())
    return frozenset(safe)


def _require_fixed_safe_apex_message(
    code_argument: str,
    source_argument: str,
    *,
    scope_code: str,
    object_name: str,
    safe_message_constants: frozenset[str],
) -> None:
    """Require one literal or unshadowed class safe-message constant."""

    code_argument = code_argument.strip()
    if code_argument:
        identifier = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", code_argument)
        _require(
            identifier is not None and code_argument.casefold() in safe_message_constants,
            f"{object_name} fixed safe error message",
        )
        # A method-local declaration with the same identifier would shadow the
        # proven class constant, so the value at the throw site would no longer
        # be statically bound to the screened literal.
        shadow = re.search(
            rf"\b[A-Za-z_][A-Za-z0-9_.]*(?:\s*<[^{{}}();]+>)?(?:\s*\[\s*\])?"
            rf"\s+{re.escape(code_argument)}\b",
            scope_code,
            re.I,
        )
        _require(shadow is None, f"{object_name} unshadowed safe error message")
        return

    literal = re.fullmatch(
        r"\s*'(?P<message>(?:\\.|[^'\\\r\n]){1,200})'\s*",
        source_argument,
    )
    _require(literal is not None, f"{object_name} fixed safe error message")
    assert literal is not None
    _require(
        _is_nontechnical_apex_message(literal.group("message")),
        f"{object_name} nontechnical error message",
    )


def _require_controlled_query_failure(
    code_body: str,
    source_body: str,
    query: str,
    *,
    query_offset: int,
    object_name: str,
    safe_message_constants: frozenset[str],
    safe_exception_helpers: frozenset[str],
) -> tuple[str, int] | None:
    _require(
        query_offset >= 0 and code_body[query_offset : query_offset + len(query)] == query,
        f"{object_name} query position",
    )
    try_blocks = tuple(re.finditer(r"\btry\s*\{", code_body[:query_offset], re.I))
    _require(bool(try_blocks), f"{object_name} guarded query")
    try_opening = try_blocks[-1].end() - 1
    try_end = _matching_block_end(code_body, try_opening, f"{object_name} try block")
    _require(query_offset < try_end, f"{object_name} query inside try block")

    catch = re.match(
        r"\s*catch\s*\(\s*(?:QueryException|Exception)\s+"
        r"[A-Za-z_][A-Za-z0-9_]*\s*\)\s*\{",
        code_body[try_end + 1 :],
        re.I,
    )
    _require(catch is not None, f"{object_name} query catch block")
    assert catch is not None
    catch_opening = try_end + 1 + catch.end() - 1
    catch_end = _matching_block_end(code_body, catch_opening, f"{object_name} catch block")
    catch_code = code_body[catch_opening + 1 : catch_end]
    catch_source = source_body[catch_opening + 1 : catch_end]
    # A controlled catch throws exactly one AuraHandledException whose every
    # user-facing message is a fixed safe literal. Three equivalent shapes are
    # accepted: an inline ``throw new AuraHandledException(<message>);`` and a
    # ``AuraHandledException e = new AuraHandledException(<message>);
    # [e.setMessage(<message>);] throw e;`` construction, plus a direct throw of
    # one tightly constrained private-static helper result. In every form,
    # <message> must be an inline string literal or a reference to a class-level
    # safe ``static final String`` constant; a dynamic value, concatenation, or
    # exception passthrough is rejected.
    throw_statements = tuple(
        re.finditer(r"\bthrow\b(?P<expr>[^;]*);", catch_code, re.I | re.DOTALL)
    )
    _require(len(throw_statements) == 1, f"{object_name} controlled query failure")
    throw = throw_statements[0]
    expr_offset = throw.start("expr")
    throw_expr = throw.group("expr")
    message_spans: list[tuple[int, int]] = []

    inline = re.fullmatch(
        r"\s*new\s+AuraHandledException\s*\((?P<argument>.*?)\)\s*",
        throw_expr,
        re.I | re.DOTALL,
    )
    if inline is not None:
        _require(
            len(re.findall(r"\bnew\s+AuraHandledException\b", catch_code, re.I)) == 1,
            f"{object_name} single safe exception",
        )
        message_spans.append(
            (expr_offset + inline.start("argument"), expr_offset + inline.end("argument"))
        )
    else:
        thrown = re.fullmatch(r"\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*", throw_expr)
        if thrown is not None:
            _require(
                len(re.findall(r"\bnew\s+AuraHandledException\b", catch_code, re.I)) == 1,
                f"{object_name} single safe exception",
            )
            variable = thrown.group("name")
            constructions = tuple(
                re.finditer(
                    rf"\bAuraHandledException\s+{re.escape(variable)}\s*=\s*"
                    r"new\s+AuraHandledException\s*\((?P<argument>.*?)\)\s*;",
                    catch_code,
                    re.I | re.DOTALL,
                )
            )
            _require(len(constructions) == 1, f"{object_name} controlled query failure")
            construction = constructions[0]
            message_spans.append((construction.start("argument"), construction.end("argument")))
            for setter in re.finditer(
                rf"\b{re.escape(variable)}\s*\.\s*setMessage\s*"
                r"\((?P<argument>.*?)\)\s*;",
                catch_code,
                re.I | re.DOTALL,
            ):
                message_spans.append((setter.start("argument"), setter.end("argument")))
        else:
            helper_call = re.fullmatch(
                r"\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*"
                r"\((?P<argument>.*?)\)\s*",
                throw_expr,
                re.I | re.DOTALL,
            )
            _require(helper_call is not None, f"{object_name} controlled query failure")
            assert helper_call is not None
            helper_name = helper_call.group("name").casefold()
            _require(
                helper_name in safe_exception_helpers,
                f"{object_name} controlled query failure",
            )
            # Helper-backed catches are intentionally only the throw statement;
            # this prevents side effects or shadowing from changing the proven
            # argument before it reaches the safe helper.
            _require(
                re.fullmatch(
                    r"\s*throw\s+[A-Za-z_][A-Za-z0-9_]*\s*\(.*?\)\s*;\s*",
                    catch_code,
                    re.I | re.DOTALL,
                )
                is not None,
                f"{object_name} controlled query failure",
            )
            argument_start = expr_offset + helper_call.start("argument")
            argument_end = expr_offset + helper_call.end("argument")
            _require_fixed_safe_apex_message(
                catch_code[argument_start:argument_end],
                catch_source[argument_start:argument_end],
                scope_code=code_body,
                object_name=object_name,
                safe_message_constants=safe_message_constants,
            )
            return helper_name, expr_offset + helper_call.start("name")

    for start, end in message_spans:
        _require_fixed_safe_apex_message(
            catch_code[start:end],
            catch_source[start:end],
            scope_code=code_body,
            object_name=object_name,
            safe_message_constants=safe_message_constants,
        )
    return None


def _apex_comment_free_view(source: str) -> str:
    """Blank Apex comments while preserving strings and source offsets."""

    view = list(source)
    index = 0
    while index < len(source):
        if source.startswith("//", index):
            view[index] = " "
            view[index + 1] = " "
            index += 2
            while index < len(source) and source[index] != "\n":
                view[index] = " "
                index += 1
            continue
        if source.startswith("/*", index):
            view[index] = " "
            view[index + 1] = " "
            index += 2
            terminated = False
            while index < len(source):
                if source.startswith("*/", index):
                    view[index] = " "
                    view[index + 1] = " "
                    index += 2
                    terminated = True
                    break
                if source[index] != "\n":
                    view[index] = " "
                index += 1
            _require(terminated, "terminated Apex block comment")
            continue
        if source[index] == "'":
            index += 1
            terminated = False
            while index < len(source):
                if source[index] == "\\":
                    _require(index + 1 < len(source), "terminated Apex string escape")
                    index += 2
                    continue
                if source[index] == "'":
                    index += 1
                    terminated = True
                    break
                index += 1
            _require(terminated, "terminated Apex string")
            continue
        index += 1
    return "".join(view)


_CASE_STATUS_VALUES: Final[frozenset[str]] = frozenset({"OPEN", "CLOSED", "ALL"})
_CASE_STATUS_ATOM_PATTERN: Final = r"(?:'[A-Za-z]{1,16}'|[A-Za-z_][A-Za-z0-9_]*)"


@dataclass(frozen=True)
class _CaseStatusRouting:
    """Statically proven Case status values and authoritative input aliases."""

    constants: Mapping[str, str]
    subjects: frozenset[str]


def _apex_literal_status_constants(source: str) -> dict[str, str]:
    """Resolve direct ``static final String`` status literals by value.

    Names carry no meaning here. Only a unique declaration whose initializer is
    one literal OPEN, CLOSED, or ALL value is made available to the bounded Case
    routing checks and the generated Apex-test coverage check.
    """

    code, _ = _apex_lexical_view(source)
    constants: dict[str, str] = {}
    seen_literal_names: set[str] = set()
    for match in re.finditer(
        r"\b(?:public|private|protected|global)?\s*"
        r"(?:(?:static\s+final)|(?:final\s+static))\s+String\s+"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=",
        code,
        re.I,
    ):
        value_start = match.end()
        semicolon = code.find(";", value_start)
        if semicolon == -1 or code[value_start:semicolon].strip():
            continue
        literal = re.fullmatch(
            r"\s*'(?P<value>(?:\\.|[^'\\\r\n]){1,32})'\s*",
            source[value_start:semicolon],
        )
        if literal is None:
            continue
        name = match.group("name").casefold()
        _require(name not in seen_literal_names, "unique Apex String constant")
        seen_literal_names.add(name)
        value = literal.group("value").upper()
        if value in _CASE_STATUS_VALUES:
            constants[name] = value
    return constants


def _case_status_atom_value(
    atom: str,
    constants: Mapping[str, str],
) -> str | None:
    literal = re.fullmatch(r"'(?P<value>[A-Za-z]{1,16})'", atom.strip())
    if literal is not None:
        value = literal.group("value").upper()
        return value if value in _CASE_STATUS_VALUES else None
    identifier = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", atom.strip())
    if identifier is None:
        return None
    return constants.get(identifier.group(0).casefold())


def _case_status_comparison_value(
    left: str,
    right: str,
    routing: _CaseStatusRouting,
) -> str | None:
    left_name = left.strip().casefold()
    right_name = right.strip().casefold()
    if left_name in routing.subjects:
        return _case_status_atom_value(right, routing.constants)
    if right_name in routing.subjects:
        return _case_status_atom_value(left, routing.constants)
    return None


def _case_status_condition_value(
    condition: str,
    routing: _CaseStatusRouting,
) -> str | None:
    equality = re.fullmatch(
        rf"\s*(?P<left>{_CASE_STATUS_ATOM_PATTERN})\s*==\s*"
        rf"(?P<right>{_CASE_STATUS_ATOM_PATTERN})\s*",
        condition,
        re.I,
    )
    if equality is not None:
        return _case_status_comparison_value(
            equality.group("left"),
            equality.group("right"),
            routing,
        )
    equals_ignore_case = re.fullmatch(
        rf"\s*(?P<subject>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*"
        rf"equalsIgnoreCase\s*\(\s*(?P<status>{_CASE_STATUS_ATOM_PATTERN})\s*\)\s*",
        condition,
        re.I,
    )
    if (
        equals_ignore_case is None
        or equals_ignore_case.group("subject").casefold() not in routing.subjects
    ):
        return None
    return _case_status_atom_value(equals_ignore_case.group("status"), routing.constants)


def _require_case_normalization_helper(
    controller_code: str,
    controller_source: str,
    *,
    helper_name: str,
    constants: Mapping[str, str],
) -> None:
    """Prove one bounded CLOSED/ALL pass-through helper with OPEN default."""

    helpers = tuple(
        re.finditer(
            rf"\bprivate\s+static\s+String\s+{re.escape(helper_name)}\s*"
            r"\(\s*String\s+(?P<parameter>[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*\{",
            controller_code,
            re.I,
        )
    )
    _require(len(helpers) == 1, "Case normalization helper")
    helper = helpers[0]
    opening = helper.end() - 1
    closing = _matching_block_end(controller_code, opening, "Case normalization helper")
    helper_source = controller_source[opening + 1 : closing]
    helper_comment_free = _apex_comment_free_view(helper_source)
    helper_lexical, _ = _apex_lexical_view(helper_source)
    guard = re.match(
        r"\s*if\s*\(\s*(?P<condition>[^()]{1,256}?)\s*\)\s*\{",
        helper_comment_free,
        re.I,
    )
    _require(guard is not None, "Case normalization guard")
    assert guard is not None
    guard_opening = guard.end() - 1
    guard_closing = _matching_block_end(
        helper_lexical,
        guard_opening,
        "Case normalization guard",
    )
    parameter = helper.group("parameter")
    _require(
        re.fullmatch(
            rf"\s*return\s+{re.escape(parameter)}\s*;\s*",
            helper_comment_free[guard_opening + 1 : guard_closing],
            re.I,
        )
        is not None,
        "Case normalization unchanged return",
    )
    default_return = re.fullmatch(
        rf"\s*return\s+(?P<status>{_CASE_STATUS_ATOM_PATTERN})\s*;\s*",
        helper_comment_free[guard_closing + 1 :],
        re.I,
    )
    _require(default_return is not None, "Case normalization default return")
    assert default_return is not None
    _require(
        _case_status_atom_value(default_return.group("status"), constants) == "OPEN",
        "Case normalization OPEN default",
    )

    comparisons = tuple(part.strip() for part in guard.group("condition").split("||"))
    helper_routing = _CaseStatusRouting(
        constants=constants,
        subjects=frozenset({parameter.casefold()}),
    )
    values = tuple(
        _case_status_condition_value(comparison, helper_routing) for comparison in comparisons
    )
    _require(
        len(values) == 2 and frozenset(values) == frozenset({"CLOSED", "ALL"}),
        "Case normalization CLOSED ALL pass-through",
    )


def _case_status_routing(
    controller_code: str,
    controller_source: str,
    code_body: str,
    constants: Mapping[str, str],
) -> _CaseStatusRouting:
    """Return the public status parameter and at most one proven normalized alias."""

    aliases = tuple(
        re.finditer(
            r"\b(?:final\s+)?String\s+(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
            r"(?P<helper>[A-Za-z_][A-Za-z0-9_]*)\s*\(\s*statusFilter\s*\)\s*;",
            code_body,
            re.I,
        )
    )
    _require(len(aliases) <= 1, "single Case normalization alias")
    subjects = {"statusfilter"}
    if aliases:
        alias = aliases[0].group("alias")
        _require(alias.casefold() != "statusfilter", "distinct Case normalization alias")
        assignments = tuple(
            re.finditer(
                rf"(?:\+\+|--)\s*\b{re.escape(alias)}\b|"
                rf"\b{re.escape(alias)}\b\s*(?:\+\+|--|(?:<<|>>|[+\-*/%&|^])?=(?!=))",
                code_body,
                re.I,
            )
        )
        _require(len(assignments) == 1, "immutable Case normalization alias")
        _require_case_normalization_helper(
            controller_code,
            controller_source,
            helper_name=aliases[0].group("helper"),
            constants=constants,
        )
        subjects.add(alias.casefold())
    return _CaseStatusRouting(constants=constants, subjects=frozenset(subjects))


def _case_status_condition_blocks(
    source_body: str,
    routing: _CaseStatusRouting,
) -> tuple[tuple[str, int, int], ...]:
    """Return proven public-status equality branches with their body spans."""

    comment_free = _apex_comment_free_view(source_body)
    lexical_code, _ = _apex_lexical_view(source_body)
    patterns = (
        rf"\bif\s*\(\s*(?P<condition>{_CASE_STATUS_ATOM_PATTERN}\s*==\s*"
        rf"{_CASE_STATUS_ATOM_PATTERN})\s*\)\s*\{{",
        rf"\bif\s*\(\s*(?P<condition>[A-Za-z_][A-Za-z0-9_]*\s*\.\s*"
        rf"equalsIgnoreCase\s*\(\s*{_CASE_STATUS_ATOM_PATTERN}\s*\))\s*\)\s*\{{",
    )
    blocks: list[tuple[str, int, int]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, comment_free, re.I):
            if lexical_code[match.start() : match.start() + 2].casefold() != "if":
                continue
            value = _case_status_condition_value(match.group("condition"), routing)
            if value is None:
                continue
            opening = match.end() - 1
            blocks.append(
                (
                    value,
                    opening + 1,
                    _matching_block_end(lexical_code, opening, "Case status branch"),
                )
            )
    return tuple(sorted(blocks, key=lambda item: (item[1], item[2], item[0])))


def _case_status_branch_for_offset(
    blocks: tuple[tuple[str, int, int], ...],
    offset: int,
) -> str | None:
    enclosing = tuple(block for block in blocks if block[1] <= offset < block[2])
    if not enclosing:
        return None
    return min(enclosing, key=lambda item: item[2] - item[1])[0]


def _case_query_status_shape(query: str) -> tuple[str, str | None]:
    where = re.search(r"\bWHERE\b(?P<body>.*?)\bWITH\s+USER_MODE\b", query, re.I | re.DOTALL)
    _require(where is not None, "Case query WHERE clause")
    assert where is not None
    predicate = where.group("body")
    if re.search(r"\bIsClosed\s*=\s*false\b", predicate, re.I):
        return "OPEN", None
    if re.search(r"\bIsClosed\s*=\s*true\b", predicate, re.I):
        return "CLOSED", None
    collection = re.search(
        r"\bIsClosed\s+IN\s*:(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b",
        predicate,
        re.I,
    )
    if collection is not None:
        return "BOUND_COLLECTION", collection.group("name")
    boolean = re.search(
        r"\bIsClosed\s*=\s*:(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b",
        predicate,
        re.I,
    )
    if boolean is not None:
        return "BOUND_BOOLEAN", boolean.group("name")
    _require(
        re.search(r"\b(?:IsClosed|Status)\b", predicate, re.I) is None,
        "Case supported status predicate",
    )
    return "ALL", None


def _boolean_values(material: str) -> frozenset[bool]:
    values = tuple(value.casefold() for value in re.findall(r"\b(?:true|false)\b", material, re.I))
    _require(bool(values), "Case status Boolean values")
    _require(
        re.sub(r"(?i)\b(?:true|false)\b|[\s,]", "", material) == "",
        "Case status Boolean collection",
    )
    return frozenset(value == "true" for value in values)


def _require_case_collection_filter_mapping(
    source_body: str,
    variable: str,
    blocks: tuple[tuple[str, int, int], ...],
    query_offset: int,
) -> None:
    view = _apex_comment_free_view(source_body)
    assignment = re.compile(
        rf"\b{re.escape(variable)}\s*=\s*new\s+(?:List|Set)\s*<\s*Boolean\s*>\s*"
        r"\{(?P<values>[^{};]{1,80})\}\s*;",
        re.I,
    )
    matches = tuple(assignment.finditer(view))
    all_assignments = tuple(
        re.finditer(rf"\b{re.escape(variable)}\s*(?<![=!<>])=(?!=)", view, re.I)
    )
    _require(
        bool(matches) and len(matches) == len(all_assignments),
        "Case status collection assignments",
    )
    observed = tuple(
        (
            _case_status_branch_for_offset(blocks, match.start()),
            _boolean_values(match.group("values")),
            match.start(),
        )
        for match in matches
    )
    by_branch = {branch: (values, offset) for branch, values, offset in observed}
    _require(len(observed) == 3 and len(by_branch) == 3, "Case status collection mapping")
    open_branch = None if None in by_branch else "OPEN"
    _require(
        open_branch in by_branch
        and by_branch[open_branch][0] == frozenset({False})
        and by_branch.get("CLOSED", (frozenset(), -1))[0] == frozenset({True})
        and by_branch.get("ALL", (frozenset(), -1))[0] == frozenset({False, True})
        and all(offset < query_offset for _, offset in by_branch.values()),
        "Case OPEN CLOSED ALL status mapping",
    )
    if open_branch is None:
        first_status_branch = min((start for _, start, _ in blocks), default=query_offset)
        _require(
            by_branch[None][1] < first_status_branch,
            "Case default OPEN status initialization",
        )


def _require_case_boolean_filter_mapping(
    source_body: str,
    variable: str,
    blocks: tuple[tuple[str, int, int], ...],
    query_offset: int,
    routing: _CaseStatusRouting,
) -> None:
    view = _apex_comment_free_view(source_body)
    direct_candidates = tuple(
        match
        for match in re.finditer(
            rf"\b{re.escape(variable)}\s*=\s*(?P<condition>[^;\r\n]{{1,160}})\s*;",
            view,
            re.I,
        )
        if _case_status_condition_value(match.group("condition"), routing) == "CLOSED"
    )
    if direct_candidates:
        all_assignments = tuple(
            re.finditer(rf"\b{re.escape(variable)}\s*(?<![=!<>])=(?!=)", view, re.I)
        )
        _require(
            len(direct_candidates) == 1
            and len(all_assignments) == 1
            and direct_candidates[0].start() < query_offset,
            "Case direct CLOSED Boolean mapping",
        )
        return
    assignments = tuple(
        re.finditer(
            rf"\b{re.escape(variable)}\s*(?<![=!<>])=(?!=)\s*(?P<value>true|false)\s*;",
            view,
            re.I,
        )
    )
    all_assignments = tuple(
        re.finditer(rf"\b{re.escape(variable)}\s*(?<![=!<>])=(?!=)", view, re.I)
    )
    _require(
        bool(assignments) and len(assignments) == len(all_assignments),
        "Case status Boolean assignments",
    )
    observed = tuple(
        (
            _case_status_branch_for_offset(blocks, match.start()),
            match.group("value").casefold() == "true",
            match.start(),
        )
        for match in assignments
    )
    by_branch = {branch: (value, offset) for branch, value, offset in observed}
    open_branch = None if None in by_branch else "OPEN"
    _require(
        len(observed) == 2
        and len(by_branch) == 2
        and open_branch in by_branch
        and by_branch[open_branch][0] is False
        and by_branch.get("CLOSED", (False, -1))[0] is True
        and all(offset < query_offset for _, offset in by_branch.values()),
        "Case OPEN CLOSED Boolean mapping",
    )
    if open_branch is None:
        closed_starts = tuple(start for value, start, _ in blocks if value == "CLOSED")
        _require(
            bool(closed_starts) and by_branch[None][1] < min(closed_starts),
            "Case default OPEN Boolean initialization",
        )


def _require_case_status_filter_contract(
    controller_code: str,
    controller_source: str,
    code_body: str,
    source_body: str,
    queries: tuple[tuple[str, int], ...],
    constants: Mapping[str, str],
) -> None:
    """Prove OPEN, CLOSED, and ALL map to distinct bounded query behavior."""

    routing = _case_status_routing(
        controller_code,
        controller_source,
        code_body,
        constants,
    )
    blocks = _case_status_condition_blocks(source_body, routing)
    shapes = tuple(
        (shape, variable, _case_status_branch_for_offset(blocks, offset), offset)
        for query, offset in queries
        for shape, variable in (_case_query_status_shape(query),)
    )
    if len(shapes) == 3:
        _require(
            {shape for shape, _, _, _ in shapes} == _CASE_STATUS_VALUES,
            "Case OPEN CLOSED ALL query coverage",
        )
        guarded = tuple(
            (shape, branch, offset) for shape, _, branch, offset in shapes if branch is not None
        )
        _require(
            len(guarded) >= 2
            and all(shape == branch for shape, branch, _ in guarded)
            and sum(branch is None for _, _, branch, _ in shapes) <= 1,
            "Case status branch routing",
        )
        unguarded_offsets = tuple(offset for _, _, branch, offset in shapes if branch is None)
        _require(
            not unguarded_offsets or unguarded_offsets[0] > max(offset for _, _, offset in guarded),
            "Case default status query routing",
        )
        return
    if len(shapes) == 2:
        by_shape = {shape: (variable, branch, offset) for shape, variable, branch, offset in shapes}
        _require(
            set(by_shape) == {"ALL", "BOUND_BOOLEAN"},
            "Case two-query status strategy",
        )
        _require(by_shape["ALL"][1] == "ALL", "Case ALL query routing")
        variable, bound_branch, bound_offset = by_shape["BOUND_BOOLEAN"]
        _require(bound_branch is None and variable is not None, "Case Boolean query routing")
        _require(bound_offset > by_shape["ALL"][2], "Case default Boolean query routing")
        assert variable is not None
        _require_case_boolean_filter_mapping(
            source_body,
            variable,
            blocks,
            bound_offset,
            routing,
        )
        return
    _require(len(shapes) == 1, "Case status query strategy")
    shape, variable, branch, query_offset = shapes[0]
    _require(
        shape == "BOUND_COLLECTION" and variable is not None and branch is None,
        "Case one-query status strategy",
    )
    assert variable is not None
    _require_case_collection_filter_mapping(source_body, variable, blocks, query_offset)


def _without_static_queries(code_body: str) -> str:
    """Blank static SOQL while preserving line breaks and source offsets."""

    scrubbed = list(code_body)
    for match in re.finditer(r"\[\s*SELECT\b.*?\]", code_body, re.I | re.DOTALL):
        for offset in range(match.start(), match.end()):
            if scrubbed[offset] != "\n":
                scrubbed[offset] = " "
    return "".join(scrubbed)


def _require_immutable_parameter_authority(code_body: str, parameter: str) -> None:
    """Reject local shadowing or reassignment of one caller-owned argument."""

    scrubbed = _without_static_queries(code_body)
    _require(
        re.search(
            rf"\b(?:Id|String|Object)\s+{re.escape(parameter)}\b",
            scrubbed,
            re.I,
        )
        is None,
        f"immutable {parameter} parameter",
    )
    assignment = (
        rf"(?:\+\+|--)\s*\b{re.escape(parameter)}\b|"
        rf"\b{re.escape(parameter)}\b\s*(?:\+\+|--|"
        r"(?:<<|>>|[+\-*/%&|^])?=(?!=))"
    )
    _require(
        re.search(assignment, scrubbed, re.I) is None,
        f"caller-owned {parameter} authority",
    )


def _brace_depth(code: str, offset: int) -> int:
    return code[:offset].count("{") - code[:offset].count("}")


def _query_result_local_name(
    code_body: str,
    query_offset: int,
    return_type: str,
) -> str | None:
    prefix = code_body[:query_offset]
    assignment = re.search(
        rf"(?:\bList\s*<\s*{re.escape(return_type)}\s*>\s+|\bvar\s+)?"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*$",
        prefix,
        re.I,
    )
    return assignment.group("name") if assignment is not None else None


def _require_query_result_flow(
    code_body: str,
    queries: tuple[tuple[str, int], ...],
    *,
    return_type: str,
) -> frozenset[int]:
    """Require every validated query result to reach the public List return."""

    allowed_return_offsets: set[int] = set()
    for query, query_offset in queries:
        prefix = code_body[:query_offset]
        direct_return = re.search(r"\breturn\s*$", prefix, re.I)
        if direct_return is not None:
            allowed_return_offsets.add(direct_return.start())
            continue
        local_name = _query_result_local_name(code_body, query_offset, return_type)
        _require(local_name is not None, f"{return_type} query result assignment")
        assert local_name is not None
        query_end = query_offset + len(query)
        query_depth = _brace_depth(code_body, query_offset)
        returned = False
        for match in re.finditer(
            rf"\breturn\s+{re.escape(local_name)}\s*;",
            code_body[query_end:],
            re.I,
        ):
            return_offset = query_end + match.start()
            if _brace_depth(code_body, return_offset) > query_depth:
                continue
            intervening = code_body[query_end:return_offset]
            if (
                re.search(
                    rf"\b{re.escape(local_name)}\s*(?:\+\+|--|"
                    r"(?:<<|>>|[+\-*/%&|^])?=(?!=))|"
                    rf"\b{re.escape(local_name)}\s*\.\s*"
                    r"(?:add|addAll|clear|remove|sort)\s*\(",
                    intervening,
                    re.I,
                )
                is not None
            ):
                continue
            returned = True
            allowed_return_offsets.add(return_offset)
        _require(returned, f"{return_type} query result returned")
    return frozenset(allowed_return_offsets)


def _require_bounded_return_inventory(
    code_body: str,
    queries: tuple[tuple[str, int], ...],
    *,
    return_type: str,
    null_selection_guard: str | None,
    query_return_offsets: frozenset[int],
) -> None:
    """Allow only the exact null guard and returns backed by validated queries."""

    allowed_return_offsets = set(query_return_offsets)
    if null_selection_guard is not None:
        guard_matches = tuple(re.finditer(null_selection_guard, code_body, re.I | re.DOTALL))
        _require(len(guard_matches) == 1, "single null-selection guard")
        guard = guard_matches[0]
        _require(guard.end() <= queries[0][1], "pre-query null-selection guard")
        guard_return = re.search(r"\breturn\b", guard.group(0), re.I)
        assert guard_return is not None
        allowed_return_offsets.add(guard.start() + guard_return.start())

    observed_return_offsets = frozenset(
        match.start() for match in re.finditer(r"\breturn\b.*?;", code_body, re.I | re.DOTALL)
    )
    _require(
        observed_return_offsets == frozenset(allowed_return_offsets),
        f"bounded {return_type} return inventory",
    )


def _check_controller(
    controller: str,
    profile: _SalesforceUnitProfile,
    *,
    status_constants: Mapping[str, str],
) -> None:
    code, string_literals = _apex_lexical_view(controller)
    sharing_declarations = tuple(
        match.group(1).lower() if match.group(1) is not None else "none"
        for match in re.finditer(
            r"(?im)^\s*(?:public|global)\s+"
            r"(?:(with|without|inherited)\s+sharing\s+)?"
            rf"class\s+{re.escape(profile.controller_class_name)}\b",
            code,
        )
    )
    _require(sharing_declarations == ("with",), "with-sharing Apex declaration")

    public_interface_is_valid = True
    for method in profile.controller_methods:
        annotation = (
            r"@AuraEnabled\s*\(\s*cacheable\s*=\s*true\s*\)"
            if method.cacheable
            else r"@AuraEnabled(?:\s*\(\s*cacheable\s*=\s*false\s*\))?"
        )
        public_interface_is_valid = (
            public_interface_is_valid
            and len(
                re.findall(
                    annotation + r"\s*"
                    rf"public\s+static\s+List\s*<\s*{method.return_type}\s*>\s+"
                    rf"{method.method_name}\s*{method.arguments}",
                    code,
                    re.I,
                )
            )
            == 1
        )
    public_interface_is_valid = public_interface_is_valid and (
        len(re.findall(r"@AuraEnabled\b", code, re.I)) == len(profile.controller_methods)
    )
    if not public_interface_is_valid:
        raise LocalCheckFailure(
            "local Apex public interface assertion failed",
            diagnostic_ids=(APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,),
        )

    all_static_queries = _apex_static_queries(code)
    _require(
        all(re.search(r"\bWITH\s+USER_MODE\b", query, re.I) for query in all_static_queries),
        "Apex user-mode queries",
    )
    integer_constants = _apex_integer_constants(code)
    safe_message_constants = _safe_apex_message_constants(code, controller)
    safe_exception_helpers = _safe_aura_handled_exception_helpers(
        code,
        safe_message_constants,
    )

    method_bound_queries: list[tuple[_ApexControllerMethodSpec, str, str, str, int]] = []
    method_return_contracts: list[
        tuple[
            _ApexControllerMethodSpec,
            str,
            tuple[tuple[str, int], ...],
            frozenset[int],
        ]
    ] = []
    validated_static_query_count = 0
    for method in profile.controller_methods:
        bounds = _apex_method_bounds(
            code,
            return_type=method.return_type,
            method_name=method.method_name,
            arguments=method.arguments,
        )
        code_body = code[slice(*bounds)]
        source_body = controller[slice(*bounds)]
        for parameter in method.immutable_parameters:
            _require_immutable_parameter_authority(code_body, parameter)
        query_matches = tuple(re.finditer(r"\[\s*SELECT\b.*?\]", code_body, re.I | re.DOTALL))
        queries = tuple(
            (match.group(0), match.start())
            for match in query_matches
            if re.search(
                rf"\bFROM\s+{re.escape(method.query_object)}\b",
                match.group(0),
                re.I,
            )
        )
        _require(
            len(queries) == len(query_matches),
            "method-bound static query object",
        )
        _require(
            1 <= len(queries) <= method.max_static_queries,
            "method-bound static query count",
        )
        validated_static_query_count += len(queries)
        for query, query_offset in queries:
            _require_soql_contract(
                query,
                object_name=method.query_object,
                fields=method.query_fields,
                order_by=method.query_order_by,
                order_direction=method.query_order_direction,
                max_rows=method.query_max_rows,
                integer_constants=integer_constants,
            )
            if method.selection_filter is not None:
                _require(
                    re.search(method.selection_filter, query, re.I) is not None,
                    "selected-parent method-bound query",
                )
            method_bound_queries.append((method, code_body, source_body, query, query_offset))
        query_return_offsets = _require_query_result_flow(
            code_body,
            queries,
            return_type=method.return_type,
        )
        method_return_contracts.append((method, code_body, queries, query_return_offsets))
        if method.required_status_filter_values:
            _require(
                frozenset(method.required_status_filter_values) == _CASE_STATUS_VALUES,
                "Case declared status filter inventory",
            )
            _require_case_status_filter_contract(
                code,
                controller,
                code_body,
                source_body,
                queries,
                status_constants,
            )
    _require(
        validated_static_query_count == len(all_static_queries),
        "controller static query closure",
    )

    try:
        helper_uses: set[tuple[str, str, int]] = set()
        for method, code_body, source_body, query, query_offset in method_bound_queries:
            helper_use = _require_controlled_query_failure(
                code_body,
                source_body,
                query,
                query_offset=query_offset,
                object_name=method.query_object,
                safe_message_constants=safe_message_constants,
                safe_exception_helpers=safe_exception_helpers,
            )
            if helper_use is not None:
                helper_name, helper_offset = helper_use
                helper_uses.add((method.method_name.casefold(), helper_name, helper_offset))
        uses_by_helper = {
            helper_name: sum(1 for _, used_helper, _ in helper_uses if used_helper == helper_name)
            for helper_name in safe_exception_helpers
        }
        for helper_name, permitted_uses in uses_by_helper.items():
            # The sole declaration accounts for one occurrence. Any additional
            # occurrence must be one of the query-catch throws proven above;
            # this rejects overloads and unrelated call sites fail closed.
            observed_occurrences = len(re.findall(rf"\b{re.escape(helper_name)}\s*\(", code, re.I))
            _require(
                observed_occurrences == 1 + permitted_uses,
                "safe exception helper call-site closure",
            )
    except LocalCheckFailure as exc:
        raise LocalCheckFailure(
            "local Apex controlled-query error assertion failed",
            diagnostic_ids=(APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,),
        ) from exc
    for method, code_body, queries, query_return_offsets in method_return_contracts:
        _require_bounded_return_inventory(
            code_body,
            queries,
            return_type=method.return_type,
            null_selection_guard=method.null_selection_guard,
            query_return_offsets=query_return_offsets,
        )
    _require(
        re.search(
            r"(?m)^[ \t]*(?:[A-Z][A-Z0-9_]*[ \t]+){2,}[A-Z][A-Z0-9_]*[ \t]*;[ \t]*$",
            code,
        )
        is None,
        "valid Apex executable statements",
    )

    _require(
        re.search(
            r"(?i)\bDatabase\s*\.\s*"
            r"(?:insert|update|upsert|delete|undelete|merge|query|queryWithBinds|"
            r"countQuery|countQueryWithBinds|getQueryLocator)\b",
            code,
        )
        is None,
        "forbidden Database operation",
    )
    _require(
        re.search(r"(?i)\b(?:insert|update|upsert|delete|undelete|merge)\b", code) is None,
        "read-only Apex contract",
    )
    _require(
        re.search(
            r"(?i)\b(?:Http|HttpRequest|HttpResponse|Continuation|NamedCredential|"
            r"WebServiceCallout|RemoteSiteSetting)\b|"
            r"\.\s*getContent(?:AsPDF)?\s*\(|"
            r"@future\s*\([^)]*\bcallout\s*=\s*true",
            code,
        )
        is None,
        "forbidden Apex capability",
    )

    literal_material = "\n".join(string_literals)
    _require(
        re.search(r"(?i)https?://", literal_material) is None
        and re.search(
            r"(?i)\b(?:password|passwd|client[_-]?secret|access[_-]?token|"
            r"refresh[_-]?token|authorization|api[_-]?key|private[_-]?key)\b\s*=",
            code,
        )
        is None
        and re.search(
            r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+|"
            r"\b(?:password|passwd|client[_-]?secret|access[_-]?token|"
            r"refresh[_-]?token|authorization|api[_-]?key|private[_-]?key)\b\s*[:=]",
            literal_material,
        )
        is None,
        "forbidden Apex material",
    )


def _apex_lexical_view(source: str) -> tuple[str, tuple[str, ...]]:
    """Return executable Apex text with comments and strings blanked.

    Newlines and all non-comment, non-string characters retain their original
    positions. This keeps line-oriented and whitespace-tolerant checks useful
    while preventing comments or string literals from satisfying code markers.
    Unterminated lexical constructs fail closed.
    """

    code = list(source)
    string_literals: list[str] = []
    index = 0
    while index < len(source):
        if source.startswith("//", index):
            code[index] = " "
            code[index + 1] = " "
            index += 2
            while index < len(source) and source[index] != "\n":
                code[index] = " "
                index += 1
            continue

        if source.startswith("/*", index):
            code[index] = " "
            code[index + 1] = " "
            index += 2
            terminated = False
            while index < len(source):
                if source.startswith("*/", index):
                    code[index] = " "
                    code[index + 1] = " "
                    index += 2
                    terminated = True
                    break
                if source[index] != "\n":
                    code[index] = " "
                index += 1
            _require(terminated, "terminated Apex block comment")
            continue

        if source[index] == "'":
            code[index] = " "
            index += 1
            literal: list[str] = []
            terminated = False
            while index < len(source):
                character = source[index]
                if character == "\\":
                    _require(index + 1 < len(source), "terminated Apex string escape")
                    literal.extend((character, source[index + 1]))
                    code[index] = " "
                    if source[index + 1] != "\n":
                        code[index + 1] = " "
                    index += 2
                    continue
                if character == "'":
                    code[index] = " "
                    index += 1
                    terminated = True
                    break
                literal.append(character)
                if character != "\n":
                    code[index] = " "
                index += 1
            _require(terminated, "terminated Apex string")
            string_literals.append("".join(literal))
            continue

        index += 1

    return "".join(code), tuple(string_literals)


def _case_test_status_argument_value(
    argument: str,
    *,
    controller: str,
    controller_status_constants: Mapping[str, str],
) -> str | None:
    if re.fullmatch(r"\s*'[A-Za-z]{1,16}'\s*", argument) is not None:
        return _case_status_atom_value(argument, controller_status_constants)
    qualified = re.fullmatch(
        rf"\s*{re.escape(controller)}\s*\.\s*"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*",
        argument,
        re.I,
    )
    if qualified is None:
        return None
    return controller_status_constants.get(qualified.group("name").casefold())


def _check_controller_test(
    test_source: str,
    profile: _SalesforceUnitProfile,
    *,
    controller_status_constants: Mapping[str, str],
) -> None:
    code, string_literals = _apex_lexical_view(test_source)
    comment_free = _apex_comment_free_view(test_source)
    controller = profile.controller_class_name
    # The unfiltered "list all" method carries no null-selection guard; the
    # parent-filtered method does. This resolves which method owns the plain
    # result coverage and which owns the populated/empty/null coverage.
    (list_method,) = tuple(
        method for method in profile.controller_methods if method.null_selection_guard is None
    )
    (filtered_method,) = tuple(
        method for method in profile.controller_methods if method.null_selection_guard is not None
    )
    _require(
        re.search(
            r"@IsTest\b(?:\s*\(\s*\))?\s*"
            rf"(?:private|public)?\s*class\s+{re.escape(profile.controller_test_class_name)}\b",
            code,
            re.I,
        )
        is not None,
        "Apex test class",
    )
    test_methods = tuple(
        re.finditer(
            r"@IsTest\b(?:\s*\(\s*\))?\s+static\s+void\s+"
            r"[A-Za-z_][A-Za-z0-9_]*\s*\(",
            code,
            re.I,
        )
    )
    _require(
        len(test_methods) >= 2,
        "Apex test method",
    )
    _require(
        re.search(
            rf"\b{re.escape(controller)}\s*\.\s*{re.escape(list_method.method_name)}\s*\(\s*\)",
            code,
            re.I,
        )
        is not None,
        "Apex account-result coverage",
    )
    filtered_calls = tuple(
        re.finditer(
            rf"\b{re.escape(controller)}\s*\.\s*{re.escape(filtered_method.method_name)}\s*"
            r"\(\s*(?P<argument>[^()]*)\s*\)",
            code,
            re.I,
        )
    )
    filtered_call_arguments = tuple(
        tuple(argument.strip() for argument in call.group("argument").split(","))
        for call in filtered_calls
    )
    _require(
        len(filtered_calls) >= 3
        and all(
            len(arguments) == filtered_method.argument_count
            for arguments in filtered_call_arguments
        ),
        "Apex populated, empty, and null filtered-result coverage",
    )
    _require(
        any(arguments[0].casefold() == "null" for arguments in filtered_call_arguments),
        "Apex null-selection coverage",
    )
    _require(
        sum(arguments[0].casefold() != "null" for arguments in filtered_call_arguments) >= 2,
        "Apex selected-account filtered-result coverage",
    )
    if filtered_method.required_status_filter_values:
        semantic_calls = tuple(
            re.finditer(
                rf"\b{re.escape(controller)}\s*\.\s*"
                rf"{re.escape(filtered_method.method_name)}\s*"
                r"\(\s*(?P<argument>[^()]*)\s*\)",
                comment_free,
                re.I,
            )
        )
        observed_status_values = {
            value
            for call in semantic_calls
            for arguments in (
                tuple(argument.strip() for argument in call.group("argument").split(",")),
            )
            if len(arguments) == filtered_method.argument_count
            for value in (
                _case_test_status_argument_value(
                    arguments[1],
                    controller=controller,
                    controller_status_constants=controller_status_constants,
                ),
            )
            if value is not None
        }
        _require(
            observed_status_values == set(filtered_method.required_status_filter_values),
            "Apex OPEN CLOSED ALL filtered-result coverage",
        )
    _require(
        len(re.findall(rf"\bnew\s+{re.escape(list_method.query_object)}\s*\(", code, re.I)) >= 2
        and re.search(rf"\bnew\s+{re.escape(filtered_method.query_object)}\s*\(", code, re.I)
        is not None
        and len(re.findall(r"\binsert\b", code, re.I)) >= 2,
        "isolated Apex Account and Contact test records",
    )
    assertions = tuple(
        re.finditer(
            r"\b(?:Assert\s*\.\s*[A-Za-z_][A-Za-z0-9_]*|"
            r"System\s*\.\s*assert(?:Equals|NotEquals)?)\s*\(",
            code,
            re.I,
        )
    )
    _require(
        len(assertions) >= 4,
        "meaningful Apex test assertions",
    )
    _require(
        re.search(
            r"@IsTest\s*\([^)]*\bSeeAllData\s*=\s*true\b",
            code,
            re.I,
        )
        is None,
        "isolated Apex test data",
    )
    _require(
        re.search(r"\bnew\s+User\s*\(|\bProfile\b|\bSystem\s*\.\s*runAs\s*\(", code, re.I) is None,
        "portable Apex test identity assumptions",
    )
    _require(
        re.search(
            r"(?i)\bDatabase\s*\.\s*"
            r"(?:query|queryWithBinds|countQuery|countQueryWithBinds|getQueryLocator)\b",
            code,
        )
        is None,
        "static Apex test queries",
    )
    _require(
        re.search(
            r"(?i)\b(?:Http|HttpRequest|HttpResponse|Continuation|NamedCredential|"
            r"WebServiceCallout|RemoteSiteSetting)\b|"
            r"\.\s*getContent(?:AsPDF)?\s*\(|"
            r"@future\s*\([^)]*\bcallout\s*=\s*true",
            code,
        )
        is None,
        "forbidden Apex test capability",
    )
    literal_material = "\n".join(string_literals)
    _require(
        re.search(r"(?i)https?://", literal_material) is None
        and re.search(
            r"(?i)\b(?:password|passwd|client[_-]?secret|access[_-]?token|"
            r"refresh[_-]?token|authorization|api[_-]?key|private[_-]?key)\b\s*=",
            code,
        )
        is None
        and re.search(
            r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+|"
            r"\b(?:password|passwd|client[_-]?secret|access[_-]?token|"
            r"refresh[_-]?token|authorization|api[_-]?key|private[_-]?key)\b\s*[:=]",
            literal_material,
        )
        is None,
        "forbidden Apex test material",
    )


def _forbidden_static_computed_property(source: str) -> bool:
    """Detect statically assembled prototype-escape property names."""

    dangerous_names = {"__proto__", "constructor", "prototype"}
    for bracket in re.finditer(r"\[(?P<body>[^\[\]\r\n]{1,256})\]", source):
        body = bracket.group("body")
        index = 0
        pieces: list[str] = []
        while index < len(body):
            while index < len(body) and body[index].isspace():
                index += 1
            if index >= len(body) or body[index] not in {"'", '"'}:
                break
            quote = body[index]
            index += 1
            piece: list[str] = []
            while index < len(body) and body[index] != quote:
                if body[index] == "\\" and index + 1 < len(body):
                    piece.append(body[index + 1])
                    index += 2
                else:
                    piece.append(body[index])
                    index += 1
            if index >= len(body):
                break
            index += 1
            pieces.append("".join(piece))
            while index < len(body) and body[index].isspace():
                index += 1
            if index == len(body):
                if "".join(pieces).lower() in dangerous_names:
                    return True
                break
            if body[index] != "+":
                break
            index += 1
    return re.search(r"(?i)\b(?:__proto__|constructor|prototype)\s*:", source) is not None


def _check_lwc_javascript(source: str, profile: _SalesforceUnitProfile) -> None:
    source = _without_c_style_comments(source, line_comments=True)
    code_view, _ = _javascript_code_view(source)

    import_tokens = tuple(re.finditer(r"\bimport\b", code_view))
    static_imports: list[str] = []
    import_bindings: dict[str, str] = {}
    import_inventory_is_valid = True
    for token in import_tokens:
        declaration = re.match(
            r"import\s+(?P<bindings>[\s\S]{1,2048}?)\s+from\s*"
            r"(?P<quote>['\"])(?P<module>[^'\"\r\n]+)(?P=quote)",
            source[token.start() :],
        )
        if (
            declaration is None
            or re.match(
                r"[ \t]*;?[ \t]*(?:\r?\n|$)",
                source[token.start() + declaration.end() :],
            )
            is None
        ):
            import_inventory_is_valid = False
            continue
        module_name = declaration.group("module")
        static_imports.append(module_name)
        import_bindings[module_name] = declaration.group("bindings")

    allowed_static_imports = frozenset({"lwc", *profile.lwc_apex_modules})
    lwc_named_imports = re.search(
        r"\{(?P<bindings>[\s\S]*?)\}",
        import_bindings.get("lwc", ""),
    )
    base_bindings = (
        ()
        if lwc_named_imports is None
        else tuple(
            binding.strip()
            for binding in lwc_named_imports.group("bindings").split(",")
            if binding.strip()
        )
    )
    base_binding_match = next(
        (
            match
            for binding in base_bindings
            if (
                match := re.fullmatch(
                    r"LightningElement(?:\s+as\s+(?P<local>[A-Za-z_$][\w$]*))?",
                    binding,
                )
            )
            is not None
        ),
        None,
    )
    local_base_name = (
        None
        if base_binding_match is None
        else base_binding_match.group("local") or "LightningElement"
    )
    public_export = (
        None
        if local_base_name is None
        else re.search(
            r"\bexport\s+default\s+class(?:\s+[A-Za-z_$][\w$]*)?\s+"
            rf"extends\s+{re.escape(local_base_name)}\s*\{{",
            code_view,
        )
    )
    export_tokens = tuple(re.finditer(r"\bexport\b", code_view))
    has_module_re_export = any(
        re.match(
            r"export\s+(?:\*|\{[^};]{0,2048}\})\s+from\s*['\"]",
            source[token.start() :],
        )
        is not None
        for token in export_tokens
    )
    export_inventory_is_valid = (
        public_export is not None
        and len(export_tokens) == 1
        and export_tokens[0].start() == public_export.start()
    )

    apex_modules = profile.lwc_apex_modules
    apex_bindings: list[str] = []
    apex_bindings_are_used = True
    for module_name in apex_modules:
        binding_match = re.fullmatch(
            r"\s*(?P<local>[A-Za-z_$][\w$]*)\s*",
            import_bindings.get(module_name, ""),
        )
        if binding_match is None:
            apex_bindings_are_used = False
            continue
        local_name = binding_match.group("local")
        apex_bindings.append(local_name)
        # The first occurrence is the import binding. A second executable
        # occurrence proves the generated component actually consumes it.
        if len(re.findall(rf"\b{re.escape(local_name)}\b", code_view)) < 2:
            apex_bindings_are_used = False

    has_typescript_class_modifier = (
        re.search(
            r"(?m)^[ \t]*(?:public|private|protected|readonly|declare|abstract)\s+"
            r"(?:static\s+)?[#A-Za-z_$]",
            code_view,
        )
        is not None
    )
    has_unapproved_public_api = re.search(r"@\s*api\b", code_view) is not None

    forbidden_runtime_patterns = (
        r"\b(?:eval|Function|require)\b",
        r"\bimport\s*\(",
        r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\b",
        r"\bnavigator\s*\.\s*sendBeacon\b",
        r"\b(?:globalThis|global|window|self|document|top|parent|frames|opener|"
        r"location|navigator|process|module|exports)\b",
        r"\b(?:jest|describe|it|test|expect|beforeEach|afterEach|beforeAll|afterAll)\b",
        r"\b(?:Proxy|Reflect|__proto__)\b",
        r"\.\s*(?:constructor|prototype)\b",
        r"\bObject\s*\.\s*(?:getPrototypeOf|setPrototypeOf|defineProperty|"
        r"defineProperties|create)\s*\(",
        r"\\(?:u(?:[0-9A-Fa-f]{4}|\{[0-9A-Fa-f]{1,6}\})|x[0-9A-Fa-f]{2})",
    )
    forbidden_material_patterns = (
        r"(?i)https?://",
        r"(?i)\b(?:password|passwd|client[_-]?secret|access[_-]?token|"
        r"refresh[_-]?token|authorization|api[_-]?key|private[_-]?key)\b\s*[:=]",
    )

    if (
        any(module not in allowed_static_imports for module in static_imports)
        or has_module_re_export
        or _forbidden_static_computed_property(source)
        or any(re.search(pattern, code_view) for pattern in forbidden_runtime_patterns)
        or any(re.search(pattern, source) for pattern in forbidden_material_patterns)
    ):
        raise LocalCheckFailure(
            "local LWC JavaScript contract assertion failed",
            diagnostic_ids=("lwc_forbidden_runtime_capability",),
        )
    _require(
        import_inventory_is_valid
        and len(static_imports) == len(allowed_static_imports)
        and frozenset(static_imports) == allowed_static_imports
        and export_inventory_is_valid
        and local_base_name is not None,
        "LWC public module interface",
    )
    _require(
        len(apex_bindings) == len(apex_modules)
        and apex_bindings_are_used
        and not has_typescript_class_modifier
        and not has_unapproved_public_api,
        "executable standard-JavaScript LWC implementation",
    )


def _check_lwc_template(template: str, profile: _SalesforceUnitProfile) -> None:
    template = _without_html_comments(template)
    _require(
        re.fullmatch(r"\s*<template(?:\s[^>]*)?>.*</template>\s*", template, re.DOTALL) is not None,
        "LWC template root",
    )
    _require(
        re.search(
            r"(?i)<\s*(?:script|iframe|frame|frameset|object|embed|link|base|meta)\b|"
            r"\blwc\s*:\s*dom\s*=|\b(?:https?|javascript|data)\s*:",
            template,
        )
        is None,
        "forbidden LWC template capability",
    )
    for attribute, values in (
        ("data-role", profile.lwc_template_data_roles),
        ("data-state", profile.lwc_template_data_states),
    ):
        missing_literals = tuple(
            value
            for value in values
            if re.search(
                rf"<[^>]*\b{attribute}\s*=\s*(['\"]){re.escape(value)}\1[^>]*>",
                template,
                re.DOTALL,
            )
            is None
        )
        bounded_binding = re.search(
            rf"<[^>]*\b{attribute}\s*=\s*{_LWC_SEMANTIC_BINDING_PATTERN}[^>]*>",
            template,
            re.DOTALL,
        )
        if missing_literals and bounded_binding is None:
            raise LocalCheckFailure(
                f"LWC semantic hook {attribute} assertion failed",
                diagnostic_ids=(LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID,),
            )


def _check_lwc_styles(styles: str) -> None:
    styles = _without_c_style_comments(styles, line_comments=False)
    _require(
        re.search(r"(?i)(@import|url\s*\(|expression\s*\(|behavior\s*:)", styles) is None,
        "external CSS resource",
    )


def _check_lwc_metadata(root: ElementTree.Element) -> None:
    _require(
        root.tag == f"{{{METADATA_NAMESPACE}}}LightningComponentBundle",
        "LWC metadata root",
    )
    _require(
        root.findtext(f"{{{METADATA_NAMESPACE}}}apiVersion") == API_VERSION,
        "LWC API version",
    )
    _require(
        root.findtext(f"{{{METADATA_NAMESPACE}}}isExposed") == "true",
        "LWC exposure",
    )
    targets = {
        target.text
        for target in root.findall(
            f"{{{METADATA_NAMESPACE}}}targets/{{{METADATA_NAMESPACE}}}target"
        )
    }
    _require(
        targets == {"lightning__AppPage", "lightning__Tab"},
        "LWC exposure targets",
    )


def _jest_static_import_modules(source: str, code_view: str) -> tuple[str, ...] | None:
    """Return bounded static Jest import targets, failing closed on ambiguity.

    ``code_view`` has comments and string contents blanked while retaining source
    offsets.  Import tokens therefore come only from executable source, while the
    corresponding slice of ``source`` still contains the module string that must
    be checked against the allowlist.
    """

    import_tokens = tuple(re.finditer(r"\bimport\b", code_view))
    if len(import_tokens) > 32:
        return None

    modules: list[str] = []
    for token in import_tokens:
        declaration = source[token.start() : token.start() + 4096]
        side_effect_import = re.match(
            r"import[ \t\r\n]*(?P<quote>['\"])(?P<module>[^'\"\r\n]{1,256})(?P=quote)",
            declaration,
        )
        from_import = re.match(
            r"import[ \t\r\n]+[A-Za-z0-9_$,*{} \t\r\n]{1,2048}?"
            r"\bfrom[ \t\r\n]*(?P<quote>['\"])(?P<module>[^'\"\r\n]{1,256})"
            r"(?P=quote)",
            declaration,
        )
        parsed = side_effect_import or from_import
        if parsed is None:
            return None

        tail = declaration[parsed.end() :]
        horizontal_space = re.match(r"[ \t]*", tail)
        assert horizontal_space is not None
        next_character = tail[horizontal_space.end() : horizontal_space.end() + 1]
        if next_character not in {"", ";", "\n", "\r"}:
            return None
        modules.append(parsed.group("module"))

    return tuple(modules)


def _dangerous_jest_module_target(target: str) -> bool:
    """Identify Node/runtime modules that grant capabilities beyond candidate tests."""

    if target.startswith("node:"):
        return True
    root = target.split("/", 1)[0]
    return root in {
        "async_hooks",
        "child_process",
        "cluster",
        "dgram",
        "dns",
        "fs",
        "http",
        "https",
        "inspector",
        "module",
        "net",
        "os",
        "process",
        "repl",
        "stream",
        "tls",
        "tty",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
    }


def _check_lwc_test(test_source: str, profile: _SalesforceUnitProfile) -> None:
    test_source = _without_c_style_comments(test_source, line_comments=True)
    code_view, _ = _javascript_code_view(test_source)
    _require(
        re.search(r"\b(?:it|test)(?:\s*\.\s*each)?\s*(?:\(|`)", code_view) is not None,
        "executable Jest test",
    )
    _require(
        re.search(
            r"\b(?:xit|xtest|xdescribe|pending)\s*\(|"
            r"\.\s*(?:skip|todo|only)\b",
            code_view,
        )
        is None,
        "complete Jest test source",
    )

    allowed_require_targets = frozenset({"@salesforce/sfdx-lwc-jest"})
    allowed_static_import_targets = frozenset(
        {
            "@jest/globals",
            "@salesforce/sfdx-lwc-jest",
            "lwc",
            profile.lwc_component_module,
            *profile.lwc_apex_modules,
        }
    )
    require_inventory_is_valid = True
    require_targets: list[str] = []
    for token in re.finditer(r"\brequire\b", code_view):
        require_call = re.match(
            r"require\s*\(\s*(?P<quote>['\"])(?P<target>[^'\"\r\n]+)(?P=quote)\s*\)",
            test_source[token.start() :],
        )
        if require_call is None:
            require_inventory_is_valid = False
            break
        require_targets.append(require_call.group("target"))
    static_import_targets = _jest_static_import_modules(test_source, code_view)
    observed_import_targets = () if static_import_targets is None else static_import_targets
    unapproved_module_target = any(
        target not in allowed_require_targets for target in require_targets
    ) or any(target not in allowed_static_import_targets for target in observed_import_targets)
    dangerous_module_target = any(
        _dangerous_jest_module_target(target)
        for target in (*require_targets, *observed_import_targets)
    )

    special_helper_inventory_is_valid = True
    special_helper_targets: list[str] = []
    for token in re.finditer(
        r"\bjest\s*(?:\?\.|\.)\s*"
        r"(?:requireActual|requireMock|createMockFromModule)\b",
        code_view,
    ):
        helper_call = re.match(
            r"jest\s*(?:\?\.\s*|\.\s*)"
            r"(?:requireActual|requireMock|createMockFromModule)\s*\(\s*"
            r"(?P<quote>['\"])(?P<target>[^'\"\r\n]{1,256})(?P=quote)",
            test_source[token.start() : token.start() + 1024],
        )
        if helper_call is None:
            special_helper_inventory_is_valid = False
            continue
        special_helper_targets.append(helper_call.group("target"))
    dangerous_module_target = dangerous_module_target or any(
        _dangerous_jest_module_target(target) for target in special_helper_targets
    )
    unapproved_module_target = unapproved_module_target or any(
        target not in allowed_static_import_targets and not _dangerous_jest_module_target(target)
        for target in special_helper_targets
    )
    forbidden_capability_patterns = (
        r"\bimport\s*\(",
        r"\b(?:eval|Function|fetch|XMLHttpRequest|WebSocket|EventSource)\b",
        r"\bnavigator\s*\.\s*sendBeacon\b",
        r"\b(?:globalThis|global|process|module|exports)\b",
        r"\b(?:Proxy|Reflect|__proto__)\b",
        r"\.\s*(?:constructor|prototype)\b",
        r"\bObject\s*\.\s*(?:getPrototypeOf|setPrototypeOf|defineProperty|"
        r"defineProperties|create)\s*\(",
        r"\bjest\s*(?:\?\.|\.)\s*(?:setMock|unstable_mockModule)\b",
        r"\\(?:u(?:[0-9A-Fa-f]{4}|\{[0-9A-Fa-f]{1,6}\})|x[0-9A-Fa-f]{2})",
    )
    forbidden_material_patterns = (
        r"(?i)https?://",
        r"(?i)\b(?:password|passwd|client[_-]?secret|access[_-]?token|"
        r"refresh[_-]?token|authorization|api[_-]?key|private[_-]?key)\b\s*[:=]",
    )
    dangerous_capability = (
        not require_inventory_is_valid
        or not special_helper_inventory_is_valid
        or dangerous_module_target
        or _forbidden_static_computed_property(test_source)
        or any(re.search(pattern, code_view) for pattern in forbidden_capability_patterns)
        or any(re.search(pattern, test_source) for pattern in forbidden_material_patterns)
    )
    diagnostics: list[str] = []
    if static_import_targets is not None and (
        not static_import_targets or static_import_targets[0] != "@jest/globals"
    ):
        diagnostics.append(JEST_GLOBALS_IMPORT_ORDER_DIAGNOSTIC_ID)
    if unapproved_module_target:
        diagnostics.append(JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID)
    if dangerous_capability:
        diagnostics.append("jest_forbidden_capability")
    if diagnostics:
        raise LocalCheckFailure(
            "local LWC Jest safety assertion failed",
            diagnostic_ids=diagnostics,
        )
    _require(static_import_targets is not None, "bounded Jest static import inventory")


def _javascript_code_view(source: str) -> tuple[str, bool]:
    """Blank JavaScript strings while retaining executable template expressions."""

    output = list(source)
    has_template_literal = False

    def mask_ordinary_string(index: int, quote: str) -> int:
        output[index] = " "
        index += 1
        while index < len(source):
            character = source[index]
            if character == "\\":
                output[index] = " "
                _require(index + 1 < len(source), "terminated JavaScript string escape")
                if source[index + 1] not in {"\n", "\r"}:
                    output[index + 1] = " "
                index += 2
                continue
            if character == quote:
                output[index] = " "
                return index + 1
            if character not in {"\n", "\r"}:
                output[index] = " "
            index += 1
        raise LocalCheckFailure("local contract assertion failed")

    def mask_template_expression(index: int) -> int:
        depth = 1
        while index < len(source):
            character = source[index]
            if character in {"'", '"'}:
                index = mask_ordinary_string(index, character)
                continue
            if character == "`":
                index = mask_template(index)
                continue
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    output[index] = " "
                    return index + 1
            index += 1
        raise LocalCheckFailure("local contract assertion failed")

    def mask_template(index: int) -> int:
        nonlocal has_template_literal
        has_template_literal = True
        output[index] = " "
        index += 1
        while index < len(source):
            character = source[index]
            if character == "\\":
                output[index] = " "
                _require(index + 1 < len(source), "terminated JavaScript template escape")
                if source[index + 1] not in {"\n", "\r"}:
                    output[index + 1] = " "
                index += 2
                continue
            if character == "`":
                output[index] = " "
                return index + 1
            if source.startswith("${", index):
                output[index] = " "
                output[index + 1] = " "
                index = mask_template_expression(index + 2)
                continue
            if character not in {"\n", "\r"}:
                output[index] = " "
            index += 1
        raise LocalCheckFailure("local contract assertion failed")

    index = 0
    while index < len(source):
        character = source[index]
        if character in {"'", '"'}:
            index = mask_ordinary_string(index, character)
        elif character == "`":
            index = mask_template(index)
        else:
            index += 1
    return "".join(output), has_template_literal


def _without_c_style_comments(source: str, *, line_comments: bool) -> str:
    """Blank JS/CSS comments while retaining quoted text and line positions."""

    output = list(source)
    quote: str | None = None
    index = 0
    while index < len(source):
        character = source[index]
        if quote is not None:
            if character == "\\":
                _require(index + 1 < len(source), "terminated source string escape")
                index += 2
                continue
            if character == quote:
                quote = None
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote = character
            index += 1
            continue
        if line_comments and source.startswith("//", index):
            output[index] = " "
            output[index + 1] = " "
            index += 2
            while index < len(source) and source[index] != "\n":
                output[index] = " "
                index += 1
            continue
        if source.startswith("/*", index):
            output[index] = " "
            output[index + 1] = " "
            index += 2
            terminated = False
            while index < len(source):
                if source.startswith("*/", index):
                    output[index] = " "
                    output[index + 1] = " "
                    index += 2
                    terminated = True
                    break
                if source[index] != "\n":
                    output[index] = " "
                index += 1
            _require(terminated, "terminated source block comment")
            continue
        index += 1
    _require(quote is None, "terminated source string")
    return "".join(output)


def _without_html_comments(source: str) -> str:
    output = list(source)
    index = 0
    while index < len(source):
        if not source.startswith("<!--", index):
            index += 1
            continue
        end = source.find("-->", index + 4)
        _require(end >= 0, "terminated HTML comment")
        for position in range(index, end + 3):
            if source[position] != "\n":
                output[position] = " "
        index = end + 3
    return "".join(output)


def _check_permission_set(root: ElementTree.Element, profile: _SalesforceUnitProfile) -> None:
    _require(
        root.tag == f"{{{METADATA_NAMESPACE}}}PermissionSet",
        "permission set root",
    )
    namespace = {"m": METADATA_NAMESPACE}
    _require_xml_child_inventory(
        root,
        {
            "classAccesses": len(profile.permission_class_accesses),
            "description": 1,
            "fieldPermissions": len(profile.permission_field_permissions),
            "hasActivationRequired": 1,
            "label": 1,
            "objectPermissions": len(profile.permission_objects),
            "pageAccesses": 1,
        },
    )
    _require(
        root.findtext("m:hasActivationRequired", namespaces=namespace) == "false",
        "permission set activation",
    )
    object_permissions = root.findall("m:objectPermissions", namespace)
    _require(len(object_permissions) == len(profile.permission_objects), "permission object count")
    objects: set[str] = set()
    for permission in object_permissions:
        _require_xml_child_inventory(
            permission,
            {
                "allowCreate": 1,
                "allowDelete": 1,
                "allowEdit": 1,
                "allowRead": 1,
                "modifyAllRecords": 1,
                "object": 1,
                "viewAllFields": 1,
                "viewAllRecords": 1,
            },
        )
        object_name = permission.findtext("m:object", namespaces=namespace)
        _require(object_name in profile.permission_objects, "standard object permission")
        objects.add(cast(str, object_name))
        _require(
            permission.findtext("m:allowRead", namespaces=namespace) == "true",
            "read permission",
        )
        for operation in (
            "allowCreate",
            "allowEdit",
            "allowDelete",
            "modifyAllRecords",
            "viewAllRecords",
            "viewAllFields",
        ):
            _require(
                permission.findtext(f"m:{operation}", namespaces=namespace) == "false",
                "read-only permission",
            )
    _require(objects == profile.permission_objects, "permission objects")

    class_accesses = root.findall("m:classAccesses", namespace)
    for access in class_accesses:
        _require_xml_child_inventory(access, {"apexClass": 1, "enabled": 1})
    classes = {
        access.findtext("m:apexClass", namespaces=namespace)
        for access in class_accesses
        if access.findtext("m:enabled", namespaces=namespace) == "true"
    }
    _require(
        len(class_accesses) == len(profile.permission_class_accesses)
        and classes == profile.permission_class_accesses,
        "Apex class access",
    )

    field_accesses = root.findall("m:fieldPermissions", namespace)
    fields: set[str] = set()
    for access in field_accesses:
        _require_xml_child_inventory(access, {"editable": 1, "field": 1, "readable": 1})
        field = access.findtext("m:field", namespaces=namespace)
        _require(field in profile.permission_field_permissions, "approved field permission")
        _require(
            access.findtext("m:readable", namespaces=namespace) == "true"
            and access.findtext("m:editable", namespaces=namespace) == "false",
            "read-only field permission",
        )
        fields.add(cast(str, field))
    _require(fields == profile.permission_field_permissions, "required field permissions")

    page_accesses = root.findall("m:pageAccesses", namespace)
    _require(len(page_accesses) == 1, "legacy page access count")
    page_access = page_accesses[0]
    _require_xml_child_inventory(page_access, {"apexPage": 1, "enabled": 1})
    _require(
        page_access.findtext("m:apexPage", namespaces=namespace) == profile.permission_page_access
        and page_access.findtext("m:enabled", namespaces=namespace) == "true",
        "legacy page access",
    )


def _require_xml_child_inventory(
    element: ElementTree.Element,
    expected: dict[str, int],
) -> None:
    prefix = f"{{{METADATA_NAMESPACE}}}"
    observed: dict[str, int] = {}
    for child in element:
        _require(child.tag.startswith(prefix), "metadata child namespace")
        name = child.tag[len(prefix) :]
        observed[name] = observed.get(name, 0) + 1
    _require(observed == expected, "metadata child inventory")


def _check_lwc_jest_lock(lock: dict[str, Any]) -> None:
    _require(lock.get("name") == LWC_JEST_TOOLCHAIN_NAME, "lock identity")
    _require(lock.get("version") == "1.0.0", "lock version")
    _require(lock.get("lockfileVersion") == 3, "lockfile version")
    _require(lock.get("requires") is True, "lock requires flag")
    packages = lock.get("packages")
    _require(isinstance(packages, dict) and len(packages) == 561, "lock package inventory")
    typed_packages = cast(dict[str, Any], packages)
    root_package = typed_packages.get("")
    _require(
        root_package
        == {
            "name": LWC_JEST_TOOLCHAIN_NAME,
            "version": "1.0.0",
            "devDependencies": {LWC_JEST_PACKAGE: LWC_JEST_VERSION},
        },
        "lock root package",
    )
    jest_package = typed_packages.get(f"node_modules/{LWC_JEST_PACKAGE}")
    _require(isinstance(jest_package, dict), "locked LWC Jest package")
    typed_jest = cast(dict[str, Any], jest_package)
    _require(typed_jest.get("version") == LWC_JEST_VERSION, "locked LWC Jest version")
    _require(
        typed_jest.get("resolved") == "https://registry.npmjs.org/@salesforce/sfdx-lwc-jest/-/"
        "sfdx-lwc-jest-7.9.0.tgz",
        "locked LWC Jest source",
    )
    _require(
        typed_jest.get("integrity")
        == "sha512-DVaJiXXUAeJq+ZVM8ElmH6HsAApU/t6zxa6VnPiyc9nne5XZ+7Trp010sTxrSW4pPn8Q9YDrKl2R43hayugeCA==",
        "locked LWC Jest integrity",
    )
    for package_name, value in typed_packages.items():
        if package_name == "":
            continue
        _require(isinstance(value, dict), "locked package entry")
        entry = cast(dict[str, Any], value)
        resolved = entry.get("resolved")
        integrity = entry.get("integrity")
        _require(
            isinstance(resolved, str)
            and resolved.startswith("https://registry.npmjs.org/")
            and isinstance(integrity, str)
            and integrity.startswith("sha512-")
            and entry.get("dev") is True,
            "locked dependency source and integrity",
        )


def _lwc_jest_config() -> str:
    return (
        "const { jestConfig } = require('@salesforce/sfdx-lwc-jest/config');\n"
        "\n"
        "module.exports = {\n"
        "    ...jestConfig,\n"
        "    injectGlobals: false,\n"
        "    setupFilesAfterEnv: [require.resolve('./jest.setup.js')]\n"
        "};\n"
    )


def _root(root: Path) -> Path:
    try:
        if stat.S_ISLNK(root.lstat().st_mode):
            raise LocalCheckFailure("validation root must not be a symlink")
        resolved = root.resolve(strict=True)
    except LocalCheckFailure:
        raise
    except OSError as exc:
        raise LocalCheckFailure("validation root is unavailable") from exc
    if not resolved.is_dir():
        raise LocalCheckFailure("validation root is not a directory")
    return resolved


def _file(root: Path, relative: str) -> Path:
    candidate = root
    try:
        for part in relative.split("/"):
            candidate /= part
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise LocalCheckFailure("validation path contains a symlink")
        candidate = candidate.resolve(strict=True)
    except LocalCheckFailure:
        raise
    except OSError as exc:
        raise LocalCheckFailure("required validation file is unavailable") from exc
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise LocalCheckFailure("validation file escapes the root") from exc
    if not candidate.is_file():
        raise LocalCheckFailure("validation path is not a regular file")
    return candidate


def _bytes(path: Path, *, max_bytes: int = 1_000_000) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise LocalCheckFailure("validation file could not be read") from exc
    _require(len(content) <= max_bytes, "bounded validation file")
    _require(b"\x00" not in content, "text validation file")
    return content


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LocalCheckFailure("validation file is not UTF-8") from exc


def _text(path: Path, *, max_bytes: int = 1_000_000) -> str:
    return _decode(_bytes(path, max_bytes=max_bytes))


def _json_object(path: Path, *, max_bytes: int = 1_000_000) -> dict[str, Any]:
    return _json_object_bytes(_bytes(path, max_bytes=max_bytes))


def _json_object_bytes(content: bytes) -> dict[str, Any]:
    try:
        value = json.loads(_decode(content))
    except (json.JSONDecodeError, ValueError) as exc:
        raise LocalCheckFailure("validation JSON is malformed") from exc
    _require(isinstance(value, dict), "JSON object")
    return cast(dict[str, Any], value)


def _xml_root(path: Path) -> ElementTree.Element:
    content = _bytes(path)
    upper = content.upper()
    _require(b"<!DOCTYPE" not in upper and b"<!ENTITY" not in upper, "safe metadata XML")
    try:
        return ElementTree.fromstring(content)
    except (ElementTree.ParseError, ValueError) as exc:
        raise LocalCheckFailure("validation XML is malformed") from exc


def _require(condition: bool, _label: str) -> None:
    if not condition:
        raise LocalCheckFailure("local contract assertion failed")


if __name__ == "__main__":
    raise SystemExit(main())
