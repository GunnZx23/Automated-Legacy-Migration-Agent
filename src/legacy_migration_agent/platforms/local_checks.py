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
        "sha256:cfac8b033bc470163fec5eb9498a602752c3070349647a206a35c4bd297fdf08"
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
APEX_MAX_QUERY_ROWS: Final = 200
JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID: Final = "jest_unapproved_module_target"
LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID: Final = "lwc_template_binding_invalid"

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

# These names belong only to the controller-owned independent behavior suite.
# Candidate-authored test titles are intentionally unconstrained.
SALESFORCE_CONTROLLER_LWC_BEHAVIOR_TITLES: Final[tuple[str, ...]] = (
    "controller: renders account options from the wire adapter",
    "controller: renders a safe account-wire failure",
    "controller: enables Load only after account selection",
    "controller: invokes contacts only after the Load action",
    "controller: exposes loading state while contacts are pending",
    "controller: ignores a response made stale by account change",
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
SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS: Final[frozenset[str]] = frozenset(
    {
        *SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_BY_TITLE.values(),
        SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID,
    }
)

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
        "preserved legacy artifacts. Apex metadata must be Active; expose the LWC only on "
        "lightning__AppPage and lightning__Tab."
    ),
    (
        "Expose public with sharing class AccountContactExplorerController with exactly the two "
        "public static cacheable methods getAccounts() and getContacts(Id accountId). Query only "
        "the required Account and Contact fields, use static SOQL WITH USER_MODE, filter contacts "
        "by the selected account, preserve the specified ordering, and cap each query at 1 through "
        "200 rows with either a literal LIMIT or a positive compile-time Integer constant. Return "
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
        "empty and controlled error states. Bind disabled to a disabled-state getter that returns "
        "true for a blank selection; do not bind a positive canLoadContacts getter directly because "
        "that reverses the Load gate. Clear state and show safe guidance when selection is cleared; "
        "and prevent a response made stale by an account change from "
        "overwriting current state. Do not render the contact-results hook in empty, guidance, or "
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
        "Contact data inline. Test titles, helpers, assertions, mock implementation and source "
        "order are candidate-owned and validated by the pinned Jest runner. Arrange initial-load "
        "mock outcomes before createElement and appendChild; do not call non-@api component methods "
        "through the host element. Do not include skipped "
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


def check_salesforce_candidate(root: Path) -> dict[str, Any]:
    """Validate a candidate solution without consulting a fixture or lockfile.

    This is a static, fail-closed preflight. It validates Salesforce metadata,
    source-level security constraints, and the presence of focused Apex and LWC
    tests. It intentionally makes no claim that Apex, Jest, or deployment ran.
    """

    root = _root(root)
    required = SALESFORCE_AGENT_OUTPUT_PATHS
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
                "LegacyAcctContactExplorerCtrlTest",
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
    ordered_fields = r"\s*,\s*".join(re.escape(field) for field in order_by)
    _require(
        re.search(rf"\bORDER\s+BY\s+{ordered_fields}\b", query, re.I) is not None,
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
        1 <= limit_value <= APEX_MAX_QUERY_ROWS,
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


def _require_controlled_query_failure(
    code_body: str,
    source_body: str,
    query: str,
    *,
    object_name: str,
) -> None:
    query_offset = code_body.find(query)
    _require(query_offset >= 0, f"{object_name} query position")
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
    throws = tuple(
        re.finditer(
            r"\bthrow\s+new\s+AuraHandledException\s*\((?P<argument>.*?)\)\s*;",
            catch_code,
            re.I | re.DOTALL,
        )
    )
    _require(len(throws) == 1, f"{object_name} controlled query failure")
    throw = throws[0]
    _require(
        not throw.group("argument").strip(),
        f"{object_name} fixed safe error argument",
    )
    source_argument = catch_source[throw.start("argument") : throw.end("argument")]
    literal = re.fullmatch(
        r"\s*'(?P<message>(?:\\.|[^'\\\r\n]){1,200})'\s*",
        source_argument,
    )
    _require(literal is not None, f"{object_name} fixed safe error message")
    assert literal is not None
    normalized_message = literal.group("message").casefold()
    _require(
        not any(
            marker in normalized_message
            for marker in (
                "exception",
                "stack trace",
                "select ",
                " from ",
                "accountid",
                "getmessage",
            )
        ),
        f"{object_name} nontechnical error message",
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

    public_methods = (
        (
            "Account",
            "getAccounts",
            r"\(\s*\)",
        ),
        (
            "Contact",
            "getContacts",
            r"\(\s*Id\s+accountId\s*\)",
        ),
    )
    public_interface_is_valid = True
    for return_type, method_name, arguments in public_methods:
        public_interface_is_valid = (
            public_interface_is_valid
            and len(
                re.findall(
                    r"@AuraEnabled\s*\(\s*cacheable\s*=\s*true\s*\)\s*"
                    rf"public\s+static\s+List\s*<\s*{return_type}\s*>\s+"
                    rf"{method_name}\s*{arguments}",
                    code,
                    re.I,
                )
            )
            == 1
        )
    public_interface_is_valid = public_interface_is_valid and (
        len(re.findall(r"@AuraEnabled\b", code, re.I)) == len(public_methods)
    )
    if not public_interface_is_valid:
        raise LocalCheckFailure(
            "local Apex public interface assertion failed",
            diagnostic_ids=(APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,),
        )

    get_accounts_bounds = _apex_method_bounds(
        code,
        return_type="Account",
        method_name="getAccounts",
        arguments=r"\(\s*\)",
    )
    get_contacts_bounds = _apex_method_bounds(
        code,
        return_type="Contact",
        method_name="getContacts",
        arguments=r"\(\s*Id\s+accountId\s*\)",
    )
    get_accounts_body = code[slice(*get_accounts_bounds)]
    get_contacts_body = code[slice(*get_contacts_bounds)]
    get_accounts_source = controller[slice(*get_accounts_bounds)]
    get_contacts_source = controller[slice(*get_contacts_bounds)]
    all_static_queries = _apex_static_queries(code)
    _require(
        all(re.search(r"\bWITH\s+USER_MODE\b", query, re.I) for query in all_static_queries),
        "Apex user-mode queries",
    )
    account_queries = tuple(
        query
        for query in _apex_static_queries(get_accounts_body)
        if re.search(r"\bFROM\s+Account\b", query, re.I)
    )
    contact_queries = tuple(
        query
        for query in _apex_static_queries(get_contacts_body)
        if re.search(r"\bFROM\s+Contact\b", query, re.I)
    )
    _require(
        len(account_queries) == 1 and len(contact_queries) == 1,
        "method-bound Account and Contact static queries",
    )
    integer_constants = _apex_integer_constants(code)
    _require_soql_contract(
        account_queries[0],
        object_name="Account",
        fields=frozenset({"Id", "Name"}),
        order_by=("Name",),
        integer_constants=integer_constants,
    )
    _require_soql_contract(
        contact_queries[0],
        object_name="Contact",
        fields=frozenset({"Id", "FirstName", "LastName", "Email", "Phone"}),
        order_by=("LastName", "FirstName"),
        integer_constants=integer_constants,
    )
    _require(
        re.search(
            r"\bWHERE\s+AccountId\s*=\s*:accountId\b",
            contact_queries[0],
            re.I,
        )
        is not None,
        "selected-account Contact query",
    )
    contact_query_offset = get_contacts_body.find(contact_queries[0])
    _require(contact_query_offset >= 0, "Contact query position")
    before_contact_query = get_contacts_body[:contact_query_offset]
    _require(
        re.search(
            r"\bif\s*\(\s*(?:accountId\s*==\s*null|null\s*==\s*accountId)\s*\)\s*"
            r"\{\s*return\s+new\s+List\s*<\s*Contact\s*>\s*\(\s*\)\s*;\s*\}",
            before_contact_query,
            re.I | re.DOTALL,
        )
        is not None,
        "Contact null-selection guard",
    )
    try:
        _require_controlled_query_failure(
            get_accounts_body,
            get_accounts_source,
            account_queries[0],
            object_name="Account",
        )
        _require_controlled_query_failure(
            get_contacts_body,
            get_contacts_source,
            contact_queries[0],
            object_name="Contact",
        )
    except LocalCheckFailure as exc:
        raise LocalCheckFailure(
            "local Apex controlled-query error assertion failed",
            diagnostic_ids=(APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,),
        ) from exc
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


def _check_controller_test(test_source: str) -> None:
    code, string_literals = _apex_lexical_view(test_source)
    _require(
        re.search(
            r"@IsTest\b(?:\s*\(\s*\))?\s*"
            r"(?:private|public)?\s*class\s+AccountContactExplorerControllerTest\b",
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
            r"\bAccountContactExplorerController\s*\.\s*getAccounts\s*\(\s*\)",
            code,
            re.I,
        )
        is not None,
        "Apex account-result coverage",
    )
    contact_calls = tuple(
        re.finditer(
            r"\bAccountContactExplorerController\s*\.\s*getContacts\s*"
            r"\(\s*(?P<argument>[^()]*)\s*\)",
            code,
            re.I,
        )
    )
    _require(
        len(contact_calls) >= 3,
        "Apex populated, empty, and null Contact coverage",
    )
    _require(
        any(call.group("argument").strip().casefold() == "null" for call in contact_calls),
        "Apex null-selection coverage",
    )
    _require(
        sum(call.group("argument").strip().casefold() != "null" for call in contact_calls) >= 2,
        "Apex selected-account Contact coverage",
    )
    _require(
        len(re.findall(r"\bnew\s+Account\s*\(", code, re.I)) >= 2
        and re.search(r"\bnew\s+Contact\s*\(", code, re.I) is not None
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


def _check_lwc_javascript(source: str) -> None:
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

    allowed_static_imports = frozenset(
        {
            "lwc",
            "@salesforce/apex/AccountContactExplorerController.getAccounts",
            "@salesforce/apex/AccountContactExplorerController.getContacts",
        }
    )
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

    apex_modules = (
        "@salesforce/apex/AccountContactExplorerController.getAccounts",
        "@salesforce/apex/AccountContactExplorerController.getContacts",
    )
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


def _check_lwc_template(template: str) -> None:
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
        ("data-role", LWC_SEMANTIC_DATA_ROLES),
        ("data-state", LWC_SEMANTIC_DATA_STATES),
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


def _check_lwc_test(test_source: str) -> None:
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
            "c/accountContactExplorer",
            "@salesforce/apex/AccountContactExplorerController.getAccounts",
            "@salesforce/apex/AccountContactExplorerController.getContacts",
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
