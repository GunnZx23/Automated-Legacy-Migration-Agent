"""Provider-free contracts and query boundary for final-review state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from legacy_migration_agent.contracts import (
    ActorIdentifier,
    Identifier,
    Platform,
    Sha256Digest,
    StrictModel,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
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


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("final-review timestamps must be timezone-aware UTC values")
    return value


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
    options: tuple[FinalReviewSelection, ...] = ("accept", "reject", "request_changes")
    authority_granted: Literal[False] = False

    _requested_at_utc = field_validator("requested_at")(_require_utc)
    _expires_at_utc = field_validator("expires_at")(_require_utc)

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

    _decided_at_utc = field_validator("decided_at")(_require_utc)


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

    _decided_at_utc = field_validator("decided_at")(_require_utc)

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
        "expired",
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


def read_final_review_status(
    session: AgentRunSession,
    *,
    now: datetime | None = None,
) -> FinalReviewStatus:
    """Verify and project final-review state without importing agent execution."""

    request = load_final_review_request(session)
    if session.has_runtime_anchor(FINAL_REVIEW_DECIDED_KIND):
        record = load_final_review_record(session)
        verify_final_review_lifecycle(
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
    verify_final_review_lifecycle(
        session,
        FINAL_REVIEW_REQUESTED_KIND,
        artifact_digest(request),
        exact=True,
    )
    current = datetime.now(UTC) if now is None else now
    return FinalReviewStatus(
        review_id=request.review_id,
        run_id=request.run_id,
        thread_id=request.thread_id,
        request_id=request.request_id,
        status="expired" if current > request.expires_at else "awaiting_final_review",
        request_digest=artifact_digest(request),
    )


def load_final_review_request(session: AgentRunSession) -> FinalReviewRequest:
    try:
        request = FinalReviewRequest.model_validate(
            session.store.read_json(FINAL_REVIEW_REQUEST_PATH)
        )
    except FileNotFoundError as exc:
        raise PolicyViolation("run has no pending final-review request") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("final-review request evidence is malformed") from exc
    session.validate_portable_evidence(request)
    if request.run_id != session.context.run_id or request.thread_id != session.context.thread_id:
        raise PolicyViolation("final-review request belongs to another run or thread")
    if request.session_context_digest != artifact_digest(session.context):
        raise PolicyViolation("final-review request session binding does not match")
    return request


def load_final_review_record(session: AgentRunSession) -> FinalReviewRecord:
    try:
        record = FinalReviewRecord.model_validate(session.store.read_json(FINAL_REVIEW_RECORD_PATH))
    except FileNotFoundError as exc:
        raise PolicyViolation("final-review decision lifecycle is incomplete") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("final-review record evidence is malformed") from exc
    session.validate_portable_evidence(record)
    return record


def verify_final_review_lifecycle(
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


def freeze_final_review_lifecycle(
    session: AgentRunSession,
    kind: str,
    evidence_digest: Sha256Digest,
) -> None:
    """Freeze a final-review lifecycle using the shared query contract."""

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


__all__ = [
    "FINAL_REVIEW_DECIDED_KIND",
    "FINAL_REVIEW_DECISION_PATH",
    "FINAL_REVIEW_POLICY_VERSION",
    "FINAL_REVIEW_RECORD_PATH",
    "FINAL_REVIEW_REQUESTED_KIND",
    "FINAL_REVIEW_REQUEST_PATH",
    "MAX_REVIEW_WINDOW",
    "FinalReviewDecision",
    "FinalReviewNextAction",
    "FinalReviewOutcome",
    "FinalReviewRecord",
    "FinalReviewRequest",
    "FinalReviewSelection",
    "FinalReviewStatus",
    "freeze_final_review_lifecycle",
    "load_final_review_record",
    "load_final_review_request",
    "read_final_review_status",
    "verify_final_review_lifecycle",
]
