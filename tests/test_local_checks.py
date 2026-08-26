from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import IO, Any

import pytest

from legacy_migration_agent.core.workspace import IsolatedWorkspace
from legacy_migration_agent.platforms.local_checks import (
    CONTROLLER_PATH,
    CONTROLLER_TEST_PATH,
    LWC_JEST_TOOLCHAIN_DIGESTS,
    SALESFORCE_AGENT_OUTPUT_PATHS,
    SALESFORCE_IMPLEMENTATION_CONTRACT,
    SALESFORCE_JEST_SANDBOX_SAFE_DIAGNOSTIC_IDS,
    SALESFORCE_LWC_JAVASCRIPT_DIAGNOSTIC_IDS,
    SALESFORCE_LWC_JEST_DIAGNOSTIC_IDS,
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
ORACLE = FIXTURE / "expected"
TOOLCHAIN = REPOSITORY / "tooling" / "lwc-jest"


def test_reactive_account_options_check_is_disclosed_in_implementation_contract() -> None:
    contract = "\n".join(SALESFORCE_IMPLEMENTATION_CONTRACT)

    assert "this.accountOptions = [BLANK_ACCOUNT_OPTION, ...data.map(...)]" in contract
    assert "Do not stage the mapped options in an intermediate variable" in contract
    assert "mutate the array with `push`" in contract


def test_has_loaded_reset_check_is_disclosed_in_implementation_contract() -> None:
    contract = "\n".join(SALESFORCE_IMPLEMENTATION_CONTRACT)

    assert "handleAccountChange, use the direct sequence" in contract
    assert "`this.isLoading = true;`, `this.hasLoaded = false;`, `this.contacts = [];`" in contract
    assert "after the valid-selection guard and before awaiting getContacts" in contract


def load_agent_outputs() -> dict[str, bytes]:
    """Stand in for model results loaded into memory before validation."""

    return {path: (ORACLE / path).read_bytes() for path in SALESFORCE_AGENT_OUTPUT_PATHS}


def candidate_from_memory(outputs: dict[str, bytes]) -> IsolatedWorkspace:
    workspace = IsolatedWorkspace(FIXTURE / "input", SALESFORCE_AGENT_OUTPUT_PATHS)
    for path, content in outputs.items():
        workspace.write_bytes(path, content)
    return workspace


def outputs_with_controller_statement(statement: str) -> dict[str, bytes]:
    outputs = load_agent_outputs()
    source = outputs[CONTROLLER_PATH].decode("utf-8")
    insertion_point = "        try {\n            return [\n                SELECT Id, FirstName"
    assert insertion_point in source
    outputs[CONTROLLER_PATH] = source.replace(
        insertion_point,
        f"        {statement}\n\n{insertion_point}",
        1,
    ).encode("utf-8")
    return outputs


def test_candidate_contract_uses_only_in_memory_outputs_and_candidate_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = load_agent_outputs()
    original_open = Path.open

    def reject_oracle_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> IO[Any]:
        try:
            path.resolve(strict=False).relative_to(ORACLE.resolve(strict=True))
        except ValueError:
            return original_open(path, mode, buffering, encoding, errors, newline)
        raise PermissionError("golden tree is unavailable during candidate validation")

    with candidate_from_memory(outputs) as workspace:
        monkeypatch.setattr(Path, "open", reject_oracle_open)

        assert not (workspace.root / "package.json").exists()
        assert not (workspace.root / "package-lock.json").exists()
        assert not (workspace.root / "jest.config.js").exists()
        assert all(
            Path(path).name not in {"package.json", "package-lock.json", "jest.config.js"}
            for path in SALESFORCE_AGENT_OUTPUT_PATHS
        )

        result = check_salesforce_candidate(workspace.root)
        dependency_result, graph = check_dependency_closure(workspace.root)
        changes = workspace.audit_changes()
        revision_result = check_workspace_revision(workspace.root, tree_fingerprint(workspace.root))

        assert result["check"] == "salesforce-candidate-contract"
        assert result["agent_output_files"] == 13
        assert result["apex_tests_executed"] is False
        assert result["lwc_jest_executed"] is False
        assert result["org_validation_performed"] is False
        assert result["deployment_claim"] is False
        assert changes.changed_paths == SALESFORCE_AGENT_OUTPUT_PATHS
        assert dependency_result["unresolved"] is False
        assert graph.has_unresolved is False
        assert revision_result["passed"] is True


def test_candidate_contract_rejects_security_regression() -> None:
    with candidate_from_memory(load_agent_outputs()) as workspace:
        source = (workspace.root / CONTROLLER_PATH).read_text(encoding="utf-8")
        workspace.write_text(CONTROLLER_PATH, source.replace("with sharing", "without sharing"))
        with pytest.raises(LocalCheckFailure):
            check_salesforce_candidate(workspace.root)


def test_candidate_contract_rejects_extra_exposed_apex_query() -> None:
    outputs = load_agent_outputs()
    source = outputs[CONTROLLER_PATH].decode("utf-8")
    body, closing_brace = source.rsplit("}", 1)
    outputs[CONTROLLER_PATH] = (
        body
        + """
    @AuraEnabled(cacheable=true)
    public static List<Contact> getAllContacts() {
        return [SELECT Id FROM Contact LIMIT 1];
    }
"""
        + closing_brace
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)
    assert caught.value.failure_code == "salesforce_apex_controller_contract"


def test_candidate_contract_rejects_unapproved_permission_capability() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/permissionsets/AccountContactExplorerUser.permissionset-meta.xml"
    source = outputs[path].decode("utf-8")
    outputs[path] = source.replace(
        "</PermissionSet>",
        """    <userPermissions>
        <enabled>true</enabled>
        <name>ModifyAllData</name>
    </userPermissions>
</PermissionSet>""",
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)
    assert caught.value.failure_code == "salesforce_permission_set_contract"


def test_candidate_contract_rejects_missing_stale_response_guard() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    stale_guard = (
        "            if (!this.isCurrentRequest(accountId, requestGeneration)) {\n"
        "                return;\n"
        "            }\n"
    )
    assert source.count(stale_guard) == 2
    outputs[path] = source.replace(stale_guard, "", 1).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)
    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_request_generation_increment",)


def test_candidate_contract_rejects_missing_handle_load_generation_increment() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    handle_load_increment = (
        "        const accountId = this.selectedAccountId;\n"
        "        this.loadRequestGeneration += 1;\n"
        "        const requestGeneration = this.loadRequestGeneration;\n"
    )
    assert handle_load_increment in source
    source = source.replace(
        handle_load_increment,
        (
            "        const accountId = this.selectedAccountId;\n"
            "        const requestGeneration = this.loadRequestGeneration;\n"
        ),
        1,
    )
    # Preserve the old global count so this proves method and sequence binding.
    source += "\nconst harmlessGenerationText = 'this.loadRequestGeneration += 1';\n"
    outputs[path] = source.encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_request_generation_increment",)


def test_candidate_contract_accepts_equivalent_current_request_operand_order() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    source = source.replace(
        "accountId === this.selectedAccountId",
        "this.selectedAccountId === accountId",
        1,
    ).replace(
        "requestGeneration === this.loadRequestGeneration",
        "this.loadRequestGeneration === requestGeneration",
        1,
    )
    outputs[path] = source.encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_binds_stale_guard_to_catch_block_not_source_text() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    guard = (
        "            if (!this.isCurrentRequest(accountId, requestGeneration)) {\n"
        "                return;\n"
        "            }\n"
    )
    assert source.count(guard) == 2
    source = source.replace(guard, "", 1)
    source += "\nconst staleGuardDecoy = 'this.isCurrentRequest(accountId, requestGeneration)';\n"
    outputs[path] = source.encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_request_generation_increment",)


def test_candidate_contract_rejects_component_jest_global_tampering_before_runtime() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    outputs[path] = (
        "const trustedIt = globalThis.it;\n"
        "globalThis.it = (title, body, timeout) => trustedIt(title, () => {}, timeout);\n" + source
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_forbidden_runtime_capability",)
    assert "lwc_forbidden_runtime_capability" not in SALESFORCE_JEST_SANDBOX_SAFE_DIAGNOSTIC_IDS


def test_component_capability_check_ignores_harmless_string_text() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    anchor = "const BLANK_ACCOUNT_OPTION = Object.freeze({\n"
    assert anchor in source
    outputs[path] = source.replace(
        anchor,
        "const harmlessText = 'globalThis.it';\n\n" + anchor,
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


@pytest.mark.parametrize(
    "attack",
    (
        "const runDynamic = eval;\nrunDynamic('void 0');\n",
        "const Dynamic = Function;\nDynamic('return 1')();\n",
        ("const loader = require;\nconst matcherApi = loader('@jest/globals');\n"),
        "function inspectArgs() { return arguments; }\n",
        "const reflector = Object;\n",
    ),
)
def test_candidate_contract_rejects_aliased_component_runtime_capabilities(
    attack: str,
) -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    outputs[path] = (attack + outputs[path].decode("utf-8")).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_forbidden_runtime_capability",)


def test_candidate_contract_rejects_computed_constructor_global_bypass() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    attack = (
        "const runtime = [][ 'filter' ][ 'con' + 'structor' ]('return this')();\n"
        "const trustedCall = runtime['i' + 't'];\n"
        "runtime['i' + 't'] = (title, body, timeout) => "
        "trustedCall(title, () => {}, timeout);\n"
    )
    outputs[path] = (attack + source).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_forbidden_runtime_capability",)


def test_candidate_contract_rejects_reflective_function_global_bypass() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    attack = (
        "const callableBase = Object.getPrototypeOf(() => {});\n"
        "const hiddenDescriptor = "
        "Object.getOwnPropertyDescriptor(callableBase, 'constructor');\n"
        "const evaluator = hiddenDescriptor.value;\n"
        "const runtimeObject = evaluator('return this')();\n"
    )
    outputs[path] = (attack + source).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_forbidden_runtime_capability",)


@pytest.mark.parametrize(
    "attack",
    (
        (
            "const { ['con' + 'structor']: evaluator } = Object.freeze(() => {});\n"
            "const runtimeObject = evaluator('return this')();\n"
        ),
        (
            "const evaluator = Object.freeze(() => {})?.['con' + 'structor'];\n"
            "const runtimeObject = evaluator('return this')();\n"
        ),
        (
            "const { 'constructor': evaluator } = Object.freeze(() => {});\n"
            "const runtimeObject = evaluator('return this')();\n"
        ),
    ),
)
def test_candidate_contract_rejects_indirect_constructor_extraction(
    attack: str,
) -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    outputs[path] = (attack + outputs[path].decode("utf-8")).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_forbidden_runtime_capability",)


@pytest.mark.parametrize("browser_global", ("top", "parent", "location", "navigator"))
def test_candidate_contract_rejects_unapproved_browser_globals(
    browser_global: str,
) -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    outputs[path] = (f"const runtimeObject = {browser_global};\n" + source).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_forbidden_runtime_capability",)


def test_candidate_contract_accepts_approved_component_imports_in_another_order() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    accounts_import = (
        "import getAccounts from '@salesforce/apex/AccountContactExplorerController.getAccounts';\n"
    )
    contacts_import = (
        "import getContacts from '@salesforce/apex/AccountContactExplorerController.getContacts';\n"
    )
    assert accounts_import + contacts_import in source
    outputs[path] = source.replace(
        accounts_import + contacts_import,
        contacts_import + accounts_import,
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


@pytest.mark.parametrize(
    "extra_import",
    (
        "import '@jest/globals';\n",
        "import {\n    expect\n} from '@jest/globals';\n",
    ),
)
def test_candidate_contract_rejects_extra_component_static_import_forms(
    extra_import: str,
) -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    outputs[path] = (extra_import + outputs[path].decode("utf-8")).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_forbidden_runtime_capability",)


@pytest.mark.parametrize(
    "extra_export",
    (
        "export { readFileSync as runtimeRead } from 'node:fs';\n",
        "export {\n    readFileSync as runtimeRead\n} from 'node:fs';\n",
        "export * from 'node:fs';\n",
    ),
)
def test_candidate_contract_rejects_component_module_re_exports(
    extra_export: str,
) -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    outputs[path] = (extra_export + outputs[path].decode("utf-8")).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_forbidden_runtime_capability",)


def test_candidate_contract_rejects_account_options_getter_setter_expando() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    direct_field = "    accountOptions = [BLANK_ACCOUNT_OPTION];\n"
    assert direct_field in source
    outputs[path] = source.replace(
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

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_account_options_reactive_field",)


def test_candidate_contract_rejects_later_account_change_state_rewrites() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    reset = (
        "        this.contacts = [];\n"
        "        this.isLoading = false;\n"
        "        this.hasLoaded = false;\n"
    )
    assert reset in source
    outputs[path] = source.replace(
        reset,
        reset
        + "        if (!this.selectedAccountId) {\n"
        + "            this.hasLoaded = true;\n"
        + "            this.isLoading = true;\n"
        + "        }\n",
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_has_loaded_reset",)


def test_candidate_contract_allows_read_only_account_change_state_reference() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    increment = "        this.loadRequestGeneration += 1;\n"
    assert increment in source
    outputs[path] = source.replace(
        increment,
        increment + "        const wasLoading = this.isLoading;\n",
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_rejects_account_options_push_mutation_in_wire_handler() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    direct_assignment = """            this.accountOptions = [
                BLANK_ACCOUNT_OPTION,
                ...data.map((accountRecord) => ({
                    label: accountRecord.Name,
                    value: accountRecord.Id
                }))
            ];
"""
    push_mutation = """            this.accountOptions = [BLANK_ACCOUNT_OPTION];
            this.accountOptions.push(
                ...data.map((accountRecord) => ({
                    label: accountRecord.Name,
                    value: accountRecord.Id
                }))
            );
"""
    assert direct_assignment in source
    outputs[path] = source.replace(direct_assignment, push_mutation, 1).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_account_options_reactive_field",)


def test_candidate_contract_rejects_missing_handle_load_has_loaded_reset() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    reset_anchor = "        this.isLoading = true;\n        this.hasLoaded = false;\n"
    assert reset_anchor in source
    # Keep three total occurrences so an unrelated reset cannot satisfy the
    # method-scoped lifecycle contract that the old count-based check missed.
    source = source.replace(
        reset_anchor,
        "        this.isLoading = true;\n",
        1,
    ).replace(
        "        if (data) {\n",
        "        if (data) {\n            this.hasLoaded = false;\n",
        1,
    )
    assert source.count("hasLoaded = false") == 3
    outputs[path] = source.encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == ("lwc_has_loaded_reset",)


def test_candidate_contract_rejects_has_loaded_reset_inside_blank_guard_only() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    valid_reset = "        this.isLoading = true;\n        this.hasLoaded = false;\n"
    blank_guard = (
        "            this.warningMessage = 'Select an account before loading contacts.';\n"
        "            return;\n"
    )
    assert valid_reset in source and blank_guard in source
    outputs[path] = (
        source.replace(
            valid_reset,
            "        this.isLoading = true;\n",
            1,
        )
        .replace(
            blank_guard,
            (
                "            this.warningMessage = 'Select an account before loading contacts.';\n"
                "            this.hasLoaded = false;\n"
                "            return;\n"
            ),
            1,
        )
        .encode("utf-8")
    )

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == ("lwc_has_loaded_reset",)


def test_candidate_contract_accepts_extra_guard_cleanup_with_valid_path_cleanup() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    blank_guard = (
        "            this.warningMessage = 'Select an account before loading contacts.';\n"
        "            return;\n"
    )
    assert blank_guard in source
    outputs[path] = source.replace(
        blank_guard,
        (
            "            this.warningMessage = 'Select an account before loading contacts.';\n"
            "            this.hasLoaded = false;\n"
            "            this.contacts = [];\n"
            "            return;\n"
        ),
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_accepts_extra_pre_guard_cleanup_with_valid_path_cleanup() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    method_start = "    async handleLoad() {\n"
    assert method_start in source
    outputs[path] = source.replace(
        method_start,
        (
            "    async handleLoad() {\n"
            "        this.hasLoaded = false;\n"
            "        this.contacts = [];\n"
        ),
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_rejects_conditional_valid_path_cleanup() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    direct_cleanup = "        this.hasLoaded = false;\n        this.contacts = [];\n"
    assert direct_cleanup in source
    outputs[path] = source.replace(
        direct_cleanup,
        (
            "        if (this.contacts.length === 0) {\n"
            "            this.hasLoaded = false;\n"
            "            this.contacts = [];\n"
            "        }\n"
        ),
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == ("lwc_has_loaded_reset",)


@pytest.mark.parametrize(
    "conditional_cleanup",
    (
        (
            "        if (this.contacts.length === 0) this.isLoading = true;\n"
            "        if (this.contacts.length === 0) this.hasLoaded = false;\n"
            "        if (this.contacts.length === 0) this.contacts = [];\n"
        ),
        (
            "        if (this.contacts.length === 0)\n"
            "            this.isLoading = true;\n"
            "        if (this.contacts.length === 0)\n"
            "            this.hasLoaded = false;\n"
            "        if (this.contacts.length === 0)\n"
            "            this.contacts = [];\n"
        ),
    ),
)
def test_candidate_contract_rejects_braceless_conditional_valid_path_cleanup(
    conditional_cleanup: str,
) -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    direct_cleanup = (
        "        this.isLoading = true;\n"
        "        this.hasLoaded = false;\n"
        "        this.contacts = [];\n"
    )
    assert direct_cleanup in source
    outputs[path] = source.replace(direct_cleanup, conditional_cleanup, 1).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == ("lwc_has_loaded_reset",)


def test_candidate_contract_rejects_single_braceless_guard_before_load_cleanup() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    direct_cleanup = (
        "        this.isLoading = true;\n"
        "        this.hasLoaded = false;\n"
        "        this.contacts = [];\n"
    )
    assert direct_cleanup in source
    outputs[path] = source.replace(
        direct_cleanup,
        "        if (this.contacts.length === 0)\n" + direct_cleanup,
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == ("lwc_has_loaded_reset",)


def test_candidate_contract_rejects_single_braceless_guard_before_change_cleanup() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    direct_cleanup = (
        "        this.contacts = [];\n"
        "        this.isLoading = false;\n"
        "        this.hasLoaded = false;\n"
    )
    assert direct_cleanup in source
    outputs[path] = source.replace(
        direct_cleanup,
        "        if (this.contacts.length === 0)\n" + direct_cleanup,
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == ("lwc_has_loaded_reset",)


def test_candidate_contract_rejects_missing_pre_request_contact_clear() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    anchor = (
        "        this.isLoading = true;\n"
        "        this.hasLoaded = false;\n"
        "        this.contacts = [];\n"
    )
    assert anchor in source
    outputs[path] = source.replace(
        anchor,
        "        this.isLoading = true;\n        this.hasLoaded = false;\n",
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == ("lwc_has_loaded_reset",)


def test_candidate_contract_aggregates_simultaneous_lwc_javascript_diagnostics() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    direct_field = "    accountOptions = [BLANK_ACCOUNT_OPTION];\n"
    reset_anchor = "        this.isLoading = true;\n        this.hasLoaded = false;\n"
    assert direct_field in source and reset_anchor in source
    outputs[path] = (
        source.replace(
            direct_field,
            "    accountOptions = [];\n",
            1,
        )
        .replace(
            reset_anchor,
            "        this.isLoading = true;\n",
            1,
        )
        .encode("utf-8")
    )

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == (
        "lwc_account_options_reactive_field",
        "lwc_has_loaded_reset",
    )


def test_forbidden_lwc_capability_cannot_be_masked_by_safe_diagnostic() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    reset_anchor = "        this.isLoading = true;\n        this.hasLoaded = false;\n"
    assert reset_anchor in source
    outputs[path] = (
        source.replace(reset_anchor, "        this.isLoading = true;\n", 1)
        + "\neval('forbidden');\n"
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"
    assert caught.value.diagnostic_ids == (
        "lwc_forbidden_runtime_capability",
        "lwc_has_loaded_reset",
    )


def test_candidate_contract_rejects_lwc_javascript_comment_decoy() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source = outputs[path].decode("utf-8")
    outputs[path] = (
        source.replace("extends LightningElement", "extends MissingBase", 1)
        + "\n// extends LightningElement\n"
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)
    assert caught.value.failure_code == "salesforce_lwc_javascript_contract"


def test_candidate_contract_rejects_lwc_template_comment_decoy() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.html"
    source = outputs[path].decode("utf-8")
    outputs[path] = (
        source.replace('role="alert"', 'role="status"') + '\n<!-- role="alert" -->\n'
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)
    assert caught.value.failure_code == "salesforce_lwc_template_contract"


def test_candidate_contract_rejects_native_select_for_detail_value_handler() -> None:
    outputs = load_agent_outputs()
    path = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.html"
    source = outputs[path].decode("utf-8")
    outputs[path] = source.replace(
        "<lightning-combobox",
        '<select data-form="account"',
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)
    assert caught.value.failure_code == "salesforce_lwc_template_contract"


def test_candidate_contract_rejects_lwc_jest_comment_decoy() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    outputs[path] = (
        source.replace("getAccounts.emit(ACCOUNTS)", "getAccounts.fakeEmit(ACCOUNTS)")
        + "\n// getAccounts.emit(ACCOUNTS)\n"
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)
    assert caught.value.failure_code == "salesforce_lwc_jest_contract"


def test_candidate_contract_rejects_missing_exact_stale_response_title() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    outputs[path] = source.replace(
        "ignores a stale response after the selected account changes",
        "resolves the current second request before the stale first request",
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)
    assert caught.value.failure_code == "salesforce_lwc_jest_contract"


def test_candidate_contract_requires_lexical_jest_globals() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    explicit_import = "import { afterEach, describe, expect, it, jest } from '@jest/globals';\n"
    assert explicit_import in outputs[path].decode("utf-8")
    outputs[path] = outputs[path].replace(explicit_import.encode("utf-8"), b"", 1)

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == ("jest_explicit_globals",)


def test_candidate_contract_requires_two_turn_jest_flush_helper() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    helper = (
        "async function flushPromises() {\n"
        "    await Promise.resolve();\n"
        "    await Promise.resolve();\n"
        "}\n"
    )
    assert helper in source
    outputs[path] = source.replace(
        helper,
        "function flushPromises() {\n    return Promise.resolve();\n}\n",
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == ("jest_settled_render_flush",)


def test_candidate_contract_requires_jest_dom_cleanup() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    cleanup = (
        "        while (document.body.firstChild) {\n"
        "            document.body.removeChild(document.body.firstChild);\n"
        "        }\n"
    )
    assert cleanup in source
    outputs[path] = source.replace(cleanup, "", 1).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == ("jest_dom_cleanup",)


def test_candidate_contract_accepts_once_scoped_resolved_value_mocks() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    source = source.replace(
        "getContacts.mockResolvedValue(CONTACTS)",
        "getContacts.mockResolvedValueOnce(CONTACTS)",
        1,
    ).replace(
        "getContacts.mockResolvedValue([])",
        "getContacts.mockResolvedValueOnce([])",
        1,
    )
    outputs[path] = source.encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_accepts_indexed_ordered_call_proof() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    source = re.sub(
        r"expect\(getContacts\)\.toHaveBeenNthCalledWith\(1,\s*\{\s*"
        r"accountId: ACCOUNTS\[0\]\.Id\s*\}\s*\);",
        "expect(getContacts.mock.calls[0][0].accountId).toBe(ACCOUNTS[0].Id);",
        source,
        count=1,
    )
    source = re.sub(
        r"expect\(getContacts\)\.toHaveBeenNthCalledWith\(2,\s*\{\s*"
        r"accountId: ACCOUNTS\[1\]\.Id\s*\}\s*\);",
        "expect(getContacts.mock.calls[1][0].accountId).toBe(ACCOUNTS[1].Id);",
        source,
        count=1,
    )
    assert "toHaveBeenNthCalledWith" not in source
    outputs[path] = source.encode()

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_accepts_argument_array_ordered_call_proof() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    source = re.sub(
        r"expect\(getContacts\)\.toHaveBeenNthCalledWith\(1,\s*\{\s*"
        r"accountId: ACCOUNTS\[0\]\.Id\s*\}\s*\);",
        "expect(getContacts.mock.calls[0]).toEqual([{ accountId: ACCOUNTS[0].Id }]);",
        source,
        count=1,
    )
    source = re.sub(
        r"expect\(getContacts\)\.toHaveBeenNthCalledWith\(2,\s*\{\s*"
        r"accountId: ACCOUNTS\[1\]\.Id\s*\}\s*\);",
        "expect(getContacts.mock.calls[1]).toEqual([{ accountId: ACCOUNTS[1].Id }]);",
        source,
        count=1,
    )
    assert "toHaveBeenNthCalledWith" not in source
    outputs[path] = source.encode()

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_accepts_direct_component_creation_before_wire_emit() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    direct_creation = """const element = createElement('c-account-contact-explorer', {
            is: AccountContactExplorer
        });
        document.body.appendChild(element);"""
    assert source.count("const element = createComponent();") == 10
    source = source.replace("const element = createComponent();", direct_creation)
    outputs[path] = source.encode()

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_rejects_wire_emit_before_component_subscription() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    source = source.replace(
        "        const element = createComponent();\n\n        getAccounts.emit(ACCOUNTS);",
        "        getAccounts.emit(ACCOUNTS);\n\n        const element = createComponent();",
        1,
    )
    outputs[path] = source.encode()

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert "jest_component_before_wire_emit" in caught.value.diagnostic_ids


def test_candidate_contract_requires_imperative_mock_reset_between_tests() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    outputs[path] = source.replace("        getContacts.mockReset();\n", "", 1).encode()

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == ("jest_mock_not_reset",)


def test_candidate_contract_rejects_vacuous_whole_root_stale_assertion() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    outputs[path] = source.replace(
        "expect(datatable.data[0].FirstName).not.toBe('Stale');",
        "expect(element.shadowRoot.textContent).not.toBe('Stale');",
        1,
    ).encode()

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert "jest_stale_assertion_vacuous" in caught.value.diagnostic_ids
    assert "jest_stale_render_proof" in caught.value.diagnostic_ids


def test_candidate_contract_accepts_targeted_stale_text_absence() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    outputs[path] = source.replace(
        "expect(datatable.data[0].FirstName).not.toBe('Stale');",
        "expect(element.shadowRoot.textContent).not.toContain('Stale');",
        1,
    ).encode()

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


@pytest.mark.parametrize("matcher", ("toBe", "toEqual"))
def test_candidate_contract_accepts_spinner_public_property_assertion(matcher: str) -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    assertion = "        expect(spinner.alternativeText).toBe('Loading contacts');\n"
    assert assertion in source
    outputs[path] = source.replace(
        assertion,
        f"        expect(spinner.alternativeText).{matcher}('Loading contacts');\n",
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_rejects_missing_spinner_public_property_assertion() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    assertion = b"        expect(spinner.alternativeText).toBe('Loading contacts');\n"
    assert assertion in outputs[path]
    outputs[path] = outputs[path].replace(assertion, b"", 1)

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == ("jest_spinner_public_property",)


def test_candidate_contract_rejects_inline_spinner_attribute_read_decoy() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    assertion = "        expect(spinner.alternativeText).toBe('Loading contacts');\n"
    inline_attribute_assertion = (
        "        expect(element.shadowRoot.querySelector('lightning-spinner')"
        ".getAttribute('alternative-text')).toBe('Loading contacts');\n"
    )
    assert assertion in source
    outputs[path] = source.replace(assertion, inline_attribute_assertion, 1).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == ("jest_spinner_public_property",)


def test_candidate_contract_accepts_get_accounts_mock_reset() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    anchor = "        getContacts.mockReset();\n"
    assert anchor in source
    outputs[path] = source.replace(
        anchor,
        "        getAccounts.mockReset();\n" + anchor,
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_aggregates_exact_live_lwc_and_jest_diagnostics() -> None:
    outputs = load_agent_outputs()
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

    jest_path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    jest_source = outputs[jest_path].decode("utf-8")
    spinner_anchor = "        expect(spinner.alternativeText).toBe('Loading contacts');\n"
    assert spinner_anchor in jest_source
    jest_source = jest_source.replace(
        spinner_anchor,
        "        expect(spinner.getAttribute('alternative-text')).toBe('Loading contacts');\n",
        1,
    )
    first_call = """        expect(getContacts).toHaveBeenNthCalledWith(1, {
            accountId: ACCOUNTS[0].Id
        });
"""
    second_call = """        expect(getContacts).toHaveBeenNthCalledWith(2, {
            accountId: ACCOUNTS[1].Id
        });
"""
    assert first_call in jest_source and second_call in jest_source
    jest_source = jest_source.replace(
        first_call,
        "        expect(getContacts.mock.calls[0]).toEqual({ accountId: ACCOUNTS[0].Id });\n",
        1,
    ).replace(
        second_call,
        "        expect(getContacts.mock.calls[1]).toEqual({ accountId: ACCOUNTS[1].Id });\n",
        1,
    )
    outputs[jest_path] = jest_source.encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.diagnostic_ids == (
        "lwc_account_options_reactive_field",
        "jest_spinner_public_property",
        "jest_ordered_call_proof",
    )
    assert set(caught.value.diagnostic_ids) <= SALESFORCE_JEST_SANDBOX_SAFE_DIAGNOSTIC_IDS


def test_new_lwc_runtime_diagnostics_are_canonical_and_sandbox_safe() -> None:
    expected_safe = {
        "lwc_account_options_reactive_field",
        "lwc_has_loaded_reset",
        "lwc_request_generation_increment",
        "jest_spinner_public_property",
    }
    assert {
        "lwc_account_options_reactive_field",
        "lwc_forbidden_runtime_capability",
        "lwc_has_loaded_reset",
        "lwc_request_generation_increment",
    } == SALESFORCE_LWC_JAVASCRIPT_DIAGNOSTIC_IDS
    assert expected_safe <= SALESFORCE_LWC_JEST_DIAGNOSTIC_IDS
    assert expected_safe <= SALESFORCE_JEST_SANDBOX_SAFE_DIAGNOSTIC_IDS
    assert "lwc_forbidden_runtime_capability" in SALESFORCE_LWC_JEST_DIAGNOSTIC_IDS
    assert "lwc_forbidden_runtime_capability" not in (SALESFORCE_JEST_SANDBOX_SAFE_DIAGNOSTIC_IDS)


def test_candidate_contract_aggregates_safe_stage_diagnostics() -> None:
    outputs = load_agent_outputs()
    apex_source = outputs[CONTROLLER_TEST_PATH].decode("utf-8")
    outputs[CONTROLLER_TEST_PATH] = apex_source.replace(
        "System.assertEquals(AccountContactExplorerController.MAX_CONTACTS, contacts.size());",
        "System.assert(contacts.size() > 0);",
        1,
    ).encode()
    jest_path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    outputs[jest_path] = outputs[jest_path].replace(
        b"        getContacts.mockReset();\n",
        b"",
        1,
    )

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)

    assert caught.value.failure_code == "salesforce_apex_test_contract"
    assert caught.value.diagnostic_ids == (
        "salesforce_apex_test_contract",
        "jest_mock_not_reset",
    )


def test_candidate_contract_rejects_missing_contact_cap_assertion() -> None:
    outputs = load_agent_outputs()
    source = outputs[CONTROLLER_TEST_PATH].decode("utf-8")
    required_assertion = (
        "System.assertEquals(AccountContactExplorerController.MAX_CONTACTS, contacts.size());"
    )
    assert required_assertion in source
    outputs[CONTROLLER_TEST_PATH] = source.replace(
        required_assertion,
        "System.assert(contacts.size() > 0);",
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)
    assert caught.value.failure_code == "salesforce_apex_test_contract"


@pytest.mark.parametrize(
    "replacement",
    (
        "Assert.isFalse(returnedIds.contains(foreignContact.Id));",
        "System.Assert.isFalse(returnedIds.contains(foreignContact.Id));",
    ),
)
def test_candidate_contract_accepts_valid_assert_is_false_syntax(replacement: str) -> None:
    outputs = load_agent_outputs()
    source = outputs[CONTROLLER_TEST_PATH].decode("utf-8")
    outputs[CONTROLLER_TEST_PATH] = source.replace(
        "System.assert(!returnedIds.contains(foreignContact.Id));",
        replacement,
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        assert check_salesforce_candidate(workspace.root)["passed"] is True


def test_candidate_contract_rejects_nonexistent_system_assert_false_method() -> None:
    outputs = load_agent_outputs()
    source = outputs[CONTROLLER_TEST_PATH].decode("utf-8")
    outputs[CONTROLLER_TEST_PATH] = source.replace(
        "System.assert(!returnedIds.contains(foreignContact.Id));",
        "System.assertFalse(returnedIds.contains(foreignContact.Id));",
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)
    assert caught.value.failure_code == "salesforce_apex_test_contract"


def test_candidate_contract_cli_emits_only_the_allowlisted_stage_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "planted-secret-that-must-not-cross-the-validator-boundary"
    outputs = load_agent_outputs()
    source = outputs[CONTROLLER_TEST_PATH].decode("utf-8")
    required_assertion = (
        "System.assertEquals(AccountContactExplorerController.MAX_CONTACTS, contacts.size());"
    )
    outputs[CONTROLLER_TEST_PATH] = source.replace(
        required_assertion,
        f"System.assert(contacts.size() > 0); // {secret}",
        1,
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
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
    assert str(workspace.root) not in serialized


@pytest.mark.parametrize(
    "incorrect_mock",
    (
        "return { default: createApexTestWireAdapter(jest.fn()) };",
        "return { __esModule: true, default: createApexTestWireAdapter() };",
    ),
)
def test_candidate_contract_rejects_incorrect_virtual_apex_wire_mock(
    incorrect_mock: str,
) -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    source = source.replace(
        "return { __esModule: true, default: createApexTestWireAdapter(jest.fn()) };",
        incorrect_mock,
        1,
    )
    outputs[path] = source.encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure):
            check_salesforce_candidate(workspace.root)


def test_candidate_contract_rejects_combined_or_wrong_apex_mock_modules() -> None:
    outputs = load_agent_outputs()
    path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )
    source = outputs[path].decode("utf-8")
    source = source.replace(
        "@salesforce/apex/AccountContactExplorerController.getAccounts",
        "@salesforce/apex/AccountContactExplorerController",
    ).replace(
        "@salesforce/sfdx-lwc-jest",
        "@salesforce/lwc-jest",
    )
    outputs[path] = source.encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure) as caught:
            check_salesforce_candidate(workspace.root)
    assert caught.value.failure_code == "salesforce_lwc_jest_contract"


@pytest.mark.parametrize(
    "statement",
    (
        "if (accountId != null) { insert new Account(); }",
        "if (accountId != null) { update new Account(); }",
        "if (accountId != null) { upsert new Account(); }",
        "if (accountId != null) { delete new Account(); }",
        "if (accountId != null) { undelete new Account(); }",
        "if (accountId != null) { merge new Account(), new Account(); }",
    ),
)
def test_candidate_contract_rejects_dml_tokens_anywhere(statement: str) -> None:
    with candidate_from_memory(outputs_with_controller_statement(statement)) as workspace:
        with pytest.raises(LocalCheckFailure):
            check_salesforce_candidate(workspace.root)


@pytest.mark.parametrize(
    "operation",
    ("insert", "update", "upsert", "delete", "undelete", "merge"),
)
def test_candidate_contract_rejects_database_dml_methods(operation: str) -> None:
    statement = f"Database.{operation}(new Account());"
    with candidate_from_memory(outputs_with_controller_statement(statement)) as workspace:
        with pytest.raises(LocalCheckFailure):
            check_salesforce_candidate(workspace.root)


@pytest.mark.parametrize(
    "method",
    ("Database.queryWithBinds", "database.QUERYWITHBINDS", "Database.query"),
)
def test_candidate_contract_rejects_dynamic_database_queries(method: str) -> None:
    statement = (
        f"{method}('SELECT Id FROM Account', new Map<String, Object>(), AccessLevel.USER_MODE);"
    )
    with candidate_from_memory(outputs_with_controller_statement(statement)) as workspace:
        with pytest.raises(LocalCheckFailure):
            check_salesforce_candidate(workspace.root)


@pytest.mark.parametrize(
    "declaration",
    (
        "public without sharing class AccountContactExplorerController",
        "public inherited sharing class AccountContactExplorerController",
        "public class AccountContactExplorerController",
    ),
)
def test_candidate_contract_rejects_non_with_sharing_despite_comment_decoy(
    declaration: str,
) -> None:
    outputs = load_agent_outputs()
    source = outputs[CONTROLLER_PATH].decode("utf-8")
    source = source.replace(
        "public with sharing class AccountContactExplorerController",
        declaration,
        1,
    )
    outputs[CONTROLLER_PATH] = (
        "// public with sharing class AccountContactExplorerController\n" + source
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure):
            check_salesforce_candidate(workspace.root)


def test_candidate_contract_rejects_user_mode_markers_only_in_comments() -> None:
    outputs = load_agent_outputs()
    source = outputs[CONTROLLER_PATH].decode("utf-8")
    source = source.replace("WITH USER_MODE", "WITH SYSTEM_MODE")
    outputs[CONTROLLER_PATH] = (
        source + "\n/* decoy markers: WITH USER_MODE WITH USER_MODE */\n"
    ).encode("utf-8")

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure):
            check_salesforce_candidate(workspace.root)


def test_candidate_contract_rejects_aura_markers_only_in_string_literal() -> None:
    outputs = load_agent_outputs()
    source = outputs[CONTROLLER_PATH].decode("utf-8")
    source = source.replace("@AuraEnabled(cacheable=true)", "@AuraEnabled")
    body, closing_brace = source.rsplit("}", 1)
    decoy = (
        "    private static final String DECOY = "
        "'@AuraEnabled(cacheable=true) @AuraEnabled(cacheable=true)';\n"
    )
    outputs[CONTROLLER_PATH] = f"{body}{decoy}{closing_brace}".encode()

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure):
            check_salesforce_candidate(workspace.root)


def test_candidate_contract_ignores_forbidden_tokens_in_comments_and_strings() -> None:
    statement = (
        "String harmless = 'insert update Database.queryWithBinds'; "
        "// delete Database.merge(new Account())"
    )
    with candidate_from_memory(outputs_with_controller_statement(statement)) as workspace:
        result = check_salesforce_candidate(workspace.root)
        assert result["security"] == "with-sharing-user-mode-read-only"


@pytest.mark.parametrize("suffix", ("/* unterminated", "String value = 'unterminated"))
def test_candidate_contract_fails_closed_on_unterminated_apex_lexemes(suffix: str) -> None:
    outputs = load_agent_outputs()
    source = outputs[CONTROLLER_PATH].decode("utf-8")
    outputs[CONTROLLER_PATH] = f"{source}\n{suffix}".encode()

    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure):
            check_salesforce_candidate(workspace.root)


def test_candidate_contract_rejects_xml_entities_with_controlled_failure() -> None:
    outputs = load_agent_outputs()
    outputs["manifest/package.xml"] = (
        b'<?xml version="1.0"?><!DOCTYPE Package [<!ENTITY leak SYSTEM "file:///etc/passwd">]>'
        b'<Package xmlns="http://soap.sforce.com/2006/04/metadata">&leak;</Package>'
    )
    with candidate_from_memory(outputs) as workspace:
        with pytest.raises(LocalCheckFailure, match="local contract assertion failed"):
            check_salesforce_candidate(workspace.root)


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
    with candidate_from_memory(load_agent_outputs()) as workspace:
        before = tree_fingerprint(workspace.root)
        source = (workspace.root / CONTROLLER_PATH).read_text(encoding="utf-8")
        workspace.write_text(CONTROLLER_PATH, f"{source}\n")
        with pytest.raises(LocalCheckFailure):
            check_workspace_revision(workspace.root, before)
