"""Runnable, model-backed Architect, Engineer, and Validator role classes.

The model supplies bounded structured proposals.  Deterministic code owns all
authority: request/manifest bindings, isolated file writes, actual diff
derivation, receipt integrity, and the terminal validation disposition.
"""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Any, Final, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode

from legacy_migration_agent.agent_runtime.agent_definitions import (
    AgentDefinition,
    AgentRegistry,
    AgentRole,
)
from legacy_migration_agent.agent_runtime.correction import (
    CorrectionAction,
    CorrectionAttemptEvidence,
    implementation_failure_ids,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    ModelCallRecord,
    StructuredModelClient,
    model_call_record,
    verify_model_call_record,
)
from legacy_migration_agent.contracts import (
    ApprovalAction,
    ChangeSet,
    CheckStatus,
    ImplementationIntervention,
    MigrationManifest,
    MigrationRequest,
    Platform,
    Sha256Digest,
    StrictModel,
    ValidationDisposition,
    ValidationReport,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import (
    PolicyViolation,
    validate_change_set,
    validate_manifest_for_request,
    validate_report,
)
from legacy_migration_agent.core.redaction import SecretRedactor
from legacy_migration_agent.core.scope_policy import PlatformAdapter
from legacy_migration_agent.core.workspace import IsolatedWorkspace
from legacy_migration_agent.graphs.dependency_graph import DependencyGraph
from legacy_migration_agent.knowledge.wiki import RetrievalTrace
from legacy_migration_agent.platforms.local_checks import (
    SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS,
    SALESFORCE_JEST_SANDBOX_SAFE_DIAGNOSTIC_IDS,
    SALESFORCE_LWC_JAVASCRIPT_DIAGNOSTIC_IDS,
    SALESFORCE_LWC_JEST_DIAGNOSTIC_IDS,
)

MAX_SOURCE_FILE_CHARS = 32_000
MAX_SOURCE_CONTEXT_CHARS = 256_000
MAX_UPDATE_FILE_CHARS = 180_000
MAX_UPDATE_CONTEXT_CHARS = 220_000
MAX_CONTEXT_FILES = 64
_REPAIR_GUIDANCE_BY_SIGNAL: Final[dict[str, str]] = {
    "jest_component_before_wire_emit": (
        "In every test that calls getAccounts.emit(...) or getAccounts.error(...), create and "
        "append the component before invoking the adapter. Do not emit wire values from "
        "component lifecycle hooks."
    ),
    "jest_dom_cleanup": (
        "In afterEach, remove every child from document.body with a while(firstChild) "
        "removeChild(firstChild) loop before resetting mocks so mounted components and wire "
        "subscriptions cannot leak between tests."
    ),
    "jest_exact_behavior_titles": (
        "Declare all ten implementation-contract test titles exactly in direct it(...) or "
        "test(...) calls; do not construct, alias, parameterize, or dynamically concatenate "
        "the required titles."
    ),
    "jest_explicit_load_behavior": (
        "Configure getContacts with resolved contact rows before rendering. Create and append "
        "the component, emit account options through the account wire adapter, select an "
        "account, and flush the microtask queue. Assert getContacts has not been called before "
        "the Load click. Click Load and flush again, assert exactly one getContacts call with "
        "the selected accountId, then query lightning-datatable and assert its data equals the "
        "configured contact rows."
    ),
    "jest_explicit_globals": (
        "Add the exact lexical import `import { afterEach, describe, expect, it, jest } from "
        "'@jest/globals';`. The pinned runner does not inject Jest test globals."
    ),
    "jest_fixture_result_coverage": (
        "Configure the imperative getContacts mock for both a successful resolved CONTACTS "
        "result and a successful resolved empty-array result, and assert their distinct "
        "rendered outcomes."
    ),
    "jest_forbidden_capability": (
        "Remove forbidden Node, network, dynamic-evaluation, and process APIs from the Jest "
        "test. Use only approved fixtures, promises, component DOM interaction, and Jest "
        "primitives."
    ),
    "jest_imperative_mock_contract": (
        "Mock @salesforce/apex/AccountContactExplorerController.getContacts with jest.fn(), "
        "configure resolved results for success cases, and include a mockRejectedValue case "
        "for the safe error path."
    ),
    "jest_loading_behavior": (
        "In the loading-state test, leave a deferred getContacts promise unresolved, click "
        "Load, flush the microtask queue, assert lightning-spinner is present and the Load "
        "button is disabled, then resolve the deferred promise and flush again."
    ),
    "jest_mock_module_contract": (
        "Make each of the two Apex jest.mock factories return an ES-module-shaped object with "
        "__esModule: true, producing exactly two __esModule: true occurrences in the test."
    ),
    "jest_mock_not_reset": (
        "Call getContacts.mockReset() inside beforeEach or afterEach so imperative Apex mock "
        "state cannot leak between behavior tests."
    ),
    "jest_ordered_call_proof": (
        "Prove call order with toHaveBeenNthCalledWith for calls 1 and 2, or inspect "
        "getContacts.mock.calls[0][0].accountId and [1][0].accountId. Each Jest "
        "mock.calls entry is an array of arguments, not the argument object itself."
    ),
    "jest_spinner_public_property": (
        "Query lightning-spinner and assert its public spinner.alternativeText property. "
        "Remove spinner.getAttribute('alternative-text') assertions; Lightning base-component "
        "public properties are the supported Jest contract."
    ),
    "jest_settled_render_flush": (
        "Define `async function flushPromises()` with two consecutive "
        "`await Promise.resolve();` statements. Await that helper after component events and "
        "after every resolved, rejected, or manually settled getContacts promise before reading "
        "the rendered DOM. One microtask turn is insufficient for the imperative promise and "
        "the following LWC rerender."
    ),
    "jest_required_behavior_coverage": (
        "After getContacts resolves to an empty array, flush the microtask queue and assert the "
        "component renders its .empty-state element."
    ),
    "jest_safe_error_redaction": (
        "For a rejected getContacts request, assert the rendered message is exactly "
        "'Contacts could not be loaded.' and assert the rendered output does not contain "
        "'SELECT Id FROM Contact'."
    ),
    "jest_shadow_dom_contract": (
        "Create the component with { is: AccountContactExplorer }, query controls only through "
        "element.shadowRoot.querySelector, and dispatch account selection with "
        "detail: { value: accountId }. Remove element.querySelector usage."
    ),
    "jest_stale_assertion_vacuous": (
        "Remove whole-shadow-root textContent.not.toBe('Stale') assertions; they do not prove "
        "that stale contact data was excluded."
    ),
    "jest_stale_render_proof": (
        "After resolving the newer request and then the older request, assert a specific "
        "rendered contact field or other targeted rendered text does not contain 'Stale'."
    ),
    "jest_stale_resolution_order": (
        "Resolve the second, current request before resolving the first stale request, with "
        "those resolve calls appearing in that literal source order."
    ),
    "jest_stale_scenario_setup": (
        "Create firstRequest and secondRequest, configure getContacts with chained "
        "mockReturnValueOnce(firstRequest.promise) and "
        "mockReturnValueOnce(secondRequest.promise) before the two Load clicks, then resolve "
        "secondRequest before firstRequest."
    ),
    "jest_wire_adapter_factory_argument": (
        "Create the Apex test wire adapter with exactly createApexTestWireAdapter(jest.fn())."
    ),
    "jest_wire_adapter_api": (
        "Use the pinned wire adapter's emit(...) and error(...) APIs; replace unsupported "
        "mockSuccess(...) and mockError(...) calls."
    ),
    "jest_wire_adapter_contract": (
        "Mock @salesforce/apex/AccountContactExplorerController.getAccounts, require "
        "createApexTestWireAdapter from the pinned adapter inside that hoisted mock factory, "
        "and exercise both getAccounts.emit(ACCOUNTS) and getAccounts.error(...)."
    ),
    "lwc_account_options_reactive_field": (
        "Declare accountOptions = [BLANK_ACCOUNT_OPTION]; directly as the reactive class field, "
        "and, in the successful account wire data branch, use the direct array-literal shape "
        "this.accountOptions = [BLANK_ACCOUNT_OPTION, ...data.map(...)]. Do not stage the mapped "
        "options in an intermediate variable or mutate the field with push. Remove any "
        "_accountOptions backing field and any accountOptions getter or setter accessor pair."
    ),
    "lwc_forbidden_runtime_capability": (
        "Keep the component to exactly three static module imports: `lwc` and the two exact Apex "
        "methods. Remove runtime/test-global access, dynamic require/import/evaluation, Node or "
        "browser-global mutation, prototype/metaprogramming hooks, external URLs, and secrets."
    ),
    "lwc_has_loaded_reset": (
        "Keep the direct class field `hasLoaded = false`. In handleAccountChange, use the direct "
        "sequence `this.contacts = [];`, `this.isLoading = false;`, "
        "`this.hasLoaded = false;`. In handleLoad, after the blank-selection guard and before "
        "awaiting getContacts, use the direct sequence `this.isLoading = true;`, "
        "`this.hasLoaded = false;`, `this.contacts = [];` so an "
        "old success or empty state cannot remain visible during a new request. Set hasLoaded "
        "true only after a current successful response."
    ),
    "lwc_request_generation_increment": (
        "Keep exactly one direct request-generation increment at the start of "
        "handleAccountChange. In handleLoad, after the blank-selection guard and before "
        "awaiting getContacts, use the direct sequence "
        "`const accountId = this.selectedAccountId;`, "
        "`this.loadRequestGeneration += 1;`, and "
        "`const requestGeneration = this.loadRequestGeneration;`. Apply "
        "a direct isCurrentRequest(accountId, requestGeneration) guard separately inside the "
        "try success, catch error, and finally loading-reset blocks so neither same-account "
        "overlap nor account changes allow older work to become current."
    ),
    "controller_jest_account_options": (
        "Repair the component implementation, not either Jest suite. Initialize the blank "
        "account option and replace accountOptions from the getAccounts wire result so the "
        "combobox renders the blank choice followed by every returned account."
    ),
    "controller_jest_account_error": (
        "Repair the component implementation, not either Jest suite. On a getAccounts wire "
        "error, render only the fixed safe message 'Accounts could not be loaded.' and do not "
        "expose the supplied error or query text."
    ),
    "controller_jest_selection_gate": (
        "Repair the component implementation, not either Jest suite. Keep Load disabled for a "
        "blank selectedAccountId and enabled after a nonblank account is selected when no load "
        "is pending."
    ),
    "controller_jest_explicit_load": (
        "Repair the component implementation, not either Jest suite. Do not call getContacts "
        "during selection; call it exactly after Load with { accountId }, then render the "
        "returned rows in lightning-datatable."
    ),
    "controller_jest_loading_state": (
        "Repair the component implementation, not either Jest suite. Set isLoading before the "
        "imperative getContacts promise settles, render the accessible spinner, disable Load, "
        "and clear loading only for the current request."
    ),
    "controller_jest_refresh_state": (
        "Repair the component implementation, not either Jest suite. At the start of every "
        "valid Load, reset hasLoaded and clear contacts before awaiting getContacts so a prior "
        "empty or populated state is hidden while the new request is pending."
    ),
    "controller_jest_stale_response": (
        "Repair the component implementation, not either Jest suite. Increment and capture the "
        "request generation as specified, and ignore success, error, and finally work from a "
        "request made stale by a later account selection."
    ),
    "controller_jest_same_account_overlap": (
        "Repair the component implementation, not either Jest suite. Increment the generation "
        "for every valid Load, including repeated loads for the same selected account, so an "
        "older same-account response cannot overwrite the newer response."
    ),
    "controller_jest_stale_error": (
        "Repair the component implementation, not either Jest suite. Guard the catch and finally "
        "paths separately with isCurrentRequest so an older same-account rejection cannot render "
        "an error or clear the spinner for the still-pending current request."
    ),
    "controller_jest_blank_selection": (
        "Repair the component implementation, not either Jest suite. Clearing the selection "
        "must invalidate pending work, clear contacts, stop loading, reset hasLoaded, disable "
        "Load, and render the fixed selection warning."
    ),
    "controller_jest_empty_state": (
        "Repair the component implementation, not either Jest suite. Render the empty state "
        "only after the current getContacts call succeeds with an empty result, and do not "
        "render a datatable for that result."
    ),
    "controller_jest_contacts_error": (
        "Repair the component implementation, not either Jest suite. On a current getContacts "
        "failure, render only the fixed safe message 'Contacts could not be loaded.', hide the "
        "datatable, and do not expose supplied error or query text."
    ),
}

_UNSUPPORTED_REPAIR_GUIDANCE = frozenset(_REPAIR_GUIDANCE_BY_SIGNAL) - (
    SALESFORCE_LWC_JEST_DIAGNOSTIC_IDS | SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS
)
if _UNSUPPORTED_REPAIR_GUIDANCE:  # pragma: no cover - import-time contract invariant
    raise RuntimeError(
        "Engineer repair guidance contains unsupported Jest diagnostics: "
        + ", ".join(sorted(_UNSUPPORTED_REPAIR_GUIDANCE))
    )

_TERMINAL_JEST_ASSERTION_FAILURE = re.compile(
    r"^LWC Jest failed terminally suites=1 tests=(?P<total_tests>[1-9][0-9]{0,3}) "
    r"failed-suites=1 failed-tests=(?P<failed_tests>[1-9][0-9]{0,3}); "
    r"stdout=sha256:[0-9a-f]{64}; stderr=sha256:[0-9a-f]{64}\.$"
)
_CANDIDATE_CONTRACT_FAILURE = re.compile(
    r"^Candidate contract failed; failure-code=(?P<failure_code>[A-Za-z0-9_.:-]+); "
    r"diagnostics=(?P<diagnostics>[A-Za-z0-9_.:-]+(?:,[A-Za-z0-9_.:-]+)*); "
    r"exit=-?[0-9]+; stdout=sha256:[0-9a-f]{64}; stderr=sha256:[0-9a-f]{64}\.$"
)


def _is_terminal_jest_assertion_failure(summary: str) -> bool:
    """Distinguish executed assertion failures from independent suite/runtime failures."""

    match = _TERMINAL_JEST_ASSERTION_FAILURE.fullmatch(summary)
    return bool(match and int(match.group("failed_tests")) <= int(match.group("total_tests")))


def _candidate_failure_supports_jest_correlation(
    summary: str,
    diagnostic_ids: tuple[str, ...],
) -> bool:
    """Accept only exact candidate aggregates known to explain an executed Jest failure."""

    match = _CANDIDATE_CONTRACT_FAILURE.fullmatch(summary)
    if (
        not match
        or not diagnostic_ids
        or tuple(match.group("diagnostics").split(",")) != diagnostic_ids
        or not set(diagnostic_ids).issubset(SALESFORCE_JEST_SANDBOX_SAFE_DIAGNOSTIC_IDS)
    ):
        return False
    failure_code = match.group("failure_code")
    diagnostic_set = set(diagnostic_ids)
    return (
        failure_code == "salesforce_lwc_jest_contract"
        and not diagnostic_set & SALESFORCE_LWC_JAVASCRIPT_DIAGNOSTIC_IDS
    ) or (
        failure_code == "salesforce_lwc_javascript_contract"
        and bool(diagnostic_set & SALESFORCE_LWC_JAVASCRIPT_DIAGNOSTIC_IDS)
    )


ENGINEER_INSTRUCTION = (
    "Return exactly one discriminated result: either complete UTF-8 content for every "
    "manifest-approved output, or a decision-required intervention with zero updates. "
    "Treat manifest.implementation_contract as the exact controller-owned acceptance contract. "
    "Resolve ordinary internal implementation choices from that contract and the frozen source, "
    "and record them as public assumptions; do not request a decision merely because a private "
    "helper name or safely derivable implementation detail was not preselected. Approved target "
    "files are supposed to be new: author the component, service, tests, mocks, and synthetic "
    "fixtures from the accepted contract even when no target scaffold exists. Contract-specified "
    "synthetic values are authorized test data, not fabricated legacy evidence. Do not request a "
    "decision merely because the frozen source lacks the new implementation or its tests. Do not "
    "return a patch, commands, validation claims, or private chain-of-thought. On bounded "
    "attempt two, use only the supplied controller-owned correction context to repair the "
    "listed implementation failure signals while preserving the exact approved paths and "
    "base revision. Environment-unavailable checks are deliberately excluded from the "
    "Engineer correction target and remain controller-owned validation evidence. If "
    "an attempt-two correction only changes already approved paths, that correction is "
    "implementation work, not scope expansion. An expand_scope intervention on attempt two "
    "is valid only when affected_paths identifies a specifically required path outside "
    "manifest.approved_paths; otherwise return the complete corrected file plan. If "
    "correction.requires_complete_file_plan is true, the controller has supplied complete "
    "code-owned repair directives for every repair signal: return a complete corrected file "
    "plan and do not request replanning, scope expansion, or additional toolchain evidence."
)


def _path_is_covered_by_entries(path: str, entry_paths: tuple[str, ...]) -> bool:
    target_parts = tuple(validate_relative_path(path).split("/"))
    for entry in entry_paths:
        entry_parts = tuple(validate_relative_path(entry).split("/"))
        if target_parts[: len(entry_parts)] == entry_parts:
            return True
    return False


class AgentRuntimeError(PolicyViolation):
    """Raised when a model proposal violates a deterministic role boundary."""


class ArchitectConversationMessage(StrictModel):
    """One bounded public turn supplied to the Architect intake mode."""

    role: Literal["user", "architect"]
    content: str = Field(min_length=1, max_length=2_000)

    @field_validator("content")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if "\x00" in value or any(
            ord(character) < 32 and character not in {"\n", "\t"} for character in value
        ):
            raise ValueError("conversation content contains a forbidden control character")
        return value


class ArchitectConversationContext(StrictModel):
    """Controller-owned public history for one non-authorizing intake turn."""

    mode: Literal["conversation_intake"] = "conversation_intake"
    selected_platform: Platform | None = None
    history: tuple[ArchitectConversationMessage, ...] = Field(min_length=1, max_length=24)
    instruction: str = (
        "Respond conversationally using only public conclusions. Ask for missing information "
        "needed to form one bounded migration request. The selected_platform field is the "
        "user/controller-owned slice; never infer, change, or authorize it. Mark the request "
        "ready only when that field is present and the history supports a concrete request. "
        "Do not start a workflow, approve a manifest, claim file changes, expose private "
        "chain-of-thought, or return commands."
    )

    @model_validator(mode="after")
    def validate_public_history(self) -> ArchitectConversationContext:
        if self.history[-1].role != "user":
            raise ValueError("Architect conversation history must end with a user turn")
        expected: Literal["user", "architect"] = "user"
        for turn in self.history:
            if turn.role != expected:
                raise ValueError("Architect conversation roles must alternate from the user")
            expected = "architect" if expected == "user" else "user"
        if sum(len(turn.content) for turn in self.history) > 16_000:
            raise ValueError("Architect conversation history exceeds the public context limit")
        return self


class ArchitectConversationReply(StrictModel):
    """One public Architect reply; it carries no execution or approval authority."""

    status: Literal["clarification_needed", "ready_to_launch"]
    assistant_message: str = Field(min_length=1, max_length=2_000)
    refined_request: str | None = Field(default=None, max_length=1_000)
    missing_information: tuple[str, ...] = Field(default=(), max_length=8)

    @field_validator("assistant_message")
    @classmethod
    def validate_assistant_message(cls, value: str) -> str:
        if "\x00" in value or any(
            ord(character) < 32 and character not in {"\n", "\t"} for character in value
        ):
            raise ValueError("Architect reply contains a forbidden control character")
        return value

    @field_validator("missing_information")
    @classmethod
    def validate_missing_information(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 300 for value in values):
            raise ValueError("missing-information entries must be bounded nonblank text")
        if len(values) != len(set(values)):
            raise ValueError("missing-information entries must be unique")
        return values

    @model_validator(mode="after")
    def validate_readiness_contract(self) -> ArchitectConversationReply:
        if self.status == "ready_to_launch":
            if self.refined_request is None or not 10 <= len(self.refined_request) <= 1_000:
                raise ValueError("ready Architect reply requires a bounded refined request")
            if self.missing_information:
                raise ValueError("ready Architect reply cannot retain missing information")
        else:
            if self.refined_request is not None:
                raise ValueError("clarification reply cannot claim a refined request")
            if not self.missing_information:
                raise ValueError("clarification reply must identify missing information")
        return self


class ArchitectConversationRun(StrictModel):
    reply: ArchitectConversationReply
    model_call: ModelCallRecord


class ArchitectContext(StrictModel):
    """Frozen, digest-bound input supplied to the Architect prompt."""

    request: MigrationRequest
    dependency_graph: DependencyGraph
    dependency_graph_digest: Sha256Digest
    wiki_trace: RetrievalTrace
    wiki_trace_digest: Sha256Digest
    platform_adapter: PlatformAdapter
    instruction: str = (
        "Return public implementation decisions and evidence citations only. For every "
        "transformation, copy input_paths only from "
        "platform_adapter.scope_policy.required_source_input_paths; generated outputs are "
        "never transformation inputs. Across all steps, cover every required source input, "
        "cover every approved output exactly once, and treat transformations as provenance "
        "rather than an executable dependency graph. Do not return private chain-of-thought."
        " Copy approved_paths, validation command IDs, required approvals, and the complete "
        "implementation contract exactly from the scope policy, preserving their order."
    )

    @model_validator(mode="after")
    def validate_frozen_context(self) -> ArchitectContext:
        if self.platform_adapter.platform is not self.request.platform:
            raise ValueError("platform adapter does not match the request")
        if self.request.platform.value != self.dependency_graph.platform:
            raise ValueError("dependency graph platform does not match the request")
        if self.request.base_revision != self.dependency_graph.base_revision:
            raise ValueError("dependency graph is stale for the request")
        if not _path_is_covered_by_entries(
            self.request.target.entry_path,
            self.dependency_graph.entry_paths,
        ):
            raise ValueError("request target entry path is outside the dependency graph entries")
        if self.dependency_graph_digest != artifact_digest(self.dependency_graph):
            raise ValueError("dependency graph digest does not match its content")
        if self.dependency_graph.has_unresolved:
            raise ValueError("Architect context cannot contain an unresolved dependency graph")
        if self.wiki_trace_digest != artifact_digest(self.wiki_trace):
            raise ValueError("Wiki trace digest does not match its content")
        if (
            self.wiki_trace.platform is not None
            and self.wiki_trace.platform is not self.request.platform
        ):
            raise ValueError("Wiki trace platform does not match the request")
        if (
            self.wiki_trace.source_version is not None
            and self.wiki_trace.source_version != self.request.target.source_version
        ):
            raise ValueError("Wiki trace source version does not match the request")
        if (
            self.wiki_trace.target_version is not None
            and self.wiki_trace.target_version != self.request.target.target_version
        ):
            raise ValueError("Wiki trace target version does not match the request")
        if not self.wiki_trace.hits:
            raise ValueError("Architect context requires selected Wiki content")
        return self


class ArchitectManifestProposal(StrictModel):
    """One public manifest proposal; hidden reasoning is intentionally absent."""

    manifest: MigrationManifest
    scope_policy_digest: Sha256Digest
    public_decisions: tuple[str, ...] = Field(min_length=1, max_length=32)
    cited_graph_nodes: tuple[str, ...] = Field(min_length=1, max_length=64)
    cited_wiki_pages: tuple[str, ...] = Field(max_length=16)
    unresolved_questions: tuple[str, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def require_manifest_approval_gate(self) -> ArchitectManifestProposal:
        if self.manifest.required_approvals != (ApprovalAction.APPROVE_MANIFEST,):
            raise ValueError("Architect manifest must require exactly the approve_manifest gate")
        return self

    @classmethod
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = "#/$defs/{model}",
        schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
        mode: JsonSchemaMode = "validation",
        *,
        union_format: Literal["any_of", "primitive_type_array"] = "any_of",
    ) -> dict[str, Any]:
        """Expose the gate to structured decoders, not only post-parse policy.

        ``MigrationManifest`` deliberately remains a broader controller
        contract.  Only this model-authored handoff narrows its embedded schema
        to the exact approval required by both supported capstone slices.
        """

        schema = super().model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            schema_generator=schema_generator,
            mode=mode,
            union_format=union_format,
        )
        manifest_schema = schema["$defs"]["MigrationManifest"]
        required = list(manifest_schema["required"])
        if "required_approvals" not in required:
            required.append("required_approvals")
        manifest_schema["required"] = required
        manifest_schema["properties"]["required_approvals"] = {
            "items": {
                "const": ApprovalAction.APPROVE_MANIFEST.value,
                "type": "string",
            },
            "maxItems": 1,
            "minItems": 1,
            "title": "Required Approvals",
            "type": "array",
        }
        implementation_contract = manifest_schema["properties"]["implementation_contract"]
        implementation_contract.pop("default", None)
        implementation_contract["minItems"] = 1
        if "implementation_contract" not in required:
            required.append("implementation_contract")
        return schema

    @field_validator(
        "public_decisions",
        "cited_graph_nodes",
        "cited_wiki_pages",
        "unresolved_questions",
    )
    @classmethod
    def unique_nonblank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("Architect proposal strings cannot be blank")
        if len(values) != len(set(values)):
            raise ValueError("Architect proposal values must be unique")
        return values


class ArchitectRun(StrictModel):
    proposal: ArchitectManifestProposal
    model_call: ModelCallRecord


class ArchitectAgent:
    """Read-only role that proposes a manifest from frozen graph and Wiki data."""

    def __init__(self, registry: AgentRegistry, model: StructuredModelClient) -> None:
        self.definition = _definition(registry, AgentRole.ARCHITECT)
        self.model = model

    def converse(self, context: ArchitectConversationContext) -> ArchitectConversationRun:
        """Produce one public intake reply without creating migration authority."""

        frozen_context = ArchitectConversationContext.model_validate(
            context.model_dump(mode="python")
        )
        raw = self.model.parse(
            system_prompt=self.definition.system_prompt,
            input_value=frozen_context,
            output_type=ArchitectConversationReply,
        )
        parsed = ArchitectConversationReply.model_validate(raw.model_dump(mode="python"))
        redactor = SecretRedactor()
        reply = ArchitectConversationReply(
            status=parsed.status,
            assistant_message=redactor.redact(parsed.assistant_message).text,
            refined_request=(
                None
                if parsed.refined_request is None
                else redactor.redact(parsed.refined_request).text
            ),
            missing_information=tuple(
                redactor.redact(item).text for item in parsed.missing_information
            ),
        )
        if reply.status == "ready_to_launch" and frozen_context.selected_platform is None:
            raise AgentRuntimeError(
                "Architect cannot mark an intake request ready without a controller-selected platform"
            )
        return ArchitectConversationRun(
            reply=reply,
            model_call=model_call_record(
                self.model,
                agent_version=self.definition.version,
                agent_definition_digest=self.definition.definition_digest,
                system_prompt=self.definition.system_prompt,
                input_value=frozen_context,
                output_value=reply,
            ),
        )

    def verify_conversation_replay(
        self,
        run: ArchitectConversationRun,
        context: ArchitectConversationContext,
    ) -> None:
        """Bind a persisted public reply to its exact history and definition."""

        frozen_context = ArchitectConversationContext.model_validate(
            context.model_dump(mode="python")
        )
        persisted = ArchitectConversationRun.model_validate(run.model_dump(mode="python"))
        if persisted.reply.status == "ready_to_launch" and frozen_context.selected_platform is None:
            raise AgentRuntimeError(
                "Architect cannot mark an intake request ready without a controller-selected platform"
            )
        verify_model_call_record(
            persisted.model_call,
            agent_version=self.definition.version,
            agent_definition_digest=self.definition.definition_digest,
            system_prompt=self.definition.system_prompt,
            input_value=frozen_context,
            output_value=persisted.reply,
        )

    def propose(self, context: ArchitectContext) -> ArchitectRun:
        frozen_context = ArchitectContext.model_validate(context.model_dump(mode="python"))
        raw = self.model.parse(
            system_prompt=self.definition.system_prompt,
            input_value=frozen_context,
            output_type=ArchitectManifestProposal,
        )
        proposal = ArchitectManifestProposal.model_validate(raw.model_dump(mode="python"))
        validate_architect_proposal(proposal, frozen_context)
        return ArchitectRun(
            proposal=proposal,
            model_call=model_call_record(
                self.model,
                agent_version=self.definition.version,
                agent_definition_digest=self.definition.definition_digest,
                system_prompt=self.definition.system_prompt,
                input_value=frozen_context,
                output_value=proposal,
            ),
        )

    def verify_replay(self, run: ArchitectRun, context: ArchitectContext) -> None:
        """Revalidate a persisted proposal against this exact agent definition."""

        frozen_context = ArchitectContext.model_validate(context.model_dump(mode="python"))
        persisted = ArchitectRun.model_validate(run.model_dump(mode="python"))
        validate_architect_proposal(persisted.proposal, frozen_context)
        verify_model_call_record(
            persisted.model_call,
            agent_version=self.definition.version,
            agent_definition_digest=self.definition.definition_digest,
            system_prompt=self.definition.system_prompt,
            input_value=frozen_context,
            output_value=persisted.proposal,
        )


class SourceFileEvidence(StrictModel):
    # Source bytes are evidence.  Unlike descriptive contract strings, leading
    # and trailing whitespace (including the final newline) is significant.
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    path: str
    sha256: Sha256Digest
    content: str = Field(max_length=MAX_SOURCE_FILE_CHARS)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @model_validator(mode="after")
    def validate_digest(self) -> SourceFileEvidence:
        expected = f"sha256:{hashlib.sha256(self.content.encode('utf-8')).hexdigest()}"
        if self.sha256 != expected:
            raise ValueError("source file digest does not match its content")
        return self


class EngineerWorkspaceContext(StrictModel):
    """Bounded textual view of a disposable workspace and approved manifest."""

    request: MigrationRequest
    request_digest: Sha256Digest
    manifest: MigrationManifest
    manifest_digest: Sha256Digest
    workspace_base_revision: str = Field(min_length=7, max_length=160)
    source_files: tuple[SourceFileEvidence, ...] = Field(max_length=MAX_CONTEXT_FILES)
    attempt: int = Field(ge=1, le=2)
    correction: EngineerCorrectionContext | None = None
    agent_version: str = Field(pattern=r"^engineer/v[1-9][0-9]*$", max_length=80)
    agent_definition_digest: Sha256Digest
    input_evidence_digest: Sha256Digest
    instruction: str = ENGINEER_INSTRUCTION

    @model_validator(mode="after")
    def validate_context(self) -> EngineerWorkspaceContext:
        if self.request_digest != artifact_digest(self.request):
            raise ValueError("request digest does not match its content")
        if self.manifest_digest != artifact_digest(self.manifest):
            raise ValueError("manifest digest does not match its content")
        if self.workspace_base_revision != self.request.base_revision:
            raise ValueError("workspace revision does not match the request")
        paths = tuple(item.path for item in self.source_files)
        if len(paths) != len(set(paths)):
            raise ValueError("source file evidence paths must be unique")
        if sum(len(item.content) for item in self.source_files) > MAX_SOURCE_CONTEXT_CHARS:
            raise ValueError("Engineer source context exceeds the character limit")
        if self.attempt == 1 and self.correction is not None:
            raise ValueError("Engineer attempt one cannot contain correction context")
        if self.attempt == 2 and self.correction is None:
            raise ValueError("Engineer attempt two requires correction context")
        expected_input_digest = _engineer_input_evidence_digest(
            request=self.request,
            request_digest=self.request_digest,
            manifest=self.manifest,
            manifest_digest=self.manifest_digest,
            workspace_base_revision=self.workspace_base_revision,
            source_files=self.source_files,
            attempt=self.attempt,
            correction=self.correction,
            agent_version=self.agent_version,
            agent_definition_digest=self.agent_definition_digest,
            instruction=self.instruction,
        )
        if self.input_evidence_digest != expected_input_digest:
            raise ValueError("Engineer input evidence digest does not match its content")
        return self


class EngineerFileUpdate(StrictModel):
    # Generated source must survive validation byte-for-byte.
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    path: str
    content: str = Field(max_length=MAX_UPDATE_FILE_CHARS)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class EngineerFilePlan(StrictModel):
    """Exact complete file contents proposed by the Engineer model."""

    updates: tuple[EngineerFileUpdate, ...] = Field(min_length=1, max_length=MAX_CONTEXT_FILES)
    assumptions: tuple[str, ...] = Field(default=(), max_length=24)

    @model_validator(mode="after")
    def validate_plan(self) -> EngineerFilePlan:
        paths = tuple(update.path for update in self.updates)
        if len(paths) != len(set(paths)):
            raise ValueError("Engineer update paths must be unique")
        if sum(len(update.content) for update in self.updates) > MAX_UPDATE_CONTEXT_CHARS:
            raise ValueError("Engineer file plan exceeds the character limit")
        if any(not assumption.strip() for assumption in self.assumptions):
            raise ValueError("Engineer assumptions cannot be blank")
        return self


class EngineerRepairDirective(StrictModel):
    """Code-owned repair guidance for one public deterministic diagnostic."""

    signal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    instruction: str = Field(min_length=1, max_length=2000)


class EngineerCorrectionContext(StrictModel):
    """Safe model-facing projection of exact controller correction evidence."""

    correction_id: str = Field(min_length=1, max_length=160)
    action: Literal[CorrectionAction.RETRY_IMPLEMENTATION]
    reason: str = Field(min_length=1, max_length=2000)
    implementation_failure_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    repair_signal_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    repair_directives: tuple[EngineerRepairDirective, ...] = Field(max_length=64)
    requires_complete_file_plan: bool
    completed_attempt: Literal[1]
    authorized_attempt: Literal[2]
    manifest_digest: Sha256Digest
    prior_change_set_digest: Sha256Digest
    prior_validation_report_digest: Sha256Digest
    correction_request_digest: Sha256Digest
    correction_evidence_digest: Sha256Digest
    prior_file_plan: EngineerFilePlan
    prior_file_plan_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_correction_context(self) -> EngineerCorrectionContext:
        if self.prior_file_plan_digest != artifact_digest(self.prior_file_plan):
            raise ValueError("prior Engineer file-plan digest does not match its content")
        prior_paths = tuple(update.path for update in self.prior_file_plan.updates)
        if len(prior_paths) != len(set(prior_paths)):
            raise ValueError("prior Engineer file-plan paths must be unique")
        if len(self.repair_signal_ids) != len(set(self.repair_signal_ids)):
            raise ValueError("Engineer repair signal identifiers must be unique")
        directive_ids = tuple(item.signal_id for item in self.repair_directives)
        if len(directive_ids) != len(set(directive_ids)):
            raise ValueError("Engineer repair directives must be unique")
        if not set(directive_ids).issubset(self.repair_signal_ids):
            raise ValueError("Engineer repair directives must bind listed repair signals")
        if self.requires_complete_file_plan and directive_ids != self.repair_signal_ids:
            raise ValueError(
                "a mandatory correction file plan requires guidance for every repair signal"
            )
        return self

    @classmethod
    def freeze(
        cls,
        evidence: CorrectionAttemptEvidence,
        prior_file_plan: EngineerFilePlan,
    ) -> EngineerCorrectionContext:
        request = evidence.correction_request
        fixable_failure_ids = implementation_failure_ids(evidence.prior_validation_report)
        if not fixable_failure_ids:
            raise AgentRuntimeError(
                "Engineer correction requires at least one terminal implementation failure"
            )
        typed_jest_failure_signals = frozenset(
            diagnostic_id
            for result in evidence.prior_validation_report.results
            if (
                result.required
                and result.status is CheckStatus.FAILED
                and result.check_id in fixable_failure_ids
                and result.command_id == "salesforce-candidate-contract"
                and _candidate_failure_supports_jest_correlation(
                    result.summary,
                    result.diagnostic_ids,
                )
            )
            for diagnostic_id in result.diagnostic_ids
            if diagnostic_id in SALESFORCE_LWC_JEST_DIAGNOSTIC_IDS
        )
        repair_signal_ids = tuple(
            dict.fromkeys(
                signal_id
                for result in evidence.prior_validation_report.results
                if (
                    result.required
                    and result.status is CheckStatus.FAILED
                    and result.check_id in fixable_failure_ids
                    and not (
                        result.command_id == "salesforce-lwc-jest"
                        and not result.diagnostic_ids
                        and typed_jest_failure_signals
                        and _is_terminal_jest_assertion_failure(result.summary)
                    )
                )
                for signal_id in (result.diagnostic_ids or (result.check_id,))
            )
        )
        repair_directives = tuple(
            EngineerRepairDirective(
                signal_id=signal_id,
                instruction=_REPAIR_GUIDANCE_BY_SIGNAL[signal_id],
            )
            for signal_id in repair_signal_ids
            if signal_id in _REPAIR_GUIDANCE_BY_SIGNAL
        )
        requires_complete_file_plan = bool(repair_signal_ids) and len(repair_directives) == len(
            repair_signal_ids
        )
        return cls(
            correction_id=request.correction_id,
            action=CorrectionAction.RETRY_IMPLEMENTATION,
            reason=request.reason,
            implementation_failure_ids=fixable_failure_ids,
            repair_signal_ids=repair_signal_ids,
            repair_directives=repair_directives,
            requires_complete_file_plan=requires_complete_file_plan,
            completed_attempt=evidence.completed_attempt,
            authorized_attempt=evidence.authorized_attempt,
            manifest_digest=evidence.manifest_digest,
            prior_change_set_digest=evidence.prior_change_set_digest,
            prior_validation_report_digest=evidence.prior_validation_report_digest,
            correction_request_digest=evidence.correction_request_digest,
            correction_evidence_digest=evidence.evidence_digest,
            prior_file_plan=prior_file_plan,
            prior_file_plan_digest=artifact_digest(prior_file_plan),
        )


# ``EngineerWorkspaceContext`` intentionally references the correction type
# declared after the file-plan contract it contains.
EngineerWorkspaceContext.model_rebuild()


class EngineerFilePlanOutcome(StrictModel):
    kind: Literal["file_plan"]
    file_plan: EngineerFilePlan


class EngineerInterventionOutcome(StrictModel):
    kind: Literal["decision_required"]
    intervention: ImplementationIntervention


class EngineerModelOutcome(StrictModel):
    """Exactly one discriminated structured result from the Engineer model."""

    result: Annotated[
        EngineerFilePlanOutcome | EngineerInterventionOutcome,
        Field(discriminator="kind"),
    ]

    @classmethod
    def for_file_plan(cls, file_plan: EngineerFilePlan) -> EngineerModelOutcome:
        return cls(
            result=EngineerFilePlanOutcome(
                kind="file_plan",
                file_plan=file_plan,
            )
        )


class EngineerRun(StrictModel):
    model_outcome: EngineerModelOutcome
    change_set: ChangeSet | None = None
    workspace_after_revision: str | None = Field(default=None, min_length=7, max_length=160)
    model_call: ModelCallRecord

    @model_validator(mode="after")
    def validate_outcome_state(self) -> EngineerRun:
        if isinstance(self.model_outcome.result, EngineerFilePlanOutcome):
            if self.change_set is None or self.workspace_after_revision is None:
                raise ValueError("Engineer file-plan runs require a derived change set")
        elif self.change_set is not None or self.workspace_after_revision is not None:
            raise ValueError("Engineer intervention runs cannot contain workspace changes")
        return self

    @property
    def file_plan(self) -> EngineerFilePlan | None:
        if isinstance(self.model_outcome.result, EngineerFilePlanOutcome):
            return self.model_outcome.result.file_plan
        return None

    @property
    def intervention(self) -> ImplementationIntervention | None:
        if isinstance(self.model_outcome.result, EngineerInterventionOutcome):
            return self.model_outcome.result.intervention
        return None


class EngineerAgent:
    """Role that applies model output only through an existing isolated workspace."""

    def __init__(self, registry: AgentRegistry, model: StructuredModelClient) -> None:
        self.definition = _definition(registry, AgentRole.ENGINEER)
        self.model = model

    def prepare_context(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        workspace: IsolatedWorkspace,
        *,
        attempt: int = 1,
        correction: EngineerCorrectionContext | None = None,
    ) -> EngineerWorkspaceContext:
        """Build the exact provider input without dispatching a model call."""

        _validate_clean_engineer_workspace(request, manifest, workspace)
        return _engineer_context(
            request,
            manifest,
            workspace,
            self.definition,
            attempt=attempt,
            correction=correction,
        )

    def implement(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        workspace: IsolatedWorkspace,
        *,
        attempt: int = 1,
        correction: EngineerCorrectionContext | None = None,
        prepared_context: EngineerWorkspaceContext | None = None,
    ) -> EngineerRun:
        expected_context = self.prepare_context(
            request,
            manifest,
            workspace,
            attempt=attempt,
            correction=correction,
        )
        if prepared_context is None:
            context = expected_context
        else:
            context = EngineerWorkspaceContext.model_validate(
                prepared_context.model_dump(mode="python")
            )
            if context != expected_context:
                raise AgentRuntimeError(
                    "prepared Engineer context differs from the exact workspace evidence"
                )
        raw = self.model.parse(
            system_prompt=self.definition.system_prompt,
            input_value=context,
            output_type=EngineerModelOutcome,
        )
        outcome = EngineerModelOutcome.model_validate(raw.model_dump(mode="python"))
        if isinstance(outcome.result, EngineerFilePlanOutcome):
            change_set, workspace_after_revision = apply_engineer_file_plan(
                request,
                manifest,
                workspace,
                outcome.result.file_plan,
            )
        else:
            validate_implementation_intervention(
                outcome.result.intervention,
                request,
                manifest,
                context,
                self.definition,
            )
            if workspace.audit_changes().changed_paths:
                raise AgentRuntimeError("Engineer intervention must not modify the workspace")
            workspace.assert_source_unchanged()
            change_set = None
            workspace_after_revision = None
        return EngineerRun(
            model_outcome=outcome,
            change_set=change_set,
            workspace_after_revision=workspace_after_revision,
            model_call=model_call_record(
                self.model,
                agent_version=self.definition.version,
                agent_definition_digest=self.definition.definition_digest,
                system_prompt=self.definition.system_prompt,
                input_value=context,
                output_value=outcome,
            ),
        )

    def verify_replay(
        self,
        run: EngineerRun,
        request: MigrationRequest,
        manifest: MigrationManifest,
        workspace: IsolatedWorkspace,
        *,
        attempt: int = 1,
        correction: EngineerCorrectionContext | None = None,
    ) -> None:
        """Reconstruct and verify the persisted Engineer call without applying it."""

        persisted = EngineerRun.model_validate(run.model_dump(mode="python"))
        _validate_clean_engineer_workspace(request, manifest, workspace)
        context = _engineer_context(
            request,
            manifest,
            workspace,
            self.definition,
            attempt=attempt,
            correction=correction,
        )
        if persisted.intervention is not None:
            validate_implementation_intervention(
                persisted.intervention,
                request,
                manifest,
                context,
                self.definition,
            )
        verify_model_call_record(
            persisted.model_call,
            agent_version=self.definition.version,
            agent_definition_digest=self.definition.definition_digest,
            system_prompt=self.definition.system_prompt,
            input_value=context,
            output_value=persisted.model_outcome,
        )


def apply_engineer_file_plan(
    request: MigrationRequest,
    manifest: MigrationManifest,
    workspace: IsolatedWorkspace,
    file_plan: EngineerFilePlan,
) -> tuple[ChangeSet, str]:
    """Apply a frozen model plan and derive the authoritative filesystem diff.

    This is deliberately separate from model invocation so a durable workflow
    can reconstruct a candidate after a process restart without calling the
    Engineer model a second time.  All normal request, scope, source, symlink,
    and actual-delta checks still execute during replay.
    """

    validate_manifest_for_request(manifest, request)
    plan = EngineerFilePlan.model_validate(file_plan.model_dump(mode="python"))
    if workspace.closed:
        raise AgentRuntimeError("Engineer workspace is closed")
    if workspace.base_revision != request.base_revision:
        raise AgentRuntimeError("Engineer workspace is stale for the request")
    if workspace.approved_paths != frozenset(manifest.approved_paths):
        raise AgentRuntimeError("Engineer workspace scope does not exactly match the manifest")
    if workspace.audit_changes().changed_paths:
        raise AgentRuntimeError("Engineer requires a clean isolated workspace")

    planned_paths = tuple(update.path for update in plan.updates)
    if set(planned_paths) != set(manifest.approved_paths):
        missing = sorted(set(manifest.approved_paths) - set(planned_paths))
        extra = sorted(set(planned_paths) - set(manifest.approved_paths))
        details = []
        if missing:
            details.append("missing approved paths: " + ", ".join(missing))
        if extra:
            details.append("unapproved paths: " + ", ".join(extra))
        raise AgentRuntimeError("Engineer file plan scope mismatch (" + "; ".join(details) + ")")

    try:
        for update in plan.updates:
            workspace.write_text(update.path, update.content)
        audit = workspace.audit_changes()
        if set(audit.changed_paths) != set(planned_paths):
            raise AgentRuntimeError(
                "Engineer actual filesystem delta does not equal the proposed update paths"
            )
        workspace.assert_source_unchanged()
    except BaseException:
        workspace.rollback()
        raise

    change_set = ChangeSet(
        change_set_id=(
            "changes-" + hashlib.sha256(audit.unified_diff.encode("utf-8")).hexdigest()[:24]
        ),
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        base_revision=manifest.base_revision,
        changed_paths=audit.changed_paths,
        unified_diff=audit.unified_diff,
        assumptions=plan.assumptions,
    )
    validate_change_set(change_set, manifest)
    return change_set, audit.after_revision


class ReceiptDigestBinding(StrictModel):
    check_id: str = Field(min_length=1, max_length=160)
    receipt_id: str = Field(min_length=1, max_length=160)
    receipt_digest: Sha256Digest


class ValidationEvidenceBundle(StrictModel):
    """Immutable validation artifacts and explicit receipt digest bindings."""

    change_set: ChangeSet
    change_set_digest: Sha256Digest
    report: ValidationReport
    report_digest: Sha256Digest
    receipt_bindings: tuple[ReceiptDigestBinding, ...]

    @model_validator(mode="after")
    def validate_bindings(self) -> ValidationEvidenceBundle:
        if self.change_set_digest != artifact_digest(self.change_set):
            raise ValueError("change-set digest does not match its content")
        if self.report_digest != artifact_digest(self.report):
            raise ValueError("validation-report digest does not match its content")
        expected = tuple(
            ReceiptDigestBinding(
                check_id=result.check_id,
                receipt_id=result.receipt.receipt_id,
                receipt_digest=artifact_digest(result.receipt),
            )
            for result in self.report.results
            if result.receipt is not None
        )
        if self.receipt_bindings != expected:
            raise ValueError("receipt digest bindings do not match the validation report")
        return self

    @classmethod
    def freeze(
        cls,
        change_set: ChangeSet,
        report: ValidationReport,
    ) -> ValidationEvidenceBundle:
        return cls(
            change_set=change_set,
            change_set_digest=artifact_digest(change_set),
            report=report,
            report_digest=artifact_digest(report),
            receipt_bindings=tuple(
                ReceiptDigestBinding(
                    check_id=result.check_id,
                    receipt_id=result.receipt.receipt_id,
                    receipt_digest=artifact_digest(result.receipt),
                )
                for result in report.results
                if result.receipt is not None
            ),
        )


class ValidatorEvidenceContext(StrictModel):
    """Only frozen evidence is supplied; no command or filesystem capability."""

    manifest: MigrationManifest
    manifest_digest: Sha256Digest
    evidence: ValidationEvidenceBundle
    instruction: str = (
        "Return an advisory evidence assessment. The supplied deterministic "
        "ValidationReport remains authoritative; do not return private chain-of-thought."
    )

    @model_validator(mode="after")
    def validate_context(self) -> ValidatorEvidenceContext:
        if self.manifest_digest != artifact_digest(self.manifest):
            raise ValueError("manifest digest does not match its content")
        return self

    @classmethod
    def freeze(
        cls,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        report: ValidationReport,
    ) -> ValidatorEvidenceContext:
        return cls(
            manifest=manifest,
            manifest_digest=artifact_digest(manifest),
            evidence=ValidationEvidenceBundle.freeze(change_set, report),
        )


class ValidatorAdvisory(StrictModel):
    """Review comments only; no field can alter the deterministic disposition."""

    manifest_digest: Sha256Digest
    change_set_digest: Sha256Digest
    report_digest: Sha256Digest
    assessment: Literal["supports_report", "raises_concern", "escalate"]
    summary: str = Field(min_length=1, max_length=3000)
    concerns: tuple[str, ...] = Field(default=(), max_length=24)
    cited_check_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    cited_receipt_digests: tuple[Sha256Digest, ...] = Field(default=(), max_length=64)
    advisory_only: Literal[True]

    @model_validator(mode="after")
    def validate_advisory(self) -> ValidatorAdvisory:
        for values in (self.concerns, self.cited_check_ids, self.cited_receipt_digests):
            if len(values) != len(set(values)):
                raise ValueError("Validator advisory citations and concerns must be unique")
        if any(not value.strip() for value in self.concerns):
            raise ValueError("Validator concerns cannot be blank")
        return self


class ValidatorAssessment(StrictModel):
    advisory: ValidatorAdvisory
    authoritative_disposition: ValidationDisposition
    all_required_checks_terminal_and_passed: bool
    deterministic_report_controls_disposition: Literal[True] = True
    model_call: ModelCallRecord


class ValidatorAgent:
    """Evidence-only role with no command execution or source mutation method."""

    def __init__(self, registry: AgentRegistry, model: StructuredModelClient) -> None:
        self.definition = _definition(registry, AgentRole.VALIDATOR)
        self.model = model

    def assess(self, context: ValidatorEvidenceContext) -> ValidatorAssessment:
        frozen_context = ValidatorEvidenceContext.model_validate(context.model_dump(mode="python"))
        validate_report(
            frozen_context.evidence.report,
            frozen_context.manifest,
            frozen_context.evidence.change_set,
        )
        raw = self.model.parse(
            system_prompt=self.definition.system_prompt,
            input_value=frozen_context,
            output_type=ValidatorAdvisory,
        )
        advisory = ValidatorAdvisory.model_validate(raw.model_dump(mode="python"))
        _validate_validator_advisory(advisory, frozen_context)
        all_terminal_passed = _all_required_checks_terminal_and_passed(frozen_context)
        return ValidatorAssessment(
            advisory=advisory,
            authoritative_disposition=frozen_context.evidence.report.disposition,
            all_required_checks_terminal_and_passed=all_terminal_passed,
            model_call=model_call_record(
                self.model,
                agent_version=self.definition.version,
                agent_definition_digest=self.definition.definition_digest,
                system_prompt=self.definition.system_prompt,
                input_value=frozen_context,
                output_value=advisory,
            ),
        )

    def verify_replay(
        self,
        assessment: ValidatorAssessment,
        context: ValidatorEvidenceContext,
    ) -> None:
        """Revalidate a persisted advisory against the exact frozen evidence."""

        frozen_context = ValidatorEvidenceContext.model_validate(context.model_dump(mode="python"))
        validate_report(
            frozen_context.evidence.report,
            frozen_context.manifest,
            frozen_context.evidence.change_set,
        )
        persisted = ValidatorAssessment.model_validate(assessment.model_dump(mode="python"))
        _validate_validator_advisory(persisted.advisory, frozen_context)
        if persisted.authoritative_disposition is not frozen_context.evidence.report.disposition:
            raise AgentRuntimeError(
                "Validator assessment does not preserve deterministic disposition"
            )
        expected_terminal_passed = _all_required_checks_terminal_and_passed(frozen_context)
        if persisted.all_required_checks_terminal_and_passed is not expected_terminal_passed:
            raise AgentRuntimeError(
                "Validator assessment does not preserve deterministic check state"
            )
        verify_model_call_record(
            persisted.model_call,
            agent_version=self.definition.version,
            agent_definition_digest=self.definition.definition_digest,
            system_prompt=self.definition.system_prompt,
            input_value=frozen_context,
            output_value=persisted.advisory,
        )


def _definition(registry: AgentRegistry, role: AgentRole) -> AgentDefinition:
    definition = registry.get(role)
    if definition.role is not role:
        raise AgentRuntimeError(f"registry returned the wrong prompt for {role.value}")
    return definition


def validate_architect_proposal(
    proposal: ArchitectManifestProposal,
    context: ArchitectContext,
) -> None:
    manifest = proposal.manifest
    if proposal.scope_policy_digest != context.platform_adapter.scope_policy_digest:
        raise AgentRuntimeError("Architect proposal is bound to the wrong scope policy")
    if manifest.request_id != context.request.request_id:
        raise AgentRuntimeError("Architect manifest belongs to a different request")
    if manifest.platform is not context.request.platform:
        raise AgentRuntimeError("Architect manifest platform does not match the request")
    if manifest.base_revision != context.request.base_revision:
        raise AgentRuntimeError("Architect manifest is stale for the request")
    output_paths = {
        path for transformation in manifest.transformations for path in transformation.output_paths
    }
    if output_paths != set(manifest.approved_paths):
        raise AgentRuntimeError(
            "Architect manifest approved paths must exactly equal transformation outputs"
        )
    graph_nodes = {node.node_id for node in context.dependency_graph.nodes}
    unknown_graph = sorted(set(proposal.cited_graph_nodes) - graph_nodes)
    if unknown_graph:
        raise AgentRuntimeError("Architect cited unknown graph nodes: " + ", ".join(unknown_graph))
    if not proposal.cited_wiki_pages:
        raise AgentRuntimeError("Architect proposals require a Wiki citation")
    wiki_pages = {hit.page_id for hit in context.wiki_trace.hits}
    unknown_wiki = sorted(set(proposal.cited_wiki_pages) - wiki_pages)
    if unknown_wiki:
        raise AgentRuntimeError(
            "Architect cited Wiki pages outside the frozen trace: " + ", ".join(unknown_wiki)
        )
    if proposal.unresolved_questions and manifest.status.value != "decision_required":
        raise AgentRuntimeError(
            "Architect unresolved questions require a decision_required manifest"
        )
    if proposal.unresolved_questions:
        has_unresolved_evidence = any(
            not dependency.resolved for dependency in manifest.dependencies
        )
        has_mandatory_risk = any(risk.requires_human_decision for risk in manifest.risks)
        if not (has_unresolved_evidence or has_mandatory_risk):
            raise AgentRuntimeError(
                "Architect unresolved questions require unresolved evidence or a mandatory risk"
            )
    try:
        context.platform_adapter.validate_manifest(manifest, context.request)
    except PolicyViolation as exc:
        raise AgentRuntimeError(str(exc)) from exc
    # Planned manifests must pass the normal implementation policy now.  A
    # decision-required proposal is still a valid Architect outcome but cannot
    # enter the Engineer method, whose policy rejects it.
    if manifest.status.value == "planned":
        validate_manifest_for_request(manifest, context.request)


def _engineer_context(
    request: MigrationRequest,
    manifest: MigrationManifest,
    workspace: IsolatedWorkspace,
    definition: AgentDefinition,
    *,
    attempt: int = 1,
    correction: EngineerCorrectionContext | None = None,
) -> EngineerWorkspaceContext:
    if attempt not in (1, 2):
        raise AgentRuntimeError("Engineer supports only bounded attempts 1 and 2")
    if attempt == 1 and correction is not None:
        raise AgentRuntimeError("Engineer attempt one cannot receive correction context")
    if attempt == 2 and correction is None:
        raise AgentRuntimeError("Engineer attempt two requires correction context")
    requested_source_paths = tuple(
        dict.fromkeys(
            path
            for transformation in manifest.transformations
            for path in transformation.input_paths
        )
    )
    snapshot = workspace.base_snapshot.by_path()
    missing = tuple(path for path in requested_source_paths if path not in snapshot)
    if missing:
        raise AgentRuntimeError(
            "Engineer manifest inputs are missing from the frozen workspace: " + ", ".join(missing)
        )
    evidence_paths = tuple(
        dict.fromkeys(
            (
                *requested_source_paths,
                *(path for path in manifest.approved_paths if path in snapshot),
            )
        )
    )
    if len(evidence_paths) > MAX_CONTEXT_FILES:
        raise AgentRuntimeError("Engineer source context contains too many files")
    files: list[SourceFileEvidence] = []
    for path in evidence_paths:
        entry = snapshot[path]
        try:
            content = entry.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AgentRuntimeError(f"Engineer source file is not UTF-8: {path}") from exc
        if "\x00" in content:
            raise AgentRuntimeError(f"Engineer source file is binary: {path}")
        if len(content) > MAX_SOURCE_FILE_CHARS:
            raise AgentRuntimeError(f"Engineer source file exceeds the prompt bound: {path}")
        files.append(
            SourceFileEvidence(
                path=path,
                sha256=f"sha256:{hashlib.sha256(entry.content).hexdigest()}",
                content=content,
            )
        )
    request_digest = artifact_digest(request)
    manifest_digest = artifact_digest(manifest)
    source_files = tuple(files)
    input_evidence_digest = _engineer_input_evidence_digest(
        request=request,
        request_digest=request_digest,
        manifest=manifest,
        manifest_digest=manifest_digest,
        workspace_base_revision=workspace.base_revision,
        source_files=source_files,
        attempt=attempt,
        correction=correction,
        agent_version=definition.version,
        agent_definition_digest=definition.definition_digest,
        instruction=ENGINEER_INSTRUCTION,
    )
    return EngineerWorkspaceContext(
        request=request,
        request_digest=request_digest,
        manifest=manifest,
        manifest_digest=manifest_digest,
        workspace_base_revision=workspace.base_revision,
        source_files=source_files,
        attempt=attempt,
        correction=correction,
        agent_version=definition.version,
        agent_definition_digest=definition.definition_digest,
        input_evidence_digest=input_evidence_digest,
    )


def _engineer_input_evidence_digest(
    *,
    request: MigrationRequest,
    request_digest: Sha256Digest,
    manifest: MigrationManifest,
    manifest_digest: Sha256Digest,
    workspace_base_revision: str,
    source_files: tuple[SourceFileEvidence, ...],
    attempt: int,
    correction: EngineerCorrectionContext | None,
    agent_version: str,
    agent_definition_digest: Sha256Digest,
    instruction: str,
) -> Sha256Digest:
    """Bind every immutable Engineer input without recursively hashing itself."""

    return artifact_digest(
        {
            "request": request.model_dump(mode="json"),
            "request_digest": request_digest,
            "manifest": manifest.model_dump(mode="json"),
            "manifest_digest": manifest_digest,
            "workspace_base_revision": workspace_base_revision,
            "source_files": tuple(item.model_dump(mode="json") for item in source_files),
            "attempt": attempt,
            "correction": (correction.model_dump(mode="json") if correction is not None else None),
            "agent_version": agent_version,
            "agent_definition_digest": agent_definition_digest,
            "instruction": instruction,
        }
    )


def validate_implementation_intervention(
    intervention: ImplementationIntervention,
    request: MigrationRequest,
    manifest: MigrationManifest,
    context: EngineerWorkspaceContext,
    definition: AgentDefinition,
) -> None:
    """Bind an Engineer stop to exact input evidence and deny new authority."""

    if context.correction is not None and context.correction.requires_complete_file_plan:
        raise AgentRuntimeError(
            "controller-classified correction requires a complete Engineer file plan"
        )

    if intervention.request_id != request.request_id:
        raise AgentRuntimeError("Engineer intervention belongs to another request")
    if intervention.request_digest != artifact_digest(request):
        raise AgentRuntimeError("Engineer intervention request digest does not match")
    if intervention.manifest_id != manifest.manifest_id:
        raise AgentRuntimeError("Engineer intervention belongs to another manifest")
    if intervention.manifest_digest != artifact_digest(manifest):
        raise AgentRuntimeError("Engineer intervention manifest digest does not match")
    if intervention.base_revision != request.base_revision:
        raise AgentRuntimeError("Engineer intervention is stale for the requested revision")
    if intervention.agent_version != definition.version:
        raise AgentRuntimeError("Engineer intervention agent version does not match")
    if intervention.agent_definition_digest != definition.definition_digest:
        raise AgentRuntimeError("Engineer intervention agent definition does not match")
    if intervention.input_evidence_digest != context.input_evidence_digest:
        raise AgentRuntimeError("Engineer intervention input evidence digest does not match")

    known_paths = {
        request.target.entry_path,
        *manifest.approved_paths,
        *(dependency.path for dependency in manifest.dependencies),
        *(
            path
            for transformation in manifest.transformations
            for path in (*transformation.input_paths, *transformation.output_paths)
        ),
        *(item.path for item in context.source_files),
    }
    unknown_paths = sorted(set(intervention.affected_paths) - known_paths)
    if unknown_paths:
        raise AgentRuntimeError(
            "Engineer intervention cites paths outside the frozen input: "
            + ", ".join(unknown_paths)
        )
    if (
        context.correction is not None
        and intervention.requested_action is ApprovalAction.EXPAND_SCOPE
        and all(
            _path_is_covered_by_entries(path, manifest.approved_paths)
            for path in intervention.affected_paths
        )
    ):
        raise AgentRuntimeError(
            "attempt-two scope expansion must identify a specifically required path "
            "outside manifest.approved_paths"
        )

    known_evidence = {
        "request": context.request_digest,
        "manifest": context.manifest_digest,
        "engineer_input": context.input_evidence_digest,
        "agent_definition": context.agent_definition_digest,
        **{f"source:{item.path}": item.sha256 for item in context.source_files},
    }
    for item in intervention.evidence:
        if known_evidence.get(item.source) != item.source_digest:
            raise AgentRuntimeError(
                "Engineer intervention evidence is outside the frozen input: " + item.source
            )
    if not any(
        item.source == "engineer_input" and item.source_digest == context.input_evidence_digest
        for item in intervention.evidence
    ):
        raise AgentRuntimeError(
            "Engineer intervention must cite the complete frozen Engineer input"
        )


def _validate_clean_engineer_workspace(
    request: MigrationRequest,
    manifest: MigrationManifest,
    workspace: IsolatedWorkspace,
) -> None:
    validate_manifest_for_request(manifest, request)
    if workspace.closed:
        raise AgentRuntimeError("Engineer workspace is closed")
    if workspace.base_revision != request.base_revision:
        raise AgentRuntimeError("Engineer workspace is stale for the request")
    if workspace.approved_paths != frozenset(manifest.approved_paths):
        raise AgentRuntimeError("Engineer workspace scope does not exactly match the manifest")
    if workspace.audit_changes().changed_paths:
        raise AgentRuntimeError("Engineer requires a clean isolated workspace")


def _all_required_checks_terminal_and_passed(
    context: ValidatorEvidenceContext,
) -> bool:
    required = tuple(result for result in context.evidence.report.results if result.required)
    return bool(required) and all(
        result.status is CheckStatus.PASSED
        and result.receipt is not None
        and result.receipt.terminal
        and result.receipt.exit_code == 0
        for result in required
    )


def _validate_validator_advisory(
    advisory: ValidatorAdvisory,
    context: ValidatorEvidenceContext,
) -> None:
    evidence = context.evidence
    if advisory.manifest_digest != context.manifest_digest:
        raise AgentRuntimeError("Validator advisory is bound to the wrong manifest")
    if advisory.change_set_digest != evidence.change_set_digest:
        raise AgentRuntimeError("Validator advisory is bound to the wrong change set")
    if advisory.report_digest != evidence.report_digest:
        raise AgentRuntimeError("Validator advisory is bound to the wrong validation report")
    known_checks = {result.check_id for result in evidence.report.results}
    unknown_checks = sorted(set(advisory.cited_check_ids) - known_checks)
    if unknown_checks:
        raise AgentRuntimeError("Validator cited unknown checks: " + ", ".join(unknown_checks))
    known_receipts = {binding.receipt_digest for binding in evidence.receipt_bindings}
    unknown_receipts = sorted(set(advisory.cited_receipt_digests) - known_receipts)
    if unknown_receipts:
        raise AgentRuntimeError("Validator cited a receipt outside the frozen report")
