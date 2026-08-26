"""Governed, append-only cross-run knowledge for local migration sessions.

Only :class:`KnowledgeStoreController` can append promotion or invalidation
events.  Promotion is derived from an exact, runtime-anchored
``AgentRunSession`` evidence package whose validation and independent human
review both succeeded.  The store persists concise structured facts and
digest-only provenance; it never persists prompts, model input, candidate
source, credentials, or absolute local paths.

This module is deliberately local and deterministic.  It performs no network,
model, provider, Git, deployment, or publication operation.
"""

from __future__ import annotations

import fcntl
import os
import re
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, TypeAdapter, ValidationError, field_validator, model_validator

from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectContext,
    ArchitectRun,
    EngineerRun,
    ValidatorAssessment,
)
from legacy_migration_agent.application.final_review import (
    FINAL_REVIEW_DECIDED_KIND,
    FINAL_REVIEW_DECISION_PATH,
    FINAL_REVIEW_RECORD_PATH,
    FINAL_REVIEW_REQUEST_PATH,
    FINAL_REVIEW_REQUESTED_KIND,
    FinalReviewDecision,
    FinalReviewRecord,
    FinalReviewRequest,
)
from legacy_migration_agent.contracts import (
    ActorIdentifier,
    CheckStatus,
    Identifier,
    MigrationRequest,
    Platform,
    Sha256Digest,
    StrictModel,
    ValidationDisposition,
    ValidationReport,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import artifact_digest, canonical_json_bytes
from legacy_migration_agent.core.policies import (
    PolicyViolation,
    validate_change_set,
    validate_manifest_for_request,
    validate_report,
)
from legacy_migration_agent.core.redaction import SecretRedactor
from legacy_migration_agent.core.run_session import AgentRunContext, AgentRunSession
from legacy_migration_agent.graphs.graph_contracts import DependencyGraph
from legacy_migration_agent.knowledge.wiki import RetrievalTrace

KNOWLEDGE_SCHEMA_VERSION: Literal["1.0"] = "1.0"
KNOWLEDGE_POLICY_VERSION: Literal["knowledge-store/1.0"] = "knowledge-store/1.0"
MAX_SOURCE_EVIDENCE_AGE = timedelta(days=90)
MAX_REVIEW_LAG = timedelta(days=30)
MAX_KNOWLEDGE_TTL = timedelta(days=365)
MAX_FACTS_PER_PROMOTION = 16
MAX_CITATIONS_PER_FACT = 8
MAX_AUDIT_FILE_BYTES = 256 * 1024

EvidenceKind = Literal[
    "migration_request",
    "architect_context",
    "dependency_graph",
    "wiki_trace",
    "architect_run",
    "validation_report",
    "final_review_request",
    "final_review_decision",
    "final_review_record",
]
FactPredicate = Literal[
    "depends_on",
    "incompatible_with",
    "maps_to",
    "preserves",
    "requires",
    "uses",
    "validated_by",
]
ReviewDecision = Literal["approve", "reject"]
CompletedLifecycleKind = Literal[
    "agent-run-planned",
    "agent-run-resumed",
    "agent-run-retried",
]
InvalidationReason = Literal[
    "graph_changed",
    "manual_review",
    "model_changed",
    "policy_changed",
    "source_drift",
    "version_changed",
    "wiki_superseded",
]

_AUDIT_NAME = re.compile(r"^(?P<sequence>[0-9]{20})-(?P<entry_id>[A-Za-z0-9._:-]+)\.json$")
_ABSOLUTE_LOCAL_PATH = re.compile(
    r"(?:/(?:Users|home|root|private|tmp|var|etc|opt|usr|System|Library|Volumes)(?:/|\b))"
    r"|(?:\b[A-Za-z]:[\\/])",
    re.IGNORECASE,
)
_PROMPT_MATERIAL = re.compile(
    r"(?:\b(?:system|developer|assistant|user)\s*:)|"
    r"(?:\bignore\s+(?:all|any|the|previous)\b)|"
    r"(?:\bprompt\b)|(?:\bchain[- ]of[- ]thought\b)",
    re.IGNORECASE,
)
_CANDIDATE_SOURCE = re.compile(
    r"(?:```)|(?:diff --git)|(?:^@@)|(?:<\?xml)|(?:<mule\b)|(?:%dw\s+[12]\.0)|"
    r"(?:\bpublic\s+class\b)|(?:\bexport\s+default\s+class\b)|"
    r"(?:\b(?:SELECT|FIND)\b.+\bFROM\b)|(?:=>)|(?:[{};])",
    re.IGNORECASE,
)
_API_TOKEN = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b")
_FORBIDDEN_EVIDENCE_SEGMENTS = frozenset(
    {"candidate", "expected", "golden", "oracle", "prompt", "prompts", "scratch", "workspaces"}
)
_CONTROLLER_CAPABILITY = object()
_JSON_VALUE_ADAPTER = TypeAdapter(Any)
_AGENT_RUN_EVIDENCE_KIND = "agent-run-initialized"
_AGENT_RUN_REQUEST_PATH = "request.json"
_AGENT_RUN_CONFIG_PATH = "agent-run-config.json"
_COMPLETED_AGENT_RUN_KINDS: tuple[CompletedLifecycleKind, ...] = (
    "agent-run-retried",
    "agent-run-resumed",
    "agent-run-planned",
)


class EvidenceReference(StrictModel):
    """Digest-only reference to one portable run evidence object."""

    kind: EvidenceKind
    evidence_id: str
    digest: Sha256Digest

    @field_validator("evidence_id")
    @classmethod
    def validate_evidence_id(cls, value: str) -> str:
        normalized = validate_relative_path(value)
        if not normalized.endswith(".json"):
            raise ValueError("knowledge evidence IDs must identify JSON artifacts")
        if any(part.casefold() in _FORBIDDEN_EVIDENCE_SEGMENTS for part in normalized.split("/")):
            raise ValueError("knowledge evidence IDs cannot reference candidate or runtime content")
        return normalized


class SuccessfulRunEvidencePackage(StrictModel):
    """Controller-derived successful-run evidence; callers never supply this model."""

    schema_version: Literal["1.0"] = KNOWLEDGE_SCHEMA_VERSION
    package_id: Identifier
    run_id: Identifier
    session_id: Identifier
    request_id: Identifier
    source_revision: Sha256Digest
    session_digest: Sha256Digest
    agent_run_index_digest: Sha256Digest
    completed_lifecycle_kind: CompletedLifecycleKind
    completed_lifecycle_index_digest: Sha256Digest
    final_review_requested_index_digest: Sha256Digest
    final_review_decided_index_digest: Sha256Digest
    completed_attempt: int = Field(ge=1, le=2)
    platform: Platform
    source_version: str = Field(min_length=1, max_length=160)
    target_version: str = Field(min_length=1, max_length=160)
    version_scope_digest: Sha256Digest
    model_identity_digest: Sha256Digest
    scope_policy_digest: Sha256Digest
    migration_request: EvidenceReference
    architect_context: EvidenceReference
    dependency_graph: EvidenceReference
    wiki_trace: EvidenceReference
    architect_run: EvidenceReference
    validation_report: EvidenceReference
    final_review_request: EvidenceReference
    final_review_decision: EvidenceReference
    final_review_record: EvidenceReference
    completed_at: datetime
    reviewed_at: datetime

    @field_validator("source_version", "target_version")
    @classmethod
    def validate_versions(cls, value: str) -> str:
        return _concise_text(value, role="version", maximum=160)

    @field_validator("completed_at", "reviewed_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime, info: Any) -> datetime:
        return _utc_timestamp(value, info.field_name)

    @model_validator(mode="after")
    def bind_package_roles_and_versions(self) -> SuccessfulRunEvidencePackage:
        expected_kinds: tuple[tuple[EvidenceReference, EvidenceKind], ...] = (
            (self.migration_request, "migration_request"),
            (self.architect_context, "architect_context"),
            (self.dependency_graph, "dependency_graph"),
            (self.wiki_trace, "wiki_trace"),
            (self.architect_run, "architect_run"),
            (self.validation_report, "validation_report"),
            (self.final_review_request, "final_review_request"),
            (self.final_review_decision, "final_review_decision"),
            (self.final_review_record, "final_review_record"),
        )
        for reference, expected in expected_kinds:
            if reference.kind != expected:
                raise ValueError(f"knowledge package {expected} reference has the wrong kind")
        paths = tuple(reference.evidence_id for reference, _ in expected_kinds)
        if len(paths) != len(set(paths)):
            raise ValueError("knowledge package evidence IDs must be unique")
        if self.reviewed_at < self.completed_at:
            raise ValueError("knowledge review cannot precede successful validation")
        expected_scope_digest = version_scope_digest(
            self.platform,
            self.source_version,
            self.target_version,
        )
        if self.version_scope_digest != expected_scope_digest:
            raise ValueError("knowledge package version scope digest does not match its versions")
        root = f"model-runs/{self.request_id}"
        expected_paths = {
            "migration_request": _AGENT_RUN_REQUEST_PATH,
            "architect_context": f"{root}/architect-context.json",
            "dependency_graph": f"{root}/dependency-graph.json",
            "wiki_trace": f"{root}/wiki-trace.json",
            "architect_run": f"{root}/architect.json",
            "validation_report": f"{root}/report-attempt-{self.completed_attempt}.json",
            "final_review_request": FINAL_REVIEW_REQUEST_PATH,
            "final_review_decision": FINAL_REVIEW_DECISION_PATH,
            "final_review_record": FINAL_REVIEW_RECORD_PATH,
        }
        for reference, expected_kind in expected_kinds:
            if reference.evidence_id != expected_paths[expected_kind]:
                raise ValueError(
                    f"knowledge package {expected_kind} reference is not the canonical path"
                )
        return self

    @property
    def references(self) -> tuple[EvidenceReference, ...]:
        return (
            self.migration_request,
            self.architect_context,
            self.dependency_graph,
            self.wiki_trace,
            self.architect_run,
            self.validation_report,
            self.final_review_request,
            self.final_review_decision,
            self.final_review_record,
        )


class _AgentRunConfigEvidence(StrictModel):
    schema_version: Literal["1.0"] = KNOWLEDGE_SCHEMA_VERSION
    preset_id: Identifier
    wiki_as_of: date


class _AgentRunEvidenceAnchor(StrictModel):
    schema_version: Literal["1.0"] = KNOWLEDGE_SCHEMA_VERSION
    run_id: Identifier
    thread_id: Identifier
    request_digest: Sha256Digest
    config_digest: Sha256Digest
    lifecycle_index_digest: Sha256Digest


class _LifecycleAnchor(StrictModel):
    schema_version: Literal["1.0"] = KNOWLEDGE_SCHEMA_VERSION
    lifecycle_kind: Identifier
    run_id: Identifier
    thread_id: Identifier
    request_digest: Sha256Digest
    lifecycle_index_digest: Sha256Digest


class KnowledgeCitation(StrictModel):
    """Portable citation to one exact evidence object and optional locator."""

    citation_id: Identifier
    evidence_id: str
    evidence_digest: Sha256Digest
    locator: Identifier | None = None

    @field_validator("evidence_id")
    @classmethod
    def validate_evidence_id(cls, value: str) -> str:
        return EvidenceReference(
            kind="dependency_graph",
            evidence_id=value,
            digest="sha256:" + "0" * 64,
        ).evidence_id


class KnowledgeFact(StrictModel):
    """Concise structured knowledge; raw prose and source code are prohibited."""

    fact_id: Identifier
    subject: str = Field(min_length=1, max_length=160)
    predicate: FactPredicate
    value: str = Field(min_length=1, max_length=240)
    support: Literal["direct", "reviewed_derivation"]
    citations: tuple[KnowledgeCitation, ...] = Field(
        min_length=1,
        max_length=MAX_CITATIONS_PER_FACT,
    )

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        return _concise_text(value, role="fact subject", maximum=160)

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        return _concise_text(value, role="fact value", maximum=240)

    @model_validator(mode="after")
    def require_unique_citations(self) -> KnowledgeFact:
        citation_ids = tuple(citation.citation_id for citation in self.citations)
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("knowledge fact citation IDs must be unique")
        return self


class KnowledgeProvenance(StrictModel):
    """Exact reusable scope and source-run binding copied into every lifecycle record."""

    source_run_id: Identifier
    source_session_id: Identifier
    source_session_digest: Sha256Digest
    source_evidence_package_digest: Sha256Digest
    source_request_digest: Sha256Digest
    source_graph_digest: Sha256Digest
    source_wiki_digest: Sha256Digest
    source_model_digest: Sha256Digest
    source_policy_digest: Sha256Digest
    platform: Platform
    source_version: str = Field(min_length=1, max_length=160)
    target_version: str = Field(min_length=1, max_length=160)
    version_scope_digest: Sha256Digest

    @field_validator("source_version", "target_version")
    @classmethod
    def validate_versions(cls, value: str) -> str:
        return _concise_text(value, role="provenance version", maximum=160)

    @model_validator(mode="after")
    def validate_version_scope(self) -> KnowledgeProvenance:
        expected = version_scope_digest(self.platform, self.source_version, self.target_version)
        if self.version_scope_digest != expected:
            raise ValueError("knowledge provenance version scope digest is invalid")
        return self


class KnowledgePromotionRequest(StrictModel):
    """Controller-generated request to promote exact structured facts."""

    schema_version: Literal["1.0"] = KNOWLEDGE_SCHEMA_VERSION
    policy_version: Literal["knowledge-store/1.0"] = KNOWLEDGE_POLICY_VERSION
    request_id: Identifier
    status: Literal["promotion_requested"] = "promotion_requested"
    requested_action: Literal["promote_cross_run_knowledge"] = "promote_cross_run_knowledge"
    provenance: KnowledgeProvenance
    facts: tuple[KnowledgeFact, ...] = Field(min_length=1, max_length=MAX_FACTS_PER_PROMOTION)
    facts_digest: Sha256Digest
    requested_at: datetime
    expires_at: datetime
    requester: ActorIdentifier
    designated_reviewer: ActorIdentifier

    @field_validator("requested_at", "expires_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime, info: Any) -> datetime:
        return _utc_timestamp(value, info.field_name)

    @model_validator(mode="after")
    def bind_request(self) -> KnowledgePromotionRequest:
        if self.requester == self.designated_reviewer:
            raise ValueError("knowledge promotion requires an independent reviewer")
        if self.expires_at <= self.requested_at:
            raise ValueError("knowledge expiry must follow the promotion request")
        if self.expires_at - self.requested_at > MAX_KNOWLEDGE_TTL:
            raise ValueError("knowledge expiry exceeds the maximum TTL")
        if self.facts_digest != facts_digest(self.facts):
            raise ValueError("knowledge request facts digest is invalid")
        fact_ids = tuple(fact.fact_id for fact in self.facts)
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("knowledge request fact IDs must be unique")
        return self


class KnowledgePromotionDecision(StrictModel):
    """One independent reviewer decision bound to an exact request."""

    schema_version: Literal["1.0"] = KNOWLEDGE_SCHEMA_VERSION
    policy_version: Literal["knowledge-store/1.0"] = KNOWLEDGE_POLICY_VERSION
    decision_id: Identifier
    status: Literal["promotion_decided"] = "promotion_decided"
    decision: ReviewDecision
    requested_action: Literal["promote_cross_run_knowledge"] = "promote_cross_run_knowledge"
    request_id: Identifier
    request_digest: Sha256Digest
    provenance: KnowledgeProvenance
    facts_digest: Sha256Digest
    request_requested_at: datetime
    decided_at: datetime
    requester: ActorIdentifier
    designated_reviewer: ActorIdentifier
    reviewer: ActorIdentifier

    @field_validator("request_requested_at", "decided_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime, info: Any) -> datetime:
        return _utc_timestamp(value, info.field_name)

    @model_validator(mode="after")
    def require_independent_reviewer(self) -> KnowledgePromotionDecision:
        if self.reviewer != self.designated_reviewer:
            raise ValueError("knowledge promotion decision cannot be transferred")
        if self.reviewer == self.requester:
            raise ValueError("knowledge requester cannot review their own facts")
        if self.decided_at < self.request_requested_at:
            raise ValueError("knowledge decision cannot precede its request")
        if self.decided_at - self.request_requested_at > MAX_REVIEW_LAG:
            raise ValueError("knowledge promotion request became stale before review")
        return self


class KnowledgePromotionRecord(StrictModel):
    """Immutable promoted facts and exact approval provenance."""

    schema_version: Literal["1.0"] = KNOWLEDGE_SCHEMA_VERSION
    policy_version: Literal["knowledge-store/1.0"] = KNOWLEDGE_POLICY_VERSION
    record_id: Identifier
    status: Literal["active"] = "active"
    request_id: Identifier
    request_digest: Sha256Digest
    decision_id: Identifier
    decision_digest: Sha256Digest
    provenance: KnowledgeProvenance
    facts: tuple[KnowledgeFact, ...] = Field(min_length=1, max_length=MAX_FACTS_PER_PROMOTION)
    facts_digest: Sha256Digest
    promoted_at: datetime
    expires_at: datetime
    requester: ActorIdentifier
    reviewer: ActorIdentifier

    @field_validator("promoted_at", "expires_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime, info: Any) -> datetime:
        return _utc_timestamp(value, info.field_name)

    @model_validator(mode="after")
    def validate_record(self) -> KnowledgePromotionRecord:
        if self.expires_at <= self.promoted_at:
            raise ValueError("promoted knowledge must expire after promotion")
        if self.facts_digest != facts_digest(self.facts):
            raise ValueError("promoted knowledge facts digest is invalid")
        if self.requester == self.reviewer:
            raise ValueError("promoted knowledge must retain independent review")
        return self


class KnowledgeInvalidationRecord(StrictModel):
    """Immutable invalidation of one exact promotion record."""

    schema_version: Literal["1.0"] = KNOWLEDGE_SCHEMA_VERSION
    policy_version: Literal["knowledge-store/1.0"] = KNOWLEDGE_POLICY_VERSION
    invalidation_id: Identifier
    status: Literal["invalidated"] = "invalidated"
    record_id: Identifier
    record_digest: Sha256Digest
    reason: InvalidationReason
    invalidated_at: datetime
    invalidated_by: ActorIdentifier

    @field_validator("invalidated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc_timestamp(value, "invalidated_at")


class KnowledgeConsumerScope(StrictModel):
    """Exact scope of one later run requesting reusable knowledge."""

    consumer_run_id: Identifier
    consumer_session_id: Identifier
    platform: Platform
    source_version: str = Field(min_length=1, max_length=160)
    target_version: str = Field(min_length=1, max_length=160)
    version_scope_digest: Sha256Digest
    graph_digest: Sha256Digest
    wiki_digest: Sha256Digest
    model_digest: Sha256Digest
    policy_digest: Sha256Digest

    @field_validator("source_version", "target_version")
    @classmethod
    def validate_versions(cls, value: str) -> str:
        return _concise_text(value, role="consumer version", maximum=160)

    @model_validator(mode="after")
    def validate_scope_digest(self) -> KnowledgeConsumerScope:
        expected = version_scope_digest(self.platform, self.source_version, self.target_version)
        if self.version_scope_digest != expected:
            raise ValueError("consumer version scope digest does not match")
        return self


class KnowledgeLookupMatch(StrictModel):
    record_id: Identifier
    record_digest: Sha256Digest
    provenance: KnowledgeProvenance
    promoted_at: datetime
    expires_at: datetime
    fact: KnowledgeFact


class KnowledgeLookupResult(StrictModel):
    schema_version: Literal["1.0"] = KNOWLEDGE_SCHEMA_VERSION
    policy_version: Literal["knowledge-store/1.0"] = KNOWLEDGE_POLICY_VERSION
    consumer_scope: KnowledgeConsumerScope
    as_of: datetime
    matches: tuple[KnowledgeLookupMatch, ...]


class KnowledgeAuditEvent(StrictModel):
    """One immutable entry in the append-only audit index."""

    schema_version: Literal["1.0"] = KNOWLEDGE_SCHEMA_VERSION
    sequence: int = Field(ge=1)
    entry_id: Identifier
    previous_entry_digest: Sha256Digest | None
    event_kind: Literal["promotion", "invalidation"]
    event_at: datetime
    payload_digest: Sha256Digest
    promotion: KnowledgePromotionRecord | None = None
    invalidation: KnowledgeInvalidationRecord | None = None

    @field_validator("event_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc_timestamp(value, "event_at")

    @model_validator(mode="after")
    def validate_event(self) -> KnowledgeAuditEvent:
        if self.event_kind == "promotion":
            if self.promotion is None or self.invalidation is not None:
                raise ValueError("promotion audit event must contain only a promotion record")
            payload: StrictModel = self.promotion
            expected_at = self.promotion.promoted_at
        else:
            if self.invalidation is None or self.promotion is not None:
                raise ValueError(
                    "invalidation audit event must contain only an invalidation record"
                )
            payload = self.invalidation
            expected_at = self.invalidation.invalidated_at
        if self.event_at != expected_at:
            raise ValueError("audit event timestamp does not match its payload")
        if self.payload_digest != artifact_digest(payload):
            raise ValueError("audit event payload digest does not match its payload")
        return self


class GovernedKnowledgeStore:
    """Descriptor-safe local store with an immutable hash-chained audit index."""

    def __init__(
        self,
        project_root: Path,
        store_path: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.project_root = _safe_project_root(project_root)
        self.store_path = validate_relative_path(store_path)
        self.root, self._root_identity, self._audit_identity = _prepare_store(
            self.project_root,
            self.store_path,
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self.audit_index()

    @classmethod
    def open_or_create(
        cls,
        project_root: Path,
        store_path: str = ".knowledge-store",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> GovernedKnowledgeStore:
        return cls(project_root, store_path, clock=clock)

    def controller(self) -> KnowledgeStoreController:
        """Return the only public mutation authority for this store."""

        return KnowledgeStoreController(self)

    def audit_index(self) -> tuple[KnowledgeAuditEvent, ...]:
        """Read and verify the complete append-only hash chain."""

        with self._locked_audit(exclusive=False) as audit_fd:
            return _read_audit_index(audit_fd)

    def lookup(
        self,
        consumer_scope: KnowledgeConsumerScope,
        *,
        as_of: datetime,
    ) -> KnowledgeLookupResult:
        """Return exact-scope facts from prior runs that remain active."""

        try:
            scope = KnowledgeConsumerScope.model_validate(consumer_scope.model_dump(mode="python"))
        except ValidationError as exc:
            raise PolicyViolation(f"invalid knowledge consumer scope: {exc}") from exc
        effective = _utc_timestamp(as_of, "as_of")
        if effective > self._now():
            raise PolicyViolation("knowledge lookup timestamp cannot be in the future")
        events = self.audit_index()
        invalidated = {
            event.invalidation.record_id
            for event in events
            if event.invalidation is not None and event.invalidation.invalidated_at <= effective
        }
        matches: list[KnowledgeLookupMatch] = []
        for event in events:
            record = event.promotion
            if record is None:
                continue
            if record.record_id in invalidated:
                continue
            if record.promoted_at > effective or record.expires_at <= effective:
                continue
            provenance = record.provenance
            if (
                provenance.source_run_id == scope.consumer_run_id
                or provenance.source_session_id == scope.consumer_session_id
            ):
                continue
            if not _scope_matches(provenance, scope):
                continue
            record_digest = artifact_digest(record)
            for fact in record.facts:
                matches.append(
                    KnowledgeLookupMatch(
                        record_id=record.record_id,
                        record_digest=record_digest,
                        provenance=provenance,
                        promoted_at=record.promoted_at,
                        expires_at=record.expires_at,
                        fact=fact,
                    )
                )
        ordered = tuple(
            sorted(
                matches,
                key=lambda item: (item.promoted_at, item.record_id, item.fact.fact_id),
            )
        )
        return KnowledgeLookupResult(
            consumer_scope=scope,
            as_of=effective,
            matches=ordered,
        )

    def _append_promotion(
        self,
        record: KnowledgePromotionRecord,
        capability: object,
    ) -> None:
        if capability is not _CONTROLLER_CAPABILITY:
            raise PolicyViolation("only the knowledge controller can promote facts")
        with self._locked_audit(exclusive=True) as audit_fd:
            events = _read_audit_index(audit_fd)
            if any(
                event.promotion is not None
                and event.promotion.decision_digest == record.decision_digest
                for event in events
            ):
                raise PolicyViolation("knowledge promotion decision has already been consumed")
            if any(
                event.promotion is not None and event.promotion.record_id == record.record_id
                for event in events
            ):
                raise PolicyViolation("knowledge promotion record already exists")
            _append_event(audit_fd, events, promotion=record)

    def _append_invalidation(
        self,
        invalidation: KnowledgeInvalidationRecord,
        capability: object,
    ) -> None:
        if capability is not _CONTROLLER_CAPABILITY:
            raise PolicyViolation("only the knowledge controller can invalidate facts")
        with self._locked_audit(exclusive=True) as audit_fd:
            events = _read_audit_index(audit_fd)
            promotions = {
                event.promotion.record_id: event.promotion
                for event in events
                if event.promotion is not None
            }
            target = promotions.get(invalidation.record_id)
            if target is None:
                raise PolicyViolation("knowledge invalidation target does not exist")
            if invalidation.record_digest != artifact_digest(target):
                raise PolicyViolation("knowledge invalidation target digest does not match")
            if invalidation.invalidated_at < target.promoted_at:
                raise PolicyViolation("knowledge invalidation cannot precede promotion")
            if invalidation.invalidated_by == target.requester:
                raise PolicyViolation("knowledge requester cannot invalidate their own promotion")
            if any(
                event.invalidation is not None
                and event.invalidation.record_id == invalidation.record_id
                for event in events
            ):
                raise PolicyViolation("knowledge record has already been invalidated")
            _append_event(audit_fd, events, invalidation=invalidation)

    def _now(self) -> datetime:
        return _utc_timestamp(self._clock(), "trusted clock")

    @contextmanager
    def _locked_audit(self, *, exclusive: bool) -> Iterator[int]:
        with _open_store_descriptors(
            self.project_root,
            self.store_path,
            self._root_identity,
            self._audit_identity,
        ) as (root_fd, audit_fd):
            _require_exact_root_inventory(root_fd)
            lock_fd = os.open(".lock", os.O_RDWR | _no_follow_flag(), dir_fd=root_fd)
            try:
                details = os.fstat(lock_fd)
                if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600:
                    raise PolicyViolation("knowledge store lock must be a private regular file")
                if details.st_size != 0:
                    raise PolicyViolation("knowledge store lock file must remain empty")
                fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                yield audit_fd
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)


class KnowledgeStoreController:
    """Controller-only promotion, review, invalidation, and lookup orchestration."""

    def __init__(self, store: GovernedKnowledgeStore) -> None:
        self._store = store

    def request_promotion(
        self,
        session: AgentRunSession,
        facts: Sequence[KnowledgeFact],
        *,
        requester: str,
        designated_reviewer: str,
        requested_at: datetime,
        expires_at: datetime,
    ) -> KnowledgePromotionRequest:
        now = self._store._now()
        requested = _require_not_future(requested_at, now, "promotion request")
        if now - requested > MAX_REVIEW_LAG:
            raise PolicyViolation("knowledge promotion request timestamp is stale")
        package = _derive_verified_package(session, now=now)
        if requested < package.reviewed_at:
            raise PolicyViolation("knowledge promotion request cannot precede source review")
        if requested - package.reviewed_at > MAX_SOURCE_EVIDENCE_AGE:
            raise PolicyViolation("source run evidence is stale for knowledge promotion")
        normalized_facts = _validate_facts(facts, package)
        provenance = _provenance(package)
        normalized_expiry = _utc_timestamp(expires_at, "expires_at")
        if normalized_expiry <= now:
            raise PolicyViolation("knowledge expiry must be in the future")
        values = {
            "provenance": provenance,
            "facts_digest": facts_digest(normalized_facts),
            "requested_at": requested,
            "expires_at": normalized_expiry,
            "requester": requester,
            "designated_reviewer": designated_reviewer,
        }
        return KnowledgePromotionRequest(
            request_id=_stable_id("knowledge-request", values),
            provenance=provenance,
            facts=normalized_facts,
            facts_digest=facts_digest(normalized_facts),
            requested_at=requested,
            expires_at=normalized_expiry,
            requester=requester,
            designated_reviewer=designated_reviewer,
        )

    def decide(
        self,
        request: KnowledgePromotionRequest,
        *,
        reviewer: str,
        decision: ReviewDecision,
        decided_at: datetime,
    ) -> KnowledgePromotionDecision:
        request = _validated_model(request, KnowledgePromotionRequest, "promotion request")
        decided = _require_not_future(decided_at, self._store._now(), "promotion decision")
        values = {
            "decision": decision,
            "request_id": request.request_id,
            "request_digest": artifact_digest(request),
            "provenance": request.provenance,
            "facts_digest": request.facts_digest,
            "request_requested_at": request.requested_at,
            "decided_at": decided,
            "requester": request.requester,
            "designated_reviewer": request.designated_reviewer,
            "reviewer": reviewer,
        }
        return KnowledgePromotionDecision(
            decision_id=_stable_id("knowledge-decision", values),
            decision=decision,
            request_id=request.request_id,
            request_digest=artifact_digest(request),
            provenance=request.provenance,
            facts_digest=request.facts_digest,
            request_requested_at=request.requested_at,
            decided_at=decided,
            requester=request.requester,
            designated_reviewer=request.designated_reviewer,
            reviewer=reviewer,
        )

    def promote(
        self,
        session: AgentRunSession,
        request: KnowledgePromotionRequest,
        decision: KnowledgePromotionDecision,
        *,
        promoted_at: datetime,
    ) -> KnowledgePromotionRecord:
        request = _validated_model(request, KnowledgePromotionRequest, "promotion request")
        decision = _validated_model(decision, KnowledgePromotionDecision, "promotion decision")
        promoted = _require_not_future(promoted_at, self._store._now(), "promotion")
        _require_decision_binding(request, decision)
        if decision.decision != "approve":
            raise PolicyViolation("rejected knowledge cannot be promoted")
        if promoted < decision.decided_at:
            raise PolicyViolation("knowledge promotion cannot precede its decision")
        if promoted - request.requested_at > MAX_REVIEW_LAG:
            raise PolicyViolation("knowledge promotion request is stale")
        if request.expires_at <= promoted:
            raise PolicyViolation("knowledge expired before promotion")

        package = _derive_verified_package(session, now=self._store._now())
        if _provenance(package) != request.provenance:
            raise PolicyViolation("knowledge source evidence package drifted after request")
        _validate_facts(request.facts, package)
        values = {
            "request_id": request.request_id,
            "request_digest": artifact_digest(request),
            "decision_id": decision.decision_id,
            "decision_digest": artifact_digest(decision),
            "provenance": request.provenance,
            "facts_digest": request.facts_digest,
            "promoted_at": promoted,
            "expires_at": request.expires_at,
        }
        record = KnowledgePromotionRecord(
            record_id=_stable_id("knowledge-record", values),
            request_id=request.request_id,
            request_digest=artifact_digest(request),
            decision_id=decision.decision_id,
            decision_digest=artifact_digest(decision),
            provenance=request.provenance,
            facts=request.facts,
            facts_digest=request.facts_digest,
            promoted_at=promoted,
            expires_at=request.expires_at,
            requester=request.requester,
            reviewer=decision.reviewer,
        )
        self._store._append_promotion(record, _CONTROLLER_CAPABILITY)
        return record

    def invalidate(
        self,
        record: KnowledgePromotionRecord,
        *,
        reason: InvalidationReason,
        invalidated_by: str,
        invalidated_at: datetime,
    ) -> KnowledgeInvalidationRecord:
        record = _validated_model(record, KnowledgePromotionRecord, "promotion record")
        timestamp = _require_not_future(
            invalidated_at,
            self._store._now(),
            "invalidation",
        )
        values = {
            "record_id": record.record_id,
            "record_digest": artifact_digest(record),
            "reason": reason,
            "invalidated_at": timestamp,
            "invalidated_by": invalidated_by,
        }
        invalidation = KnowledgeInvalidationRecord(
            invalidation_id=_stable_id("knowledge-invalidation", values),
            record_id=record.record_id,
            record_digest=artifact_digest(record),
            reason=reason,
            invalidated_at=timestamp,
            invalidated_by=invalidated_by,
        )
        self._store._append_invalidation(invalidation, _CONTROLLER_CAPABILITY)
        return invalidation


def version_scope_digest(platform: Platform, source_version: str, target_version: str) -> str:
    """Return the deterministic digest for one platform/version scope."""

    return artifact_digest(
        {
            "platform": platform.value,
            "source_version": source_version,
            "target_version": target_version,
        }
    )


def model_identity_digest(context: AgentRunContext) -> str:
    """Bind provider/model identity and all three agent definition digests."""

    return artifact_digest(
        {
            "provider_id": context.provider_id,
            "model_id": context.model_id,
            "agent_definition_digests": context.agent_definition_digests.model_dump(mode="json"),
        }
    )


def facts_digest(facts: Sequence[KnowledgeFact]) -> str:
    return artifact_digest([fact.model_dump(mode="json") for fact in facts])


def _derive_verified_package(
    session: AgentRunSession,
    *,
    now: datetime,
) -> SuccessfulRunEvidencePackage:
    """Derive exact source evidence from canonical lifecycle paths only."""

    loaded = AgentRunSession.load(session.project_root, session.run_dir)
    if loaded.context != session.context:
        raise PolicyViolation("knowledge promotion session context drifted")
    context = loaded.context

    try:
        migration_request = MigrationRequest.model_validate(
            loaded.store.read_json(_AGENT_RUN_REQUEST_PATH)
        )
        config = _AgentRunConfigEvidence.model_validate(
            loaded.store.read_json(_AGENT_RUN_CONFIG_PATH)
        )
        review_request = FinalReviewRequest.model_validate(
            loaded.store.read_json(FINAL_REVIEW_REQUEST_PATH)
        )
        review_decision = FinalReviewDecision.model_validate(
            loaded.store.read_json(FINAL_REVIEW_DECISION_PATH)
        )
        review_record = FinalReviewRecord.model_validate(
            loaded.store.read_json(FINAL_REVIEW_RECORD_PATH)
        )
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation(
            "knowledge promotion requires canonical request and accepted final-review evidence"
        ) from exc

    model_root = f"model-runs/{migration_request.request_id}"
    architect_context_path = f"{model_root}/architect-context.json"
    dependency_graph_path = f"{model_root}/dependency-graph.json"
    wiki_trace_path = f"{model_root}/wiki-trace.json"
    architect_run_path = f"{model_root}/architect.json"
    engineer_run_path = f"{model_root}/engineer-attempt-{review_request.completed_attempt}.json"
    validation_path = f"{model_root}/report-attempt-{review_request.completed_attempt}.json"
    validator_path = f"{model_root}/validator-attempt-{review_request.completed_attempt}.json"
    try:
        architect_context = ArchitectContext.model_validate(
            loaded.store.read_json(architect_context_path)
        )
        dependency_graph = DependencyGraph.model_validate(
            loaded.store.read_json(dependency_graph_path)
        )
        wiki_trace = RetrievalTrace.model_validate(loaded.store.read_json(wiki_trace_path))
        architect_run = ArchitectRun.model_validate(loaded.store.read_json(architect_run_path))
        engineer_run = EngineerRun.model_validate(loaded.store.read_json(engineer_run_path))
        validation = ValidationReport.model_validate(loaded.store.read_json(validation_path))
        validator = ValidatorAssessment.model_validate(loaded.store.read_json(validator_path))
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation(
            "knowledge promotion requires canonical Architect and validation evidence"
        ) from exc

    agent_run_index = _verified_index(
        loaded,
        _AGENT_RUN_EVIDENCE_KIND,
        exact=False,
        anchor=_AgentRunEvidenceAnchor(
            run_id=context.run_id,
            thread_id=context.thread_id,
            request_digest=artifact_digest(migration_request),
            config_digest=artifact_digest(config),
            lifecycle_index_digest=_index_payload_digest(loaded, _AGENT_RUN_EVIDENCE_KIND),
        ),
    )
    expected_agent_run_index = {
        "run-context.json": artifact_digest(context),
        _AGENT_RUN_REQUEST_PATH: artifact_digest(migration_request),
        _AGENT_RUN_CONFIG_PATH: artifact_digest(config),
    }
    if agent_run_index != expected_agent_run_index:
        raise PolicyViolation("canonical agent-run request lifecycle has a custom inventory")
    if config.preset_id != context.slice_id:
        raise PolicyViolation("canonical agent-run config differs from the run session")
    completed_kind = _latest_completed_lifecycle(loaded)
    completed_index_digest = _index_payload_digest(loaded, completed_kind)
    completed_index = _verified_index(
        loaded,
        completed_kind,
        exact=False,
        anchor=_LifecycleAnchor(
            lifecycle_kind=completed_kind,
            run_id=context.run_id,
            thread_id=context.thread_id,
            request_digest=context.request_digest,
            lifecycle_index_digest=completed_index_digest,
        ),
    )
    if (
        review_request.completed_lifecycle_kind != completed_kind
        or review_request.completed_lifecycle_index_digest != completed_index_digest
    ):
        raise PolicyViolation("final review does not bind the canonical completed lifecycle")

    requested_index_digest = _index_payload_digest(loaded, FINAL_REVIEW_REQUESTED_KIND)
    requested_index = _verified_index(
        loaded,
        FINAL_REVIEW_REQUESTED_KIND,
        exact=False,
        anchor=_LifecycleAnchor(
            lifecycle_kind=FINAL_REVIEW_REQUESTED_KIND,
            run_id=context.run_id,
            thread_id=context.thread_id,
            request_digest=artifact_digest(review_request),
            lifecycle_index_digest=requested_index_digest,
        ),
    )
    decided_index_digest = _index_payload_digest(loaded, FINAL_REVIEW_DECIDED_KIND)
    decided_index = _verified_index(
        loaded,
        FINAL_REVIEW_DECIDED_KIND,
        exact=True,
        anchor=_LifecycleAnchor(
            lifecycle_kind=FINAL_REVIEW_DECIDED_KIND,
            run_id=context.run_id,
            thread_id=context.thread_id,
            request_digest=artifact_digest(review_record),
            lifecycle_index_digest=decided_index_digest,
        ),
    )

    references = (
        EvidenceReference(
            kind="migration_request",
            evidence_id=_AGENT_RUN_REQUEST_PATH,
            digest=artifact_digest(migration_request),
        ),
        EvidenceReference(
            kind="architect_context",
            evidence_id=architect_context_path,
            digest=artifact_digest(architect_context),
        ),
        EvidenceReference(
            kind="dependency_graph",
            evidence_id=dependency_graph_path,
            digest=artifact_digest(dependency_graph),
        ),
        EvidenceReference(
            kind="wiki_trace",
            evidence_id=wiki_trace_path,
            digest=artifact_digest(wiki_trace),
        ),
        EvidenceReference(
            kind="architect_run",
            evidence_id=architect_run_path,
            digest=artifact_digest(architect_run),
        ),
        EvidenceReference(
            kind="validation_report",
            evidence_id=validation_path,
            digest=artifact_digest(validation),
        ),
        EvidenceReference(
            kind="final_review_request",
            evidence_id=FINAL_REVIEW_REQUEST_PATH,
            digest=artifact_digest(review_request),
        ),
        EvidenceReference(
            kind="final_review_decision",
            evidence_id=FINAL_REVIEW_DECISION_PATH,
            digest=artifact_digest(review_decision),
        ),
        EvidenceReference(
            kind="final_review_record",
            evidence_id=FINAL_REVIEW_RECORD_PATH,
            digest=artifact_digest(review_record),
        ),
    )
    _require_indexed(completed_index, references[:6], "completed agent-run")
    _require_indexed(requested_index, references[:7], "final-review request")
    _require_indexed(decided_index, references, "final-review decision")
    role_artifact_digests = {
        engineer_run_path: artifact_digest(engineer_run),
        validator_path: artifact_digest(validator),
    }
    for lifecycle, indexed in (
        ("completed agent-run", completed_index),
        ("final-review request", requested_index),
        ("final-review decision", decided_index),
    ):
        for path, digest in role_artifact_digests.items():
            if indexed.get(path) != digest:
                raise PolicyViolation(
                    f"{lifecycle} lifecycle does not bind canonical role evidence: {path}"
                )
    _validate_derived_source_evidence(
        context=context,
        migration_request=migration_request,
        architect_context=architect_context,
        dependency_graph=dependency_graph,
        wiki_trace=wiki_trace,
        architect_run=architect_run,
        engineer_run=engineer_run,
        validation=validation,
        validator=validator,
        review_request=review_request,
        review_decision=review_decision,
        review_record=review_record,
        now=now,
    )

    reference_by_kind = {reference.kind: reference for reference in references}
    package_values = {
        "run_id": context.run_id,
        "session_id": context.thread_id,
        "request_id": migration_request.request_id,
        "source_revision": context.source_revision,
        "session_digest": artifact_digest(context),
        "agent_run_index_digest": _index_payload_digest(loaded, _AGENT_RUN_EVIDENCE_KIND),
        "completed_lifecycle_kind": completed_kind,
        "completed_lifecycle_index_digest": completed_index_digest,
        "final_review_requested_index_digest": requested_index_digest,
        "final_review_decided_index_digest": decided_index_digest,
        "references": references,
    }
    return SuccessfulRunEvidencePackage(
        package_id=_stable_id("successful-run-evidence", package_values),
        run_id=context.run_id,
        session_id=context.thread_id,
        request_id=migration_request.request_id,
        source_revision=context.source_revision,
        session_digest=artifact_digest(context),
        agent_run_index_digest=_index_payload_digest(loaded, _AGENT_RUN_EVIDENCE_KIND),
        completed_lifecycle_kind=completed_kind,
        completed_lifecycle_index_digest=completed_index_digest,
        final_review_requested_index_digest=requested_index_digest,
        final_review_decided_index_digest=decided_index_digest,
        completed_attempt=review_request.completed_attempt,
        platform=migration_request.platform,
        source_version=migration_request.target.source_version,
        target_version=migration_request.target.target_version,
        version_scope_digest=version_scope_digest(
            migration_request.platform,
            migration_request.target.source_version,
            migration_request.target.target_version,
        ),
        model_identity_digest=model_identity_digest(context),
        scope_policy_digest=architect_context.platform_adapter.scope_policy_digest,
        migration_request=reference_by_kind["migration_request"],
        architect_context=reference_by_kind["architect_context"],
        dependency_graph=reference_by_kind["dependency_graph"],
        wiki_trace=reference_by_kind["wiki_trace"],
        architect_run=reference_by_kind["architect_run"],
        validation_report=reference_by_kind["validation_report"],
        final_review_request=reference_by_kind["final_review_request"],
        final_review_decision=reference_by_kind["final_review_decision"],
        final_review_record=reference_by_kind["final_review_record"],
        completed_at=validation.completed_at,
        reviewed_at=review_record.decided_at,
    )


def _index_payload_digest(session: AgentRunSession, kind: str) -> str:
    try:
        return artifact_digest(session.store.read_json(f"indexes/{kind}.json"))
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation(f"required knowledge source lifecycle is missing: {kind}") from exc


def _verified_index(
    session: AgentRunSession,
    kind: str,
    *,
    exact: bool,
    anchor: StrictModel,
) -> dict[str, str]:
    try:
        session.verify_index(kind, exact=exact)
        payload = session.store.read_json(f"indexes/{kind}.json")
        session.verify_runtime_anchor(kind, anchor)
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation(f"required knowledge source lifecycle is invalid: {kind}") from exc
    return _index_digest_map(payload)


def _latest_completed_lifecycle(session: AgentRunSession) -> CompletedLifecycleKind:
    for kind in _COMPLETED_AGENT_RUN_KINDS:
        if session.has_runtime_anchor(kind):
            return kind
    raise PolicyViolation("knowledge promotion requires a completed agent-run lifecycle")


def _require_indexed(
    indexed: Mapping[str, str],
    references: Sequence[EvidenceReference],
    lifecycle: str,
) -> None:
    for reference in references:
        if indexed.get(reference.evidence_id) != reference.digest:
            raise PolicyViolation(
                f"{lifecycle} lifecycle does not bind canonical {reference.kind} evidence"
            )


def _validate_derived_source_evidence(
    *,
    context: AgentRunContext,
    migration_request: MigrationRequest,
    architect_context: ArchitectContext,
    dependency_graph: DependencyGraph,
    wiki_trace: RetrievalTrace,
    architect_run: ArchitectRun,
    engineer_run: EngineerRun,
    validation: ValidationReport,
    validator: ValidatorAssessment,
    review_request: FinalReviewRequest,
    review_decision: FinalReviewDecision,
    review_record: FinalReviewRecord,
    now: datetime,
) -> None:
    request_digest = artifact_digest(migration_request)
    session_digest = artifact_digest(context)
    if request_digest != context.request_digest:
        raise PolicyViolation("canonical migration request differs from the run session")
    if migration_request.base_revision != context.source_revision:
        raise PolicyViolation("canonical migration request differs from the source revision")
    if architect_context.request != migration_request:
        raise PolicyViolation("Architect context differs from the canonical migration request")
    if architect_context.dependency_graph != dependency_graph:
        raise PolicyViolation("Architect dependency graph differs from its canonical artifact")
    if architect_context.wiki_trace != wiki_trace:
        raise PolicyViolation("Architect Wiki trace differs from its canonical artifact")
    validate_manifest_for_request(architect_run.proposal.manifest, migration_request)
    call = architect_run.model_call
    if (
        call.provider != context.provider_id
        or call.model_id != context.model_id
        or call.agent_definition_digest != context.agent_definition_digests.architect
        or not call.agent_version.startswith("architect/v")
        or call.input_digest != artifact_digest(architect_context)
        or call.output_digest != artifact_digest(architect_run.proposal)
    ):
        raise PolicyViolation("Architect model run is not bound to its canonical context")
    for role_call, expected_digest in (
        (engineer_run.model_call, context.agent_definition_digests.engineer),
        (validator.model_call, context.agent_definition_digests.validator),
    ):
        if (
            role_call.provider != context.provider_id
            or role_call.model_id != context.model_id
            or role_call.agent_definition_digest != expected_digest
            or (role_call.live_invocation and not role_call.store_false_sent)
        ):
            raise PolicyViolation("persisted role model identity differs from the source run")

    change_set = engineer_run.change_set
    if change_set is None:
        raise PolicyViolation("knowledge source Engineer run has no canonical change set")
    validate_change_set(change_set, architect_run.proposal.manifest)
    validate_report(validation, architect_run.proposal.manifest, change_set)
    advisory = validator.advisory
    if (
        advisory.manifest_digest != artifact_digest(architect_run.proposal.manifest)
        or advisory.change_set_digest != artifact_digest(change_set)
        or advisory.report_digest != artifact_digest(validation)
        or validator.authoritative_disposition is not validation.disposition
        or not validator.all_required_checks_terminal_and_passed
    ):
        raise PolicyViolation("Validator assessment differs from deterministic validation evidence")

    required = tuple(result for result in validation.results if result.required)
    if (
        validation.disposition is not ValidationDisposition.READY_FOR_HUMAN_REVIEW
        or not required
        or any(
            result.status is not CheckStatus.PASSED
            or result.receipt is None
            or not result.receipt.terminal
            or result.receipt.exit_code != 0
            for result in required
        )
    ):
        raise PolicyViolation("knowledge source validation is not terminally successful")
    if (
        validation.request_id != migration_request.request_id
        or validation.manifest_id != architect_run.proposal.manifest.manifest_id
        or validation.base_revision != migration_request.base_revision
        or validation.attempt != review_request.completed_attempt
    ):
        raise PolicyViolation("knowledge validation is not bound to the canonical Architect run")
    for result in required:
        receipt = result.receipt
        assert receipt is not None
        if (
            receipt.request_id != migration_request.request_id
            or receipt.run_id != context.run_id
            or receipt.base_revision != context.source_revision
            or receipt.attempt != validation.attempt
        ):
            raise PolicyViolation("knowledge validation receipt differs from the source run")

    expected_review_binding: Mapping[str, object] = {
        "run_id": context.run_id,
        "thread_id": context.thread_id,
        "request_id": migration_request.request_id,
        "manifest_id": architect_run.proposal.manifest.manifest_id,
        "platform": migration_request.platform,
        "source_revision": context.source_revision,
        "session_context_digest": session_digest,
        "request_digest": request_digest,
        "manifest_digest": artifact_digest(architect_run.proposal.manifest),
        "validation_report_id": validation.report_id,
        "validation_report_digest": artifact_digest(validation),
        "architect_context_digest": artifact_digest(architect_context),
        "dependency_graph_digest": artifact_digest(dependency_graph),
        "wiki_trace_digest": artifact_digest(wiki_trace),
        "scope_policy_digest": architect_context.platform_adapter.scope_policy_digest,
        "architect_run_digest": artifact_digest(architect_run),
        "engineer_run_digest": artifact_digest(engineer_run),
        "validator_assessment_digest": artifact_digest(validator),
        "agent_definition_digests": context.agent_definition_digests,
        "provider_id": context.provider_id,
        "model_id": context.model_id,
    }
    for field_name, expected in expected_review_binding.items():
        if getattr(review_request, field_name) != expected:
            raise PolicyViolation(f"final-review request {field_name} differs from source evidence")
    required_receipts = tuple(
        artifact_digest(result.receipt) for result in required if result.receipt is not None
    )
    if review_request.required_command_ids != tuple(result.command_id for result in required):
        raise PolicyViolation("final-review request command inventory differs from validation")
    if review_request.required_receipt_digests != required_receipts:
        raise PolicyViolation("final-review request receipt inventory differs from validation")

    if (
        review_decision.selection != "accept"
        or review_decision.review_id != review_request.review_id
        or review_decision.review_request_digest != artifact_digest(review_request)
        or review_decision.reviewer != review_request.designated_reviewer
    ):
        raise PolicyViolation(
            "knowledge promotion requires the bound accepted final-review decision"
        )
    if (
        review_record.outcome != "accepted"
        or not review_record.candidate_accepted
        or review_record.review_id != review_request.review_id
        or review_record.review_request_digest != artifact_digest(review_request)
        or review_record.decision_id != review_decision.decision_id
        or review_record.decision_digest != artifact_digest(review_decision)
        or review_record.run_id != context.run_id
        or review_record.thread_id != context.thread_id
        or review_record.request_id != migration_request.request_id
        or review_record.manifest_digest != review_request.manifest_digest
        or review_record.change_set_digest != review_request.change_set_digest
        or review_record.validation_report_digest != artifact_digest(validation)
        or review_record.source_revision != context.source_revision
        or review_record.reviewer != review_decision.reviewer
        or review_record.decided_at != review_decision.decided_at
    ):
        raise PolicyViolation("accepted final-review record is not bound to the source run")
    if (
        validation.completed_at > review_request.requested_at
        or review_request.requested_at > review_decision.decided_at
        or review_decision.decided_at > now
    ):
        raise PolicyViolation("knowledge source review timestamps are not causally ordered")
    if now - review_record.decided_at > MAX_SOURCE_EVIDENCE_AGE:
        raise PolicyViolation("knowledge source accepted final review is stale")


def _validate_facts(
    facts: Sequence[KnowledgeFact],
    package: SuccessfulRunEvidencePackage,
) -> tuple[KnowledgeFact, ...]:
    try:
        normalized = tuple(
            KnowledgeFact.model_validate(fact.model_dump(mode="python")) for fact in facts
        )
    except (AttributeError, ValidationError) as exc:
        raise PolicyViolation(f"knowledge facts are malformed: {exc}") from exc
    if not normalized:
        raise PolicyViolation("knowledge promotion requires at least one fact")
    if len(normalized) > MAX_FACTS_PER_PROMOTION:
        raise PolicyViolation("knowledge promotion contains too many facts")
    fact_ids = tuple(fact.fact_id for fact in normalized)
    if len(fact_ids) != len(set(fact_ids)):
        raise PolicyViolation("knowledge fact IDs must be unique")
    references = {
        reference.evidence_id: reference
        for reference in package.references
        if reference.kind
        in {
            "architect_context",
            "dependency_graph",
            "wiki_trace",
            "architect_run",
            "validation_report",
            "final_review_request",
            "final_review_decision",
            "final_review_record",
        }
    }
    for fact in normalized:
        for citation in fact.citations:
            reference = references.get(citation.evidence_id)
            if reference is None or citation.evidence_digest != reference.digest:
                raise PolicyViolation("knowledge citation is not bound to approved source evidence")
    return normalized


def _provenance(package: SuccessfulRunEvidencePackage) -> KnowledgeProvenance:
    return KnowledgeProvenance(
        source_run_id=package.run_id,
        source_session_id=package.session_id,
        source_session_digest=package.session_digest,
        source_evidence_package_digest=artifact_digest(package),
        source_request_digest=package.migration_request.digest,
        source_graph_digest=package.dependency_graph.digest,
        source_wiki_digest=package.wiki_trace.digest,
        source_model_digest=package.model_identity_digest,
        source_policy_digest=package.scope_policy_digest,
        platform=package.platform,
        source_version=package.source_version,
        target_version=package.target_version,
        version_scope_digest=package.version_scope_digest,
    )


def _require_decision_binding(
    request: KnowledgePromotionRequest,
    decision: KnowledgePromotionDecision,
) -> None:
    expected: Mapping[str, object] = {
        "policy_version": request.policy_version,
        "requested_action": request.requested_action,
        "request_id": request.request_id,
        "request_digest": artifact_digest(request),
        "provenance": request.provenance,
        "facts_digest": request.facts_digest,
        "request_requested_at": request.requested_at,
        "requester": request.requester,
        "designated_reviewer": request.designated_reviewer,
        "reviewer": request.designated_reviewer,
    }
    for field_name, expected_value in expected.items():
        if getattr(decision, field_name) != expected_value:
            raise PolicyViolation(f"knowledge promotion decision {field_name} does not match")


def _scope_matches(provenance: KnowledgeProvenance, scope: KnowledgeConsumerScope) -> bool:
    return (
        provenance.platform is scope.platform
        and provenance.source_version == scope.source_version
        and provenance.target_version == scope.target_version
        and provenance.version_scope_digest == scope.version_scope_digest
        and provenance.source_graph_digest == scope.graph_digest
        and provenance.source_wiki_digest == scope.wiki_digest
        and provenance.source_model_digest == scope.model_digest
        and provenance.source_policy_digest == scope.policy_digest
    )


def _index_digest_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not isinstance(value.get("artifacts"), list):
        raise PolicyViolation("knowledge source lifecycle index is malformed")
    entries: dict[str, str] = {}
    for raw in value["artifacts"]:
        if not isinstance(raw, dict):
            raise PolicyViolation("knowledge source lifecycle index entry is malformed")
        path = raw.get("path")
        digest = raw.get("canonical_sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise PolicyViolation("knowledge source lifecycle index entry is malformed")
        normalized = validate_relative_path(path)
        if normalized in entries:
            raise PolicyViolation("knowledge source lifecycle index contains duplicate paths")
        entries[normalized] = digest
    return entries


def _append_event(
    audit_fd: int,
    existing: Sequence[KnowledgeAuditEvent],
    *,
    promotion: KnowledgePromotionRecord | None = None,
    invalidation: KnowledgeInvalidationRecord | None = None,
) -> KnowledgeAuditEvent:
    if (promotion is None) == (invalidation is None):
        raise ValueError("exactly one knowledge audit payload is required")
    if promotion is not None:
        payload: StrictModel = promotion
        event_kind: Literal["promotion", "invalidation"] = "promotion"
        event_at = promotion.promoted_at
    else:
        assert invalidation is not None
        payload = invalidation
        event_kind = "invalidation"
        event_at = invalidation.invalidated_at
    if existing and event_at < existing[-1].event_at:
        raise PolicyViolation("knowledge audit timestamps must be monotonic")
    sequence = len(existing) + 1
    previous_digest = artifact_digest(existing[-1]) if existing else None
    payload_digest = artifact_digest(payload)
    values = {
        "sequence": sequence,
        "previous_entry_digest": previous_digest,
        "event_kind": event_kind,
        "event_at": event_at,
        "payload_digest": payload_digest,
    }
    event = KnowledgeAuditEvent(
        sequence=sequence,
        entry_id=_stable_id("knowledge-audit", values),
        previous_entry_digest=previous_digest,
        event_kind=event_kind,
        event_at=event_at,
        payload_digest=payload_digest,
        promotion=promotion,
        invalidation=invalidation,
    )
    filename = f"{sequence:020d}-{event.entry_id}.json"
    payload_bytes = canonical_json_bytes(event) + b"\n"
    if len(payload_bytes) > MAX_AUDIT_FILE_BYTES:
        raise PolicyViolation("knowledge audit event exceeds the size limit")
    descriptor = os.open(
        filename,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
        0o600,
        dir_fd=audit_fd,
    )
    try:
        _write_all(descriptor, payload_bytes)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(audit_fd)
    verified = _read_audit_index(audit_fd)
    if not verified or verified[-1] != event:
        raise PolicyViolation("knowledge audit append could not be verified")
    return event


def _read_audit_index(audit_fd: int) -> tuple[KnowledgeAuditEvent, ...]:
    names = tuple(sorted(os.listdir(audit_fd)))
    events: list[KnowledgeAuditEvent] = []
    expected_previous: str | None = None
    promotion_ids: set[str] = set()
    promotions: dict[str, KnowledgePromotionRecord] = {}
    request_digests: set[str] = set()
    decision_digests: set[str] = set()
    invalidated_ids: set[str] = set()
    last_timestamp: datetime | None = None
    for expected_sequence, name in enumerate(names, start=1):
        match = _AUDIT_NAME.fullmatch(name)
        if match is None:
            raise PolicyViolation(f"knowledge audit contains an unexpected entry: {name}")
        if int(match.group("sequence")) != expected_sequence:
            raise PolicyViolation("knowledge audit sequence has a gap or duplicate")
        payload = _read_private_file_at(audit_fd, name, "knowledge audit entry")
        try:
            event = KnowledgeAuditEvent.model_validate_json(payload)
        except (ValidationError, ValueError) as exc:
            raise PolicyViolation(f"knowledge audit entry is malformed: {name}") from exc
        if event.sequence != expected_sequence or event.entry_id != match.group("entry_id"):
            raise PolicyViolation("knowledge audit filename does not match its entry")
        if event.entry_id != _expected_event_id(event):
            raise PolicyViolation("knowledge audit entry ID does not match its contents")
        if event.previous_entry_digest != expected_previous:
            raise PolicyViolation("knowledge audit hash chain is broken")
        if last_timestamp is not None and event.event_at < last_timestamp:
            raise PolicyViolation("knowledge audit timestamps are not monotonic")
        if event.promotion is not None:
            record = event.promotion
            if record.record_id != _expected_record_id(record):
                raise PolicyViolation("knowledge promotion record ID does not match its contents")
            if record.record_id in promotion_ids:
                raise PolicyViolation("knowledge audit repeats a promotion record")
            if record.request_digest in request_digests:
                raise PolicyViolation("knowledge audit replays a promotion request")
            if record.decision_digest in decision_digests:
                raise PolicyViolation("knowledge audit replays a promotion decision")
            promotion_ids.add(record.record_id)
            promotions[record.record_id] = record
            request_digests.add(record.request_digest)
            decision_digests.add(record.decision_digest)
        else:
            invalidation = event.invalidation
            assert invalidation is not None
            if invalidation.invalidation_id != _expected_invalidation_id(invalidation):
                raise PolicyViolation("knowledge invalidation ID does not match its contents")
            if invalidation.record_id not in promotion_ids:
                raise PolicyViolation("knowledge audit invalidates an unknown promotion")
            if invalidation.record_id in invalidated_ids:
                raise PolicyViolation("knowledge audit repeats an invalidation")
            target = promotions[invalidation.record_id]
            if invalidation.record_digest != artifact_digest(target):
                raise PolicyViolation("knowledge audit invalidation target digest is invalid")
            if invalidation.invalidated_at < target.promoted_at:
                raise PolicyViolation("knowledge audit invalidation precedes promotion")
            if invalidation.invalidated_by == target.requester:
                raise PolicyViolation("knowledge audit contains a self-invalidation")
            invalidated_ids.add(invalidation.record_id)
        events.append(event)
        expected_previous = artifact_digest(event)
        last_timestamp = event.event_at
    return tuple(events)


def _expected_record_id(record: KnowledgePromotionRecord) -> str:
    return _stable_id(
        "knowledge-record",
        {
            "request_id": record.request_id,
            "request_digest": record.request_digest,
            "decision_id": record.decision_id,
            "decision_digest": record.decision_digest,
            "provenance": record.provenance,
            "facts_digest": record.facts_digest,
            "promoted_at": record.promoted_at,
            "expires_at": record.expires_at,
        },
    )


def _expected_invalidation_id(invalidation: KnowledgeInvalidationRecord) -> str:
    return _stable_id(
        "knowledge-invalidation",
        {
            "record_id": invalidation.record_id,
            "record_digest": invalidation.record_digest,
            "reason": invalidation.reason,
            "invalidated_at": invalidation.invalidated_at,
            "invalidated_by": invalidation.invalidated_by,
        },
    )


def _expected_event_id(event: KnowledgeAuditEvent) -> str:
    return _stable_id(
        "knowledge-audit",
        {
            "sequence": event.sequence,
            "previous_entry_digest": event.previous_entry_digest,
            "event_kind": event.event_kind,
            "event_at": event.event_at,
            "payload_digest": event.payload_digest,
        },
    )


def _prepare_store(
    project_root: Path, store_path: str
) -> tuple[Path, tuple[int, int], tuple[int, int]]:
    parts = store_path.split("/")
    descriptors = [os.open(project_root, os.O_RDONLY | _directory_flag() | _no_follow_flag())]
    current = project_root
    try:
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | _directory_flag() | _no_follow_flag(),
                    dir_fd=descriptors[-1],
                )
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
                child = os.open(
                    part,
                    os.O_RDONLY | _directory_flag() | _no_follow_flag(),
                    dir_fd=descriptors[-1],
                )
            except OSError as exc:
                raise PolicyViolation("knowledge store path contains an unsafe component") from exc
            descriptors.append(child)
            current = current / part
            if final and stat.S_IMODE(os.fstat(child).st_mode) != 0o700:
                raise PolicyViolation("knowledge store root must use mode 0700")
        root_fd = descriptors[-1]
        try:
            os.mkdir("audit", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        audit_fd = os.open(
            "audit",
            os.O_RDONLY | _directory_flag() | _no_follow_flag(),
            dir_fd=root_fd,
        )
        try:
            if stat.S_IMODE(os.fstat(audit_fd).st_mode) != 0o700:
                raise PolicyViolation("knowledge audit directory must use mode 0700")
            try:
                lock_fd = os.open(
                    ".lock",
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
                    0o600,
                    dir_fd=root_fd,
                )
            except FileExistsError:
                lock_fd = os.open(".lock", os.O_RDWR | _no_follow_flag(), dir_fd=root_fd)
            try:
                lock_details = os.fstat(lock_fd)
                if (
                    not stat.S_ISREG(lock_details.st_mode)
                    or stat.S_IMODE(lock_details.st_mode) != 0o600
                    or lock_details.st_size != 0
                ):
                    raise PolicyViolation("knowledge store lock is unsafe")
            finally:
                os.close(lock_fd)
            root_details = os.fstat(root_fd)
            audit_details = os.fstat(audit_fd)
            root = current.resolve(strict=True)
            try:
                root.relative_to(project_root)
            except ValueError as exc:
                raise PolicyViolation("knowledge store path escapes the project root") from exc
            return (
                root,
                (root_details.st_dev, root_details.st_ino),
                (audit_details.st_dev, audit_details.st_ino),
            )
        finally:
            os.close(audit_fd)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _open_store_descriptors(
    project_root: Path,
    store_path: str,
    root_identity: tuple[int, int],
    audit_identity: tuple[int, int],
) -> Iterator[tuple[int, int]]:
    descriptors = [os.open(project_root, os.O_RDONLY | _directory_flag() | _no_follow_flag())]
    try:
        for part in store_path.split("/"):
            descriptors.append(
                os.open(
                    part,
                    os.O_RDONLY | _directory_flag() | _no_follow_flag(),
                    dir_fd=descriptors[-1],
                )
            )
        root_fd = descriptors[-1]
        root_details = os.fstat(root_fd)
        if (root_details.st_dev, root_details.st_ino) != root_identity:
            raise PolicyViolation("knowledge store root identity changed")
        audit_fd = os.open(
            "audit",
            os.O_RDONLY | _directory_flag() | _no_follow_flag(),
            dir_fd=root_fd,
        )
        descriptors.append(audit_fd)
        audit_details = os.fstat(audit_fd)
        if (audit_details.st_dev, audit_details.st_ino) != audit_identity:
            raise PolicyViolation("knowledge audit directory identity changed")
        yield root_fd, audit_fd
    except OSError as exc:
        raise PolicyViolation("knowledge store layout is unsafe") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _require_exact_root_inventory(root_fd: int) -> None:
    if set(os.listdir(root_fd)) != {".lock", "audit"}:
        raise PolicyViolation("knowledge store root contains unexpected content")


def _read_private_file_at(parent_fd: int, name: str, role: str) -> bytes:
    expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
        raise PolicyViolation(f"{role} must be a regular non-symlink file")
    if stat.S_IMODE(expected.st_mode) != 0o600:
        raise PolicyViolation(f"{role} must use mode 0600")
    if expected.st_size > MAX_AUDIT_FILE_BYTES:
        raise PolicyViolation(f"{role} exceeds the size limit")
    descriptor = os.open(name, os.O_RDONLY | _no_follow_flag(), dir_fd=parent_fd)
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise PolicyViolation(f"{role} changed while being opened")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        if len(payload) != actual.st_size:
            raise PolicyViolation(f"{role} changed while being read")
        return payload
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("knowledge audit write did not make progress")
        view = view[written:]


def _safe_project_root(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PolicyViolation("knowledge project root is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PolicyViolation("knowledge project root must be a non-symlink directory")
    return path.resolve(strict=True)


def _validated_model(value: Any, model: type[Any], role: str) -> Any:
    try:
        return model.model_validate(value.model_dump(mode="python"))
    except (AttributeError, ValidationError) as exc:
        raise PolicyViolation(f"invalid knowledge {role}: {exc}") from exc


def _stable_id(prefix: str, value: Any) -> str:
    json_value = _JSON_VALUE_ADAPTER.dump_python(value, mode="json")
    return f"{prefix}-{artifact_digest(json_value).split(':', 1)[1][:20]}"


def _utc_timestamp(value: datetime, role: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{role} must be timezone-aware")
    return value.astimezone(UTC)


def _require_not_future(value: datetime, now: datetime, role: str) -> datetime:
    try:
        normalized = _utc_timestamp(value, role)
    except ValueError as exc:
        raise PolicyViolation(str(exc)) from exc
    if normalized > now:
        raise PolicyViolation(f"knowledge {role} timestamp cannot be in the future")
    return normalized


def _concise_text(value: str, *, role: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{role} must be concise")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{role} cannot contain control characters or multiline content")
    if SecretRedactor().redact(normalized).findings or _API_TOKEN.search(normalized):
        raise ValueError(f"{role} cannot contain credential material")
    if _ABSOLUTE_LOCAL_PATH.search(normalized):
        raise ValueError(f"{role} cannot contain absolute local paths")
    if _PROMPT_MATERIAL.search(normalized):
        raise ValueError(f"{role} cannot contain prompt material")
    if _CANDIDATE_SOURCE.search(normalized):
        raise ValueError(f"{role} cannot contain candidate source content")
    return normalized


def _no_follow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as exc:  # pragma: no cover - supported target is POSIX
        raise RuntimeError("knowledge store requires O_NOFOLLOW") from exc


def _directory_flag() -> int:
    try:
        return os.O_DIRECTORY
    except AttributeError as exc:  # pragma: no cover - supported target is POSIX
        raise RuntimeError("knowledge store requires O_DIRECTORY") from exc


__all__ = [
    "EvidenceReference",
    "GovernedKnowledgeStore",
    "KnowledgeCitation",
    "KnowledgeConsumerScope",
    "KnowledgeFact",
    "KnowledgeInvalidationRecord",
    "KnowledgeLookupMatch",
    "KnowledgeLookupResult",
    "KnowledgePromotionDecision",
    "KnowledgePromotionRecord",
    "KnowledgePromotionRequest",
    "KnowledgeProvenance",
    "KnowledgeStoreController",
    "facts_digest",
    "model_identity_digest",
    "version_scope_digest",
]
