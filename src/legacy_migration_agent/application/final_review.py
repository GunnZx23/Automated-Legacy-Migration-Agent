"""Durable final-human-review evidence for a completed local agent run.

The migration workflow deliberately stops at ``ready_for_human_review``.  This
module turns that stop into an explicit, one-use review checkpoint without
granting Git, deployment, publication, or production authority.  Every request
and decision is bound to the exact session, request, manifest, change set,
validation report, and immutable lifecycle inventory that the reviewer saw.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectContext,
    ArchitectRun,
    EngineerRun,
    ValidatorAssessment,
    ValidatorEvidenceContext,
    validate_architect_proposal,
)
from legacy_migration_agent.contracts import (
    ActorIdentifier,
    ChangeSet,
    Identifier,
    MigrationManifest,
    MigrationRequest,
    Platform,
    Sha256Digest,
    StrictModel,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import (
    PolicyViolation,
    validate_change_set,
    validate_manifest_for_request,
    validate_report,
)
from legacy_migration_agent.core.run_session import AgentDefinitionDigests, AgentRunSession

FINAL_REVIEW_POLICY_VERSION: Literal["final-review/1.0"] = "final-review/1.0"
FINAL_REVIEW_REQUEST_PATH = "final-review/request.json"
FINAL_REVIEW_DECISION_PATH = "final-review/decision.json"
FINAL_REVIEW_RECORD_PATH = "final-review/record.json"
FINAL_REVIEW_REQUESTED_KIND = "final-review-requested"
FINAL_REVIEW_DECIDED_KIND = "final-review-decided"
MAX_REVIEW_WINDOW = timedelta(days=14)

FinalReviewSelection = Literal["accept", "reject", "request_changes"]
FinalReviewOutcome = Literal["accepted", "rejected", "changes_requested"]
FinalReviewNextAction = Literal[
    "separate_external_action_required",
    "stop_request",
    "revise_and_start_new_review",
]


class FinalReviewRequest(StrictModel):
    """Exact evidence package presented to one independent reviewer."""

    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["final-review/1.0"] = FINAL_REVIEW_POLICY_VERSION
    review_id: Identifier
    status: Literal["awaiting_final_review"] = "awaiting_final_review"
    requested_action: Literal["accept_candidate_for_next_manual_action"] = (
        "accept_candidate_for_next_manual_action"
    )
    run_id: Identifier
    thread_id: Identifier
    request_id: Identifier
    manifest_id: Identifier
    change_set_id: Identifier
    validation_report_id: Identifier
    platform: Platform
    source_revision: Sha256Digest
    session_context_digest: Sha256Digest
    request_digest: Sha256Digest
    manifest_digest: Sha256Digest
    change_set_digest: Sha256Digest
    validation_report_digest: Sha256Digest
    architect_context_digest: Sha256Digest
    dependency_graph_digest: Sha256Digest
    wiki_trace_digest: Sha256Digest
    scope_policy_digest: Sha256Digest
    architect_run_digest: Sha256Digest
    engineer_run_digest: Sha256Digest
    validator_assessment_digest: Sha256Digest
    required_command_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=64)
    required_receipt_digests: tuple[Sha256Digest, ...] = Field(min_length=1, max_length=64)
    changed_paths: tuple[str, ...] = Field(min_length=1, max_length=256)
    completed_attempt: int = Field(ge=1, le=2)
    agent_definition_digests: AgentDefinitionDigests
    provider_id: str = Field(min_length=1, max_length=160)
    model_id: str = Field(min_length=1, max_length=300)
    completed_lifecycle_kind: Identifier
    completed_lifecycle_index_digest: Sha256Digest
    requester: ActorIdentifier
    designated_reviewer: ActorIdentifier
    reviewer_identity_assurance: Literal["declarative_unverified"] = "declarative_unverified"
    requested_at: datetime
    expires_at: datetime
    options: tuple[FinalReviewSelection, ...] = (
        "accept",
        "reject",
        "request_changes",
    )
    authority_granted: Literal[False] = False

    @field_validator("requested_at", "expires_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("final-review timestamps must be timezone-aware UTC values")
        return value

    @model_validator(mode="after")
    def validate_review_boundary(self) -> FinalReviewRequest:
        if self.requester == self.designated_reviewer:
            raise ValueError("final review requires an independent designated reviewer")
        if self.expires_at <= self.requested_at:
            raise ValueError("final review expiry must be after its request time")
        if self.expires_at - self.requested_at > MAX_REVIEW_WINDOW:
            raise ValueError("final review window cannot exceed fourteen days")
        if len(self.required_command_ids) != len(set(self.required_command_ids)):
            raise ValueError("final review required command IDs must be unique")
        if len(self.required_receipt_digests) != len(set(self.required_receipt_digests)):
            raise ValueError("final review receipt digests must be unique")
        if len(self.changed_paths) != len(set(self.changed_paths)):
            raise ValueError("final review changed paths must be unique")
        if self.options != ("accept", "reject", "request_changes"):
            raise ValueError("final review options cannot be changed or reordered")
        return self


class FinalReviewDecision(StrictModel):
    """One named human's decision on one exact final-review request."""

    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["final-review/1.0"] = FINAL_REVIEW_POLICY_VERSION
    decision_id: Identifier
    review_id: Identifier
    review_request_digest: Sha256Digest
    selection: FinalReviewSelection
    reviewer: ActorIdentifier
    reviewer_identity_assurance: Literal["declarative_unverified"] = "declarative_unverified"
    decided_at: datetime
    comment: str = Field(default="", max_length=2000)
    authority_granted: Literal[False] = False

    @field_validator("decided_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("final-review timestamps must be timezone-aware UTC values")
        return value


class FinalReviewRecord(StrictModel):
    """Terminal, non-authorizing final-review outcome."""

    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["final-review/1.0"] = FINAL_REVIEW_POLICY_VERSION
    record_id: Identifier
    review_id: Identifier
    review_request_digest: Sha256Digest
    decision_id: Identifier
    decision_digest: Sha256Digest
    outcome: FinalReviewOutcome
    next_action: FinalReviewNextAction
    candidate_accepted: bool
    run_id: Identifier
    thread_id: Identifier
    request_id: Identifier
    manifest_digest: Sha256Digest
    change_set_digest: Sha256Digest
    validation_report_digest: Sha256Digest
    source_revision: Sha256Digest
    reviewer: ActorIdentifier
    reviewer_identity_assurance: Literal["declarative_unverified"] = "declarative_unverified"
    decided_at: datetime
    external_actions_authorized: tuple[()] = ()
    source_mutated: Literal[False] = False
    deployment_performed: Literal[False] = False
    publication_performed: Literal[False] = False

    @field_validator("decided_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("final-review timestamps must be timezone-aware UTC values")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> FinalReviewRecord:
        expected = {
            "accepted": (True, "separate_external_action_required"),
            "rejected": (False, "stop_request"),
            "changes_requested": (False, "revise_and_start_new_review"),
        }[self.outcome]
        if (self.candidate_accepted, self.next_action) != expected:
            raise ValueError("final-review outcome does not match its next action")
        return self


class FinalReviewStatus(StrictModel):
    """Provider-free status projection for the final-review checkpoint."""

    schema_version: Literal["1.0"] = "1.0"
    review_id: Identifier
    run_id: Identifier
    thread_id: Identifier
    request_id: Identifier
    status: Literal[
        "awaiting_final_review",
        "accepted",
        "rejected",
        "changes_requested",
    ]
    request_digest: Sha256Digest
    decision_digest: Sha256Digest | None = None
    candidate_accepted: bool | None = None
    external_actions_authorized: tuple[()] = ()


class _FinalReviewLifecycleAnchor(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    lifecycle_kind: Identifier
    run_id: Identifier
    thread_id: Identifier
    request_digest: Sha256Digest
    lifecycle_index_digest: Sha256Digest


class _CompletedAgentRunLifecycleAnchor(StrictModel):
    """Canonical shape used by the agent-run operation lifecycle."""

    schema_version: Literal["1.0"] = "1.0"
    lifecycle_kind: Identifier
    run_id: Identifier
    thread_id: Identifier
    request_digest: Sha256Digest
    lifecycle_index_digest: Sha256Digest


class _FinalReviewRoleEvidence(StrictModel):
    """Digests derived from the canonical persisted three-role handoff."""

    architect_context_digest: Sha256Digest
    dependency_graph_digest: Sha256Digest
    wiki_trace_digest: Sha256Digest
    scope_policy_digest: Sha256Digest
    architect_run_digest: Sha256Digest
    engineer_run_digest: Sha256Digest
    validator_assessment_digest: Sha256Digest


def request_final_review(
    session: AgentRunSession,
    request: MigrationRequest,
    manifest: MigrationManifest,
    change_set: ChangeSet,
    report: ValidationReport,
    *,
    requester: str,
    designated_reviewer: str,
    requested_at: datetime,
    expires_at: datetime,
) -> FinalReviewRequest:
    """Persist the exact final-review request for a terminally passing run."""

    if _artifact_exists(session, FINAL_REVIEW_REQUEST_PATH):
        raise PolicyViolation("final review has already been requested for this run")
    role_evidence = _validate_completed_run(session, request, manifest, change_set, report)
    now = datetime.now(UTC)
    if requested_at > now + timedelta(minutes=5):
        raise PolicyViolation("final-review request time cannot be in the future")
    if expires_at <= now:
        raise PolicyViolation("final-review request is already expired")

    lifecycle_kind, lifecycle_digest = _completed_lifecycle(session)
    required = tuple(result for result in report.results if result.required)
    receipt_digests = tuple(
        artifact_digest(result.receipt) for result in required if result.receipt is not None
    )
    values = {
        "run_id": session.context.run_id,
        "thread_id": session.context.thread_id,
        "request_digest": artifact_digest(request),
        "manifest_digest": artifact_digest(manifest),
        "change_set_digest": artifact_digest(change_set),
        "validation_report_digest": artifact_digest(report),
        "architect_context_digest": role_evidence.architect_context_digest,
        "dependency_graph_digest": role_evidence.dependency_graph_digest,
        "wiki_trace_digest": role_evidence.wiki_trace_digest,
        "scope_policy_digest": role_evidence.scope_policy_digest,
        "architect_run_digest": role_evidence.architect_run_digest,
        "engineer_run_digest": role_evidence.engineer_run_digest,
        "validator_assessment_digest": role_evidence.validator_assessment_digest,
        "completed_lifecycle_kind": lifecycle_kind,
        "completed_lifecycle_index_digest": lifecycle_digest,
        "requester": requester,
        "designated_reviewer": designated_reviewer,
        "requested_at": requested_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    review = FinalReviewRequest(
        review_id=_stable_id("final-review", values),
        run_id=session.context.run_id,
        thread_id=session.context.thread_id,
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        change_set_id=change_set.change_set_id,
        validation_report_id=report.report_id,
        platform=request.platform,
        source_revision=session.context.source_revision,
        session_context_digest=artifact_digest(session.context),
        request_digest=artifact_digest(request),
        manifest_digest=artifact_digest(manifest),
        change_set_digest=artifact_digest(change_set),
        validation_report_digest=artifact_digest(report),
        architect_context_digest=role_evidence.architect_context_digest,
        dependency_graph_digest=role_evidence.dependency_graph_digest,
        wiki_trace_digest=role_evidence.wiki_trace_digest,
        scope_policy_digest=role_evidence.scope_policy_digest,
        architect_run_digest=role_evidence.architect_run_digest,
        engineer_run_digest=role_evidence.engineer_run_digest,
        validator_assessment_digest=role_evidence.validator_assessment_digest,
        required_command_ids=tuple(result.command_id for result in required),
        required_receipt_digests=receipt_digests,
        changed_paths=change_set.changed_paths,
        completed_attempt=report.attempt,
        agent_definition_digests=session.context.agent_definition_digests,
        provider_id=session.context.provider_id,
        model_id=session.context.model_id,
        completed_lifecycle_kind=lifecycle_kind,
        completed_lifecycle_index_digest=lifecycle_digest,
        requester=requester,
        designated_reviewer=designated_reviewer,
        requested_at=requested_at,
        expires_at=expires_at,
    )
    session.validate_portable_evidence(review)
    session.store.write_json(FINAL_REVIEW_REQUEST_PATH, review)
    _freeze_lifecycle(session, FINAL_REVIEW_REQUESTED_KIND, artifact_digest(review))
    return review


def request_final_review_for_run(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    thread_id: str,
    requester: str,
    designated_reviewer: str,
    requested_at: datetime,
    expires_at: datetime,
) -> FinalReviewRequest:
    """Derive a review request exclusively from one canonical completed run."""

    from legacy_migration_agent.application.agent_run import get_agent_run_status

    status = get_agent_run_status(
        project_root,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
    )
    if (
        status.status != "completed"
        or status.terminal_disposition != ValidationDisposition.READY_FOR_HUMAN_REVIEW.value
        or status.failure is not None
        or status.pending_nodes
    ):
        raise PolicyViolation("final review requires one completed ready-for-review agent run")
    session = AgentRunSession.load(project_root, run_dir)
    request = MigrationRequest.model_validate(session.store.read_json("request.json"))
    root = f"model-runs/{request.request_id}"
    try:
        architect = ArchitectRun.model_validate(session.store.read_json(f"{root}/architect.json"))
        engineer = EngineerRun.model_validate(
            session.store.read_json(f"{root}/engineer-attempt-{status.execution_attempt}.json")
        )
        report = ValidationReport.model_validate(
            session.store.read_json(f"{root}/report-attempt-{status.execution_attempt}.json")
        )
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation("completed run lacks canonical final-review artifacts") from exc
    if engineer.change_set is None:
        raise PolicyViolation("completed run has no Engineer change set for final review")
    return request_final_review(
        session,
        request,
        architect.proposal.manifest,
        engineer.change_set,
        report,
        requester=requester,
        designated_reviewer=designated_reviewer,
        requested_at=requested_at,
        expires_at=expires_at,
    )


def decide_final_review(
    session: AgentRunSession,
    *,
    reviewer: str,
    selection: FinalReviewSelection,
    decided_at: datetime,
    comment: str = "",
) -> FinalReviewRecord:
    """Consume the pending final-review request exactly once."""

    if _artifact_exists(session, FINAL_REVIEW_DECISION_PATH) or _artifact_exists(
        session, FINAL_REVIEW_RECORD_PATH
    ):
        raise PolicyViolation("final review has already been decided")
    request = _load_request(session)
    _verify_lifecycle(
        session,
        FINAL_REVIEW_REQUESTED_KIND,
        artifact_digest(request),
        exact=False,
    )
    now = datetime.now(UTC)
    if decided_at > now + timedelta(minutes=5):
        raise PolicyViolation("final-review decision time cannot be in the future")
    if decided_at < request.requested_at:
        raise PolicyViolation("final-review decision cannot predate its request")
    if decided_at > request.expires_at or now > request.expires_at:
        raise PolicyViolation("final-review request has expired")
    if reviewer != request.designated_reviewer:
        raise PolicyViolation("final review cannot be transferred to another reviewer")
    if reviewer == request.requester:
        raise PolicyViolation("final-review requester cannot review their own candidate")

    decision_values = {
        "review_id": request.review_id,
        "review_request_digest": artifact_digest(request),
        "selection": selection,
        "reviewer": reviewer,
        "decided_at": decided_at.isoformat(),
        "comment": comment,
    }
    decision = FinalReviewDecision(
        decision_id=_stable_id("final-review-decision", decision_values),
        review_id=request.review_id,
        review_request_digest=artifact_digest(request),
        selection=selection,
        reviewer=reviewer,
        decided_at=decided_at,
        comment=comment,
    )
    session.validate_portable_evidence(decision)

    if selection == "accept":
        outcome: FinalReviewOutcome = "accepted"
        accepted = True
        next_action: FinalReviewNextAction = "separate_external_action_required"
    elif selection == "reject":
        outcome = "rejected"
        accepted = False
        next_action = "stop_request"
    else:
        outcome = "changes_requested"
        accepted = False
        next_action = "revise_and_start_new_review"
    record_values = {
        "review_id": request.review_id,
        "decision_digest": artifact_digest(decision),
        "outcome": outcome,
    }
    record = FinalReviewRecord(
        record_id=_stable_id("final-review-record", record_values),
        review_id=request.review_id,
        review_request_digest=artifact_digest(request),
        decision_id=decision.decision_id,
        decision_digest=artifact_digest(decision),
        outcome=outcome,
        next_action=next_action,
        candidate_accepted=accepted,
        run_id=request.run_id,
        thread_id=request.thread_id,
        request_id=request.request_id,
        manifest_digest=request.manifest_digest,
        change_set_digest=request.change_set_digest,
        validation_report_digest=request.validation_report_digest,
        source_revision=request.source_revision,
        reviewer=reviewer,
        decided_at=decided_at,
    )
    session.validate_portable_evidence(record)
    session.store.write_json(FINAL_REVIEW_DECISION_PATH, decision)
    session.store.write_json(FINAL_REVIEW_RECORD_PATH, record)
    _freeze_lifecycle(session, FINAL_REVIEW_DECIDED_KIND, artifact_digest(record))
    return record


def decide_final_review_for_run(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    thread_id: str,
    reviewer: str,
    selection: FinalReviewSelection,
    decided_at: datetime,
    comment: str = "",
) -> FinalReviewRecord:
    """Load the exact run identity and consume its pending review once."""

    session = AgentRunSession.load(project_root, run_dir)
    if session.context.run_id != run_id or session.context.thread_id != thread_id:
        raise PolicyViolation("final-review run or thread identity does not match")
    return decide_final_review(
        session,
        reviewer=reviewer,
        selection=selection,
        decided_at=decided_at,
        comment=comment,
    )


def get_final_review_status_for_run(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    thread_id: str,
) -> FinalReviewStatus:
    """Load an exact run and return its provider-free final-review status."""

    session = AgentRunSession.load(project_root, run_dir)
    if session.context.run_id != run_id or session.context.thread_id != thread_id:
        raise PolicyViolation("final-review run or thread identity does not match")
    return get_final_review_status(session)


def get_final_review_status(session: AgentRunSession) -> FinalReviewStatus:
    """Read final-review state without a provider call or external action."""

    request = _load_request(session)
    if session.has_runtime_anchor(FINAL_REVIEW_DECIDED_KIND):
        record = _load_record(session)
        _verify_lifecycle(
            session,
            FINAL_REVIEW_DECIDED_KIND,
            artifact_digest(record),
            exact=True,
        )
        return FinalReviewStatus(
            review_id=request.review_id,
            run_id=request.run_id,
            thread_id=request.thread_id,
            request_id=request.request_id,
            status=record.outcome,
            request_digest=artifact_digest(request),
            decision_digest=record.decision_digest,
            candidate_accepted=record.candidate_accepted,
        )
    _verify_lifecycle(
        session,
        FINAL_REVIEW_REQUESTED_KIND,
        artifact_digest(request),
        exact=True,
    )
    return FinalReviewStatus(
        review_id=request.review_id,
        run_id=request.run_id,
        thread_id=request.thread_id,
        request_id=request.request_id,
        status="awaiting_final_review",
        request_digest=artifact_digest(request),
    )


def _validate_completed_run(
    session: AgentRunSession,
    request: MigrationRequest,
    manifest: MigrationManifest,
    change_set: ChangeSet,
    report: ValidationReport,
) -> _FinalReviewRoleEvidence:
    session.verify_source_revision()
    if artifact_digest(request) != session.context.request_digest:
        raise PolicyViolation("final review request does not match the run session")
    if request.base_revision != session.context.source_revision:
        raise PolicyViolation("final review source revision does not match the run session")
    validate_manifest_for_request(manifest, request)
    validate_change_set(change_set, manifest)
    validate_report(report, manifest, change_set)
    if report.disposition is not ValidationDisposition.READY_FOR_HUMAN_REVIEW:
        raise PolicyViolation("final review requires ready_for_human_review evidence")
    required = tuple(result for result in report.results if result.required)
    if not required or any(result.receipt is None for result in required):
        raise PolicyViolation("final review requires terminal receipts for every required check")
    session.validate_portable_evidence(request)
    session.validate_portable_evidence(manifest)
    session.validate_portable_evidence(change_set)
    session.validate_portable_evidence(report)
    return _validate_persisted_role_artifacts(
        session,
        request,
        manifest,
        change_set,
        report,
    )


def _validate_persisted_role_artifacts(
    session: AgentRunSession,
    request: MigrationRequest,
    manifest: MigrationManifest,
    change_set: ChangeSet,
    report: ValidationReport,
) -> _FinalReviewRoleEvidence:
    root = f"model-runs/{request.request_id}"
    try:
        context = ArchitectContext.model_validate(
            session.store.read_json(f"{root}/architect-context.json")
        )
        graph_payload = session.store.read_json(f"{root}/dependency-graph.json")
        wiki_payload = session.store.read_json(f"{root}/wiki-trace.json")
        architect = ArchitectRun.model_validate(session.store.read_json(f"{root}/architect.json"))
        engineer = EngineerRun.model_validate(
            session.store.read_json(f"{root}/engineer-attempt-{report.attempt}.json")
        )
        persisted_report = ValidationReport.model_validate(
            session.store.read_json(f"{root}/report-attempt-{report.attempt}.json")
        )
        validator = ValidatorAssessment.model_validate(
            session.store.read_json(f"{root}/validator-attempt-{report.attempt}.json")
        )
    except FileNotFoundError as exc:
        raise PolicyViolation(
            "final review requires persisted Architect inputs and all three role artifacts"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("persisted final-review role evidence is malformed") from exc

    if context.request != request:
        raise PolicyViolation("final-review Architect context differs from the request")
    if context.dependency_graph.model_dump(mode="json") != graph_payload:
        raise PolicyViolation("final-review dependency graph differs from Architect context")
    if context.wiki_trace.model_dump(mode="json") != wiki_payload:
        raise PolicyViolation("final-review Wiki trace differs from Architect context")
    validate_architect_proposal(architect.proposal, context)
    if architect.proposal.manifest != manifest:
        raise PolicyViolation("final-review manifest differs from the Architect artifact")
    if engineer.change_set != change_set:
        raise PolicyViolation("final-review change set differs from the Engineer artifact")
    if persisted_report != report:
        raise PolicyViolation("final-review report differs from deterministic evidence")
    advisory = validator.advisory
    validator_context = ValidatorEvidenceContext.freeze(manifest, change_set, report)
    if (
        advisory.manifest_digest != artifact_digest(manifest)
        or advisory.change_set_digest != artifact_digest(change_set)
        or advisory.report_digest != artifact_digest(report)
        or validator.authoritative_disposition is not report.disposition
        or not validator.all_required_checks_terminal_and_passed
    ):
        raise PolicyViolation("final-review Validator evidence does not bind the exact report")
    calls = (
        (architect.model_call, session.context.agent_definition_digests.architect),
        (engineer.model_call, session.context.agent_definition_digests.engineer),
        (validator.model_call, session.context.agent_definition_digests.validator),
    )
    for call, expected_definition_digest in calls:
        if (
            call.provider != session.context.provider_id
            or call.model_id != session.context.model_id
            or call.agent_definition_digest != expected_definition_digest
        ):
            raise PolicyViolation("final-review role identity differs from the run session")
        if call.live_invocation and not call.store_false_sent:
            raise PolicyViolation("final-review live model evidence lacks storage control")
    expected_model_digests = (
        (architect.model_call.input_digest, artifact_digest(context)),
        (architect.model_call.output_digest, artifact_digest(architect.proposal)),
        (engineer.model_call.output_digest, artifact_digest(engineer.model_outcome)),
        (validator.model_call.input_digest, artifact_digest(validator_context)),
        (validator.model_call.output_digest, artifact_digest(validator.advisory)),
    )
    if any(actual != expected for actual, expected in expected_model_digests):
        raise PolicyViolation("final-review model-call evidence differs from persisted handoffs")
    return _FinalReviewRoleEvidence(
        architect_context_digest=artifact_digest(context),
        dependency_graph_digest=artifact_digest(context.dependency_graph),
        wiki_trace_digest=artifact_digest(context.wiki_trace),
        scope_policy_digest=context.platform_adapter.scope_policy_digest,
        architect_run_digest=artifact_digest(architect),
        engineer_run_digest=artifact_digest(engineer),
        validator_assessment_digest=artifact_digest(validator),
    )


def _completed_lifecycle(session: AgentRunSession) -> tuple[str, Sha256Digest]:
    for kind in ("agent-run-retried", "agent-run-resumed", "agent-run-planned"):
        if not session.has_runtime_anchor(kind):
            continue
        session.verify_index(kind, exact=True)
        index_payload = session.store.read_json(f"indexes/{kind}.json")
        session.verify_runtime_anchor(
            kind,
            _CompletedAgentRunLifecycleAnchor(
                lifecycle_kind=kind,
                run_id=session.context.run_id,
                thread_id=session.context.thread_id,
                request_digest=session.context.request_digest,
                lifecycle_index_digest=artifact_digest(index_payload),
            ),
        )
        return kind, artifact_digest(index_payload)
    raise PolicyViolation("final review requires a completed immutable agent-run lifecycle")


def _freeze_lifecycle(
    session: AgentRunSession,
    kind: str,
    evidence_digest: Sha256Digest,
) -> None:
    session.write_index(kind)
    index_payload = session.store.read_json(f"indexes/{kind}.json")
    anchor = _FinalReviewLifecycleAnchor(
        lifecycle_kind=kind,
        run_id=session.context.run_id,
        thread_id=session.context.thread_id,
        request_digest=evidence_digest,
        lifecycle_index_digest=artifact_digest(index_payload),
    )
    session.bind_runtime_anchor(kind, anchor)
    session.verify_index(kind, exact=True)
    session.verify_runtime_anchor(kind, anchor)


def _verify_lifecycle(
    session: AgentRunSession,
    kind: str,
    evidence_digest: Sha256Digest,
    *,
    exact: bool,
) -> None:
    session.verify_index(kind, exact=exact)
    index_payload = session.store.read_json(f"indexes/{kind}.json")
    session.verify_runtime_anchor(
        kind,
        _FinalReviewLifecycleAnchor(
            lifecycle_kind=kind,
            run_id=session.context.run_id,
            thread_id=session.context.thread_id,
            request_digest=evidence_digest,
            lifecycle_index_digest=artifact_digest(index_payload),
        ),
    )


def _load_request(session: AgentRunSession) -> FinalReviewRequest:
    try:
        request = FinalReviewRequest.model_validate(
            session.store.read_json(FINAL_REVIEW_REQUEST_PATH)
        )
    except FileNotFoundError as exc:
        raise PolicyViolation("run has no pending final-review request") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("final-review request evidence is malformed") from exc
    if request.run_id != session.context.run_id or request.thread_id != session.context.thread_id:
        raise PolicyViolation("final-review request belongs to another run or thread")
    if request.session_context_digest != artifact_digest(session.context):
        raise PolicyViolation("final-review request session binding does not match")
    return request


def _load_record(session: AgentRunSession) -> FinalReviewRecord:
    try:
        record = FinalReviewRecord.model_validate(session.store.read_json(FINAL_REVIEW_RECORD_PATH))
    except FileNotFoundError as exc:
        raise PolicyViolation("final-review decision lifecycle is incomplete") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("final-review record evidence is malformed") from exc
    return record


def _artifact_exists(session: AgentRunSession, path: str) -> bool:
    try:
        session.store.read_json(path)
    except FileNotFoundError:
        return False
    return True


def _stable_id(prefix: str, value: object) -> str:
    return f"{prefix}-{artifact_digest(value).removeprefix('sha256:')[:32]}"


__all__ = [
    "FINAL_REVIEW_DECIDED_KIND",
    "FINAL_REVIEW_POLICY_VERSION",
    "FINAL_REVIEW_REQUESTED_KIND",
    "FinalReviewDecision",
    "FinalReviewRecord",
    "FinalReviewRequest",
    "FinalReviewStatus",
    "decide_final_review",
    "decide_final_review_for_run",
    "get_final_review_status",
    "get_final_review_status_for_run",
    "request_final_review",
    "request_final_review_for_run",
]
