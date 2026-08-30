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

from legacy_migration_agent.application.final_review_status import (
    FINAL_REVIEW_DECIDED_KIND,
    FINAL_REVIEW_DECISION_PATH,
    FINAL_REVIEW_POLICY_VERSION,
    FINAL_REVIEW_RECORD_PATH,
    FINAL_REVIEW_REQUEST_PATH,
    FINAL_REVIEW_REQUESTED_KIND,
    FinalReviewDecision,
    FinalReviewNextAction,
    FinalReviewOutcome,
    FinalReviewRecord,
    FinalReviewRequest,
    FinalReviewSelection,
    FinalReviewStatus,
    freeze_final_review_lifecycle,
    load_final_review_request,
    read_final_review_status,
    verify_final_review_lifecycle,
)
from legacy_migration_agent.application.run_query import (
    VerifiedRunSnapshot,
    load_verified_run_snapshot,
)
from legacy_migration_agent.contracts import (
    ChangeSet,
    Identifier,
    MigrationManifest,
    MigrationRequest,
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
from legacy_migration_agent.core.run_session import AgentRunSession


class _CompletedAgentRunLifecycleAnchor(StrictModel):
    """Canonical shape used by the agent-run operation lifecycle."""

    schema_version: Literal["1.0"] = "1.0"
    lifecycle_kind: Identifier
    run_id: Identifier
    thread_id: Identifier
    request_digest: Sha256Digest
    lifecycle_index_digest: Sha256Digest


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

    return _request_final_review(
        session,
        request,
        manifest,
        change_set,
        report,
        requester=requester,
        designated_reviewer=designated_reviewer,
        requested_at=requested_at,
        expires_at=expires_at,
        verified_snapshot=None,
    )


def _request_final_review(
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
    verified_snapshot: VerifiedRunSnapshot | None,
) -> FinalReviewRequest:
    """Implement final review with an optional already-verified read snapshot."""

    if session.has_runtime_anchor(FINAL_REVIEW_REQUESTED_KIND):
        raise PolicyViolation("final review has already been requested for this run")
    existing_request = _load_optional_request(session)
    if existing_request is not None:
        if (
            existing_request.requester != requester
            or existing_request.designated_reviewer != designated_reviewer
        ):
            raise PolicyViolation("incomplete final-review request differs from the retry")
        # The first immutable request owns its timestamps. A retried UI request
        # can therefore resume after an interrupted lifecycle write even when
        # the server generated a new current timestamp for the retry.
        requested_at = existing_request.requested_at
        expires_at = existing_request.expires_at
    role_evidence = _validate_completed_run(
        session,
        request,
        manifest,
        change_set,
        report,
        verified_snapshot=verified_snapshot,
    )
    now = datetime.now(UTC)
    if requested_at > now + timedelta(minutes=5):
        raise PolicyViolation("final-review request time cannot be in the future")
    if existing_request is None and expires_at <= now:
        raise PolicyViolation("final-review request is already expired")

    lifecycle_kind, lifecycle_digest = _completed_lifecycle(
        session,
        exact=existing_request is None,
    )
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
        **(
            {}
            if role_evidence.graph_assurance_report_digest is None
            else {
                "graph_assurance_report_digest": (role_evidence.graph_assurance_report_digest),
                "graph_assurance_status": "assured",
            }
        ),
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
    graph_assurance_status: Literal["assured"] | None = (
        "assured" if role_evidence.graph_assurance_report is not None else None
    )
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
        graph_assurance_report_digest=role_evidence.graph_assurance_report_digest,
        graph_assurance_status=graph_assurance_status,
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
    if existing_request is not None and existing_request != review:
        raise PolicyViolation("incomplete final-review request differs from the retry")
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
    try:
        snapshot = load_verified_run_snapshot(session, attempt=status.execution_attempt)
    except PolicyViolation as exc:
        raise PolicyViolation("completed run lacks canonical final-review artifacts") from exc
    return _request_final_review(
        session,
        snapshot.request,
        snapshot.manifest,
        snapshot.change_set,
        snapshot.report,
        requester=requester,
        designated_reviewer=designated_reviewer,
        requested_at=requested_at,
        expires_at=expires_at,
        verified_snapshot=snapshot,
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

    if session.has_runtime_anchor(FINAL_REVIEW_DECIDED_KIND):
        raise PolicyViolation("final review has already been decided")
    existing_decision = _load_optional_decision(session)
    existing_record = _load_optional_record(session)
    if existing_record is not None and existing_decision is None:
        raise PolicyViolation("final-review decision lifecycle is incomplete")
    if existing_decision is not None:
        if (
            existing_decision.selection != selection
            or existing_decision.reviewer != reviewer
            or existing_decision.comment != comment
        ):
            raise PolicyViolation("incomplete final-review decision differs from the retry")
        decided_at = existing_decision.decided_at
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
    expired = decided_at > request.expires_at or now > request.expires_at
    if expired and selection == "accept":
        raise PolicyViolation("expired final-review request cannot be accepted")
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
    if existing_decision is not None and existing_decision != decision:
        raise PolicyViolation("incomplete final-review decision differs from the retry")

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
    if existing_record is not None and existing_record != record:
        raise PolicyViolation("incomplete final-review record differs from the retry")
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

    return read_final_review_status(session, now=datetime.now(UTC))


def _validate_completed_run(
    session: AgentRunSession,
    request: MigrationRequest,
    manifest: MigrationManifest,
    change_set: ChangeSet,
    report: ValidationReport,
    *,
    verified_snapshot: VerifiedRunSnapshot | None,
) -> VerifiedRunSnapshot:
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
    snapshot = verified_snapshot or load_verified_run_snapshot(session, attempt=report.attempt)
    if snapshot.request != request:
        raise PolicyViolation("final-review Architect context differs from the request")
    if snapshot.manifest != manifest:
        raise PolicyViolation("final-review manifest differs from the Architect artifact")
    if snapshot.change_set != change_set:
        raise PolicyViolation("final-review change set differs from the Engineer artifact")
    if snapshot.report != report:
        raise PolicyViolation("final-review report differs from deterministic evidence")
    if not snapshot.validator.all_required_checks_terminal_and_passed:
        raise PolicyViolation("final-review Validator evidence does not bind the exact report")
    return snapshot


def _completed_lifecycle(
    session: AgentRunSession,
    *,
    exact: bool = True,
) -> tuple[str, Sha256Digest]:
    for kind in ("agent-run-retried", "agent-run-resumed", "agent-run-planned"):
        if not session.has_runtime_anchor(kind):
            continue
        session.verify_index(kind, exact=exact)
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
    freeze_final_review_lifecycle(session, kind, evidence_digest)


def _verify_lifecycle(
    session: AgentRunSession,
    kind: str,
    evidence_digest: Sha256Digest,
    *,
    exact: bool,
) -> None:
    verify_final_review_lifecycle(session, kind, evidence_digest, exact=exact)


def _load_request(session: AgentRunSession) -> FinalReviewRequest:
    return load_final_review_request(session)


def _load_optional_request(session: AgentRunSession) -> FinalReviewRequest | None:
    try:
        return _load_request(session)
    except PolicyViolation as exc:
        try:
            session.store.read_json(FINAL_REVIEW_REQUEST_PATH)
        except FileNotFoundError:
            return None
        raise exc


def _load_optional_decision(session: AgentRunSession) -> FinalReviewDecision | None:
    try:
        payload = session.store.read_json(FINAL_REVIEW_DECISION_PATH)
    except FileNotFoundError:
        return None
    try:
        return FinalReviewDecision.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("final-review decision evidence is malformed") from exc


def _load_optional_record(session: AgentRunSession) -> FinalReviewRecord | None:
    try:
        payload = session.store.read_json(FINAL_REVIEW_RECORD_PATH)
    except FileNotFoundError:
        return None
    try:
        return FinalReviewRecord.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("final-review record evidence is malformed") from exc


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
    "FINAL_REVIEW_DECISION_PATH",
    "FINAL_REVIEW_POLICY_VERSION",
    "FINAL_REVIEW_RECORD_PATH",
    "FINAL_REVIEW_REQUEST_PATH",
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
