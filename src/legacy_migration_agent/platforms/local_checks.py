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
from collections.abc import Callable, Sequence
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
        "sha256:94227c924960d13d73acada1fa3923db7faa9cc3f193b3e305cd76b8b4f5c0bf"
    ),
}

PROJECT_PATH = "sfdx-project.json"
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
LWC_ACCOUNTS_DATA_PATH = f"{LWC_ROOT}/__tests__/data/accounts.json"
LWC_CONTACTS_DATA_PATH = f"{LWC_ROOT}/__tests__/data/contacts.json"
PERMISSION_SET_PATH = (
    "force-app/main/default/permissionsets/AccountContactExplorerUser.permissionset-meta.xml"
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
            LWC_ACCOUNTS_DATA_PATH,
            LWC_CONTACTS_DATA_PATH,
            PERMISSION_SET_PATH,
        )
    )
)

# Human-reviewable acceptance requirements derived from the deterministic
# candidate checks below. These describe required behavior and syntax without
# exposing reviewed target bytes or granting the model any execution authority.
SALESFORCE_REQUIRED_LWC_BEHAVIOR_TITLES: Final[tuple[str, ...]] = (
    "renders a blank option followed by wired accounts",
    "renders a controlled account wire error",
    "keeps Load disabled until an account is selected",
    "loads contacts only after the explicit button click",
    "shows loading state and disables Load while contacts are pending",
    "ignores a stale response after the selected account changes",
    "warns and disables Load when the selection is cleared",
    "renders an empty state after a successful empty result",
    "renders a controlled contacts error",
    "uses a safe fallback instead of exposing a generic technical error",
)
SALESFORCE_REQUIRED_LWC_BEHAVIORS: Final[frozenset[str]] = frozenset(
    SALESFORCE_REQUIRED_LWC_BEHAVIOR_TITLES
)
SALESFORCE_CONTROLLER_LWC_BEHAVIOR_TITLES: Final[tuple[str, ...]] = (
    "controller: renders account options from the wire adapter",
    "controller: renders a safe account-wire failure",
    "controller: enables Load only after account selection",
    "controller: invokes contacts only after the Load action",
    "controller: exposes loading state while contacts are pending",
    "controller: hides prior empty state during a new request",
    "controller: ignores a response made stale by account change",
    "controller: ignores an older overlapping Load for the same account",
    "controller: ignores an older same-account rejection while the current request is pending",
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
            "controller_jest_refresh_state",
            "controller_jest_stale_response",
            "controller_jest_same_account_overlap",
            "controller_jest_stale_error",
            "controller_jest_blank_selection",
            "controller_jest_empty_state",
            "controller_jest_contacts_error",
        ),
        strict=True,
    )
}
SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS: Final[frozenset[str]] = frozenset(
    SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_BY_TITLE.values()
)

SALESFORCE_IMPLEMENTATION_CONTRACT = (
    (
        "Create API 67.0 `public with sharing class AccountContactExplorerController` with "
        "`@TestVisible` immediately above each exact private declaration `private static final "
        "Integer MAX_ACCOUNTS = 50` and `private static final Integer MAX_CONTACTS = 100`, plus exactly two "
        "`@AuraEnabled(cacheable=true)` methods with signatures `public static List<Account> "
        "getAccounts()` and `public static List<Contact> getContacts(Id accountId)`. Both new "
        "Apex metadata files must use API 67.0 and Active status."
    ),
    (
        "Use exactly two static SOQL queries with `WITH USER_MODE`: accounts select Id and Name, "
        "`ORDER BY Name LIMIT :MAX_ACCOUNTS`; contacts return an empty List<Contact> when "
        "`accountId == null`, select only Id, FirstName, LastName, Email and Phone for that "
        "account, then `ORDER BY LastName, FirstName LIMIT :MAX_CONTACTS`. Throw exactly one "
        "AuraHandledException with `Accounts could not be read.` and one with `Contacts could "
        "not be read.`. Include no DML, dynamic Database query, callout, credential or secret."
    ),
    (
        "Create AccountContactExplorerControllerTest with @IsTest, @TestSetup, and methods named "
        "`returnsAccountsInNameOrder`, `returnsContactsInLastAndFirstNameOrder`, "
        "`returnsEmptyListForBlankSelection`, and `returnsEmptyListWhenAccountHasNoContacts`. "
        "Use exactly four Test.startTest/Test.stopTest blocks. Create Accounts with an exact "
        "`index < AccountContactExplorerController.MAX_ACCOUNTS + 2` loop. Use `Synthetic "
        "Account 000` as the target, `Synthetic Account 050` for `foreignContact`, and leave "
        "`Synthetic Account 051` empty. For the target, create exactly MAX_CONTACTS + 2 Contacts "
        "total: `Alpha Tie`/`Ada`, `Alpha Tie`/`Zoe`, 98 records whose LastName is exactly "
        "`'Synthetic ' + paddedNumber(index)`, and two later-sorting records whose LastName is "
        "exactly `'Zulu Overflow ' + paddedNumber(index)`."
    ),
    (
        "In AccountContactExplorerControllerTest, query COUNT() in the account test, "
        "assert `AccountContactExplorerController.MAX_ACCOUNTS + 2`, assert the cap with "
        "`System.assertEquals(AccountContactExplorerController.MAX_ACCOUNTS, accounts.size())`, "
        "and prove `Synthetic Account 000`/`Synthetic Account 049` ordering. In the contact test, "
        "query the target COUNT() with `WHERE AccountId = :accountRecord.Id`, assert "
        "`AccountContactExplorerController.MAX_CONTACTS + 2`, assert the cap with "
        "`System.assertEquals(AccountContactExplorerController.MAX_CONTACTS, contacts.size())`, "
        "prove `Alpha Tie`/`Ada` and `Alpha Tie`/`Zoe` at indexes 0 and 1, prove `Synthetic 097` "
        "at index 99, and prove foreign exclusion with the valid Apex statement "
        "`System.assert(!returnedIds.contains(foreignContact.Id))`. Also prove null and "
        "empty-account behavior. Generate padded names with a "
        "`private static String paddedNumber(Integer numberValue)` helper using String.valueOf "
        "and concatenation; do not use String.format or `{0:03}`-style format specifiers. Query "
        "contacts with `WHERE "
        "AccountId = :accountRecord.Id`, construct `Map<Id, Contact> "
        "returnedContactsById = new Map<Id, Contact>(contacts)`, derive `Set<Id> returnedIds = "
        "returnedContactsById.keySet()`, and assert that foreignContact.Id is absent."
    ),
    (
        "Build accountContactExplorer.js with `import { LightningElement, wire } from 'lwc'`, "
        "`extends LightningElement`, the exact Apex imports, `@wire(getAccounts)`, "
        "`async handleLoad()`, `handleAccountChange`, imperative `getContacts({ accountId })` "
        "only after Load, `get isLoadDisabled()` and `get showEmptyState()`. Support blank "
        "selection, warning, loading, populated, empty, account-error and contact-error states "
        "using safe fallbacks `Accounts could not be loaded.` and `Contacts could not be loaded.`. "
        "Define top-level `CONTACT_COLUMNS` with four datatable columns for FirstName, LastName, "
        "Email and Phone, then expose them with `columns = CONTACT_COLUMNS`. Declare the reactive "
        "account options directly on the component as `accountOptions = [BLANK_ACCOUNT_OPTION]`; "
        "in the successful `wiredAccounts` data branch, replace the field with an array literal "
        "whose first element is `BLANK_ACCOUNT_OPTION` and whose remaining elements are spread "
        "directly from `data.map(...)`, using `this.accountOptions = [BLANK_ACCOUNT_OPTION, "
        "...data.map(...)]`. Do not stage the mapped options in an intermediate variable, mutate "
        "the array with `push`, proxy it through `get accountOptions()`/`set accountOptions()`, or "
        "assign an undeclared `_accountOptions` expando. Initialize `hasLoaded` to false. In "
        "handleAccountChange, use the direct sequence `this.contacts = [];`, "
        "`this.isLoading = false;`, `this.hasLoaded = false;`. In handleLoad, after the "
        "valid-selection guard and before awaiting getContacts, use the direct sequence "
        "`this.isLoading = true;`, `this.hasLoaded = false;`, `this.contacts = [];`. Set "
        "it true only after a current successful contacts response, and make empty state depend on "
        "it. When the account changes, "
        "invalidate the prior request, "
        "set isLoading false so another Load is possible, and set `warningMessage` to `Select an "
        "account before loading contacts.` when the new selection is blank. Repeat that same "
        "guard and safe warning in handleLoad when no account is selected."
    ),
    (
        "Implement the stale-response guard with `loadRequestGeneration = 0`: increment "
        "`this.loadRequestGeneration += 1` once at the start of handleAccountChange so changing "
        "the selection immediately invalidates an in-flight request. In handleLoad, after the "
        "valid-selection guard and before awaiting getContacts, use the direct sequence "
        "`const accountId = this.selectedAccountId;`, "
        "`this.loadRequestGeneration += 1;`, and "
        "`const requestGeneration = this.loadRequestGeneration;`. Apply an "
        "`isCurrentRequest(accountId, requestGeneration)` guard directly inside each of the "
        "try success, catch error, and finally loading-reset paths. This must reject an older "
        "overlapping Load for the same account as well as work invalidated by an account change. "
        "Use only the three approved static imports (`lwc` plus the two exact Apex modules). "
        "Include no runtime/test-global access, fetch, XMLHttpRequest, WebSocket, eval, dynamic "
        "module loading, external URL or secret."
    ),
    (
        "Use `lightning-combobox` with `value={selectedAccountId}`, `options={accountOptions}` and "
        "`onchange={handleAccountChange}`, plus `lightning-button` with `onclick={handleLoad}`, "
        "`disabled={isLoadDisabled}`, `lwc:if={warningMessage}`, `lwc:if={errorMessage}`, "
        "`lwc:if={isLoading}`, `lwc:elseif={hasContacts}` and "
        "`lwc:elseif={showEmptyState}` in the template, with role=alert, spinner alternative text "
        '`Loading contacts`, and datatable `key-field="Id"`. CSS must include :host, .controls, '
        ".loading-region, .warning and .empty-state with no import or URL. Metadata must be API "
        "67.0, exposed, and target exactly lightning__AppPage and lightning__Tab."
    ),
    (
        "Create LWC Jest coverage with these ten exact accepted titles: "
        + "; ".join(SALESFORCE_REQUIRED_LWC_BEHAVIOR_TITLES)
        + ". Import the fixtures as ACCOUNTS and CONTACTS. Use `createElement` with `{ is: "
        "AccountContactExplorer }`, inspect component DOM through `element.shadowRoot`, dispatch "
        "the `lightning-combobox` change event with `detail: { value: accountId }`. Create the "
        "component before every `getAccounts.emit/error` call; do not emit wire data in a "
        "beforeEach before a subscriber exists. Use two "
        "separate ES-module virtual mocks for the exact `.getAccounts` and `.getContacts` Apex "
        "module imports. The wire mock must return `{ __esModule: true, default: "
        "createApexTestWireAdapter(jest.fn()) }`, load that factory from the pinned "
        "`@salesforce/sfdx-lwc-jest` package, and use "
        "`getAccounts.emit/error`; "
        "the imperative mock must return `{ __esModule: true, default: jest.fn() }`."
    ),
    (
        "For AccountContactExplorer LWC Jest execution, lexically import `afterEach`, "
        "`describe`, `expect`, `it`, and `jest` from `@jest/globals`; the pinned runner does not "
        "inject test globals. Put the "
        "`require('@salesforce/sfdx-lwc-jest')` inside the getAccounts mock factory so Jest "
        "hoisting cannot reference an out-of-scope imported adapter. Cover normal, "
        "empty and error results with `getContacts.mockResolvedValue(CONTACTS)`, "
        "`getContacts.mockResolvedValue([])` and `getContacts.mockRejectedValue(...)`. Implement "
        "In the explicit-load test, configure `getContacts.mockResolvedValue(CONTACTS)` before "
        "clicking Load and assert the rendered datatable data. Implement the stale-response test "
        "with a `createDeferredPromise` helper, `firstRequest` and "
        "`secondRequest`; resolve `secondRequest.resolve(CONTACTS)` before `firstRequest.resolve([` "
        "a record containing `Stale`, prove both ordered call arguments using either nth-call "
        "matchers or indexed `getContacts.mock.calls`, and prove stale data is not rendered with "
        "a targeted `not.toContain('Stale')` assertion or a field-level `not.toBe('Stale')` "
        "assertion. In the loading-state test, query the spinner and assert the pinned stub's "
        "public property with `expect(spinner.alternativeText).toBe('Loading contacts')` (or "
        "`.toEqual('Loading contacts')`); do not use "
        "`spinner.getAttribute('alternative-text')`. Define `async function flushPromises()` "
        "with two consecutive `await Promise.resolve()` turns and use it after component events "
        "and every settled, rejected, or manually resolved imperative Apex promise before DOM "
        "assertions. In `afterEach`, remove every child from `document.body`, then reset "
        "`getContacts` with `getContacts.mockReset()`. Resetting `getAccounts` with "
        "`getAccounts.mockReset()` is also supported. The "
        "safe-fallback test must prove a technical "
        "`SELECT Id FROM Contact` message is not exposed. Include no skipped, pending or todo test."
    ),
    (
        "Provide sorted JSON fixtures with at least two records each: Accounts have exactly Id and "
        "Name with 18-character synthetic 001-prefixed IDs; Contacts have exactly Id, FirstName, "
        "LastName, Email and Phone with 18-character synthetic 003-prefixed IDs and "
        "@example.invalid emails, sorted by LastName then FirstName."
    ),
    (
        "The permission set must contain exactly read-only Account and Contact object permissions "
        "with create/edit/delete/modifyAll/viewAll/viewAllFields false; exactly enabled legacy and "
        "new controller class accesses; readable non-editable Contact.Email and Contact.Phone; "
        "and exactly one enabled LegacyAccountContactExplorer page access."
    ),
    (
        "manifest/package.xml must use API 67.0 and contain exactly ApexClass members "
        "AccountContactExplorerController, AccountContactExplorerControllerTest, "
        "LegacyAccountContactExplorerController and LegacyAccountContactExplorerControllerTest; "
        "ApexPage LegacyAccountContactExplorer; LightningComponentBundle accountContactExplorer; "
        "and PermissionSet AccountContactExplorerUser. Preserve every legacy source file and do "
        "not generate package.json, package-lock.json or jest.config.js."
    ),
)

SALESFORCE_CANDIDATE_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {
        "salesforce_candidate_inventory",
        "salesforce_project_contract",
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
        "salesforce_lwc_fixture_contract",
        "salesforce_permission_set_contract",
        "salesforce_candidate_unclassified",
    }
)

# These identifiers are the complete public vocabulary for candidate-contract
# diagnostics. They are intentionally code-owned and contain no candidate text.
SALESFORCE_LWC_JAVASCRIPT_DIAGNOSTIC_IDS: Final[frozenset[str]] = frozenset(
    {
        "lwc_account_options_reactive_field",
        "lwc_forbidden_runtime_capability",
        "lwc_has_loaded_reset",
        "lwc_request_generation_increment",
    }
)
SALESFORCE_LWC_JEST_DIAGNOSTIC_IDS: Final[frozenset[str]] = frozenset(
    {
        "jest_component_before_wire_emit",
        "jest_dom_cleanup",
        "jest_exact_behavior_titles",
        "jest_explicit_load_behavior",
        "jest_explicit_globals",
        "jest_fixture_result_coverage",
        "jest_forbidden_capability",
        "jest_imperative_mock_contract",
        "jest_loading_behavior",
        "jest_mock_module_contract",
        "jest_mock_not_reset",
        "jest_ordered_call_proof",
        "jest_spinner_public_property",
        "jest_settled_render_flush",
        "jest_required_behavior_coverage",
        "jest_safe_error_redaction",
        "jest_shadow_dom_contract",
        "jest_stale_assertion_vacuous",
        "jest_stale_render_proof",
        "jest_stale_resolution_order",
        "jest_stale_scenario_setup",
        "jest_wire_adapter_factory_argument",
        "jest_wire_adapter_api",
        "jest_wire_adapter_contract",
        *SALESFORCE_LWC_JAVASCRIPT_DIAGNOSTIC_IDS,
    }
)
SALESFORCE_CANDIDATE_DIAGNOSTIC_IDS: Final[frozenset[str]] = frozenset(
    {*SALESFORCE_CANDIDATE_FAILURE_CODES, *SALESFORCE_LWC_JEST_DIAGNOSTIC_IDS}
)
# A failed static Jest behavior contract may still be executed inside the
# controller-owned sandbox to collect richer evidence. Capability violations
# and missing lexical Jest globals are excluded and remain hard prerequisites.
SALESFORCE_JEST_SANDBOX_SAFE_DIAGNOSTIC_IDS: Final[frozenset[str]] = frozenset(
    SALESFORCE_LWC_JEST_DIAGNOSTIC_IDS
    - {
        "jest_explicit_globals",
        "jest_forbidden_capability",
        "lwc_forbidden_runtime_capability",
    }
)

_PRESERVED_LEGACY_PATHS = (
    "force-app/main/default/pages/LegacyAccountContactExplorer.page",
    "force-app/main/default/pages/LegacyAccountContactExplorer.page-meta.xml",
    "force-app/main/default/classes/LegacyAccountContactExplorerController.cls",
    "force-app/main/default/classes/LegacyAccountContactExplorerController.cls-meta.xml",
    "force-app/main/default/classes/LegacyAccountContactExplorerControllerTest.cls",
    "force-app/main/default/classes/LegacyAccountContactExplorerControllerTest.cls-meta.xml",
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
        if not set(normalized).issubset(SALESFORCE_CANDIDATE_DIAGNOSTIC_IDS):
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


def check_salesforce_candidate(root: Path) -> dict[str, Any]:
    """Validate a candidate solution without consulting a fixture or lockfile.

    This is a static, fail-closed preflight. It validates Salesforce metadata,
    source-level security constraints, and the presence of focused Apex and LWC
    tests. It intentionally makes no claim that Apex, Jest, or deployment ran.
    """

    root = _root(root)
    required = (PROJECT_PATH, *_PRESERVED_LEGACY_PATHS, *SALESFORCE_AGENT_OUTPUT_PATHS)
    try:
        paths = {relative: _file(root, relative) for relative in required}
    except LocalCheckFailure as exc:
        raise LocalCheckFailure(
            "local contract assertion failed",
            failure_code="salesforce_candidate_inventory",
            diagnostic_ids=("salesforce_candidate_inventory",),
        ) from exc

    stages: tuple[tuple[str, Callable[[], None]], ...] = (
        (
            "salesforce_project_contract",
            lambda: _check_project(_json_object(paths[PROJECT_PATH])),
        ),
        (
            "salesforce_manifest_contract",
            lambda: _check_manifest(_xml_root(paths[MANIFEST_PATH])),
        ),
        (
            "salesforce_apex_controller_metadata_contract",
            lambda: _check_apex_metadata(_xml_root(paths[CONTROLLER_METADATA_PATH])),
        ),
        (
            "salesforce_apex_test_metadata_contract",
            lambda: _check_apex_metadata(_xml_root(paths[CONTROLLER_TEST_METADATA_PATH])),
        ),
        (
            "salesforce_apex_controller_contract",
            lambda: _check_controller(_text(paths[CONTROLLER_PATH])),
        ),
        (
            "salesforce_apex_test_contract",
            lambda: _check_controller_test(_text(paths[CONTROLLER_TEST_PATH])),
        ),
        (
            "salesforce_lwc_javascript_contract",
            lambda: _check_lwc_javascript(_text(paths[LWC_JAVASCRIPT_PATH])),
        ),
        (
            "salesforce_lwc_template_contract",
            lambda: _check_lwc_template(_text(paths[LWC_HTML_PATH])),
        ),
        (
            "salesforce_lwc_styles_contract",
            lambda: _check_lwc_styles(_text(paths[LWC_CSS_PATH])),
        ),
        (
            "salesforce_lwc_metadata_contract",
            lambda: _check_lwc_metadata(_xml_root(paths[LWC_METADATA_PATH])),
        ),
        (
            "salesforce_lwc_jest_contract",
            lambda: _check_lwc_test(_text(paths[LWC_TEST_PATH])),
        ),
        (
            "salesforce_lwc_fixture_contract",
            lambda: _check_lwc_data(
                _json_array(paths[LWC_ACCOUNTS_DATA_PATH]),
                _json_array(paths[LWC_CONTACTS_DATA_PATH]),
            ),
        ),
        (
            "salesforce_permission_set_contract",
            lambda: _check_permission_set(_xml_root(paths[PERMISSION_SET_PATH])),
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


def check_lwc_jest_toolchain(toolchain_root: Path) -> dict[str, Any]:
    """Validate the immutable Jest harness without inspecting a candidate."""

    root = _root(toolchain_root)
    paths = {relative: _file(root, relative) for relative in LWC_JEST_TOOLCHAIN_DIGESTS}
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
    _require(digests == LWC_JEST_TOOLCHAIN_DIGESTS, "toolchain content digests")

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
        "controller_test_sha256": digests[LWC_CONTROLLER_TEST_PATH],
        "candidate_content_inspected": False,
        "network_accessed": False,
        "install_performed": False,
        "lwc_jest_executed": False,
    }


def check_dependency_closure(root: Path) -> tuple[dict[str, Any], Any]:
    """Require a resolved target LWC/Apex/test/permission dependency closure."""

    root = _root(root)
    revision = content_revision(root)
    graph = build_salesforce_dependency_graph(root, (TARGET_ENTRY,), revision)
    _require(not graph.has_unresolved, "resolved dependency closure")
    expected = {
        (NodeKind.LWC_COMPONENT, "accountContactExplorer"),
        (NodeKind.APEX_CLASS, "AccountContactExplorerController"),
        (NodeKind.APEX_TEST, "AccountContactExplorerControllerTest"),
        (NodeKind.PERMISSION_SET, "AccountContactExplorerUser"),
        (NodeKind.VISUALFORCE_PAGE, "LegacyAccountContactExplorer"),
    }
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
    subparsers.add_parser("candidate-contract")
    subparsers.add_parser("dependency-closure")
    toolchain = subparsers.add_parser("toolchain-contract")
    toolchain.add_argument("--toolchain-root", type=Path, required=True)
    revision = subparsers.add_parser("workspace-revision")
    revision.add_argument("--expected", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "candidate-contract":
            result = check_salesforce_candidate(Path.cwd())
        elif args.command == "dependency-closure":
            result, _ = check_dependency_closure(Path.cwd())
        elif args.command == "toolchain-contract":
            result = check_lwc_jest_toolchain(args.toolchain_root)
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
            if diagnostic_id in SALESFORCE_CANDIDATE_DIAGNOSTIC_IDS
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


def _check_project(project: dict[str, Any]) -> None:
    _require(project.get("sourceApiVersion") == API_VERSION, "project API version")
    package_directories = project.get("packageDirectories")
    _require(isinstance(package_directories, list), "project package directories")
    _require(
        any(
            isinstance(entry, dict)
            and entry.get("path") == "force-app"
            and entry.get("default") is True
            for entry in cast(list[Any], package_directories)
        ),
        "default source directory",
    )
    login_url = project.get("sfdcLoginUrl")
    _require(
        login_url in {"https://login.salesforce.com", "https://test.salesforce.com"},
        "Salesforce login URL",
    )


def _check_manifest(package_root: ElementTree.Element) -> None:
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
        observed
        == {
            "ApexClass": {
                "AccountContactExplorerController",
                "AccountContactExplorerControllerTest",
                "LegacyAccountContactExplorerController",
                "LegacyAccountContactExplorerControllerTest",
            },
            "ApexPage": {"LegacyAccountContactExplorer"},
            "LightningComponentBundle": {"accountContactExplorer"},
            "PermissionSet": {"AccountContactExplorerUser"},
        },
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


def _check_controller(controller: str) -> None:
    code, string_literals = _apex_lexical_view(controller)
    sharing_declarations = tuple(
        match.group(1).lower() if match.group(1) is not None else "none"
        for match in re.finditer(
            r"(?im)^\s*(?:public|global)\s+"
            r"(?:(with|without|inherited)\s+sharing\s+)?"
            r"class\s+AccountContactExplorerController\b",
            code,
        )
    )
    _require(sharing_declarations == ("with",), "with-sharing Apex declaration")

    for marker in (
        "public with sharing class AccountContactExplorerController",
        "private static final Integer MAX_ACCOUNTS = 50",
        "private static final Integer MAX_CONTACTS = 100",
        "public static List<Account> getAccounts()",
        "public static List<Contact> getContacts(Id accountId)",
        "LIMIT :MAX_ACCOUNTS",
        "LIMIT :MAX_CONTACTS",
        "if (accountId == null)",
    ):
        _require(marker in code, "Apex behavior and safety")
    for name, value in (("MAX_ACCOUNTS", "50"), ("MAX_CONTACTS", "100")):
        _require(
            re.search(
                rf"@TestVisible\s+private\s+static\s+final\s+Integer\s+"
                rf"{name}\s*=\s*{value}\b",
                code,
            )
            is not None,
            "test-visible Apex limits",
        )
    _require(
        len(re.findall(r"@AuraEnabled\b", code)) == 2
        and code.count("@AuraEnabled(cacheable=true)") == 2,
        "exact cacheable Apex methods",
    )
    _require(
        re.search(
            r"@AuraEnabled\(cacheable=true\)\s+"
            r"public\s+static\s+List<Account>\s+getAccounts\(\)",
            code,
        )
        is not None
        and re.search(
            r"@AuraEnabled\(cacheable=true\)\s+"
            r"public\s+static\s+List<Contact>\s+getContacts\(Id\s+accountId\)",
            code,
        )
        is not None,
        "cacheable Apex method bindings",
    )
    _require(
        len(re.findall(r"\[\s*SELECT\b", code, re.I)) == 2,
        "exact static SOQL query count",
    )
    _require(
        len(re.findall(r"\bWITH\s+USER_MODE\b", code, re.I)) == 2,
        "Apex user-mode queries",
    )
    _require(
        len(re.findall(r"\bthrow\s+new\s+AuraHandledException\s*\(", code, re.I)) == 2,
        "controlled Apex exceptions",
    )
    _require(
        string_literals.count("Accounts could not be read.") == 1
        and string_literals.count("Contacts could not be read.") == 1,
        "controlled Apex exception messages",
    )
    accounts_body = _apex_method_body(code, "public static List<Account> getAccounts()")
    contacts_body = _apex_method_body(
        code,
        "public static List<Contact> getContacts(Id accountId)",
    )
    _require(
        re.search(
            r"\[\s*SELECT\s+Id\s*,\s*Name\s+FROM\s+Account\s+"
            r"WITH\s+USER_MODE\s+ORDER\s+BY\s+Name\s+LIMIT\s+:MAX_ACCOUNTS\s*\]",
            accounts_body,
            re.I,
        )
        is not None,
        "Account query fields ordering and limit",
    )
    _require(
        re.search(r"if\s*\(\s*accountId\s*==\s*null\s*\)", contacts_body, re.I) is not None
        and re.search(
            r"\[\s*SELECT\s+Id\s*,\s*FirstName\s*,\s*LastName\s*,\s*Email\s*,\s*Phone\s+"
            r"FROM\s+Contact\s+WHERE\s+AccountId\s*=\s*:accountId\s+WITH\s+USER_MODE\s+"
            r"ORDER\s+BY\s+LastName\s*,\s*FirstName\s+LIMIT\s+:MAX_CONTACTS\s*\]",
            contacts_body,
            re.I,
        )
        is not None,
        "Contact query fields filter ordering and limit",
    )
    _require(
        len(re.findall(r"\bthrow\s+new\s+AuraHandledException\s*\(", accounts_body, re.I)) == 1
        and len(re.findall(r"\bthrow\s+new\s+AuraHandledException\s*\(", contacts_body, re.I)) == 1,
        "controlled Apex exception bindings",
    )

    _require(
        re.search(
            r"(?i)\bDatabase\s*\.\s*"
            r"(?:insert|update|upsert|delete|undelete|merge|query|queryWithBinds)\b",
            code,
        )
        is None,
        "forbidden Database operation",
    )
    _require(
        re.search(r"(?i)\b(?:insert|update|upsert|delete|undelete|merge)\b", code) is None,
        "read-only Apex contract",
    )
    for forbidden in (r"(?i)\b(Http|HttpRequest|HttpResponse|Continuation|NamedCredential)\b",):
        _require(re.search(forbidden, code) is None, "forbidden Apex capability")
    for forbidden in (
        r"(?i)\bcallout\s*:",
        r"(?i)\b(password|client_secret|access_token|authorization)\b\s*=",
    ):
        _require(re.search(forbidden, controller) is None, "forbidden Apex material")


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


def _apex_method_body(code: str, signature: str) -> str:
    _require(code.count(signature) == 1, "unique Apex method signature")
    signature_index = code.index(signature)
    opening = code.find("{", signature_index + len(signature))
    _require(opening >= 0, "Apex method opening brace")
    depth = 0
    for index in range(opening, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                return code[opening + 1 : index]
    raise LocalCheckFailure("local contract assertion failed")


def _check_controller_test(test_source: str) -> None:
    code, string_literals = _apex_lexical_view(test_source)
    for marker in (
        "@IsTest",
        "@TestSetup",
        "returnsAccountsInNameOrder",
        "returnsContactsInLastAndFirstNameOrder",
        "returnsEmptyListForBlankSelection",
        "returnsEmptyListWhenAccountHasNoContacts",
        "AccountContactExplorerController.getAccounts()",
        "AccountContactExplorerController.getContacts(null)",
        "AccountContactExplorerController.MAX_ACCOUNTS + 2",
        "AccountContactExplorerController.MAX_CONTACTS + 2",
        "System.assertEquals(AccountContactExplorerController.MAX_ACCOUNTS, accounts.size())",
        "System.assertEquals(AccountContactExplorerController.MAX_CONTACTS, contacts.size())",
        "WHERE AccountId = :accountRecord.Id",
        "Map<Id, Contact> returnedContactsById = new Map<Id, Contact>(contacts)",
        "Set<Id> returnedIds = returnedContactsById.keySet()",
        "private static String paddedNumber(Integer numberValue)",
    ):
        _require(marker in code, "Apex test behavior")
    _require("String.format" not in code, "supported Apex padding helper")
    _require(
        re.search(r"\{\d+\s*:\s*\d+\}", test_source) is None,
        "supported Apex format syntax",
    )
    _require(
        re.search(
            r"System\.assert\s*\(\s*!\s*returnedIds\.contains\(foreignContact\.Id\)\s*\)"
            r"|(?:System\.)?Assert\.isFalse\s*\(\s*"
            r"returnedIds\.contains\(foreignContact\.Id\)\s*\)",
            code,
        )
        is not None,
        "foreign Contact exclusion assertion",
    )
    for marker in (
        "System.assertEquals('Synthetic Account 000', accounts[0].Name)",
        "System.assertEquals('Synthetic Account 049', accounts[49].Name)",
        "System.assertEquals('Alpha Tie', contacts[0].LastName)",
        "System.assertEquals('Ada', contacts[0].FirstName)",
        "System.assertEquals('Alpha Tie', contacts[1].LastName)",
        "System.assertEquals('Zoe', contacts[1].FirstName)",
        "System.assertEquals('Synthetic 097', contacts[99].LastName)",
    ):
        _require(marker in test_source, "Apex test ordering and cap assertions")
    for expected_literal in (
        "Synthetic Account 000",
        "Synthetic Account 049",
        "Alpha Tie",
        "Ada",
        "Zoe",
        "Synthetic 097",
    ):
        _require(expected_literal in string_literals, "Apex test expected values")
    _require(code.count("Test.startTest()") == 4, "isolated Apex test executions")


def _javascript_braced_body(source: str, opening: int) -> str | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(source)):
        character = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"', "`"}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    return None


def _javascript_brace_depth_at(source: str, position: int) -> int:
    """Return structural brace depth while ignoring quoted JavaScript text."""

    depth = 0
    quote: str | None = None
    escaped = False
    for character in source[:position]:
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"', "`"}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
    return depth


def _javascript_top_level_matches(source: str, pattern: str) -> tuple[re.Match[str], ...]:
    return tuple(
        match
        for match in re.finditer(pattern, source)
        if _javascript_brace_depth_at(source, match.start()) == 0
    )


def _javascript_top_level_match(source: str, pattern: str) -> re.Match[str] | None:
    return next(iter(_javascript_top_level_matches(source, pattern)), None)


def _javascript_direct_sequence_matches(
    source: str,
    pattern: str,
) -> tuple[re.Match[str], ...]:
    """Find top-level blocks that cannot be braceless control-statement bodies."""

    return tuple(
        match
        for match in _javascript_top_level_matches(source, pattern)
        if not source[: match.start()].strip()
        or source[: match.start()].rstrip().endswith((";", "}"))
    )


def _check_lwc_javascript(source: str) -> None:
    source = _without_c_style_comments(source, line_comments=True)
    code_view, has_template_literal = _javascript_code_view(source)
    javascript_diagnostics: list[str] = []
    import_declarations = tuple(re.finditer(r"(?ms)^[ \t]*import\b(?P<body>.*?);", code_view))
    static_imports: list[str] = []
    import_inventory_is_valid = True
    for declaration in import_declarations:
        body = source[declaration.start("body") : declaration.end("body")].strip()
        from_match = re.fullmatch(
            r".+?\bfrom\s*(['\"])(?P<module>[^'\"]+)\1\s*",
            body,
            flags=re.DOTALL,
        )
        side_effect_match = re.fullmatch(
            r"(['\"])(?P<module>[^'\"]+)\1\s*",
            body,
            flags=re.DOTALL,
        )
        module_match = from_match or side_effect_match
        if module_match is None:
            import_inventory_is_valid = False
        else:
            static_imports.append(module_match.group("module"))
    import_tokens = tuple(re.finditer(r"\bimport\b", code_view))
    import_inventory_is_valid = import_inventory_is_valid and all(
        any(
            declaration.start() <= token.start() < declaration.end()
            for declaration in import_declarations
        )
        for token in import_tokens
    )
    allowed_static_imports = frozenset(
        {
            "lwc",
            "@salesforce/apex/AccountContactExplorerController.getAccounts",
            "@salesforce/apex/AccountContactExplorerController.getContacts",
        }
    )
    class_start = re.search(
        r"\bexport\s+default\s+class\s+[A-Za-z_$][\w$]*\s+"
        r"extends\s+LightningElement\s*\{",
        code_view,
    )
    export_tokens = tuple(re.finditer(r"\bexport\b", code_view))
    export_inventory_is_valid = (
        class_start is not None
        and len(export_tokens) == 1
        and export_tokens[0].start() == class_start.start()
    )
    forbidden_runtime_patterns = (
        r"\bfetch\s*\(",
        r"\bXMLHttpRequest\b",
        r"\bWebSocket\b",
        # Reject these executable bindings even when they are first aliased and
        # called later. The source contract is intentionally narrower than the
        # JavaScript runtime available inside Jest.
        r"\b(?:eval|Function|require|arguments)\b",
        r"\bimport\s*\(",
        r"\b(?:globalThis|global|window|self|document|top|parent|frames|opener|"
        r"location|navigator|process|module|exports)\b",
        r"\b(?:jest|describe|it|test|expect|beforeEach|afterEach|beforeAll|afterAll)\b",
        r"\b(?:Proxy|Reflect|__proto__|prototype|ownerDocument|defaultView)\b",
        r"\bconstructor\b",
        r"\\u(?:[0-9A-Fa-f]{4}|\{[0-9A-Fa-f]{1,6}\})",
        # Object.freeze is the only Object API required by this bounded
        # component. Reflective Object APIs can otherwise recover Function or
        # hidden runtime globals without spelling a forbidden identifier.
        r"\bObject\b(?!\s*\.\s*freeze\s*\()",
        r"\?\.\s*\[",
        r"(?:\{|,)\s*\[",
        r"(?:[A-Za-z_$][\w$]*|\)|\])\s*\[",
    )
    forbidden_sensitive_patterns = (
        r"(?i)\b(password|client_secret|access_token|authorization)\b\s*[:=]",
        r"https?://",
        r"(['\"])@jest/globals\1",
        r"(?:\{|,)\s*(?:'(?:\\.|[^'\\\r\n])*'|\"(?:\\.|[^\"\\\r\n])*\")\s*:",
    )
    if (
        not import_inventory_is_valid
        or not export_inventory_is_valid
        or len(static_imports) != len(allowed_static_imports)
        or frozenset(static_imports) != allowed_static_imports
        or has_template_literal
        or any(re.search(pattern, code_view) for pattern in forbidden_runtime_patterns)
        or any(re.search(pattern, source) for pattern in forbidden_sensitive_patterns)
    ):
        javascript_diagnostics.append("lwc_forbidden_runtime_capability")

    class_body = (
        _javascript_braced_body(source, class_start.end() - 1) if class_start is not None else ""
    )
    direct_account_options_field = _javascript_top_level_match(
        class_body or "",
        r"(?m)^[ \t]*accountOptions\s*=\s*\[\s*BLANK_ACCOUNT_OPTION\s*\]\s*;",
    )
    wire_handler_start = _javascript_top_level_match(
        class_body or "",
        r"@wire\s*\(\s*getAccounts\s*\)\s*wiredAccounts\s*\([^)]*\)\s*\{",
    )
    wire_handler = (
        _javascript_braced_body(class_body or "", wire_handler_start.end() - 1)
        if wire_handler_start is not None
        else ""
    )
    direct_wire_assignment = re.search(
        r"this\s*\.\s*accountOptions\s*=\s*\[\s*BLANK_ACCOUNT_OPTION\s*,\s*"
        r"\.\.\.\s*data\s*\.\s*map\s*\(",
        wire_handler or "",
    )
    proxied_account_options = (
        re.search(r"\b(?:get|set)\s+accountOptions\s*\(", source) is not None
        or re.search(r"\b(?:this\s*\.\s*)?_accountOptions\b", source) is not None
        or re.search(r"\bthis\s*\.\s*accountOptions\s*\.\s*push\s*\(", source) is not None
    )
    if (
        direct_account_options_field is None
        or direct_wire_assignment is None
        or proxied_account_options
    ):
        javascript_diagnostics.append("lwc_account_options_reactive_field")
    direct_has_loaded_field = _javascript_top_level_match(
        class_body or "",
        r"(?m)^[ \t]*hasLoaded\s*=\s*false\s*;",
    )
    account_change_start = _javascript_top_level_match(
        class_body or "",
        r"\bhandleAccountChange\s*\([^)]*\)\s*\{",
    )
    account_change_body = (
        _javascript_braced_body(class_body or "", account_change_start.end() - 1)
        if account_change_start is not None
        else ""
    )
    handle_load_start = _javascript_top_level_match(
        class_body or "",
        r"\basync\s+handleLoad\s*\(\s*\)\s*\{",
    )
    handle_load_body = (
        _javascript_braced_body(class_body or "", handle_load_start.end() - 1)
        if handle_load_start is not None
        else ""
    )
    try_start = _javascript_top_level_match(handle_load_body or "", r"\btry\s*\{")
    try_body = (
        _javascript_braced_body(handle_load_body or "", try_start.end() - 1)
        if try_start is not None
        else ""
    )
    catch_start = _javascript_top_level_match(
        handle_load_body or "",
        r"\bcatch\s*\([^)]*\)\s*\{",
    )
    catch_body = (
        _javascript_braced_body(handle_load_body or "", catch_start.end() - 1)
        if catch_start is not None
        else ""
    )
    finally_start = _javascript_top_level_match(handle_load_body or "", r"\bfinally\s*\{")
    finally_body = (
        _javascript_braced_body(handle_load_body or "", finally_start.end() - 1)
        if finally_start is not None
        else ""
    )
    current_request_method_start = _javascript_top_level_match(
        class_body or "",
        r"\bisCurrentRequest\s*\(\s*accountId\s*,\s*requestGeneration\s*\)\s*\{",
    )
    current_request_method_body = (
        _javascript_braced_body(
            class_body or "",
            current_request_method_start.end() - 1,
        )
        if current_request_method_start is not None
        else ""
    )
    account_change_cleanups = _javascript_direct_sequence_matches(
        account_change_body or "",
        r"(?m)^(?P<indent>[ \t]*)this\s*\.\s*contacts\s*=\s*\[\s*\]\s*;[ \t]*\r?\n"
        r"(?:[ \t]*\r?\n)*(?P=indent)this\s*\.\s*isLoading\s*=\s*false\s*;"
        r"[ \t]*\r?\n(?:[ \t]*\r?\n)*(?P=indent)this\s*\.\s*hasLoaded\s*=\s*false\s*;",
    )
    handle_load_cleanup_blocks = _javascript_direct_sequence_matches(
        handle_load_body or "",
        r"(?m)^(?P<indent>[ \t]*)this\s*\.\s*isLoading\s*=\s*true\s*;[ \t]*\r?\n"
        r"(?:[ \t]*\r?\n)*(?P=indent)this\s*\.\s*hasLoaded\s*=\s*false\s*;"
        r"[ \t]*\r?\n(?:[ \t]*\r?\n)*(?P=indent)this\s*\.\s*contacts\s*=\s*\[\s*\]\s*;",
    )
    account_change_generation_increments = _javascript_direct_sequence_matches(
        account_change_body or "",
        r"(?m)^[ \t]*this\s*\.\s*loadRequestGeneration\s*\+=\s*1\s*;",
    )
    handle_load_generation_blocks = _javascript_direct_sequence_matches(
        handle_load_body or "",
        r"(?m)^(?P<indent>[ \t]*)const\s+accountId\s*=\s*"
        r"this\s*\.\s*selectedAccountId\s*;[ \t]*\r?\n"
        r"(?:[ \t]*\r?\n)*(?P=indent)this\s*\.\s*loadRequestGeneration\s*\+=\s*1\s*;"
        r"[ \t]*\r?\n(?:[ \t]*\r?\n)*(?P=indent)const\s+requestGeneration\s*=\s*"
        r"this\s*\.\s*loadRequestGeneration\s*;",
    )
    handle_load_request = re.search(
        r"\bawait\s+getContacts\s*\(",
        handle_load_body or "",
    )
    blank_guard_start = _javascript_top_level_match(
        handle_load_body or "",
        r"\bif\s*\(\s*!\s*this\s*\.\s*selectedAccountId\s*\)\s*\{",
    )
    blank_guard_opening = blank_guard_start.end() - 1 if blank_guard_start is not None else -1
    blank_guard_body = (
        _javascript_braced_body(handle_load_body or "", blank_guard_opening)
        if blank_guard_opening >= 0
        else None
    )
    blank_guard_end = (
        blank_guard_opening + 1 + len(blank_guard_body) if blank_guard_body is not None else -1
    )
    valid_interval_end = handle_load_request.start() if handle_load_request is not None else -1
    account_change_cleanup = next(iter(account_change_cleanups), None)
    account_change_code_view, _ = _javascript_code_view(account_change_body or "")
    assignment_operator = r"(?:\?\?=|&&=|\|\|=|\*\*=|>>>=|<<=|>>=|[+\-*/%&|^]=|=(?!=|>))"

    def account_change_state_write_count(field: str) -> int:
        member = rf"\bthis\s*\.\s*{field}\b"
        return len(
            re.findall(
                rf"(?:\+\+|--)\s*{member}|{member}\s*(?:\+\+|--|{assignment_operator})",
                account_change_code_view,
            )
        )

    account_change_state_has_single_write = all(
        account_change_state_write_count(field) == 1
        for field in ("contacts", "isLoading", "hasLoaded")
    )
    valid_path_cleanup = next(
        (
            match
            for match in handle_load_cleanup_blocks
            if blank_guard_end < match.start() and match.end() < valid_interval_end
        ),
        None,
    )
    account_change_generation_increment = next(
        (
            match
            for match in account_change_generation_increments
            if not (account_change_body or "")[: match.start()].strip()
        ),
        None,
    )
    valid_path_generation_block = next(
        (
            match
            for match in handle_load_generation_blocks
            if blank_guard_end < match.start() and match.end() < valid_interval_end
        ),
        None,
    )
    if (
        direct_has_loaded_field is None
        or account_change_cleanup is None
        or not account_change_state_has_single_write
        or handle_load_request is None
        or blank_guard_body is None
        or re.search(r"\breturn\s*;", blank_guard_body) is None
        or valid_path_cleanup is None
    ):
        javascript_diagnostics.append("lwc_has_loaded_reset")
    stale_guard_pattern = (
        r"(?m)^[ \t]*if\s*\(\s*!\s*this\s*\.\s*isCurrentRequest\s*\(\s*"
        r"accountId\s*,\s*requestGeneration\s*\)\s*\)\s*\{\s*return\s*;\s*\}"
    )
    try_stale_guards = _javascript_direct_sequence_matches(try_body or "", stale_guard_pattern)
    catch_stale_guards = _javascript_direct_sequence_matches(
        catch_body or "",
        stale_guard_pattern,
    )
    finally_current_guard = _javascript_direct_sequence_matches(
        finally_body or "",
        r"(?m)^[ \t]*if\s*\(\s*this\s*\.\s*isCurrentRequest\s*\(\s*"
        r"accountId\s*,\s*requestGeneration\s*\)\s*\)\s*\{\s*"
        r"this\s*\.\s*isLoading\s*=\s*false\s*;\s*\}",
    )
    current_request_comparison_is_complete = (
        re.fullmatch(
            r"\s*return\s*\(\s*"
            r"(?:accountId\s*===\s*this\.selectedAccountId|"
            r"this\.selectedAccountId\s*===\s*accountId)\s*&&\s*"
            r"(?:requestGeneration\s*===\s*this\.loadRequestGeneration|"
            r"this\.loadRequestGeneration\s*===\s*requestGeneration)\s*"
            r"\)\s*;\s*",
            current_request_method_body or "",
        )
        is not None
    )
    if (
        account_change_generation_increment is None
        or len(account_change_generation_increments) != 1
        or valid_path_generation_block is None
        or len(handle_load_generation_blocks) != 1
        or len(try_stale_guards) != 1
        or len(catch_stale_guards) != 1
        or len(finally_current_guard) != 1
        or not current_request_comparison_is_complete
    ):
        javascript_diagnostics.append("lwc_request_generation_increment")
    if javascript_diagnostics:
        raise LocalCheckFailure(
            "local LWC JavaScript contract assertion failed",
            diagnostic_ids=javascript_diagnostics,
        )
    for marker in (
        "import { LightningElement, wire } from 'lwc'",
        "extends LightningElement",
        "@salesforce/apex/AccountContactExplorerController.getAccounts",
        "@salesforce/apex/AccountContactExplorerController.getContacts",
        "@wire(getAccounts)",
        "async handleLoad()",
        "if (!this.selectedAccountId)",
        "this.isLoading = true",
        "this.isLoading = false",
        "loadRequestGeneration = 0",
        "const accountId = this.selectedAccountId",
        "const requestGeneration = this.loadRequestGeneration",
        "getContacts({ accountId })",
        "isCurrentRequest(accountId, requestGeneration)",
        "this.hasLoaded = true",
        "columns = CONTACT_COLUMNS",
        "fieldName: 'FirstName'",
        "fieldName: 'LastName'",
        "fieldName: 'Email'",
        "fieldName: 'Phone'",
        "get isLoadDisabled()",
        "get showEmptyState()",
        "Accounts could not be loaded.",
        "Contacts could not be loaded.",
    ):
        _require(marker in source, "LWC behavior")
    _require(source.count("isLoading = false") >= 3, "LWC selection loading reset")
    _require(
        source.count("Select an account before loading contacts.") >= 2,
        "LWC blank-selection warning",
    )


def _check_lwc_template(template: str) -> None:
    template = _without_html_comments(template)
    for marker in (
        "<lightning-combobox",
        "value={selectedAccountId}",
        "options={accountOptions}",
        "onchange={handleAccountChange}",
        "<lightning-button",
        "onclick={handleLoad}",
        "disabled={isLoadDisabled}",
        "lwc:if={warningMessage}",
        "lwc:if={errorMessage}",
        "lwc:if={isLoading}",
        "lwc:elseif={hasContacts}",
        "lwc:elseif={showEmptyState}",
        'role="alert"',
        'alternative-text="Loading contacts"',
        'key-field="Id"',
    ):
        _require(marker in template, "LWC template state and accessibility")


def _check_lwc_styles(styles: str) -> None:
    styles = _without_c_style_comments(styles, line_comments=False)
    for marker in (":host", ".controls", ".loading-region", ".warning", ".empty-state"):
        _require(marker in styles, "LWC styles")
    _require(re.search(r"(?i)(@import|url\s*\()", styles) is None, "external CSS resource")


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


def _check_lwc_test(test_source: str) -> None:
    test_source = _without_c_style_comments(test_source, line_comments=True)
    diagnostics: list[str] = []

    def require_clause(condition: bool, diagnostic_id: str) -> None:
        if not condition and diagnostic_id not in diagnostics:
            diagnostics.append(diagnostic_id)

    explicit_globals = re.search(
        r"(?m)^[ \t]*import\s*\{(?P<names>[^}\r\n]+)\}\s*from\s*"
        r"(['\"])@jest/globals\2\s*;",
        test_source,
    )
    imported_jest_globals = (
        {name.strip() for name in explicit_globals.group("names").split(",") if name.strip()}
        if explicit_globals is not None
        else set()
    )
    require_clause(
        imported_jest_globals == {"afterEach", "describe", "expect", "it", "jest"},
        "jest_explicit_globals",
    )

    flush_start = re.search(
        r"\basync\s+function\s+flushPromises\s*\(\s*\)\s*\{",
        test_source,
    )
    flush_body = (
        _javascript_braced_body(test_source, flush_start.end() - 1)
        if flush_start is not None
        else ""
    )
    flush_sequence = _javascript_direct_sequence_matches(
        flush_body or "",
        r"(?m)^(?P<indent>[ \t]*)await\s+Promise\s*\.\s*resolve\s*\(\s*\)\s*;"
        r"[ \t]*\r?\n(?:[ \t]*\r?\n)*(?P=indent)await\s+Promise\s*\.\s*resolve\s*"
        r"\(\s*\)\s*;",
    )
    require_clause(
        len(flush_sequence) == 1
        and len(re.findall(r"\bawait\s+flushPromises\s*\(\s*\)\s*;", test_source)) >= 10,
        "jest_settled_render_flush",
    )

    after_each_start = re.search(r"\bafterEach\s*\(\s*\(\s*\)\s*=>\s*\{", test_source)
    after_each_body = (
        _javascript_braced_body(test_source, after_each_start.end() - 1)
        if after_each_start is not None
        else ""
    )
    require_clause(
        re.search(
            r"while\s*\(\s*document\s*\.\s*body\s*\.\s*firstChild\s*\)\s*\{\s*"
            r"document\s*\.\s*body\s*\.\s*removeChild\s*\(\s*"
            r"document\s*\.\s*body\s*\.\s*firstChild\s*\)\s*;\s*\}",
            after_each_body or "",
        )
        is not None,
        "jest_dom_cleanup",
    )

    groups = {
        "jest_wire_adapter_contract": (
            "createApexTestWireAdapter",
            "@salesforce/apex/AccountContactExplorerController.getAccounts",
            "require('@salesforce/sfdx-lwc-jest')",
            "getAccounts.emit(ACCOUNTS)",
            "getAccounts.error",
        ),
        "jest_imperative_mock_contract": (
            "@salesforce/apex/AccountContactExplorerController.getContacts",
            "getContacts.mockRejectedValue",
        ),
        "jest_explicit_load_behavior": (
            "expect(getContacts).not.toHaveBeenCalled()",
            "lightning-datatable",
        ),
        "jest_loading_behavior": ("lightning-spinner",),
        "jest_required_behavior_coverage": (".empty-state",),
        "jest_safe_error_redaction": (
            "Contacts could not be loaded.",
            "not.toContain('SELECT Id FROM Contact')",
        ),
        "jest_stale_scenario_setup": (
            "createDeferredPromise",
            "mockReturnValueOnce(firstRequest.promise)",
            "mockReturnValueOnce(secondRequest.promise)",
            "secondRequest.resolve(CONTACTS)",
            "firstRequest.resolve([",
        ),
        "jest_shadow_dom_contract": (
            "is: AccountContactExplorer",
            "element.shadowRoot.querySelector",
        ),
    }
    for diagnostic_id, markers in groups.items():
        require_clause(all(marker in test_source for marker in markers), diagnostic_id)

    for title in SALESFORCE_REQUIRED_LWC_BEHAVIOR_TITLES:
        require_clause(
            re.search(
                r"\b(?:it|test)\s*\(\s*(['\"])" + re.escape(title) + r"\1\s*,",
                test_source,
            )
            is not None,
            "jest_exact_behavior_titles",
        )
    require_clause(
        re.search(
            r"getContacts\.mockResolvedValue(?:Once)?\(\s*CONTACTS\s*\)",
            test_source,
        )
        is not None
        and re.search(
            r"getContacts\.mockResolvedValue(?:Once)?\(\s*\[\s*\]\s*\)",
            test_source,
        )
        is not None,
        "jest_fixture_result_coverage",
    )
    require_clause(
        test_source.count("__esModule: true") == 2,
        "jest_mock_module_contract",
    )
    require_clause(
        re.search(
            r"createApexTestWireAdapter\s*\(\s*jest\s*\.\s*fn\s*\(\s*\)\s*\)",
            test_source,
        )
        is not None,
        "jest_wire_adapter_factory_argument",
    )
    require_clause(
        "element.querySelector" not in test_source,
        "jest_shadow_dom_contract",
    )
    require_clause(
        re.search(
            r"detail\s*:\s*\{\s*value\s*:\s*(?:accountId|"
            r"ACCOUNTS\s*\[\s*\d+\s*\]\s*\.\s*Id|"
            r"(['\"])001[A-Za-z0-9]{15}\1)\s*\}",
            test_source,
        )
        is not None,
        "jest_shadow_dom_contract",
    )
    require_clause(
        "getAccounts.mockSuccess" not in test_source and "getAccounts.mockError" not in test_source,
        "jest_wire_adapter_api",
    )
    loading_test_title = "shows loading state and disables Load while contacts are pending"
    loading_test_match = re.search(
        r"\b(?:it|test)\s*\(\s*(['\"])" + re.escape(loading_test_title) + r"\1\s*,",
        test_source,
    )
    loading_test_opening = (
        test_source.find("{", loading_test_match.end()) if loading_test_match is not None else -1
    )
    loading_test_body = (
        _javascript_braced_body(test_source, loading_test_opening)
        if loading_test_opening >= 0
        else None
    )
    spinner_attribute_read = re.search(
        r"\.\s*getAttribute\s*\(\s*(['\"])alternative-text\1\s*\)",
        test_source,
    )
    spinner_public_property_assertion = re.search(
        r"expect\s*\(\s*spinner\s*\.\s*alternativeText\s*\)\s*\.\s*"
        r"(?:toBe|toEqual)\s*\(\s*(['\"])Loading contacts\1\s*\)",
        loading_test_body or "",
    )
    require_clause(
        spinner_attribute_read is None and spinner_public_property_assertion is not None,
        "jest_spinner_public_property",
    )

    second_resolution = test_source.find("secondRequest.resolve(CONTACTS)")
    first_resolution = test_source.find("firstRequest.resolve([")
    require_clause(
        second_resolution >= 0 and first_resolution >= 0 and second_resolution < first_resolution,
        "jest_stale_resolution_order",
    )

    lifecycle_or_test = tuple(
        (match.start(), match.group(1))
        for match in re.finditer(r"\b(beforeEach|afterEach|it|test)\s*\(", test_source)
    )
    helper_component_positions = tuple(
        match.start() for match in re.finditer(r"\bcreateComponent\s*\(\s*\)", test_source)
    )
    direct_component_positions = tuple(
        match.start()
        for match in re.finditer(
            r"\bcreateElement\s*\(\s*(['\"])c-account-contact-explorer\1\s*,",
            test_source,
        )
    )
    append_positions = tuple(
        match.start()
        for match in re.finditer(
            r"\bdocument\s*\.\s*body\s*\.\s*appendChild\s*\(",
            test_source,
        )
    )
    wire_calls = tuple(
        match.start()
        for match in re.finditer(r"\bgetAccounts\s*\.\s*(?:emit|error)\s*\(", test_source)
    )
    component_precedes_every_wire_call = bool(wire_calls)
    for wire_call in wire_calls:
        preceding_constructs = tuple(item for item in lifecycle_or_test if item[0] < wire_call)
        if not preceding_constructs:
            component_precedes_every_wire_call = False
            break
        construct_position, construct_kind = preceding_constructs[-1]
        helper_created = any(
            construct_position < component_position < wire_call
            for component_position in helper_component_positions
        )
        directly_created_and_appended = any(
            construct_position < component_position < append_position < wire_call
            for component_position in direct_component_positions
            for append_position in append_positions
        )
        if construct_kind not in {"it", "test"} or not (
            helper_created or directly_created_and_appended
        ):
            component_precedes_every_wire_call = False
            break
    require_clause(
        component_precedes_every_wire_call,
        "jest_component_before_wire_emit",
    )

    reset_is_in_lifecycle_hook = False
    for reset in re.finditer(r"\bgetContacts\s*\.\s*mockReset\s*\(\s*\)", test_source):
        preceding_constructs = tuple(item for item in lifecycle_or_test if item[0] < reset.start())
        if preceding_constructs and preceding_constructs[-1][1] in {"beforeEach", "afterEach"}:
            reset_is_in_lifecycle_hook = True
            break
    require_clause(reset_is_in_lifecycle_hook, "jest_mock_not_reset")

    nth_call_proof = all(
        re.search(
            rf"\.toHaveBeenNthCalledWith\s*\(\s*{call_number}\s*,\s*\{{\s*"
            rf"accountId\s*:\s*ACCOUNTS\s*\[\s*{index}\s*\]\s*\.\s*Id\s*\}}\s*\)",
            test_source,
        )
        is not None
        for call_number, index in ((1, 0), (2, 1))
    )
    indexed_call_proof = all(
        re.search(
            rf"getContacts\s*\.\s*mock\s*\.\s*calls\s*\[\s*{index}\s*\]"
            r"\s*\[\s*0\s*\]\s*\.\s*accountId\s*\)\s*\.\s*"
            rf"(?:toBe|toEqual)\s*\(\s*ACCOUNTS\s*\[\s*{index}\s*\]\s*\.\s*Id\s*\)",
            test_source,
        )
        is not None
        for index in (0, 1)
    )
    argument_array_call_proof = all(
        re.search(
            rf"expect\s*\(\s*getContacts\s*\.\s*mock\s*\.\s*calls\s*"
            rf"\[\s*{index}\s*\]\s*\)\s*\.\s*toEqual\s*\(\s*\[\s*"
            rf"\{{\s*accountId\s*:\s*ACCOUNTS\s*\[\s*{index}\s*\]\s*\.\s*Id\s*\}}"
            r"\s*\]\s*\)",
            test_source,
        )
        is not None
        for index in (0, 1)
    )
    require_clause(
        nth_call_proof or indexed_call_proof or argument_array_call_proof,
        "jest_ordered_call_proof",
    )

    vacuous_root_assertion = re.search(
        r"expect\s*\(\s*(?:element\s*\.\s*)?shadowRoot\s*\.\s*textContent\s*\)"
        r"\s*\.\s*not\s*\.\s*toBe\s*\(\s*(['\"])Stale\1\s*\)",
        test_source,
    )
    require_clause(vacuous_root_assertion is None, "jest_stale_assertion_vacuous")
    targeted_text_absence = re.search(
        r"expect\s*\([^\n)]*textContent[^\n)]*\)\s*\.\s*not\s*\.\s*toContain"
        r"\s*\(\s*(['\"])Stale\1\s*\)",
        test_source,
    )
    field_level_absence = re.search(
        r"expect\s*\([^\n)]*\.data\s*\[[^\]]+\]\s*\.\s*[A-Za-z_$][\w$]*\s*\)"
        r"\s*\.\s*not\s*\.\s*toBe\s*\(\s*(['\"])Stale\1\s*\)",
        test_source,
    )
    require_clause(
        targeted_text_absence is not None or field_level_absence is not None,
        "jest_stale_render_proof",
    )

    forbidden_capability_patterns = (
        r"\brequire\s*\(\s*(['\"])(?:node:)?(?:child_process|dgram|dns|fs|http|https|net|vm|worker_threads)\1\s*\)",
        r"\bimport\s+[^;\n]*\s+from\s*(['\"])(?:node:)?(?:child_process|dgram|dns|fs|http|https|net|vm|worker_threads)\1",
        r"\b(?:eval|Function|fetch|XMLHttpRequest|WebSocket)\s*\(",
        r"\bprocess\s*\.",
    )
    require_clause(
        not any(re.search(pattern, test_source) for pattern in forbidden_capability_patterns),
        "jest_forbidden_capability",
    )

    if diagnostics:
        raise LocalCheckFailure(
            "local LWC Jest contract assertions failed",
            diagnostic_ids=tuple(diagnostics),
        )


def _check_lwc_data(accounts: list[Any], contacts: list[Any]) -> None:
    _require(len(accounts) >= 2 and len(contacts) >= 2, "synthetic test data inventory")
    account_names: list[str] = []
    for value in accounts:
        _require(isinstance(value, dict), "Account test record")
        record = cast(dict[str, Any], value)
        _require(set(record) == {"Id", "Name"}, "Account test fields")
        _require(
            isinstance(record["Id"], str)
            and re.fullmatch(r"001[A-Za-z0-9]{15}", record["Id"]) is not None,
            "synthetic Account Id",
        )
        _require(isinstance(record["Name"], str), "synthetic Account name")
        account_names.append(cast(str, record["Name"]))
    _require(account_names == sorted(account_names), "Account fixture ordering")

    contact_names: list[tuple[str, str]] = []
    for value in contacts:
        _require(isinstance(value, dict), "Contact test record")
        record = cast(dict[str, Any], value)
        _require(
            set(record) == {"Id", "FirstName", "LastName", "Email", "Phone"},
            "Contact test fields",
        )
        _require(
            isinstance(record["Id"], str)
            and re.fullmatch(r"003[A-Za-z0-9]{15}", record["Id"]) is not None,
            "synthetic Contact Id",
        )
        _require(
            all(isinstance(record[field], str) for field in ("FirstName", "LastName", "Phone")),
            "synthetic Contact text",
        )
        _require(
            isinstance(record["Email"], str) and record["Email"].endswith("@example.invalid"),
            "non-routable Contact email",
        )
        contact_names.append((cast(str, record["LastName"]), cast(str, record["FirstName"])))
    _require(contact_names == sorted(contact_names), "Contact fixture ordering")


def _javascript_code_view(source: str) -> tuple[str, bool]:
    """Blank quoted JavaScript text while preserving code offsets and newlines.

    Template literals are deliberately unsupported by this bounded component
    contract; masking their complete contents prevents string fragments from
    satisfying or tripping identifier checks.
    """

    output = list(source)
    index = 0
    has_template_literal = False
    while index < len(source):
        quote = source[index]
        if quote not in {"'", '"', "`"}:
            index += 1
            continue
        has_template_literal = has_template_literal or quote == "`"
        output[index] = " "
        index += 1
        while index < len(source):
            character = source[index]
            if character == "\\":
                output[index] = " "
                if index + 1 < len(source):
                    if source[index + 1] not in {"\n", "\r"}:
                        output[index + 1] = " "
                    index += 2
                    continue
            if character == quote:
                output[index] = " "
                index += 1
                break
            if character not in {"\n", "\r"}:
                output[index] = " "
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


def _check_permission_set(root: ElementTree.Element) -> None:
    _require(
        root.tag == f"{{{METADATA_NAMESPACE}}}PermissionSet",
        "permission set root",
    )
    namespace = {"m": METADATA_NAMESPACE}
    _require_xml_child_inventory(
        root,
        {
            "classAccesses": 2,
            "description": 1,
            "fieldPermissions": 2,
            "hasActivationRequired": 1,
            "label": 1,
            "objectPermissions": 2,
            "pageAccesses": 1,
        },
    )
    _require(
        root.findtext("m:hasActivationRequired", namespaces=namespace) == "false",
        "permission set activation",
    )
    object_permissions = root.findall("m:objectPermissions", namespace)
    _require(len(object_permissions) == 2, "permission object count")
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
        _require(object_name in {"Account", "Contact"}, "standard object permission")
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
    _require(objects == {"Account", "Contact"}, "permission objects")

    class_accesses = root.findall("m:classAccesses", namespace)
    for access in class_accesses:
        _require_xml_child_inventory(access, {"apexClass": 1, "enabled": 1})
    classes = {
        access.findtext("m:apexClass", namespaces=namespace)
        for access in class_accesses
        if access.findtext("m:enabled", namespaces=namespace) == "true"
    }
    _require(
        len(class_accesses) == 2
        and classes
        == {
            "LegacyAccountContactExplorerController",
            "AccountContactExplorerController",
        },
        "Apex class access",
    )

    field_accesses = root.findall("m:fieldPermissions", namespace)
    fields: set[str] = set()
    for access in field_accesses:
        _require_xml_child_inventory(access, {"editable": 1, "field": 1, "readable": 1})
        field = access.findtext("m:field", namespaces=namespace)
        _require(field in {"Contact.Email", "Contact.Phone"}, "Contact field permission")
        _require(
            access.findtext("m:readable", namespaces=namespace) == "true"
            and access.findtext("m:editable", namespaces=namespace) == "false",
            "read-only field permission",
        )
        fields.add(cast(str, field))
    _require(fields == {"Contact.Email", "Contact.Phone"}, "required Contact fields")

    page_accesses = root.findall("m:pageAccesses", namespace)
    _require(len(page_accesses) == 1, "legacy page access count")
    page_access = page_accesses[0]
    _require_xml_child_inventory(page_access, {"apexPage": 1, "enabled": 1})
    _require(
        page_access.findtext("m:apexPage", namespaces=namespace) == "LegacyAccountContactExplorer"
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


def _json_array(path: Path) -> list[Any]:
    try:
        value = json.loads(_text(path))
    except (json.JSONDecodeError, ValueError) as exc:
        raise LocalCheckFailure("validation JSON is malformed") from exc
    _require(isinstance(value, list), "JSON array")
    return cast(list[Any], value)


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
