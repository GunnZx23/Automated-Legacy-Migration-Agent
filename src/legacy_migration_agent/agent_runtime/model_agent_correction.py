"""Engineer file-plan and targeted-correction contracts.

This module owns the model-facing correction projection, deterministic repair
signal mapping, and Wiki query contract. Applying a plan remains in the
model-agents facade so its existing workspace and observability seams stay
stable.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from legacy_migration_agent.agent_runtime.correction import (
    CorrectionAction,
    CorrectionAttemptEvidence,
    implementation_failure_ids,
    validate_correction_attempt_evidence,
)
from legacy_migration_agent.agent_runtime.model_agent_common import (
    MAX_CONTEXT_FILES,
    MAX_UPDATE_CONTEXT_CHARS,
    MAX_UPDATE_FILE_CHARS,
    AgentRuntimeError,
)
from legacy_migration_agent.contracts import (
    CheckStatus,
    MigrationManifest,
    MigrationRequest,
    Platform,
    Sha256Digest,
    StrictModel,
    ValidationReport,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.redaction import assert_no_high_confidence_secrets
from legacy_migration_agent.knowledge.wiki import (
    RetrievalTrace,
    contains_exact_diagnostic_id,
)
from legacy_migration_agent.platforms.local_checks import (
    APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,
    APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,
    CONTROLLER_METADATA_PATH,
    CONTROLLER_PATH,
    CONTROLLER_TEST_METADATA_PATH,
    CONTROLLER_TEST_PATH,
    JEST_GLOBALS_IMPORT_ORDER_DIAGNOSTIC_ID,
    JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID,
    LWC_CSS_PATH,
    LWC_HTML_PATH,
    LWC_JAVASCRIPT_PATH,
    LWC_METADATA_PATH,
    LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID,
    LWC_TEST_PATH,
    MANIFEST_PATH,
    PERMISSION_SET_PATH,
    SALESFORCE_CANDIDATE_FAILURE_CODES,
    SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID,
    SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS,
    SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID,
)

# Candidate code and candidate-authored tests are intentionally not repaired
# against a source-shaped recipe.  Static failures identify the violated
# outcome/safety stage, executed candidate tests identify a real test failure,
# and the immutable controller suite supplies behavior-specific signals.  Keep
# only those three classes of directive active.
_CONTROLLER_BEHAVIOR_REPAIR_GUIDANCE: Final[dict[str, str]] = {
    "controller_jest_account_options": (
        "Repair the component implementation, not either Jest suite. The accessible account "
        "selection control must include a blank choice and every account returned by the wire "
        "adapter. Control type, choice order, internal state, and mapping helpers remain "
        "implementation choices."
    ),
    "controller_jest_account_error": (
        "Repair the component implementation, not either Jest suite. On a getAccounts wire "
        "error, render a nonempty accessible safe alert and do not expose the supplied error, "
        "query text, or other technical details. Exact safe wording is candidate-owned."
    ),
    "controller_jest_selection_gate": (
        "Repair the component implementation, not either Jest suite. Keep Load disabled for a "
        "blank selection and enabled after a nonblank account is selected. Whether a candidate "
        "also disables it while work is pending is an internal UX choice. Do not depend on any "
        "particular selection-field name."
    ),
    "controller_jest_explicit_load": (
        "Repair the component implementation, not either Jest suite. Do not call getContacts "
        "during selection; call it exactly after the explicit load action with { accountId }, "
        "then render the returned rows in an accessible results presentation chosen by the "
        "candidate."
    ),
    "controller_jest_loading_state": (
        "Repair the component implementation, not either Jest suite. While the current contacts "
        "request is pending, render an accessible loading or busy state and remove that state "
        "only when the current request settles. Whether Load remains enabled is candidate-owned."
    ),
    "controller_jest_stale_response": (
        "Repair the component implementation, not either Jest suite. Ignore every state update "
        "from a request made stale by a later account selection, including success, failure, and "
        "loading completion. The stale-request mechanism is an implementation choice."
    ),
    "controller_jest_blank_selection": (
        "Repair the component implementation, not either Jest suite. Clearing the selection "
        "must invalidate pending work, hide results and loading, disable Load, and render the "
        "user safe selection guidance. Exact wording, internal fields, and reset order are "
        "implementation choices."
    ),
    "controller_jest_empty_state": (
        "Repair the component implementation, not either Jest suite. Render an accessible empty "
        "state only after the current getContacts call succeeds with an empty result, and render "
        "no contact rows for that result. Markup and wording are candidate-owned."
    ),
    "controller_jest_contacts_error": (
        "Repair the component implementation, not either Jest suite. On a current getContacts "
        "failure, render a nonempty accessible safe alert, hide contact results, and do not "
        "expose supplied error, query text, or other technical details. Exact safe wording and "
        "results markup are candidate-owned."
    ),
}
_REPAIR_GUIDANCE_BY_SIGNAL: Final[dict[str, str]] = {
    **{
        signal_id: (
            "Repair the model-authored candidate against the approved outcome, public-interface, "
            "metadata, and safety contract for this failed validation stage. Choose any valid "
            "internal implementation and candidate-test structure; do not copy a reference "
            "candidate or target a source-text shape. Return only files that actually change in "
            "the correction delta."
        )
        for signal_id in SALESFORCE_CANDIDATE_FAILURE_CODES
        if signal_id
        not in {
            "salesforce_candidate_inventory",
            "salesforce_candidate_unclassified",
        }
    },
    APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID: (
        "Change only the approved Apex controller class. Preserve its public interface and put "
        "the exact @AuraEnabled(cacheable=true) annotation on each public read method; do not "
        "supply a golden implementation or change unrelated files."
    ),
    APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID: (
        "Change only the approved Apex controller class. Both generated query methods must "
        "translate query failures to AuraHandledException with fixed safe, nontechnical "
        "user-facing messages; do not pass through an exception message, exception object, SOQL, "
        "stack trace, or other technical detail. Exact safe wording and internal helper or catch "
        "layout remain candidate-owned."
    ),
    JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID: (
        "Change only the approved candidate-authored LWC Jest file. Remove unapproved module "
        "targets and use the exact virtual Apex module specifiers already imported by the LWC "
        "implementation when declaring Jest mocks."
    ),
    JEST_GLOBALS_IMPORT_ORDER_DIAGNOSTIC_ID: (
        "Change only the approved candidate-authored LWC Jest file. Move the named import of "
        "every used Jest API from @jest/globals to the first static import, before lwc, the "
        "component, and Apex imports. The pinned transform hoists virtual mock factories, so jest "
        "must be initialized before component loading can invoke those factories. Preserve the "
        "remaining test behavior and imports."
    ),
    LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID: (
        "Change only the approved LWC HTML and, when a computed value is needed, its JavaScript "
        "controller. Replace template operators or expressions with a simple property binding "
        "and compute the value in a JavaScript getter."
    ),
    "lwc_forbidden_runtime_capability": (
        "Remove unapproved runtime capabilities from the component while preserving its public "
        "behavior. Use only the approved static Salesforce modules; do not access network, Node, "
        "process, dynamic-evaluation, host-global mutation, external URL, credential, or secret "
        "capabilities. Internal state and helper design remain implementation choices."
    ),
    "jest_forbidden_capability": (
        "Remove network, filesystem, process, child-process, dynamic-evaluation, external endpoint, "
        "credential, and secret capabilities from the candidate-authored Jest tests. Repair the "
        "tests with supported Jest/LWC mocks and public component interaction without weakening "
        "the intended behavior assertions."
    ),
    SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID: (
        "Repair only the generated candidate-authored LWC Jest file using the listed bounded "
        "failure summaries; the independent controller suite owns component-behavior correction. "
        "Keep meaningful assertions and make every test execute without failures, skips, or todos. "
        "Reset queued mock implementations before installing per-test defaults, configure deferred "
        "or one-shot mock results before the action that invokes them, and settle overlapping calls "
        "in the intended order. Assert Salesforce base-component stubs through their supported "
        "public properties (for example, lightning-datatable.data), not their internal rendered "
        "text. Statically import createElement from lwc and the used Jest APIs, retain the virtual "
        "Apex mock contract with __esModule: true and { virtual: true }, and await bounded microtask "
        "plus LWC rerender turns. Query component-rendered template elements through "
        "element.shadowRoot.querySelector or element.shadowRoot.querySelectorAll after rendering; "
        "host element.querySelector calls inspect light DOM and do not cross the LWC shadow "
        "boundary. Do not edit or imitate the "
        "controller-owned suite."
    ),
    SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID: (
        "Zero immutable controller-owned assertions ran, so no component behavior was proven. "
        "Repair only the generated LWC JavaScript, HTML, and CSS so the bundle can load, render, "
        "and execute, then re-evaluate the entire approved component behavior contract: account "
        "loading, explicit contact loading, loading/empty/safe-error states, and stale-response "
        "handling, including selection-change invalidation and safe account-load and "
        "clear-selection errors. Keep the component controller as plain JavaScript without "
        "TypeScript access modifiers, consume getAccounts through a supported wire adapter or "
        "imperative call, retain the datatable key-field value in every rendered row, and expose no "
        "unapproved @api state. These are observable outcomes, not a prescribed internal "
        "implementation. Do not edit either the candidate-authored Jest test or the controller-owned "
        "suite."
    ),
    **_CONTROLLER_BEHAVIOR_REPAIR_GUIDANCE,
}

_UNSUPPORTED_REPAIR_GUIDANCE = frozenset(_REPAIR_GUIDANCE_BY_SIGNAL) - (
    SALESFORCE_CANDIDATE_FAILURE_CODES
    | SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS
    | {
        "lwc_forbidden_runtime_capability",
        "jest_forbidden_capability",
        APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID,
        APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID,
        JEST_GLOBALS_IMPORT_ORDER_DIAGNOSTIC_ID,
        JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID,
        LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID,
        SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID,
    }
)
if _UNSUPPORTED_REPAIR_GUIDANCE:  # pragma: no cover - import-time contract invariant
    raise RuntimeError(
        "Engineer repair guidance contains unsupported diagnostics: "
        + ", ".join(sorted(_UNSUPPORTED_REPAIR_GUIDANCE))
    )

_SALESFORCE_STAGE_CORRECTION_PATHS: Final[dict[str, tuple[str, ...]]] = {
    "salesforce_manifest_contract": (MANIFEST_PATH,),
    "salesforce_apex_controller_metadata_contract": (CONTROLLER_METADATA_PATH,),
    "salesforce_apex_test_metadata_contract": (CONTROLLER_TEST_METADATA_PATH,),
    "salesforce_apex_controller_contract": (CONTROLLER_PATH,),
    "salesforce_apex_test_contract": (CONTROLLER_TEST_PATH,),
    "salesforce_lwc_javascript_contract": (LWC_JAVASCRIPT_PATH,),
    "salesforce_lwc_template_contract": (LWC_HTML_PATH, LWC_JAVASCRIPT_PATH),
    "salesforce_lwc_styles_contract": (LWC_CSS_PATH,),
    "salesforce_lwc_metadata_contract": (LWC_METADATA_PATH,),
    "salesforce_lwc_jest_contract": (LWC_TEST_PATH,),
    "salesforce_permission_set_contract": (PERMISSION_SET_PATH,),
    "lwc_forbidden_runtime_capability": (LWC_JAVASCRIPT_PATH,),
    "jest_forbidden_capability": (LWC_TEST_PATH,),
    SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID: (LWC_TEST_PATH,),
    APEX_PUBLIC_INTERFACE_ANNOTATION_DIAGNOSTIC_ID: (CONTROLLER_PATH,),
    APEX_CONTROLLED_QUERY_ERROR_MISSING_DIAGNOSTIC_ID: (CONTROLLER_PATH,),
    JEST_GLOBALS_IMPORT_ORDER_DIAGNOSTIC_ID: (LWC_TEST_PATH,),
    JEST_UNAPPROVED_MODULE_TARGET_DIAGNOSTIC_ID: (LWC_TEST_PATH,),
    LWC_TEMPLATE_BINDING_INVALID_DIAGNOSTIC_ID: (LWC_HTML_PATH, LWC_JAVASCRIPT_PATH),
    SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID: (
        LWC_JAVASCRIPT_PATH,
        LWC_HTML_PATH,
        LWC_CSS_PATH,
    ),
    **{
        signal_id: (LWC_JAVASCRIPT_PATH, LWC_HTML_PATH)
        for signal_id in SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS
        if signal_id != SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID
    },
}

ENGINEER_INSTRUCTION = (
    "Return exactly one typed result: complete UTF-8 content for every manifest-approved output "
    "on attempt one, a nonempty changed-file-only delta on attempt two, or a zero-update "
    "decision-required intervention only for a genuine authority or evidence gap. Treat the "
    "manifest, frozen source, and architect_wiki_trace as evidence; the implementation contract "
    "defines outcomes and safety, not reference source text. Approved target files are expected "
    "to be new, so derive bounded internal choices and generated tests without requesting a "
    "decision. Never return patches, commands, validation claims, or private chain-of-thought. "
    "For attempt two, preserve the exact scope and prior plan, follow every controller-owned "
    "repair directive using correction.correction_wiki_trace, and return complete content only "
    "for approved files that actually change."
)


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
    allowed_paths: tuple[str, ...] = Field(min_length=1, max_length=MAX_CONTEXT_FILES)
    instruction: str = Field(min_length=1, max_length=2000)

    @field_validator("allowed_paths")
    @classmethod
    def validate_allowed_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        paths = tuple(validate_relative_path(value) for value in values)
        if len(paths) != len(set(paths)):
            raise ValueError("Engineer repair directive paths must be unique")
        return paths


class EngineerCorrectionContext(StrictModel):
    """Safe model-facing projection of exact controller correction evidence."""

    correction_id: str = Field(min_length=1, max_length=160)
    action: Literal[CorrectionAction.RETRY_IMPLEMENTATION]
    platform: Platform
    reason: str = Field(min_length=1, max_length=2000)
    implementation_failure_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    implementation_failure_summaries: tuple[
        Annotated[str, Field(min_length=1, max_length=2200)], ...
    ] = Field(min_length=1, max_length=64)
    repair_signal_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    repair_directives: tuple[EngineerRepairDirective, ...] = Field(max_length=64)
    allowed_correction_paths: tuple[str, ...] = Field(min_length=1, max_length=MAX_CONTEXT_FILES)
    requires_correction_delta: bool
    completed_attempt: Literal[1]
    authorized_attempt: Literal[2]
    manifest_digest: Sha256Digest
    prior_change_set_digest: Sha256Digest
    prior_validation_report_digest: Sha256Digest
    correction_request_digest: Sha256Digest
    correction_evidence_digest: Sha256Digest
    prior_file_plan: EngineerFilePlan
    prior_file_plan_digest: Sha256Digest
    prior_candidate_revision: str = Field(min_length=7, max_length=160)
    correction_wiki_trace: RetrievalTrace
    correction_wiki_trace_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_correction_context(self) -> EngineerCorrectionContext:
        if self.prior_file_plan_digest != artifact_digest(self.prior_file_plan):
            raise ValueError("prior Engineer file-plan digest does not match its content")
        prior_paths = tuple(update.path for update in self.prior_file_plan.updates)
        if len(prior_paths) != len(set(prior_paths)):
            raise ValueError("prior Engineer file-plan paths must be unique")
        if len(self.allowed_correction_paths) != len(set(self.allowed_correction_paths)):
            raise ValueError("allowed correction paths must be unique")
        if not set(self.allowed_correction_paths).issubset(prior_paths):
            raise ValueError("allowed correction paths must be part of the prior file plan")
        if len(self.repair_signal_ids) != len(set(self.repair_signal_ids)):
            raise ValueError("Engineer repair signal identifiers must be unique")
        if len(self.implementation_failure_summaries) != len(
            set(self.implementation_failure_summaries)
        ):
            raise ValueError("Engineer implementation failure summaries must be unique")
        try:
            assert_no_high_confidence_secrets(
                self.implementation_failure_summaries,
                boundary="Engineer implementation failure summaries",
            )
        except PolicyViolation as exc:
            raise ValueError(str(exc)) from None
        expected_repair_signal_ids = _expected_repair_signal_ids(
            self.implementation_failure_ids,
            self.platform,
        )
        if self.repair_signal_ids != expected_repair_signal_ids:
            raise ValueError(
                "Engineer repair signal identifiers differ from the classified failures"
            )
        directive_ids = tuple(item.signal_id for item in self.repair_directives)
        if len(directive_ids) != len(set(directive_ids)):
            raise ValueError("Engineer repair directives must be unique")
        if not set(directive_ids).issubset(self.repair_signal_ids):
            raise ValueError("Engineer repair directives must bind listed repair signals")
        if directive_ids != self.repair_signal_ids:
            raise ValueError("Engineer correction requires guidance for every repair signal")
        repair_specs = _repair_signal_specs(self.platform)
        expected_allowed_paths = _allowed_correction_paths(
            self.prior_file_plan,
            self.repair_signal_ids,
            repair_specs,
        )
        if self.allowed_correction_paths != expected_allowed_paths:
            raise ValueError(
                "Engineer allowed correction paths differ from the exact code-owned mapping"
            )
        expected_directives = _expected_repair_directives(
            self.prior_file_plan,
            self.repair_signal_ids,
            repair_specs,
        )
        if self.repair_directives != expected_directives:
            raise ValueError("Engineer repair directives differ from the exact code-owned mapping")
        if not self.requires_correction_delta:
            raise ValueError("Engineer correction must require a changed-file delta")
        if self.correction_wiki_trace_digest != artifact_digest(self.correction_wiki_trace):
            raise ValueError("correction Wiki trace digest does not match its content")
        if not self.correction_wiki_trace.hits:
            raise ValueError("correction Wiki trace must contain relevant evidence")
        if self.correction_wiki_trace.platform is not self.platform:
            raise ValueError("correction Wiki trace platform does not match correction evidence")
        try:
            _require_wiki_signal_coverage(
                self.correction_wiki_trace,
                self.repair_signal_ids,
            )
        except AgentRuntimeError as exc:
            raise ValueError(str(exc)) from None
        return self

    @classmethod
    def repair_signals(
        cls,
        evidence: CorrectionAttemptEvidence,
    ) -> tuple[str, ...]:
        return _expected_repair_signal_ids(
            implementation_failure_ids(evidence.prior_validation_report),
            evidence.manifest.platform,
        )

    @classmethod
    def freeze(
        cls,
        evidence: CorrectionAttemptEvidence,
        prior_file_plan: EngineerFilePlan,
        *,
        prior_candidate_revision: str,
        correction_wiki_trace: RetrievalTrace,
    ) -> EngineerCorrectionContext:
        request = evidence.correction_request
        fixable_failure_ids = implementation_failure_ids(evidence.prior_validation_report)
        if not fixable_failure_ids:
            raise AgentRuntimeError(
                "Engineer correction requires at least one terminal implementation failure"
            )
        repair_signal_ids = cls.require_repair_contract(evidence, prior_file_plan)
        expected_repair_signal_ids = _expected_repair_signal_ids(
            fixable_failure_ids,
            evidence.manifest.platform,
        )
        if repair_signal_ids != expected_repair_signal_ids:
            raise AgentRuntimeError(
                "Engineer classified failures cannot reproduce the exact report repair signals"
            )
        repair_specs = _repair_signal_specs(evidence.manifest.platform)
        allowed_correction_paths = _allowed_correction_paths(
            prior_file_plan,
            repair_signal_ids,
            repair_specs,
        )
        repair_directives = _expected_repair_directives(
            prior_file_plan,
            repair_signal_ids,
            repair_specs,
        )
        failure_summaries = _implementation_failure_summaries(evidence.prior_validation_report)
        cls.require_wiki_signal_coverage(correction_wiki_trace, repair_signal_ids)
        return cls(
            correction_id=request.correction_id,
            action=CorrectionAction.RETRY_IMPLEMENTATION,
            platform=evidence.manifest.platform,
            reason=request.reason,
            implementation_failure_ids=fixable_failure_ids,
            implementation_failure_summaries=failure_summaries,
            repair_signal_ids=repair_signal_ids,
            repair_directives=repair_directives,
            allowed_correction_paths=allowed_correction_paths,
            requires_correction_delta=True,
            completed_attempt=evidence.completed_attempt,
            authorized_attempt=evidence.authorized_attempt,
            manifest_digest=evidence.manifest_digest,
            prior_change_set_digest=evidence.prior_change_set_digest,
            prior_validation_report_digest=evidence.prior_validation_report_digest,
            correction_request_digest=evidence.correction_request_digest,
            correction_evidence_digest=evidence.evidence_digest,
            prior_file_plan=prior_file_plan,
            prior_file_plan_digest=artifact_digest(prior_file_plan),
            prior_candidate_revision=prior_candidate_revision,
            correction_wiki_trace=correction_wiki_trace,
            correction_wiki_trace_digest=artifact_digest(correction_wiki_trace),
        )

    @classmethod
    def require_repair_contract(
        cls,
        evidence: CorrectionAttemptEvidence,
        prior_file_plan: EngineerFilePlan,
    ) -> tuple[str, ...]:
        """Require complete semantic and exact-path coverage before retry dispatch."""

        repair_signal_ids = cls.repair_signals(evidence)
        if not repair_signal_ids:
            raise AgentRuntimeError(
                "Engineer correction requires at least one exact implementation repair signal"
            )
        repair_specs = _repair_signal_specs(evidence.manifest.platform)
        unsupported = tuple(
            signal_id for signal_id in repair_signal_ids if signal_id not in repair_specs
        )
        if unsupported:
            raise AgentRuntimeError(
                "Engineer correction has no code-owned repair contract for signals: "
                + ", ".join(unsupported)
            )
        _allowed_correction_paths(
            prior_file_plan,
            repair_signal_ids,
            repair_specs,
        )
        return repair_signal_ids

    @staticmethod
    def require_wiki_signal_coverage(
        trace: RetrievalTrace,
        repair_signal_ids: tuple[str, ...],
    ) -> None:
        """Require retrieved excerpts to mention every exact repair signal."""

        _require_wiki_signal_coverage(trace, repair_signal_ids)


class EngineerCorrectionAuthority(StrictModel):
    """Controller-only binding between attempt-one evidence and model context.

    ``EngineerCorrectionContext`` is deliberately model-facing and therefore
    cannot prove its own relationship to the failed validation report.  This
    wrapper retains the complete controller evidence and requires every public
    attempt-two boundary to reconstruct the one canonical model projection.
    Human approval identity and comments are absent from both artifacts.
    """

    evidence: CorrectionAttemptEvidence
    model_context: EngineerCorrectionContext

    @classmethod
    def freeze(
        cls,
        evidence: CorrectionAttemptEvidence,
        prior_file_plan: EngineerFilePlan,
        *,
        prior_candidate_revision: str,
        correction_wiki_trace: RetrievalTrace,
    ) -> EngineerCorrectionAuthority:
        """Freeze exact evidence together with its canonical model projection."""

        frozen_evidence = _revalidate_correction_attempt_evidence(evidence)
        frozen_plan = EngineerFilePlan.model_validate(prior_file_plan.model_dump(mode="python"))
        frozen_trace = RetrievalTrace.model_validate(
            correction_wiki_trace.model_dump(mode="python")
        )
        context = EngineerCorrectionContext.freeze(
            frozen_evidence,
            frozen_plan,
            prior_candidate_revision=prior_candidate_revision,
            correction_wiki_trace=frozen_trace,
        )
        return cls(evidence=frozen_evidence, model_context=context)

    def require_canonical_context(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
    ) -> EngineerCorrectionContext:
        """Deeply revalidate and reproduce the exact model-facing context."""

        frozen_evidence = _revalidate_correction_attempt_evidence(self.evidence)
        try:
            frozen_evidence = validate_correction_attempt_evidence(
                frozen_evidence,
                request,
                manifest,
            )
            supplied = EngineerCorrectionContext.model_validate(
                self.model_context.model_dump(mode="python")
            )
            expected_repair_signals = EngineerCorrectionContext.require_repair_contract(
                frozen_evidence,
                supplied.prior_file_plan,
            )
            trace = supplied.correction_wiki_trace
            if trace.query != correction_wiki_query(
                request.platform,
                expected_repair_signals,
            ):
                raise AgentRuntimeError(
                    "Engineer correction Wiki query differs from exact report signals"
                )
            if (
                trace.platform is not request.platform
                or trace.source_version != request.target.source_version
                or trace.target_version != request.target.target_version
            ):
                raise AgentRuntimeError(
                    "Engineer correction Wiki trace differs from request version scope"
                )
            expected = EngineerCorrectionContext.freeze(
                frozen_evidence,
                supplied.prior_file_plan,
                prior_candidate_revision=supplied.prior_candidate_revision,
                correction_wiki_trace=supplied.correction_wiki_trace,
            )
        except (PolicyViolation, TypeError, ValueError, AgentRuntimeError) as exc:
            raise AgentRuntimeError(
                "Engineer correction authority is invalid or incomplete"
            ) from exc
        if supplied != expected:
            raise AgentRuntimeError(
                "Engineer correction context differs from exact attempt-one evidence"
            )
        return expected


def _revalidate_correction_attempt_evidence(
    evidence: CorrectionAttemptEvidence,
) -> CorrectionAttemptEvidence:
    """Force nested evidence validators even for unchecked Pydantic copies."""

    try:
        return CorrectionAttemptEvidence.model_validate(evidence.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise AgentRuntimeError("Engineer correction evidence is invalid") from exc


def _require_engineer_correction_authority(
    authority: EngineerCorrectionAuthority,
    request: MigrationRequest,
    manifest: MigrationManifest,
) -> EngineerCorrectionAuthority:
    """Rebuild an authority from bytes and replace its projection canonically."""

    try:
        frozen = EngineerCorrectionAuthority.model_validate(authority.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise AgentRuntimeError("Engineer correction authority is invalid") from exc
    canonical_context = frozen.require_canonical_context(request, manifest)
    return EngineerCorrectionAuthority(
        evidence=frozen.evidence,
        model_context=canonical_context,
    )


def _allowed_correction_paths(
    prior_file_plan: EngineerFilePlan,
    repair_signal_ids: tuple[str, ...],
    repair_specs: dict[str, tuple[tuple[str, ...], str]],
) -> tuple[str, ...]:
    """Derive a code-owned repair boundary from exact deterministic signals."""

    prior_paths = tuple(update.path for update in prior_file_plan.updates)
    selected: set[str] = set()
    for signal_id in repair_signal_ids:
        try:
            mapped = repair_specs[signal_id][0]
        except KeyError as exc:  # pragma: no cover - freeze checks before this helper
            raise AgentRuntimeError(
                f"Engineer correction signal is not mapped: {signal_id}"
            ) from exc
        if not mapped:
            raise AgentRuntimeError(
                f"Engineer correction signal has no exact candidate paths: {signal_id}"
            )
        approved_for_signal = tuple(path for path in mapped if path in prior_paths)
        if not approved_for_signal:
            raise AgentRuntimeError(
                "Engineer correction signal does not bind an approved prior candidate path: "
                + signal_id
            )
        selected.update(approved_for_signal)
    allowed = tuple(path for path in prior_paths if path in selected)
    if not allowed:
        raise AgentRuntimeError(
            "Engineer correction signals do not authorize changes to any approved path"
        )
    return allowed


def _implementation_failure_summaries(report: ValidationReport) -> tuple[str, ...]:
    """Project only bounded public summaries for failed Engineer-owned checks."""

    fixable = set(implementation_failure_ids(report))
    summaries = tuple(
        f"check={result.check_id}; {result.summary}"
        for result in report.results
        if (result.required and result.status is CheckStatus.FAILED and result.check_id in fixable)
    )
    if not summaries:
        raise AgentRuntimeError(
            "Engineer correction requires bounded implementation failure summaries"
        )
    return summaries


def _repair_signal_specs(
    platform: Platform,
) -> dict[str, tuple[tuple[str, ...], str]]:
    """Return the code-owned semantic and path contract for one platform."""

    if platform is Platform.SALESFORCE:
        return {
            signal_id: (_SALESFORCE_STAGE_CORRECTION_PATHS.get(signal_id, ()), instruction)
            for signal_id, instruction in _REPAIR_GUIDANCE_BY_SIGNAL.items()
        }
    if platform is Platform.MULESOFT:
        # Imported lazily to avoid the platform-runtime/model-agent import cycle.
        from legacy_migration_agent.platforms.mulesoft_runtime import (  # noqa: PLC0415
            MULESOFT_REPAIR_SIGNALS,
        )

        return {
            signal_id: (spec.allowed_paths, spec.instruction)
            for signal_id, spec in MULESOFT_REPAIR_SIGNALS.items()
        }
    raise AgentRuntimeError(f"unsupported correction platform: {platform.value}")


def _expected_repair_signal_ids(
    implementation_failure_ids: tuple[str, ...],
    platform: Platform,
) -> tuple[str, ...]:
    """Recover report-ordered repair signals from classified failure identifiers."""

    repair_specs = _repair_signal_specs(platform)
    controller_signals = frozenset(implementation_failure_ids) & (
        SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS if platform is Platform.SALESFORCE else frozenset()
    )
    normalized: list[str] = []
    for failure_id in implementation_failure_ids:
        if platform is Platform.SALESFORCE and failure_id == "salesforce-lwc-controller-jest":
            if not controller_signals:
                normalized.append(SALESFORCE_CONTROLLER_LWC_EXECUTION_FAILURE_DIAGNOSTIC_ID)
        elif (
            platform is Platform.SALESFORCE
            and failure_id == SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID
            and controller_signals
        ):
            # Candidate-authored tests are supporting evidence when the independent
            # controller suite identifies the same behavior defect. Rerun them after
            # the component repair; do not force a valid test file to change.
            continue
        elif failure_id in repair_specs:
            normalized.append(failure_id)
    return tuple(dict.fromkeys(normalized))


def _expected_repair_directives(
    prior_file_plan: EngineerFilePlan,
    repair_signal_ids: tuple[str, ...],
    repair_specs: dict[str, tuple[tuple[str, ...], str]],
) -> tuple[EngineerRepairDirective, ...]:
    """Bind each signal to its exact approved paths and code-owned guidance."""

    prior_paths = {update.path for update in prior_file_plan.updates}
    return tuple(
        EngineerRepairDirective(
            signal_id=signal_id,
            allowed_paths=tuple(path for path in repair_specs[signal_id][0] if path in prior_paths),
            instruction=repair_specs[signal_id][1],
        )
        for signal_id in repair_signal_ids
    )


def _require_wiki_signal_coverage(
    trace: RetrievalTrace,
    repair_signal_ids: tuple[str, ...],
) -> None:
    """Fail closed unless retrieved excerpts explicitly cover every repair signal."""

    selected_content = "\n".join(hit.selected_content for hit in trace.hits)
    missing = tuple(
        signal_id
        for signal_id in repair_signal_ids
        if not contains_exact_diagnostic_id(selected_content, signal_id)
    )
    if missing:
        raise AgentRuntimeError(
            "targeted correction Wiki evidence does not cover signals: " + ", ".join(missing)
        )


def correction_wiki_query(
    platform: Platform,
    repair_signal_ids: tuple[str, ...],
) -> str:
    """Return the one code-owned targeted correction query."""

    normalized = tuple(sorted(dict.fromkeys(repair_signal_ids)))
    if not normalized:
        raise AgentRuntimeError("correction Wiki query requires repair signals")
    return " ".join((*normalized, platform.value, "correction", "validation"))
