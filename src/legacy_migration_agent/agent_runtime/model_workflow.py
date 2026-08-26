"""Durable LangGraph integration for the three model-backed role agents.

The role implementations remain narrow and provider-neutral.  This adapter
connects them to :class:`MigrationWorkflow`, persists their typed handoffs in
an immutable :class:`ArtifactStore`, and reconstructs the Engineer candidate
from the stored file plan before deterministic validation.  A process restart
therefore does not require a second model call or retain a mutable workspace.
"""

from __future__ import annotations

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
    ArchitectAgent,
    ArchitectContext,
    ArchitectRun,
    EngineerAgent,
    EngineerCorrectionContext,
    EngineerRun,
    EngineerWorkspaceContext,
    ValidatorAgent,
    ValidatorAssessment,
    ValidatorEvidenceContext,
    apply_engineer_file_plan,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    ModelEvidenceError,
    StructuredModelClient,
)
from legacy_migration_agent.contracts import (
    ChangeSet,
    Identifier,
    ImplementationIntervention,
    MigrationManifest,
    MigrationRequest,
    PlanningIntervention,
    Sha256Digest,
    StrictModel,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import ArtifactStore, artifact_digest
from legacy_migration_agent.core.policies import (
    PolicyViolation,
    validate_change_set,
    validate_manifest_for_request,
    validate_report,
)
from legacy_migration_agent.core.workspace import IsolatedWorkspace
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
                    self.change_set_digest,
                    self.report_digest,
                )
            ):
                raise ValueError("Engineer attempt one cannot contain correction bindings")
            if self.attempt == 2 and any(
                value is None
                for value in (
                    self.correction_evidence_digest,
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
    "policy_rejected",
]


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
        workspace_factory: WorkspaceFactory,
        deterministic_validator: DeterministicValidator,
        artifact_store: ArtifactStore,
    ) -> None:
        self.architect_agent = ArchitectAgent(registry, architect_model)
        self.engineer_agent = EngineerAgent(registry, engineer_model)
        self.validator_agent = ValidatorAgent(registry, validator_model)
        self.architect_context_factory = architect_context_factory
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

        supplied = self.architect_context_factory(request)
        path = self._architect_path(request)
        existing = self._read_optional(path)
        preflight_path = self._architect_preflight_path(request)
        existing_preflight = self._read_optional(preflight_path)
        if isinstance(supplied, PlanningIntervention):
            intervention = PlanningIntervention.model_validate(supplied.model_dump(mode="python"))
            validate_planning_intervention_for_request(intervention, request)
            if existing is not None:
                raise ModelWorkflowIntegrationError(
                    "persisted Architect model run conflicts with current preflight stop"
                )
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
        if existing is None:
            try:
                run = self.architect_agent.propose(context)
            except PolicyViolation as error:
                raise _sanitized_role_policy_error("architect", error) from None
            self.artifact_store.write_json(path, run)
        else:
            run = ArchitectRun.model_validate(existing)
            self._verify_architect_replay(run, context)
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
        path = self._engineer_path(request, attempt)
        existing = self._read_optional(path)
        correction_context, prior_run = self._prepare_engineer_correction(
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
                        attempt=1,
                        correction=None,
                    )
                context = self.engineer_agent.prepare_context(
                    request,
                    manifest,
                    workspace,
                    attempt=attempt,
                    correction=correction_context,
                )
                binding = self._engineer_invocation_binding(
                    request,
                    manifest,
                    attempt=attempt,
                    context=context,
                    correction=correction_context,
                )
                self._verify_role_invocation_lease_if_present(
                    self._engineer_lease_path(request, attempt),
                    binding,
                )
                self._verify_engineer_replay(
                    run,
                    request,
                    manifest,
                    workspace,
                    attempt=attempt,
                    correction=correction_context,
                )
            return self._engineer_outcome(run)

        with self.workspace_factory(request, manifest, attempt) as workspace:
            if prior_run is not None:
                self._verify_engineer_replay(
                    prior_run,
                    request,
                    manifest,
                    workspace,
                    attempt=1,
                    correction=None,
                )
            context = self.engineer_agent.prepare_context(
                request,
                manifest,
                workspace,
                attempt=attempt,
                correction=correction_context,
            )
            binding = self._engineer_invocation_binding(
                request,
                manifest,
                attempt=attempt,
                context=context,
                correction=correction_context,
            )
            self._claim_role_invocation(
                self._engineer_lease_path(request, attempt),
                binding,
            )
            try:
                run = self.engineer_agent.implement(
                    request,
                    manifest,
                    workspace,
                    attempt=attempt,
                    correction=correction_context,
                    prepared_context=context,
                )
            except PolicyViolation as error:
                raise _sanitized_role_policy_error("engineer", error) from None
        self._validate_engineer_run(run, request, manifest)
        self.artifact_store.write_json(path, run)
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
        correction_context, prior_run = self._prepare_engineer_correction(
            request,
            manifest,
            attempt=attempt,
            correction=correction,
            allow_create=False,
        )
        engineer_run = EngineerRun.model_validate(
            self.artifact_store.read_json(self._engineer_path(request, attempt))
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

        report_path = self._report_path(request, attempt)
        existing_report = self._read_optional(report_path)
        with self.workspace_factory(request, manifest, attempt) as workspace:
            if prior_run is not None:
                self._verify_engineer_replay(
                    prior_run,
                    request,
                    manifest,
                    workspace,
                    attempt=1,
                    correction=None,
                )
            self._verify_engineer_replay(
                engineer_run,
                request,
                manifest,
                workspace,
                attempt=attempt,
                correction=correction_context,
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

        assessment_path = self._assessment_path(request, attempt)
        existing_assessment = self._read_optional(assessment_path)
        evidence = ValidatorEvidenceContext.freeze(manifest, change_set, report)
        validator_binding = self._validator_invocation_binding(
            request,
            manifest,
            attempt=attempt,
            evidence=evidence,
            correction=correction_context,
        )
        if existing_assessment is None:
            self._claim_role_invocation(
                self._validator_lease_path(request, attempt),
                validator_binding,
            )
            try:
                assessment = self.validator_agent.assess(evidence)
            except PolicyViolation as error:
                raise _sanitized_role_policy_error("validator", error) from None
            self.artifact_store.write_json(assessment_path, assessment)
        else:
            assessment = ValidatorAssessment.model_validate(existing_assessment)
            self._verify_role_invocation_lease_if_present(
                self._validator_lease_path(request, attempt),
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
        except ModelEvidenceError as exc:
            raise ModelWorkflowIntegrationError(str(exc)) from exc

    def _verify_engineer_replay(
        self,
        run: EngineerRun,
        request: MigrationRequest,
        manifest: MigrationManifest,
        workspace: IsolatedWorkspace,
        *,
        attempt: int,
        correction: EngineerCorrectionContext | None,
    ) -> None:
        try:
            self.engineer_agent.verify_replay(
                run,
                request,
                manifest,
                workspace,
                attempt=attempt,
                correction=correction,
            )
        except ModelEvidenceError as exc:
            raise ModelWorkflowIntegrationError(str(exc)) from exc

    def _verify_validator_replay(
        self,
        assessment: ValidatorAssessment,
        context: ValidatorEvidenceContext,
    ) -> None:
        try:
            self.validator_agent.verify_replay(assessment, context)
        except ModelEvidenceError as exc:
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

    def _architect_path(self, request: MigrationRequest) -> str:
        return f"model-runs/{request.request_id}/architect.json"

    def _architect_preflight_path(self, request: MigrationRequest) -> str:
        return f"model-runs/{request.request_id}/architect-preflight.json"

    def _architect_context_path(self, request: MigrationRequest) -> str:
        return f"model-runs/{request.request_id}/architect-context.json"

    def _architect_graph_path(self, request: MigrationRequest) -> str:
        return f"model-runs/{request.request_id}/dependency-graph.json"

    def _architect_wiki_path(self, request: MigrationRequest) -> str:
        return f"model-runs/{request.request_id}/wiki-trace.json"

    def _engineer_path(self, request: MigrationRequest, attempt: int) -> str:
        return f"model-runs/{request.request_id}/engineer-attempt-{attempt}.json"

    def _engineer_lease_path(self, request: MigrationRequest, attempt: int) -> str:
        return f"model-runs/{request.request_id}/engineer-invocation-lease-attempt-{attempt}.json"

    def _engineer_correction_path(self, request: MigrationRequest) -> str:
        return f"model-runs/{request.request_id}/engineer-correction-attempt-2.json"

    def _report_path(self, request: MigrationRequest, attempt: int) -> str:
        return f"model-runs/{request.request_id}/report-attempt-{attempt}.json"

    def _assessment_path(self, request: MigrationRequest, attempt: int) -> str:
        return f"model-runs/{request.request_id}/validator-attempt-{attempt}.json"

    def _validator_lease_path(self, request: MigrationRequest, attempt: int) -> str:
        return f"model-runs/{request.request_id}/validator-invocation-lease-attempt-{attempt}.json"

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
        return RoleInvocationBinding(
            role="engineer",
            request_id=request.request_id,
            request_digest=artifact_digest(request),
            attempt=cast(Literal[1, 2], attempt),
            input_evidence_digest=artifact_digest(context),
            agent_version=self.engineer_agent.definition.version,
            agent_definition_digest=self.engineer_agent.definition.definition_digest,
            manifest_digest=artifact_digest(manifest),
            correction_evidence_digest=(
                correction.correction_evidence_digest if correction is not None else None
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
    ) -> tuple[EngineerCorrectionContext | None, EngineerRun | None]:
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

        try:
            prior_run = EngineerRun.model_validate(
                self.artifact_store.read_json(self._engineer_path(request, 1))
            )
            prior_report = ValidationReport.model_validate(
                self.artifact_store.read_json(self._report_path(request, 1))
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
        validate_report(prior_report, manifest, frozen.prior_change_set)
        if prior_report != frozen.prior_validation_report:
            raise ModelWorkflowIntegrationError(
                "correction report differs from persisted attempt-one validation evidence"
            )

        expected = EngineerCorrectionContext.freeze(frozen, prior_run.file_plan)
        correction_path = self._engineer_correction_path(request)
        existing = self._read_optional(correction_path)
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
        if persisted != expected:
            raise ModelWorkflowIntegrationError(
                "persisted Engineer correction context differs from exact prior evidence"
            )
        return persisted, prior_run

    def _persist_architect_evidence(
        self,
        request: MigrationRequest,
        context: ArchitectContext,
    ) -> None:
        """Persist the exact graph and Wiki inputs before any Architect call."""

        artifacts = (
            (self._architect_graph_path(request), context.dependency_graph),
            (self._architect_wiki_path(request), context.wiki_trace),
            (self._architect_context_path(request), context),
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
    return SanitizedModelPolicyError(role, reason)


__all__ = [
    "ArchitectContextFactory",
    "DeterministicValidator",
    "ModelAgentWorkflowRoles",
    "ModelWorkflowIntegrationError",
    "SanitizedModelPolicyError",
    "WorkspaceFactory",
    "filesystem_workspace_factory",
]
