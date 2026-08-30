"""Durable LangGraph integration for the three model-backed role agents.

The role implementations remain narrow and provider-neutral.  This adapter
connects them to :class:`MigrationWorkflow`, persists their typed handoffs in
an immutable :class:`ArtifactStore`, and reconstructs the Engineer candidate
from the stored file plan before deterministic validation.  A process restart
therefore does not require a second model call or retain a mutable workspace.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import Field, model_validator

from legacy_migration_agent.agent_runtime.agent_definitions import AgentRegistry
from legacy_migration_agent.agent_runtime.correction import (
    CorrectionAttemptEvidence,
    validate_correction_attempt_evidence,
)
from legacy_migration_agent.agent_runtime.model_agents import (
    AgentRuntimeError,
    ArchitectAgent,
    ArchitectContext,
    ArchitectRun,
    EngineerAgent,
    EngineerCorrectionAuthority,
    EngineerCorrectionContext,
    EngineerRun,
    EngineerWorkspaceContext,
    ValidatorAgent,
    ValidatorAssessment,
    ValidatorEvidenceContext,
    apply_engineer_file_plan,
    correction_wiki_query,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    ModelCallRecord,
    ModelEvidenceError,
    StructuredModelClient,
)
from legacy_migration_agent.agent_runtime.run_artifact_paths import RunArtifactPaths
from legacy_migration_agent.contracts import (
    ChangeSet,
    Identifier,
    ImplementationIntervention,
    MigrationManifest,
    MigrationRequest,
    PlanningIntervention,
    Sha256Digest,
    StrictModel,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import ArtifactStore, artifact_digest
from legacy_migration_agent.core.observability import lifecycle_event
from legacy_migration_agent.core.policies import (
    PolicyViolation,
    validate_change_set,
    validate_manifest_for_request,
    validate_report,
)
from legacy_migration_agent.core.run_session import PortableEvidencePolicyViolation
from legacy_migration_agent.core.workspace import IsolatedWorkspace
from legacy_migration_agent.knowledge.wiki import RetrievalTrace
from legacy_migration_agent.workflow import (
    MigrationWorkflow,
    build_workflow,
    validate_planning_intervention_for_request,
)


class ArchitectContextFactory(Protocol):
    """Build a frozen model context or a typed non-model preflight stop."""

    def __call__(
        self,
        request: MigrationRequest,
    ) -> ArchitectContext | PlanningIntervention: ...


class WorkspaceFactory(Protocol):
    """Create a fresh exact-manifest disposable workspace for one attempt."""

    def __call__(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        attempt: int,
    ) -> IsolatedWorkspace: ...


class DeterministicValidator(Protocol):
    """Run allowlisted checks; this callback, never the model, sets disposition."""

    def __call__(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        workspace: IsolatedWorkspace,
        attempt: int,
    ) -> ValidationReport: ...


class CorrectionWikiRetriever(Protocol):
    """Retrieve version-bound Wiki evidence for one deterministic repair query."""

    def __call__(
        self,
        request: MigrationRequest,
        query: str,
    ) -> RetrievalTrace: ...


class ModelWorkflowIntegrationError(PolicyViolation):
    """Raised when a persisted role handoff cannot be safely replayed."""


class RoleInvocationBinding(StrictModel):
    """Non-secret identity of one exact model-provider dispatch."""

    role: Literal["engineer", "validator"]
    request_id: Identifier
    request_digest: Sha256Digest
    attempt: Literal[1, 2]
    input_evidence_digest: Sha256Digest
    agent_version: str = Field(min_length=1, max_length=80)
    agent_definition_digest: Sha256Digest
    manifest_digest: Sha256Digest
    correction_evidence_digest: Sha256Digest | None = None
    correction_wiki_trace_digest: Sha256Digest | None = None
    change_set_digest: Sha256Digest | None = None
    report_digest: Sha256Digest | None = None

    @model_validator(mode="after")
    def validate_role_bindings(self) -> RoleInvocationBinding:
        if self.role == "engineer":
            if (self.change_set_digest is None) is not (self.report_digest is None):
                raise ValueError(
                    "Engineer dispatch must bind both prior change-set and report digests"
                )
            if self.attempt == 1 and any(
                value is not None
                for value in (
                    self.correction_evidence_digest,
                    self.correction_wiki_trace_digest,
                    self.change_set_digest,
                    self.report_digest,
                )
            ):
                raise ValueError("Engineer attempt one cannot contain correction bindings")
            if self.attempt == 2 and any(
                value is None
                for value in (
                    self.correction_evidence_digest,
                    self.correction_wiki_trace_digest,
                    self.change_set_digest,
                    self.report_digest,
                )
            ):
                raise ValueError("Engineer attempt two requires complete correction bindings")
        else:
            if self.change_set_digest is None or self.report_digest is None:
                raise ValueError("Validator dispatch requires change-set and report digests")
            if (self.attempt == 1) is (self.correction_evidence_digest is not None):
                raise ValueError("Validator correction binding must be present only on attempt two")
            if (self.attempt == 1) is (self.correction_wiki_trace_digest is not None):
                raise ValueError(
                    "Validator correction Wiki binding must be present only on attempt two"
                )
        return self


class RoleInvocationLease(StrictModel):
    """Immutable tombstone written before a provider call is attempted."""

    invocation_id: Identifier
    binding: RoleInvocationBinding
    lease_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_lease_digest(self) -> RoleInvocationLease:
        expected = artifact_digest(
            {
                "invocation_id": self.invocation_id,
                "binding": self.binding.model_dump(mode="json"),
            }
        )
        if self.lease_digest != expected:
            raise ValueError("role invocation lease digest does not match its content")
        return self

    @classmethod
    def issue(cls, binding: RoleInvocationBinding) -> RoleInvocationLease:
        invocation_id = f"dispatch-{uuid4().hex}"
        return cls(
            invocation_id=invocation_id,
            binding=binding,
            lease_digest=artifact_digest(
                {
                    "invocation_id": invocation_id,
                    "binding": binding.model_dump(mode="json"),
                }
            ),
        )


SanitizedRolePolicyReason = Literal[
    "required_approval_missing",
    "implementation_contract_invalid",
    "transformation_scope_invalid",
    "unresolved_question_risk_missing",
    "correction_delta_required",
    "correction_no_material_changes",
    "correction_signal_coverage_missing",
    "correction_scope_invalid",
    "correction_identical_candidate",
    "file_plan_scope_mismatch",
    "file_plan_delta_mismatch",
    "workspace_scope_mismatch",
    "workspace_not_clean",
    "attempt_two_scope_expansion_invalid",
    "output_evidence_local_path",
    "policy_rejected",
]


class RejectedArchitectModelCallReceipt(StrictModel):
    """Internal proof of one schema-valid call rejected by controller policy."""

    schema_version: Literal["1.0"] = "1.0"
    receipt_kind: Literal["architect_controller_policy_rejection"] = (
        "architect_controller_policy_rejection"
    )
    request_id: Identifier
    request_digest: Sha256Digest
    role: Literal["architect"] = "architect"
    attempt: Literal[1] = 1
    phase: Literal["controller_policy_validation"] = "controller_policy_validation"
    provider_response_received: Literal[True] = True
    structured_output_schema_valid: Literal[True] = True
    controller_policy_valid: Literal[False] = False
    accepted_role_artifact_persisted: Literal[False] = False
    downstream_authority_granted: Literal[False] = False
    reason_code: SanitizedRolePolicyReason
    model_call: ModelCallRecord


class SanitizedModelPolicyError(ValueError):
    """Fixed-code role failure safe for LangGraph's durable task metadata."""

    def __init__(
        self,
        role: Literal["architect", "engineer", "validator"],
        reason_code: SanitizedRolePolicyReason,
    ) -> None:
        self.role = role
        self.reason_code = reason_code
        super().__init__(f"model_role_policy_failure:{role}:{reason_code}")


class ModelAgentWorkflowRoles:
    """Three callable roles compatible with the existing LangGraph workflow.

    The same structured client may back all roles, or callers may supply a
    separate client per role.  Model invocations are persisted before their
    outputs advance to the next node.  Validation reports and Validator
    advisories are also immutable, so retrying a failed LangGraph task reuses
    the exact completed evidence instead of repeating a paid or mutable action.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        architect_model: StructuredModelClient,
        engineer_model: StructuredModelClient,
        validator_model: StructuredModelClient,
        architect_context_factory: ArchitectContextFactory,
        correction_wiki_retriever: CorrectionWikiRetriever | None = None,
        workspace_factory: WorkspaceFactory,
        deterministic_validator: DeterministicValidator,
        artifact_store: ArtifactStore,
    ) -> None:
        self.architect_agent = ArchitectAgent(registry, architect_model)
        self.engineer_agent = EngineerAgent(registry, engineer_model)
        self.validator_agent = ValidatorAgent(registry, validator_model)
        self.architect_context_factory = architect_context_factory
        self.correction_wiki_retriever = correction_wiki_retriever
        self.workspace_factory = workspace_factory
        self.deterministic_validator = deterministic_validator
        self.artifact_store = artifact_store

    def build(
        self,
        *,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> MigrationWorkflow:
        """Compile the existing deterministic graph around these three roles."""

        return build_workflow(
            self.architect,
            self.engineer,
            self.validator,
            checkpointer=checkpointer,
        )

    def architect(
        self,
        request: MigrationRequest,
    ) -> MigrationManifest | PlanningIntervention:
        """Run/replay the Architect or persist a non-model preflight stop."""

        paths = RunArtifactPaths(request.request_id)
        path = paths.architect
        existing = self._read_optional(path)
        preflight_path = paths.architect_preflight
        existing_preflight = self._read_optional(preflight_path)
        rejection_path = paths.architect_policy_rejection
        existing_rejection = self._read_optional(rejection_path)
        if existing is not None:
            if existing_preflight is not None or existing_rejection is not None:
                raise ModelWorkflowIntegrationError(
                    "persisted Architect outcomes conflict with the completed model run"
                )
            context = self._load_architect_evidence(request)
            run = ArchitectRun.model_validate(existing)
            self._verify_architect_replay(run, context)
            return run.proposal.manifest

        if existing_rejection is not None:
            if existing_preflight is not None:
                raise ModelWorkflowIntegrationError(
                    "persisted Architect policy rejection conflicts with a preflight stop"
                )
            context = self._load_architect_evidence(request)
            receipt = self._validate_architect_policy_rejection(
                existing_rejection,
                request,
                context,
            )
            raise SanitizedModelPolicyError("architect", receipt.reason_code)

        supplied = self.architect_context_factory(request)
        if isinstance(supplied, PlanningIntervention):
            intervention = PlanningIntervention.model_validate(supplied.model_dump(mode="python"))
            validate_planning_intervention_for_request(intervention, request)
            if existing_preflight is None:
                self.artifact_store.write_json(preflight_path, intervention)
                return intervention
            persisted = PlanningIntervention.model_validate(existing_preflight)
            validate_planning_intervention_for_request(persisted, request)
            if persisted != intervention:
                raise ModelWorkflowIntegrationError(
                    "persisted planning intervention differs from current preflight evidence"
                )
            return persisted

        if existing_preflight is not None:
            raise ModelWorkflowIntegrationError(
                "persisted preflight stop conflicts with current Architect model context"
            )
        context = ArchitectContext.model_validate(supplied.model_dump(mode="python"))
        self._persist_architect_evidence(request, context)
        generation = self.architect_agent.generate(context)
        try:
            run = self.architect_agent.finalize(generation, context)
        except PolicyViolation as error:
            sanitized = _sanitized_role_policy_error("architect", error)
            receipt = RejectedArchitectModelCallReceipt(
                request_id=request.request_id,
                request_digest=artifact_digest(request),
                reason_code=sanitized.reason_code,
                model_call=generation.model_call,
            )
            self.artifact_store.write_json(rejection_path, receipt)
            persisted_receipt = self._read_optional(rejection_path)
            if persisted_receipt is None:
                raise ModelWorkflowIntegrationError(
                    "Architect policy-rejection evidence was not persisted"
                ) from None
            self._validate_architect_policy_rejection(
                persisted_receipt,
                request,
                context,
            )
            lifecycle_event(
                "model.policy.rejected",
                level=logging.WARNING,
                role="architect",
                policy_code=sanitized.reason_code,
            )
            raise sanitized from None
        try:
            self.artifact_store.write_json(path, run)
        except PolicyViolation as error:
            reason_code = _safe_role_artifact_persistence_code(error)
            evidence_category, evidence_field_class = _safe_role_artifact_persistence_diagnostics(
                error
            )
            lifecycle_event(
                "model.output.persistence.rejected",
                level=logging.WARNING,
                role="architect",
                phase="role_artifact_persistence",
                policy_code=reason_code,
                evidence_category=evidence_category,
                evidence_field_class=evidence_field_class,
            )
            raise SanitizedModelPolicyError("architect", reason_code) from None
        return run.proposal.manifest

    def engineer(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        *,
        attempt: int = 1,
        correction: CorrectionAttemptEvidence | None = None,
    ) -> ChangeSet | ImplementationIntervention:
        """Run or replay the Engineer without retaining a mutable workspace."""

        self._require_attempt(attempt)
        validate_manifest_for_request(manifest, request)
        paths = RunArtifactPaths(request.request_id)
        architect_wiki_trace = self._load_engineer_architect_wiki_handoff(request, manifest)
        path = paths.engineer(attempt)
        existing = self._read_optional(path)
        correction_authority, prior_run = self._prepare_engineer_correction(
            request,
            manifest,
            attempt=attempt,
            correction=correction,
            allow_create=existing is None,
        )
        if existing is not None:
            run = EngineerRun.model_validate(existing)
            self._validate_engineer_run(run, request, manifest)
            with self.workspace_factory(request, manifest, attempt) as workspace:
                if prior_run is not None:
                    self._verify_engineer_replay(
                        prior_run,
                        request,
                        manifest,
                        workspace,
                        architect_wiki_trace=architect_wiki_trace,
                        attempt=1,
                        correction_authority=None,
                    )
                context = self.engineer_agent.prepare_context(
                    request,
                    manifest,
                    workspace,
                    architect_wiki_trace=architect_wiki_trace,
                    attempt=attempt,
                    correction_authority=correction_authority,
                )
                binding = self._engineer_invocation_binding(
                    request,
                    manifest,
                    attempt=attempt,
                    context=context,
                    correction=context.correction,
                )
                self._verify_role_invocation_lease_if_present(
                    paths.engineer_invocation_lease(attempt),
                    binding,
                )
                self._verify_engineer_replay(
                    run,
                    request,
                    manifest,
                    workspace,
                    architect_wiki_trace=architect_wiki_trace,
                    attempt=attempt,
                    correction_authority=correction_authority,
                )
            return self._engineer_outcome(run)

        with self.workspace_factory(request, manifest, attempt) as workspace:
            if prior_run is not None:
                self._verify_engineer_replay(
                    prior_run,
                    request,
                    manifest,
                    workspace,
                    architect_wiki_trace=architect_wiki_trace,
                    attempt=1,
                    correction_authority=None,
                )
            context = self.engineer_agent.prepare_context(
                request,
                manifest,
                workspace,
                architect_wiki_trace=architect_wiki_trace,
                attempt=attempt,
                correction_authority=correction_authority,
            )
            binding = self._engineer_invocation_binding(
                request,
                manifest,
                attempt=attempt,
                context=context,
                correction=context.correction,
            )
            self._claim_role_invocation(
                paths.engineer_invocation_lease(attempt),
                binding,
            )
            try:
                run = self.engineer_agent.implement(
                    request,
                    manifest,
                    workspace,
                    architect_wiki_trace=architect_wiki_trace,
                    attempt=attempt,
                    correction_authority=correction_authority,
                    prepared_context=context,
                )
                if run.model_call.input_digest != binding.input_evidence_digest:
                    raise ModelWorkflowIntegrationError(
                        "Engineer model-call input differs from the claimed provider dispatch"
                    )
            except PolicyViolation as error:
                lifecycle_event(
                    "model.policy.rejected",
                    level=logging.WARNING,
                    role="engineer",
                    policy_code=_safe_engineer_policy_code(error),
                )
                raise _sanitized_role_policy_error("engineer", error) from None
        self._validate_engineer_run(run, request, manifest)
        try:
            self.artifact_store.write_json(path, run)
        except PolicyViolation as error:
            reason_code = _safe_role_artifact_persistence_code(error)
            evidence_category, evidence_field_class = _safe_role_artifact_persistence_diagnostics(
                error
            )
            lifecycle_event(
                "model.output.persistence.rejected",
                level=logging.WARNING,
                role="engineer",
                phase="role_artifact_persistence",
                policy_code=reason_code,
                evidence_category=evidence_category,
                evidence_field_class=evidence_field_class,
            )
            raise SanitizedModelPolicyError("engineer", reason_code) from None
        return self._engineer_outcome(run)

    def validator(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        *,
        attempt: int = 1,
        correction: CorrectionAttemptEvidence | None = None,
    ) -> ValidationReport:
        """Rebuild the candidate, run deterministic checks, then seek advice."""

        self._require_attempt(attempt)
        validate_manifest_for_request(manifest, request)
        validate_change_set(change_set, manifest)
        paths = RunArtifactPaths(request.request_id)
        architect_wiki_trace = self._load_engineer_architect_wiki_handoff(request, manifest)
        correction_authority, prior_run = self._prepare_engineer_correction(
            request,
            manifest,
            attempt=attempt,
            correction=correction,
            allow_create=False,
        )
        engineer_run = EngineerRun.model_validate(
            self.artifact_store.read_json(paths.engineer(attempt))
        )
        self._validate_engineer_run(engineer_run, request, manifest)
        if engineer_run.intervention is not None:
            raise ModelWorkflowIntegrationError(
                "Validator cannot run after an Engineer decision-required intervention"
            )
        if engineer_run.change_set != change_set:
            raise ModelWorkflowIntegrationError(
                "LangGraph change set differs from the persisted Engineer run"
            )

        report_path = paths.report(attempt)
        existing_report = self._read_optional(report_path)
        with self.workspace_factory(request, manifest, attempt) as workspace:
            if prior_run is not None:
                self._verify_engineer_replay(
                    prior_run,
                    request,
                    manifest,
                    workspace,
                    architect_wiki_trace=architect_wiki_trace,
                    attempt=1,
                    correction_authority=None,
                )
            self._verify_engineer_replay(
                engineer_run,
                request,
                manifest,
                workspace,
                architect_wiki_trace=architect_wiki_trace,
                attempt=attempt,
                correction_authority=correction_authority,
            )
            if existing_report is None:
                file_plan = engineer_run.file_plan
                if file_plan is None:
                    raise ModelWorkflowIntegrationError(
                        "persisted Engineer run has no replayable file plan"
                    )
                replayed, after_revision = apply_engineer_file_plan(
                    request,
                    manifest,
                    workspace,
                    file_plan,
                )
                if replayed != change_set:
                    raise ModelWorkflowIntegrationError(
                        "replayed Engineer file plan produced a different ChangeSet"
                    )
                if after_revision != engineer_run.workspace_after_revision:
                    raise ModelWorkflowIntegrationError(
                        "replayed Engineer workspace revision does not match"
                    )
                report = self.deterministic_validator(
                    request,
                    manifest,
                    change_set,
                    workspace,
                    attempt,
                )
                post_validation = workspace.audit_changes()
                if (
                    post_validation.changed_paths != change_set.changed_paths
                    or post_validation.unified_diff.strip() != change_set.unified_diff.strip()
                ):
                    raise ModelWorkflowIntegrationError(
                        "deterministic Validator mutated the Engineer candidate"
                    )
                workspace.assert_source_unchanged()
                validate_report(report, manifest, change_set)
                self.artifact_store.write_json(report_path, report)
            else:
                report = ValidationReport.model_validate(existing_report)
                validate_report(report, manifest, change_set)
        if report.attempt != attempt:
            raise ModelWorkflowIntegrationError(
                "persisted validation report belongs to another attempt"
            )

        assessment_path = paths.validator(attempt)
        existing_assessment = self._read_optional(assessment_path)
        evidence = ValidatorEvidenceContext.freeze(manifest, change_set, report)
        validator_binding = self._validator_invocation_binding(
            request,
            manifest,
            attempt=attempt,
            evidence=evidence,
            correction=(
                correction_authority.model_context if correction_authority is not None else None
            ),
        )
        if existing_assessment is None:
            if attempt == 1 and report.disposition is ValidationDisposition.RECOVERABLE_FAILURE:
                assessment = self.validator_agent.unavailable(
                    evidence,
                    reason_code="deferred_recoverable_attempt",
                    attempted=False,
                )
                lifecycle_event(
                    "validator.advisory.deferred",
                    attempt=attempt,
                    disposition=report.disposition.value,
                )
            else:
                validator_lease_path = paths.validator_invocation_lease(attempt)
                existing_lease = self._read_optional(validator_lease_path)
                if existing_lease is not None:
                    self._validate_role_invocation_lease(existing_lease, validator_binding)
                    assessment = self.validator_agent.unavailable(
                        evidence,
                        reason_code="invocation_incomplete",
                        attempted=True,
                    )
                else:
                    self._claim_role_invocation(
                        validator_lease_path,
                        validator_binding,
                    )
                    try:
                        assessment = self.validator_agent.assess(evidence)
                    except (TypeError, ValueError, PolicyViolation):
                        assessment = self.validator_agent.unavailable(
                            evidence,
                            reason_code="model_output_invalid",
                            attempted=True,
                        )
                    except Exception:  # noqa: BLE001 - advisory failure is explicitly non-fatal
                        assessment = self.validator_agent.unavailable(
                            evidence,
                            reason_code="model_call_failed",
                            attempted=True,
                        )
                    if assessment.model_call is None:
                        lifecycle_event(
                            "validator.advisory.unavailable",
                            level=logging.WARNING,
                            attempt=attempt,
                            reason_code=assessment.unavailable_receipt.reason_code
                            if assessment.unavailable_receipt is not None
                            else "unknown",
                        )
            try:
                self.artifact_store.write_json(assessment_path, assessment)
            except PolicyViolation as error:
                if not _is_role_artifact_portability_error(error):
                    raise
                evidence_category, evidence_field_class = (
                    _safe_role_artifact_persistence_diagnostics(error)
                )
                lifecycle_event(
                    "model.output.persistence.rejected",
                    level=logging.WARNING,
                    role="validator",
                    phase="role_artifact_persistence",
                    policy_code=_safe_role_artifact_persistence_code(error),
                    evidence_category=evidence_category,
                    evidence_field_class=evidence_field_class,
                )
                assessment = self.validator_agent.unavailable(
                    evidence,
                    reason_code="model_output_invalid",
                    attempted=True,
                )
                lifecycle_event(
                    "validator.advisory.unavailable",
                    level=logging.WARNING,
                    attempt=attempt,
                    reason_code="model_output_invalid",
                )
                self.artifact_store.write_json(assessment_path, assessment)
        else:
            assessment = ValidatorAssessment.model_validate(existing_assessment)
            self._verify_role_invocation_lease_if_present(
                paths.validator_invocation_lease(attempt),
                validator_binding,
            )
            self._verify_validator_replay(assessment, evidence)
        if assessment.authoritative_disposition is not report.disposition:
            raise ModelWorkflowIntegrationError(
                "Validator advisory does not preserve deterministic disposition"
            )
        return report

    def _verify_architect_replay(
        self,
        run: ArchitectRun,
        context: ArchitectContext,
    ) -> None:
        try:
            self.architect_agent.verify_replay(run, context)
        except (AgentRuntimeError, ModelEvidenceError) as exc:
            raise ModelWorkflowIntegrationError(str(exc)) from exc

    def _validate_architect_policy_rejection(
        self,
        value: object,
        request: MigrationRequest,
        context: ArchitectContext,
    ) -> RejectedArchitectModelCallReceipt:
        """Cross-bind one sanitized rejection to its request and provider input."""

        try:
            receipt = RejectedArchitectModelCallReceipt.model_validate(value)
        except (TypeError, ValueError) as exc:
            raise ModelWorkflowIntegrationError(
                "persisted Architect policy-rejection evidence is structurally invalid"
            ) from exc
        if (
            receipt.request_id != request.request_id
            or receipt.request_digest != artifact_digest(request)
            or context.request != request
        ):
            raise ModelWorkflowIntegrationError(
                "persisted Architect policy rejection belongs to another request"
            )
        try:
            self.architect_agent.verify_rejected_call_input(receipt.model_call, context)
        except (AgentRuntimeError, ModelEvidenceError, TypeError, ValueError) as exc:
            raise ModelWorkflowIntegrationError(
                "persisted Architect policy rejection differs from exact model input"
            ) from exc
        return receipt

    def _verify_engineer_replay(
        self,
        run: EngineerRun,
        request: MigrationRequest,
        manifest: MigrationManifest,
        workspace: IsolatedWorkspace,
        *,
        architect_wiki_trace: RetrievalTrace,
        attempt: int,
        correction_authority: EngineerCorrectionAuthority | None,
    ) -> None:
        try:
            self.engineer_agent.verify_replay(
                run,
                request,
                manifest,
                workspace,
                architect_wiki_trace=architect_wiki_trace,
                attempt=attempt,
                correction_authority=correction_authority,
            )
        except (AgentRuntimeError, ModelEvidenceError) as exc:
            raise ModelWorkflowIntegrationError(str(exc)) from exc

    def _verify_validator_replay(
        self,
        assessment: ValidatorAssessment,
        context: ValidatorEvidenceContext,
    ) -> None:
        try:
            self.validator_agent.verify_replay(assessment, context)
        except (AgentRuntimeError, ModelEvidenceError) as exc:
            raise ModelWorkflowIntegrationError(str(exc)) from exc

    @staticmethod
    def _require_attempt(attempt: int) -> None:
        if attempt not in (1, 2):
            raise ModelWorkflowIntegrationError(
                "model workflow supports only bounded attempts 1 and 2"
            )

    @staticmethod
    def _validate_engineer_run(
        run: EngineerRun,
        request: MigrationRequest,
        manifest: MigrationManifest,
    ) -> None:
        if run.intervention is not None:
            if run.intervention.request_id != request.request_id:
                raise ModelWorkflowIntegrationError(
                    "persisted Engineer intervention belongs to another request"
                )
            if run.intervention.request_digest != artifact_digest(request):
                raise ModelWorkflowIntegrationError(
                    "persisted Engineer intervention request digest does not match"
                )
            if run.intervention.manifest_id != manifest.manifest_id:
                raise ModelWorkflowIntegrationError(
                    "persisted Engineer intervention belongs to another manifest"
                )
            if run.intervention.manifest_digest != artifact_digest(manifest):
                raise ModelWorkflowIntegrationError(
                    "persisted Engineer intervention manifest digest does not match"
                )
            if run.intervention.base_revision != request.base_revision:
                raise ModelWorkflowIntegrationError(
                    "persisted Engineer intervention belongs to another revision"
                )
            return

        change_set = run.change_set
        file_plan = run.file_plan
        if change_set is None or file_plan is None:
            raise ModelWorkflowIntegrationError("persisted Engineer file-plan run is incomplete")
        validate_change_set(change_set, manifest)
        if change_set.request_id != request.request_id:
            raise ModelWorkflowIntegrationError("persisted Engineer run belongs to another request")
        if set(update.path for update in file_plan.updates) != set(manifest.approved_paths):
            raise ModelWorkflowIntegrationError(
                "persisted Engineer file plan differs from manifest scope"
            )

    @staticmethod
    def _engineer_outcome(run: EngineerRun) -> ChangeSet | ImplementationIntervention:
        if run.intervention is not None:
            return run.intervention
        if run.change_set is None:
            raise ModelWorkflowIntegrationError("Engineer file-plan run has no change set")
        return run.change_set

    def _read_optional(self, relative_path: str) -> object | None:
        try:
            return cast(object, self.artifact_store.read_json(relative_path))
        except FileNotFoundError:
            return None

    def _claim_role_invocation(
        self,
        path: str,
        binding: RoleInvocationBinding,
    ) -> RoleInvocationLease:
        """Claim one provider dispatch or fail closed after any prior claim."""

        existing = self._read_optional(path)
        if existing is not None:
            self._validate_role_invocation_lease(existing, binding)
            raise ModelWorkflowIntegrationError(
                f"{binding.role} provider invocation already started without a completed "
                "role artifact; refusing a duplicate provider call"
            )

        lease = RoleInvocationLease.issue(binding)
        try:
            self.artifact_store.write_json(path, lease)
        except PolicyViolation:
            raced = self._read_optional(path)
            if raced is None:
                raise
            self._validate_role_invocation_lease(raced, binding)
            raise ModelWorkflowIntegrationError(
                f"{binding.role} provider invocation was concurrently claimed; refusing "
                "a duplicate provider call"
            ) from None
        persisted = self._read_optional(path)
        if persisted is None:
            raise ModelWorkflowIntegrationError(
                f"{binding.role} provider invocation lease was not persisted"
            )
        verified = self._validate_role_invocation_lease(persisted, binding)
        if verified != lease:
            raise ModelWorkflowIntegrationError(
                f"{binding.role} provider invocation lease changed before dispatch"
            )
        return verified

    def _verify_role_invocation_lease_if_present(
        self,
        path: str,
        binding: RoleInvocationBinding,
    ) -> None:
        """Verify a new-style lease while permitting immutable legacy completions."""

        existing = self._read_optional(path)
        if existing is not None:
            self._validate_role_invocation_lease(existing, binding)

    @staticmethod
    def _validate_role_invocation_lease(
        value: object,
        binding: RoleInvocationBinding,
    ) -> RoleInvocationLease:
        try:
            lease = RoleInvocationLease.model_validate(value)
        except (TypeError, ValueError) as exc:
            raise ModelWorkflowIntegrationError(
                "persisted role invocation lease is structurally invalid"
            ) from exc
        if lease.binding != binding:
            raise ModelWorkflowIntegrationError(
                "persisted role invocation lease differs from exact provider input"
            )
        return lease

    def _engineer_invocation_binding(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        *,
        attempt: int,
        context: EngineerWorkspaceContext,
        correction: EngineerCorrectionContext | None,
    ) -> RoleInvocationBinding:
        provider_input = self.engineer_agent.provider_input(context)
        return RoleInvocationBinding(
            role="engineer",
            request_id=request.request_id,
            request_digest=artifact_digest(request),
            attempt=cast(Literal[1, 2], attempt),
            input_evidence_digest=artifact_digest(provider_input),
            agent_version=self.engineer_agent.definition.version,
            agent_definition_digest=self.engineer_agent.definition.definition_digest,
            manifest_digest=artifact_digest(manifest),
            correction_evidence_digest=(
                correction.correction_evidence_digest if correction is not None else None
            ),
            correction_wiki_trace_digest=(
                correction.correction_wiki_trace_digest if correction is not None else None
            ),
            change_set_digest=(
                correction.prior_change_set_digest if correction is not None else None
            ),
            report_digest=(
                correction.prior_validation_report_digest if correction is not None else None
            ),
        )

    def _validator_invocation_binding(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        *,
        attempt: int,
        evidence: ValidatorEvidenceContext,
        correction: EngineerCorrectionContext | None,
    ) -> RoleInvocationBinding:
        return RoleInvocationBinding(
            role="validator",
            request_id=request.request_id,
            request_digest=artifact_digest(request),
            attempt=cast(Literal[1, 2], attempt),
            input_evidence_digest=artifact_digest(evidence),
            agent_version=self.validator_agent.definition.version,
            agent_definition_digest=self.validator_agent.definition.definition_digest,
            manifest_digest=artifact_digest(manifest),
            correction_evidence_digest=(
                correction.correction_evidence_digest if correction is not None else None
            ),
            correction_wiki_trace_digest=(
                correction.correction_wiki_trace_digest if correction is not None else None
            ),
            change_set_digest=evidence.evidence.change_set_digest,
            report_digest=evidence.evidence.report_digest,
        )

    def _prepare_engineer_correction(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        *,
        attempt: int,
        correction: CorrectionAttemptEvidence | None,
        allow_create: bool,
    ) -> tuple[EngineerCorrectionAuthority | None, EngineerRun | None]:
        """Validate and persist the safe attempt-two model correction projection."""

        if attempt == 1:
            if correction is not None:
                raise ModelWorkflowIntegrationError(
                    "Engineer attempt one cannot receive correction evidence"
                )
            return None, None
        if correction is None:
            raise ModelWorkflowIntegrationError(
                "Engineer attempt two requires exact correction evidence"
            )
        frozen = validate_correction_attempt_evidence(correction, request, manifest)
        paths = RunArtifactPaths(request.request_id)

        try:
            prior_run = EngineerRun.model_validate(self.artifact_store.read_json(paths.engineer(1)))
            prior_report = ValidationReport.model_validate(
                self.artifact_store.read_json(paths.report(1))
            )
        except FileNotFoundError as exc:
            raise ModelWorkflowIntegrationError(
                "attempt two requires persisted attempt-one Engineer and report evidence"
            ) from exc
        self._validate_engineer_run(prior_run, request, manifest)
        if prior_run.intervention is not None or prior_run.file_plan is None:
            raise ModelWorkflowIntegrationError(
                "attempt two requires a replayable attempt-one Engineer file plan"
            )
        if prior_run.change_set != frozen.prior_change_set:
            raise ModelWorkflowIntegrationError(
                "correction change set differs from persisted attempt-one Engineer evidence"
            )
        if artifact_digest(prior_run.change_set) != frozen.prior_change_set_digest:
            raise ModelWorkflowIntegrationError(
                "persisted attempt-one ChangeSet digest differs from correction evidence"
            )
        validate_report(prior_report, manifest, frozen.prior_change_set)
        if prior_report != frozen.prior_validation_report:
            raise ModelWorkflowIntegrationError(
                "correction report differs from persisted attempt-one validation evidence"
            )

        if prior_run.workspace_after_revision is None:
            raise ModelWorkflowIntegrationError(
                "attempt two requires a persisted attempt-one candidate revision"
            )
        try:
            repair_signal_ids = EngineerCorrectionContext.require_repair_contract(
                frozen,
                prior_run.file_plan,
            )
        except AgentRuntimeError as exc:
            raise ModelWorkflowIntegrationError(
                "attempt two is not authorized because deterministic repair coverage is "
                f"incomplete: {exc}"
            ) from exc
        wiki_query = correction_wiki_query(request.platform, repair_signal_ids)
        wiki_path = paths.correction_wiki
        existing_wiki = self._read_optional(wiki_path)
        if existing_wiki is None:
            if not allow_create:
                raise ModelWorkflowIntegrationError(
                    "persisted attempt-two correction Wiki trace is missing"
                )
            if self.correction_wiki_retriever is None:
                raise ModelWorkflowIntegrationError(
                    "attempt two requires targeted correction Wiki retrieval before model dispatch"
                )
            try:
                supplied_trace = self.correction_wiki_retriever(request, wiki_query)
                trace = RetrievalTrace.model_validate(supplied_trace.model_dump(mode="python"))
            except (PolicyViolation, TypeError, ValueError) as exc:
                raise ModelWorkflowIntegrationError(
                    "targeted correction Wiki retrieval could not be completed safely"
                ) from exc
            self._validate_correction_wiki_trace(
                trace,
                request,
                wiki_query,
                repair_signal_ids,
            )
            self.artifact_store.write_json(wiki_path, trace)
            persisted_trace = RetrievalTrace.model_validate(
                self.artifact_store.read_json(wiki_path)
            )
            if persisted_trace != trace:
                raise ModelWorkflowIntegrationError(
                    "persisted correction Wiki trace differs from retrieved evidence"
                )
            lifecycle_event(
                "engineer.correction.wiki.retrieved",
                signal_count=len(repair_signal_ids),
                hit_count=len(trace.hits),
                trace_digest=artifact_digest(trace),
            )
        else:
            try:
                trace = RetrievalTrace.model_validate(existing_wiki)
            except (TypeError, ValueError) as exc:
                raise ModelWorkflowIntegrationError(
                    "persisted correction Wiki trace is structurally invalid"
                ) from exc
            self._validate_correction_wiki_trace(
                trace,
                request,
                wiki_query,
                repair_signal_ids,
            )

        expected_authority = EngineerCorrectionAuthority.freeze(
            frozen,
            prior_run.file_plan,
            prior_candidate_revision=prior_run.workspace_after_revision,
            correction_wiki_trace=trace,
        )
        expected = expected_authority.model_context
        correction_path = paths.engineer_correction
        existing = self._read_optional(correction_path)
        try:
            if existing is None:
                if not allow_create:
                    raise ModelWorkflowIntegrationError(
                        "persisted attempt-two Engineer correction context is missing"
                    )
                self.artifact_store.write_json(correction_path, expected)
                persisted = EngineerCorrectionContext.model_validate(
                    self.artifact_store.read_json(correction_path)
                )
            else:
                persisted = EngineerCorrectionContext.model_validate(existing)
        except (TypeError, ValueError) as exc:
            raise ModelWorkflowIntegrationError(
                "persisted Engineer correction context differs from exact prior evidence"
            ) from exc
        if persisted != expected:
            raise ModelWorkflowIntegrationError(
                "persisted Engineer correction context differs from exact prior evidence"
            )
        authority = EngineerCorrectionAuthority(
            evidence=frozen,
            model_context=persisted,
        )
        try:
            authority.require_canonical_context(request, manifest)
        except AgentRuntimeError as exc:
            raise ModelWorkflowIntegrationError(
                "persisted Engineer correction context differs from exact prior evidence"
            ) from exc
        return authority, prior_run

    @staticmethod
    def _validate_correction_wiki_trace(
        trace: RetrievalTrace,
        request: MigrationRequest,
        expected_query: str,
        repair_signal_ids: tuple[str, ...],
    ) -> None:
        if trace.query != expected_query:
            raise ModelWorkflowIntegrationError(
                "correction Wiki trace query differs from exact repair signals"
            )
        if not trace.hits:
            raise ModelWorkflowIntegrationError(
                "targeted correction Wiki retrieval returned no relevant evidence"
            )
        if (
            trace.platform is not request.platform
            or trace.source_version != request.target.source_version
            or trace.target_version != request.target.target_version
        ):
            raise ModelWorkflowIntegrationError(
                "correction Wiki trace differs from the request version scope"
            )
        try:
            EngineerCorrectionContext.require_wiki_signal_coverage(
                trace,
                repair_signal_ids,
            )
        except AgentRuntimeError as exc:
            raise ModelWorkflowIntegrationError(
                "targeted correction Wiki evidence does not cover every repair signal"
            ) from exc

    def _persist_architect_evidence(
        self,
        request: MigrationRequest,
        context: ArchitectContext,
    ) -> None:
        """Persist exact source, graph, and Wiki inputs before any Architect call."""

        paths = RunArtifactPaths(request.request_id)
        artifacts = (
            (paths.dependency_graph, context.dependency_graph),
            *(
                ((paths.graph_assurance_report, context.graph_assurance_report),)
                if context.graph_assurance_report is not None
                else ()
            ),
            (paths.wiki_trace, context.wiki_trace),
            (paths.architect_context, context),
        )
        for path, value in artifacts:
            try:
                self.artifact_store.write_json(path, value)
                persisted = self.artifact_store.read_json(path)
            except (TypeError, ValueError) as exc:
                raise ModelWorkflowIntegrationError(
                    "Architect input evidence could not be persisted safely"
                ) from exc
            expected = value.model_dump(mode="json")
            if persisted != expected:
                raise ModelWorkflowIntegrationError(
                    "persisted Architect input evidence differs from current context"
                )

    def _load_architect_evidence(self, request: MigrationRequest) -> ArchitectContext:
        """Load and cross-bind the immutable planning evidence for a completed run."""

        paths = RunArtifactPaths(request.request_id)
        try:
            raw_context = self.artifact_store.read_json(paths.architect_context)
            raw_graph = self.artifact_store.read_json(paths.dependency_graph)
            raw_wiki = self.artifact_store.read_json(paths.wiki_trace)
            context = ArchitectContext.model_validate(raw_context)
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise ModelWorkflowIntegrationError(
                "persisted Architect input evidence is missing or structurally invalid"
            ) from exc
        if context.request != request:
            raise ModelWorkflowIntegrationError(
                "persisted Architect context belongs to another request"
            )
        if raw_graph != context.dependency_graph.model_dump(mode="json"):
            raise ModelWorkflowIntegrationError(
                "persisted Architect dependency graph differs from frozen context"
            )
        if raw_wiki != context.wiki_trace.model_dump(mode="json"):
            raise ModelWorkflowIntegrationError(
                "persisted Architect Wiki trace differs from frozen context"
            )
        raw_assurance = self._read_optional(paths.graph_assurance_report)
        if context.graph_assurance_report is None:
            if raw_assurance is not None:
                raise ModelWorkflowIntegrationError(
                    "persisted graph assurance report conflicts with legacy Architect context"
                )
        elif raw_assurance != context.graph_assurance_report.model_dump(mode="json"):
            raise ModelWorkflowIntegrationError(
                "persisted graph assurance report differs from frozen Architect context"
            )
        return context

    def _load_engineer_architect_wiki_handoff(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
    ) -> RetrievalTrace:
        """Bind the Engineer to the exact Wiki trace used by the approved Architect run."""

        context = self._load_architect_evidence(request)
        paths = RunArtifactPaths(request.request_id)
        try:
            run = ArchitectRun.model_validate(self.artifact_store.read_json(paths.architect))
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise ModelWorkflowIntegrationError(
                "persisted Architect run is missing or structurally invalid for Engineer handoff"
            ) from exc
        self._verify_architect_replay(run, context)
        if run.proposal.manifest != manifest:
            raise ModelWorkflowIntegrationError(
                "Engineer manifest differs from the persisted Architect proposal"
            )
        return RetrievalTrace.model_validate(context.wiki_trace.model_dump(mode="python"))


def filesystem_workspace_factory(
    source_root: Path,
    *,
    temp_parent: Path | None = None,
) -> WorkspaceFactory:
    """Return a small reusable factory for one explicitly selected source root."""

    resolved_source = source_root.resolve(strict=True)

    def create(
        request: MigrationRequest,
        manifest: MigrationManifest,
        attempt: int,
    ) -> IsolatedWorkspace:
        ModelAgentWorkflowRoles._require_attempt(attempt)
        return IsolatedWorkspace(
            resolved_source,
            manifest.approved_paths,
            temp_parent=temp_parent,
            expected_revision=request.base_revision,
        )

    return create


def _sanitized_role_policy_error(
    role: Literal["architect", "engineer", "validator"],
    error: PolicyViolation,
) -> SanitizedModelPolicyError:
    message = str(error)
    reason: SanitizedRolePolicyReason = "policy_rejected"
    if role == "architect" and message.startswith(
        "manifest implementation contract does not match"
    ):
        reason = "implementation_contract_invalid"
    elif role == "architect" and message.startswith(
        "manifest omits scope-required approval actions:"
    ):
        reason = "required_approval_missing"
    elif role == "architect" and message.startswith(
        (
            "Architect manifest approved paths must exactly equal transformation outputs",
            "manifest transformation ",
            "manifest transformations ",
            "manifest approved paths do not match the caller-owned output boundary",
            "each approved output must be owned by exactly one transformation",
            "manifest must contain at least one transformation",
        )
    ):
        reason = "transformation_scope_invalid"
    elif role == "architect" and message.startswith(
        "Architect unresolved questions require a material human-decision risk"
    ):
        reason = "unresolved_question_risk_missing"
    elif role == "engineer":
        reason = _safe_engineer_policy_code(error)
    return SanitizedModelPolicyError(role, reason)


def _safe_engineer_policy_code(error: PolicyViolation) -> SanitizedRolePolicyReason:
    """Classify an Engineer rejection without logging model text or paths."""

    message = str(error)
    prefixes: tuple[tuple[str, SanitizedRolePolicyReason], ...] = (
        (
            "controller-classified correction requires a nonempty Engineer delta",
            "correction_delta_required",
        ),
        (
            "controller-classified correction requires a changed-file Engineer delta",
            "correction_delta_required",
        ),
        (
            "Engineer correction delta contains no material file changes",
            "correction_no_material_changes",
        ),
        (
            "Engineer correction delta does not cover repair signals",
            "correction_signal_coverage_missing",
        ),
        (
            "Engineer correction delta contains paths outside the code-owned repair boundary",
            "correction_scope_invalid",
        ),
        (
            "Engineer correction delta produced an identical attempt-one candidate",
            "correction_identical_candidate",
        ),
        ("Engineer file plan scope mismatch", "file_plan_scope_mismatch"),
        (
            "Engineer actual filesystem delta does not equal the proposed update paths",
            "file_plan_delta_mismatch",
        ),
        ("Engineer workspace scope does not exactly match", "workspace_scope_mismatch"),
        ("Engineer requires a clean isolated workspace", "workspace_not_clean"),
        ("attempt-two scope expansion must identify", "attempt_two_scope_expansion_invalid"),
    )
    return next(
        (code for prefix, code in prefixes if message.startswith(prefix)),
        "policy_rejected",
    )


def _safe_role_artifact_persistence_code(
    error: PolicyViolation,
) -> SanitizedRolePolicyReason:
    """Classify a role artifact write without exposing authored output."""

    if str(error) in {
        "portable evidence contains a local absolute path",
        "portable evidence contains an absolute project or source path",
    }:
        return "output_evidence_local_path"
    return "policy_rejected"


def _safe_role_artifact_persistence_diagnostics(
    error: PolicyViolation,
) -> tuple[str, str]:
    """Return only fixed classifier tokens, never authored field names or text."""

    if isinstance(error, PortableEvidencePolicyViolation):
        return error.evidence_category, error.field_class
    return "unknown", "unknown"


def _is_role_artifact_portability_error(error: PolicyViolation) -> bool:
    """Identify portability rejections without inspecting or relaying authored text."""

    return str(error).startswith("portable evidence contains ")


__all__ = [
    "ArchitectContextFactory",
    "CorrectionWikiRetriever",
    "DeterministicValidator",
    "ModelAgentWorkflowRoles",
    "ModelWorkflowIntegrationError",
    "SanitizedModelPolicyError",
    "WorkspaceFactory",
    "filesystem_workspace_factory",
]
