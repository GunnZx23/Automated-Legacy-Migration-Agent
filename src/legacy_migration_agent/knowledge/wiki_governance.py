"""Human review governance for promoting one exact pilot Wiki page.

This module deliberately stops before mutating the repository.  It binds a
review request and one-use decision to a clean Git revision and to a
descriptor-safe :class:`~legacy_migration_agent.knowledge.wiki.LlmWiki` snapshot.  A
successful approval produces deterministic catalog and index bytes in memory;
another, explicitly authorized layer may later publish those bytes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import AnyHttpUrl, Field, ValidationError, field_validator, model_validator

from legacy_migration_agent.contracts import (
    SCHEMA_VERSION,
    ActorIdentifier,
    GitRevision,
    Identifier,
    Sha256Digest,
    StrictModel,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import artifact_digest, canonical_json_bytes
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.redaction import SecretRedactor
from legacy_migration_agent.core.workspace import TreeSnapshot, snapshot_tree
from legacy_migration_agent.knowledge.wiki import (
    LlmWiki,
    WikiCatalog,
    WikiPageRecord,
    _LoadedPage,
    render_wiki_index,
)

GOVERNANCE_POLICY_VERSION: Literal["wiki-review/1.0"] = "wiki-review/1.0"
REQUESTED_ACTION: Literal["promote_to_reviewed"] = "promote_to_reviewed"
MAX_PAGE_AGE_DAYS = 365
MAX_GOVERNED_FILE_BYTES = 8 * 1024 * 1024
MAX_GOVERNED_TREE_BYTES = 32 * 1024 * 1024
MAX_RECEIPT_BYTES = 512 * 1024

WikiPlatform = Literal["salesforce", "mulesoft", "workflow"]
ReviewDecision = Literal["approve", "reject"]
ReviewerIdentityAssurance = Literal["declarative_unverified"]
_ABSOLUTE_USER_PATH = re.compile(r"/(?:Users|home)/[^/\s]+/")
_RECEIPT_NAME = re.compile(r"(?P<digest>[0-9a-f]{64})\.json")


class WikiReviewRequest(StrictModel):
    """Deterministic request to review one exact pilot page at one Git commit."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    governance_policy_version: Literal["wiki-review/1.0"] = GOVERNANCE_POLICY_VERSION
    request_id: Identifier
    status: Literal["review_requested"] = "review_requested"
    requested_action: Literal["promote_to_reviewed"] = REQUESTED_ACTION
    page_id: Identifier
    page_path: str
    page_status: Literal["pilot"] = "pilot"
    page_content_digest: Sha256Digest
    catalog_digest: Sha256Digest
    catalog_content_digest: Sha256Digest
    index_content_digest: Sha256Digest
    wiki_inventory_digest: Sha256Digest
    platform: WikiPlatform
    source_version: str = Field(min_length=1, max_length=160)
    target_version: str = Field(min_length=1, max_length=160)
    owner: str = Field(min_length=1, max_length=160)
    authoritative_source_urls: tuple[AnyHttpUrl, ...] = Field(min_length=1, max_length=32)
    page_last_verified: date
    as_of: date
    repository_revision: GitRevision
    requester: ActorIdentifier
    designated_reviewer: ActorIdentifier
    reviewer_identity_assurance: ReviewerIdentityAssurance = "declarative_unverified"

    @field_validator("page_path")
    @classmethod
    def validate_page_path(cls, value: str) -> str:
        normalized = validate_relative_path(value)
        if not normalized.startswith("pages/") or not normalized.endswith(".md"):
            raise ValueError("Wiki review paths must identify a page Markdown file")
        return normalized

    @field_validator("source_version", "target_version", "owner")
    @classmethod
    def prohibit_sensitive_metadata(cls, value: str) -> str:
        _require_portable_text(value, role="Wiki review metadata")
        return value

    @field_validator("authoritative_source_urls")
    @classmethod
    def validate_source_urls(
        cls,
        values: tuple[AnyHttpUrl, ...],
    ) -> tuple[AnyHttpUrl, ...]:
        _require_authoritative_urls(values)
        return values

    @model_validator(mode="after")
    def bind_review_boundary(self) -> WikiReviewRequest:
        if self.requester == self.designated_reviewer:
            raise ValueError("Wiki promotion requires an independent designated reviewer")
        if self.page_last_verified > self.as_of:
            raise ValueError("Wiki page verification date cannot be after the review as-of date")
        if self.as_of - self.page_last_verified > timedelta(days=MAX_PAGE_AGE_DAYS):
            raise ValueError("Wiki page evidence is stale for review")
        return self


class WikiReviewDecision(StrictModel):
    """Explicit approve or reject decision bound to one exact review request."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    governance_policy_version: Literal["wiki-review/1.0"] = GOVERNANCE_POLICY_VERSION
    decision_id: Identifier
    status: Literal["review_decided"] = "review_decided"
    decision: ReviewDecision
    requested_action: Literal["promote_to_reviewed"] = REQUESTED_ACTION
    request_id: Identifier
    request_digest: Sha256Digest
    page_id: Identifier
    page_path: str
    page_content_digest: Sha256Digest
    catalog_digest: Sha256Digest
    wiki_inventory_digest: Sha256Digest
    repository_revision: GitRevision
    request_as_of: date
    reviewed_on: date
    requester: ActorIdentifier
    designated_reviewer: ActorIdentifier
    reviewer: ActorIdentifier
    reviewer_identity_assurance: ReviewerIdentityAssurance = "declarative_unverified"

    @field_validator("page_path")
    @classmethod
    def validate_page_path(cls, value: str) -> str:
        normalized = validate_relative_path(value)
        if not normalized.startswith("pages/") or not normalized.endswith(".md"):
            raise ValueError("Wiki decision paths must identify a page Markdown file")
        return normalized

    @model_validator(mode="after")
    def require_bound_independent_reviewer(self) -> WikiReviewDecision:
        if self.reviewer != self.designated_reviewer:
            raise ValueError("Wiki decision cannot be transferred to another reviewer")
        if self.reviewer == self.requester:
            raise ValueError("Wiki requester cannot review their own promotion")
        if self.reviewed_on < self.request_as_of:
            raise ValueError("Wiki review date cannot precede the request as-of date")
        return self


class WikiPromotionReport(StrictModel):
    """Immutable evidence for a rejected decision or an in-memory promotion plan."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    governance_policy_version: Literal["wiki-review/1.0"] = GOVERNANCE_POLICY_VERSION
    report_id: Identifier
    status: Literal["promotion_planned", "rejected"]
    promotion_applied: Literal[False] = False
    decision: ReviewDecision
    requested_action: Literal["promote_to_reviewed"] = REQUESTED_ACTION
    request_id: Identifier
    request_digest: Sha256Digest
    decision_id: Identifier
    decision_digest: Sha256Digest
    page_id: Identifier
    page_path: str
    original_page_status: Literal["pilot"] = "pilot"
    proposed_page_status: Literal["reviewed"] | None
    page_content_digest: Sha256Digest
    catalog_digest: Sha256Digest
    catalog_content_digest: Sha256Digest
    index_content_digest: Sha256Digest
    wiki_inventory_digest: Sha256Digest
    proposed_catalog_digest: Sha256Digest | None
    proposed_catalog_content_digest: Sha256Digest | None
    proposed_index_content_digest: Sha256Digest | None
    proposed_wiki_inventory_digest: Sha256Digest | None
    verified_reviewed_page_ids: tuple[Identifier, ...]
    platform: WikiPlatform
    source_version: str = Field(min_length=1, max_length=160)
    target_version: str = Field(min_length=1, max_length=160)
    owner: str = Field(min_length=1, max_length=160)
    authoritative_source_urls: tuple[AnyHttpUrl, ...] = Field(min_length=1, max_length=32)
    page_last_verified: date
    request_as_of: date
    reviewed_on: date
    repository_revision: GitRevision
    requester: ActorIdentifier
    reviewer: ActorIdentifier
    reviewer_identity_assurance: ReviewerIdentityAssurance = "declarative_unverified"

    @field_validator("page_path")
    @classmethod
    def validate_page_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("source_version", "target_version", "owner")
    @classmethod
    def prohibit_sensitive_metadata(cls, value: str) -> str:
        _require_portable_text(value, role="Wiki promotion metadata")
        return value

    @field_validator("authoritative_source_urls")
    @classmethod
    def validate_source_urls(
        cls,
        values: tuple[AnyHttpUrl, ...],
    ) -> tuple[AnyHttpUrl, ...]:
        _require_authoritative_urls(values)
        return values

    @model_validator(mode="after")
    def bind_outcome(self) -> WikiPromotionReport:
        proposed_digests = (
            self.proposed_catalog_digest,
            self.proposed_catalog_content_digest,
            self.proposed_index_content_digest,
            self.proposed_wiki_inventory_digest,
        )
        if self.decision == "approve":
            if self.status != "promotion_planned":
                raise ValueError("approved Wiki decisions must produce a promotion plan")
            if self.proposed_page_status != "reviewed":
                raise ValueError("approved Wiki decisions must propose reviewed status")
            if self.verified_reviewed_page_ids != (self.page_id,):
                raise ValueError("approved Wiki report must expose only its exact reviewed page")
            if any(value is None for value in proposed_digests):
                raise ValueError("approved Wiki report is missing proposed artifact digests")
        else:
            if self.status != "rejected":
                raise ValueError("rejected Wiki decisions cannot plan a promotion")
            if self.proposed_page_status is not None or self.verified_reviewed_page_ids:
                raise ValueError("rejected Wiki decisions cannot expose reviewed pages")
            if any(value is not None for value in proposed_digests):
                raise ValueError("rejected Wiki decisions cannot propose artifact bytes")
        return self


@dataclass(frozen=True)
class WikiPromotionResult:
    """Non-serializing container for exact proposed bytes and portable evidence."""

    report: WikiPromotionReport
    source_page_bytes: bytes
    source_catalog_bytes: bytes
    source_index_bytes: bytes
    proposed_catalog_bytes: bytes | None
    proposed_index_bytes: bytes | None
    reviewed_page_ids: frozenset[str]

    def __post_init__(self) -> None:
        expected_ids = frozenset(self.report.verified_reviewed_page_ids)
        if self.reviewed_page_ids != expected_ids:
            raise ValueError("Wiki promotion result page set does not match its report")
        if _bytes_digest(self.source_page_bytes) != self.report.page_content_digest:
            raise ValueError("Wiki promotion result page bytes do not match its report")
        if _bytes_digest(self.source_catalog_bytes) != self.report.catalog_content_digest:
            raise ValueError("Wiki promotion result catalog bytes do not match its report")
        if _bytes_digest(self.source_index_bytes) != self.report.index_content_digest:
            raise ValueError("Wiki promotion result index bytes do not match its report")
        try:
            source_catalog = WikiCatalog.model_validate_json(self.source_catalog_bytes)
        except ValidationError as exc:
            raise ValueError("Wiki promotion result catalog bytes are invalid") from exc
        if artifact_digest(source_catalog) != self.report.catalog_digest:
            raise ValueError("Wiki promotion result catalog model does not match its report")
        if self.report.decision == "approve":
            if self.proposed_catalog_bytes is None or self.proposed_index_bytes is None:
                raise ValueError("approved Wiki promotion result requires exact proposed bytes")
            if (
                _bytes_digest(self.proposed_catalog_bytes)
                != self.report.proposed_catalog_content_digest
            ):
                raise ValueError("proposed Wiki catalog bytes do not match their report")
            if (
                _bytes_digest(self.proposed_index_bytes)
                != self.report.proposed_index_content_digest
            ):
                raise ValueError("proposed Wiki index bytes do not match their report")
            try:
                proposed_catalog = WikiCatalog.model_validate_json(self.proposed_catalog_bytes)
            except ValidationError as exc:
                raise ValueError("proposed Wiki catalog bytes are invalid") from exc
            if artifact_digest(proposed_catalog) != self.report.proposed_catalog_digest:
                raise ValueError("proposed Wiki catalog model does not match its report")
        elif self.proposed_catalog_bytes is not None or self.proposed_index_bytes is not None:
            raise ValueError("rejected Wiki promotion result cannot contain proposed bytes")


class WikiPromotionReceipt(StrictModel):
    """Private durable record that one exact declarative decision was consumed."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    governance_policy_version: Literal["wiki-review/1.0"] = GOVERNANCE_POLICY_VERSION
    receipt_id: Identifier
    status: Literal["decision_consumed"] = "decision_consumed"
    decision_digest: Sha256Digest
    report_digest: Sha256Digest
    report: WikiPromotionReport

    @model_validator(mode="after")
    def bind_receipt(self) -> WikiPromotionReceipt:
        if self.decision_digest != self.report.decision_digest:
            raise ValueError("Wiki promotion receipt decision digest does not match its report")
        if self.report_digest != artifact_digest(self.report):
            raise ValueError("Wiki promotion receipt report digest does not match its report")
        expected_id = _stable_id(
            "wiki-promotion-receipt",
            {
                "decision_digest": self.decision_digest,
                "report_digest": self.report_digest,
            },
        )
        if self.receipt_id != expected_id:
            raise ValueError("Wiki promotion receipt ID does not match its content")
        return self


@dataclass(frozen=True)
class _CapturedWiki:
    wiki: LlmWiki
    snapshot: TreeSnapshot
    page: WikiPageRecord
    page_bytes: bytes
    catalog_bytes: bytes
    index_bytes: bytes
    inventory_digest: str


class WikiPromotionLifecycle:
    """Durably consume decisions and produce non-mutating promotion evidence.

    Reviewer identifiers in these records are declarative labels.  This store
    proves exact byte and decision continuity; it does not authenticate a human
    or make an expert-review claim.
    """

    def __init__(self, evidence_root: Path) -> None:
        (
            self.evidence_root,
            self._root_identity,
            self._receipts_identity,
        ) = _prepare_receipt_store(evidence_root)

    @property
    def consumed_decision_digests(self) -> tuple[str, ...]:
        """Read and verify the exact durable decision receipt inventory."""

        with self._locked_receipts() as receipts_fd:
            return tuple(sorted(_read_receipt_inventory(receipts_fd)))

    def finalize(
        self,
        project_root: Path,
        wiki_root: str,
        request: WikiReviewRequest,
        decision: WikiReviewDecision,
    ) -> WikiPromotionResult:
        """Consume one decision, revalidate exact bytes, and emit a plan or rejection."""

        try:
            request = WikiReviewRequest.model_validate(request.model_dump(mode="python"))
        except ValidationError as exc:
            raise PolicyViolation(f"invalid Wiki review request record: {exc}") from exc
        try:
            decision = WikiReviewDecision.model_validate(decision.model_dump(mode="python"))
        except ValidationError as exc:
            raise PolicyViolation(f"invalid Wiki review decision record: {exc}") from exc
        decision_digest = artifact_digest(decision)
        with self._locked_receipts() as receipts_fd:
            receipts = _read_receipt_inventory(receipts_fd)
            if decision_digest in receipts:
                raise PolicyViolation("Wiki review decision has already been consumed")

            # Consumption occurs only after every binding/snapshot/byte check and
            # after the complete immutable result has been constructed.
            _require_decision_binding(request, decision)
            captured = _capture_reviewable_wiki(
                project_root,
                wiki_root,
                page_id=request.page_id,
                as_of=request.as_of,
                repository_revision=request.repository_revision,
            )
            actual_request = _build_request(
                captured,
                requester=request.requester,
                designated_reviewer=request.designated_reviewer,
                as_of=request.as_of,
                repository_revision=request.repository_revision,
            )
            if actual_request != request:
                raise PolicyViolation(
                    "Wiki review request does not match the current exact snapshot"
                )

            request_digest = artifact_digest(request)
            if decision.decision == "reject":
                report = _build_report(
                    request,
                    decision,
                    request_digest=request_digest,
                    decision_digest=decision_digest,
                )
                result = WikiPromotionResult(
                    report=report,
                    source_page_bytes=captured.page_bytes,
                    source_catalog_bytes=captured.catalog_bytes,
                    source_index_bytes=captured.index_bytes,
                    proposed_catalog_bytes=None,
                    proposed_index_bytes=None,
                    reviewed_page_ids=frozenset(),
                )
            else:
                promoted_catalog, catalog_bytes, index_bytes = _render_promotion(
                    captured,
                    request.page_id,
                    decision.reviewed_on,
                )
                replacements = {
                    "catalog.json": catalog_bytes,
                    "index.md": index_bytes,
                }
                report = _build_report(
                    request,
                    decision,
                    request_digest=request_digest,
                    decision_digest=decision_digest,
                    proposed_catalog_digest=artifact_digest(promoted_catalog),
                    proposed_catalog_content_digest=_bytes_digest(catalog_bytes),
                    proposed_index_content_digest=_bytes_digest(index_bytes),
                    proposed_wiki_inventory_digest=_inventory_digest(
                        captured.snapshot,
                        replacements=replacements,
                    ),
                )
                result = WikiPromotionResult(
                    report=report,
                    source_page_bytes=captured.page_bytes,
                    source_catalog_bytes=captured.catalog_bytes,
                    source_index_bytes=captured.index_bytes,
                    proposed_catalog_bytes=catalog_bytes,
                    proposed_index_bytes=index_bytes,
                    reviewed_page_ids=frozenset((request.page_id,)),
                )

            report_digest = artifact_digest(result.report)
            receipt = WikiPromotionReceipt(
                receipt_id=_stable_id(
                    "wiki-promotion-receipt",
                    {
                        "decision_digest": decision_digest,
                        "report_digest": report_digest,
                    },
                ),
                decision_digest=decision_digest,
                report_digest=report_digest,
                report=result.report,
            )
            _persist_receipt(receipts_fd, receipt)
            return result

    @contextmanager
    def _locked_receipts(self) -> Iterator[int]:
        with _open_receipt_store(
            self.evidence_root,
            self._root_identity,
            self._receipts_identity,
        ) as (root_fd, receipts_fd):
            _require_exact_receipt_root(root_fd)
            lock_fd = os.open(".lock", os.O_RDWR | _no_follow_flag(), dir_fd=root_fd)
            try:
                lock_details = os.fstat(lock_fd)
                if (
                    not stat.S_ISREG(lock_details.st_mode)
                    or stat.S_IMODE(lock_details.st_mode) != 0o600
                    or lock_details.st_size != 0
                ):
                    raise PolicyViolation("Wiki governance receipt lock is unsafe")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                yield receipts_fd
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)


def create_wiki_review_request(
    project_root: Path,
    wiki_root: str,
    *,
    page_id: str,
    requester: str,
    designated_reviewer: str,
    as_of: date,
    repository_revision: str,
) -> WikiReviewRequest:
    """Create a deterministic request from a clean, exact, versioned Wiki snapshot."""

    if as_of > date.today():
        raise PolicyViolation("Wiki review as-of date cannot be in the future")
    captured = _capture_reviewable_wiki(
        project_root,
        wiki_root,
        page_id=page_id,
        as_of=as_of,
        repository_revision=repository_revision,
    )
    return _build_request(
        captured,
        requester=requester,
        designated_reviewer=designated_reviewer,
        as_of=as_of,
        repository_revision=repository_revision,
    )


def decide_wiki_review(
    request: WikiReviewRequest,
    *,
    reviewer: str,
    decision: ReviewDecision,
    reviewed_on: date,
) -> WikiReviewDecision:
    """Record an explicit decision without applying or implying a promotion."""

    if reviewed_on > date.today():
        raise PolicyViolation("Wiki review decision date cannot be in the future")
    if reviewed_on - request.page_last_verified > timedelta(days=MAX_PAGE_AGE_DAYS):
        raise PolicyViolation("Wiki page evidence became stale before review decision")
    request_digest = artifact_digest(request)
    values = {
        "governance_policy_version": request.governance_policy_version,
        "decision": decision,
        "requested_action": request.requested_action,
        "request_id": request.request_id,
        "request_digest": request_digest,
        "page_id": request.page_id,
        "page_path": request.page_path,
        "page_content_digest": request.page_content_digest,
        "catalog_digest": request.catalog_digest,
        "wiki_inventory_digest": request.wiki_inventory_digest,
        "repository_revision": request.repository_revision,
        "request_as_of": request.as_of,
        "reviewed_on": reviewed_on,
        "requester": request.requester,
        "designated_reviewer": request.designated_reviewer,
        "reviewer": reviewer,
        "reviewer_identity_assurance": request.reviewer_identity_assurance,
    }
    decision_id = _stable_id("wiki-review-decision", values)
    return WikiReviewDecision(
        decision_id=decision_id,
        governance_policy_version=request.governance_policy_version,
        decision=decision,
        requested_action=request.requested_action,
        request_id=request.request_id,
        request_digest=request_digest,
        page_id=request.page_id,
        page_path=request.page_path,
        page_content_digest=request.page_content_digest,
        catalog_digest=request.catalog_digest,
        wiki_inventory_digest=request.wiki_inventory_digest,
        repository_revision=request.repository_revision,
        request_as_of=request.as_of,
        reviewed_on=reviewed_on,
        requester=request.requester,
        designated_reviewer=request.designated_reviewer,
        reviewer=reviewer,
        reviewer_identity_assurance=request.reviewer_identity_assurance,
    )


def _capture_reviewable_wiki(
    project_root: Path,
    wiki_root: str,
    *,
    page_id: str,
    as_of: date,
    repository_revision: str,
) -> _CapturedWiki:
    root = _require_git_root(project_root)
    normalized_wiki_root = validate_relative_path(wiki_root)
    wiki_path = _confined_wiki_root(root, normalized_wiki_root)
    before = snapshot_tree(wiki_path)
    wiki = LlmWiki.load(wiki_path)
    after = snapshot_tree(wiki_path)
    if before != after:
        raise PolicyViolation("Wiki tree changed while the governance snapshot was captured")
    _require_versioned_snapshot(
        root,
        normalized_wiki_root,
        after,
        repository_revision,
    )
    by_path = after.by_path()
    try:
        page = next(record for record in wiki.catalog.pages if record.page_id == page_id)
    except StopIteration as exc:
        raise PolicyViolation(f"Wiki page is not in the catalog: {page_id}") from exc
    if page.status != "pilot":
        raise PolicyViolation("only an exact pilot Wiki page can request promotion")
    if page.last_verified > as_of:
        raise PolicyViolation("Wiki page verification date is in the future for this review")
    if as_of - page.last_verified > timedelta(days=MAX_PAGE_AGE_DAYS):
        raise PolicyViolation("Wiki page evidence is stale for review")
    if not page.owner or not page.source_version or not page.target_version:
        raise PolicyViolation("Wiki page is missing owner or version governance metadata")
    if len(page.platforms) > 1:
        raise PolicyViolation("Wiki governance requires one platform or workflow scope")
    try:
        _require_authoritative_urls(tuple(source.url for source in page.sources))
    except ValueError as exc:
        raise PolicyViolation(f"Wiki page source governance is invalid: {exc}") from exc

    try:
        page_bytes = by_path[page.path].content
        catalog_bytes = by_path["catalog.json"].content
        index_bytes = by_path["index.md"].content
    except KeyError as exc:
        raise PolicyViolation("Wiki snapshot is missing a governed evidence file") from exc
    try:
        page_body = page_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyViolation("Wiki page is not valid UTF-8") from exc
    if wiki.read_page(page.page_id) != page_body:
        raise PolicyViolation("Wiki loaded page does not match the governed tree snapshot")
    if wiki.catalog_digest != artifact_digest(wiki.catalog):
        raise PolicyViolation("Wiki catalog digest does not match the loaded catalog")
    if wiki.index_digest != _bytes_digest(index_bytes):
        raise PolicyViolation("Wiki index digest does not match the loaded Wiki snapshot")
    _require_confidential_wiki_evidence(wiki, catalog_bytes, index_bytes)

    return _CapturedWiki(
        wiki=wiki,
        snapshot=after,
        page=page,
        page_bytes=page_bytes,
        catalog_bytes=catalog_bytes,
        index_bytes=index_bytes,
        inventory_digest=_inventory_digest(after),
    )


def _build_request(
    captured: _CapturedWiki,
    *,
    requester: str,
    designated_reviewer: str,
    as_of: date,
    repository_revision: str,
) -> WikiReviewRequest:
    record = captured.page
    if not record.platforms:
        platform: WikiPlatform = "workflow"
    elif record.platforms[0].value == "salesforce":
        platform = "salesforce"
    else:
        platform = "mulesoft"
    values = {
        "page_id": record.page_id,
        "page_path": record.path,
        "page_status": record.status,
        "page_content_digest": _bytes_digest(captured.page_bytes),
        "catalog_digest": captured.wiki.catalog_digest,
        "catalog_content_digest": _bytes_digest(captured.catalog_bytes),
        "index_content_digest": _bytes_digest(captured.index_bytes),
        "wiki_inventory_digest": captured.inventory_digest,
        "platform": platform,
        "source_version": record.source_version,
        "target_version": record.target_version,
        "owner": record.owner,
        "authoritative_source_urls": tuple(source.url for source in record.sources),
        "page_last_verified": record.last_verified,
        "as_of": as_of,
        "repository_revision": repository_revision,
        "requester": requester,
        "designated_reviewer": designated_reviewer,
        "reviewer_identity_assurance": "declarative_unverified",
    }
    request_id = _stable_id("wiki-review-request", values)
    return WikiReviewRequest(
        request_id=request_id,
        page_id=record.page_id,
        page_path=record.path,
        page_status="pilot",
        page_content_digest=_bytes_digest(captured.page_bytes),
        catalog_digest=captured.wiki.catalog_digest,
        catalog_content_digest=_bytes_digest(captured.catalog_bytes),
        index_content_digest=_bytes_digest(captured.index_bytes),
        wiki_inventory_digest=captured.inventory_digest,
        platform=platform,
        source_version=record.source_version,
        target_version=record.target_version,
        owner=record.owner,
        authoritative_source_urls=tuple(source.url for source in record.sources),
        page_last_verified=record.last_verified,
        as_of=as_of,
        repository_revision=repository_revision,
        requester=requester,
        designated_reviewer=designated_reviewer,
        reviewer_identity_assurance="declarative_unverified",
    )


def _require_decision_binding(
    request: WikiReviewRequest,
    decision: WikiReviewDecision,
) -> None:
    if request.as_of > date.today():
        raise PolicyViolation("Wiki review request as-of date is in the future")
    if decision.reviewed_on > date.today():
        raise PolicyViolation("Wiki review decision date is in the future")
    if decision.reviewed_on - request.page_last_verified > timedelta(days=MAX_PAGE_AGE_DAYS):
        raise PolicyViolation("Wiki page evidence is stale for finalization")
    expected = {
        "governance_policy_version": request.governance_policy_version,
        "requested_action": request.requested_action,
        "request_id": request.request_id,
        "request_digest": artifact_digest(request),
        "page_id": request.page_id,
        "page_path": request.page_path,
        "page_content_digest": request.page_content_digest,
        "catalog_digest": request.catalog_digest,
        "wiki_inventory_digest": request.wiki_inventory_digest,
        "repository_revision": request.repository_revision,
        "request_as_of": request.as_of,
        "requester": request.requester,
        "designated_reviewer": request.designated_reviewer,
        "reviewer": request.designated_reviewer,
        "reviewer_identity_assurance": request.reviewer_identity_assurance,
    }
    for field_name, expected_value in expected.items():
        if getattr(decision, field_name) != expected_value:
            raise PolicyViolation(f"Wiki decision {field_name} does not match its request")
    if decision.requested_action != REQUESTED_ACTION:
        raise PolicyViolation("Wiki decision does not authorize promote_to_reviewed")
    if decision.reviewer == decision.requester:
        raise PolicyViolation("Wiki requester cannot review their own promotion")
    expected_id = _stable_id(
        "wiki-review-decision",
        {
            field_name: getattr(decision, field_name)
            for field_name in (
                "governance_policy_version",
                "decision",
                "requested_action",
                "request_id",
                "request_digest",
                "page_id",
                "page_path",
                "page_content_digest",
                "catalog_digest",
                "wiki_inventory_digest",
                "repository_revision",
                "request_as_of",
                "reviewed_on",
                "requester",
                "designated_reviewer",
                "reviewer",
                "reviewer_identity_assurance",
            )
        },
    )
    if decision.decision_id != expected_id:
        raise PolicyViolation("Wiki decision ID does not match its bound decision content")


def _render_promotion(
    captured: _CapturedWiki,
    page_id: str,
    reviewed_on: date,
) -> tuple[WikiCatalog, bytes, bytes]:
    pages = tuple(
        record.model_copy(update={"status": "reviewed", "last_verified": reviewed_on})
        if record.page_id == page_id
        else record
        for record in captured.wiki.catalog.pages
    )
    promoted_catalog = WikiCatalog.model_validate(
        captured.wiki.catalog.model_copy(update={"pages": pages})
    )
    catalog_bytes = canonical_json_bytes(promoted_catalog) + b"\n"
    loaded_pages = {
        record.page_id: _LoadedPage(
            record=record,
            body=captured.wiki.read_page(record.page_id),
        )
        for record in promoted_catalog.pages
    }
    index_bytes = render_wiki_index(promoted_catalog, loaded_pages).encode("utf-8")
    return promoted_catalog, catalog_bytes, index_bytes


def _build_report(
    request: WikiReviewRequest,
    decision: WikiReviewDecision,
    *,
    request_digest: str,
    decision_digest: str,
    proposed_catalog_digest: str | None = None,
    proposed_catalog_content_digest: str | None = None,
    proposed_index_content_digest: str | None = None,
    proposed_wiki_inventory_digest: str | None = None,
) -> WikiPromotionReport:
    approved = decision.decision == "approve"
    status: Literal["promotion_planned", "rejected"] = (
        "promotion_planned" if approved else "rejected"
    )
    proposed_status: Literal["reviewed"] | None = "reviewed" if approved else None
    reviewed_page_ids: tuple[str, ...] = (request.page_id,) if approved else ()
    values = {
        "status": status,
        "decision": decision.decision,
        "requested_action": request.requested_action,
        "request_id": request.request_id,
        "request_digest": request_digest,
        "decision_id": decision.decision_id,
        "decision_digest": decision_digest,
        "page_id": request.page_id,
        "page_path": request.page_path,
        "original_page_status": request.page_status,
        "proposed_page_status": proposed_status,
        "page_content_digest": request.page_content_digest,
        "catalog_digest": request.catalog_digest,
        "catalog_content_digest": request.catalog_content_digest,
        "index_content_digest": request.index_content_digest,
        "wiki_inventory_digest": request.wiki_inventory_digest,
        "proposed_catalog_digest": proposed_catalog_digest,
        "proposed_catalog_content_digest": proposed_catalog_content_digest,
        "proposed_index_content_digest": proposed_index_content_digest,
        "proposed_wiki_inventory_digest": proposed_wiki_inventory_digest,
        "verified_reviewed_page_ids": reviewed_page_ids,
        "platform": request.platform,
        "source_version": request.source_version,
        "target_version": request.target_version,
        "owner": request.owner,
        "authoritative_source_urls": request.authoritative_source_urls,
        "page_last_verified": request.page_last_verified,
        "request_as_of": request.as_of,
        "reviewed_on": decision.reviewed_on,
        "repository_revision": request.repository_revision,
        "requester": request.requester,
        "reviewer": decision.reviewer,
        "reviewer_identity_assurance": decision.reviewer_identity_assurance,
    }
    report_id = _stable_id("wiki-promotion-report", values)
    return WikiPromotionReport(
        report_id=report_id,
        status=status,
        decision=decision.decision,
        requested_action=request.requested_action,
        request_id=request.request_id,
        request_digest=request_digest,
        decision_id=decision.decision_id,
        decision_digest=decision_digest,
        page_id=request.page_id,
        page_path=request.page_path,
        original_page_status=request.page_status,
        proposed_page_status=proposed_status,
        page_content_digest=request.page_content_digest,
        catalog_digest=request.catalog_digest,
        catalog_content_digest=request.catalog_content_digest,
        index_content_digest=request.index_content_digest,
        wiki_inventory_digest=request.wiki_inventory_digest,
        proposed_catalog_digest=proposed_catalog_digest,
        proposed_catalog_content_digest=proposed_catalog_content_digest,
        proposed_index_content_digest=proposed_index_content_digest,
        proposed_wiki_inventory_digest=proposed_wiki_inventory_digest,
        verified_reviewed_page_ids=reviewed_page_ids,
        platform=request.platform,
        source_version=request.source_version,
        target_version=request.target_version,
        owner=request.owner,
        authoritative_source_urls=request.authoritative_source_urls,
        page_last_verified=request.page_last_verified,
        request_as_of=request.as_of,
        reviewed_on=decision.reviewed_on,
        repository_revision=request.repository_revision,
        requester=request.requester,
        reviewer=decision.reviewer,
        reviewer_identity_assurance=decision.reviewer_identity_assurance,
    )


def _prepare_receipt_store(
    evidence_root: Path,
) -> tuple[Path, tuple[int, int], tuple[int, int]]:
    candidate = Path(evidence_root)
    try:
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = candidate.lstat()
    except OSError as exc:
        raise PolicyViolation("Wiki governance evidence root cannot be prepared") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PolicyViolation("Wiki governance evidence root must be a non-symlink directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PolicyViolation("Wiki governance evidence root must use mode 0700")
    root = candidate.resolve(strict=True)
    root_fd = os.open(root, os.O_RDONLY | _directory_flag() | _no_follow_flag())
    try:
        try:
            os.mkdir("receipts", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        receipts_fd = os.open(
            "receipts",
            os.O_RDONLY | _directory_flag() | _no_follow_flag(),
            dir_fd=root_fd,
        )
        try:
            receipts_details = os.fstat(receipts_fd)
            if stat.S_IMODE(receipts_details.st_mode) != 0o700:
                raise PolicyViolation("Wiki governance receipts directory must use mode 0700")
            try:
                lock_fd = os.open(
                    ".lock",
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
                    0o600,
                    dir_fd=root_fd,
                )
            except FileExistsError:
                lock_fd = os.open(
                    ".lock",
                    os.O_RDWR | _no_follow_flag(),
                    dir_fd=root_fd,
                )
            try:
                lock_details = os.fstat(lock_fd)
                if (
                    not stat.S_ISREG(lock_details.st_mode)
                    or stat.S_IMODE(lock_details.st_mode) != 0o600
                    or lock_details.st_size != 0
                ):
                    raise PolicyViolation("Wiki governance receipt lock is unsafe")
            finally:
                os.close(lock_fd)
            _require_exact_receipt_root(root_fd)
            root_details = os.fstat(root_fd)
            return (
                root,
                (root_details.st_dev, root_details.st_ino),
                (receipts_details.st_dev, receipts_details.st_ino),
            )
        finally:
            os.close(receipts_fd)
    except OSError as exc:
        raise PolicyViolation("Wiki governance receipt store layout is unsafe") from exc
    finally:
        os.close(root_fd)


@contextmanager
def _open_receipt_store(
    evidence_root: Path,
    root_identity: tuple[int, int],
    receipts_identity: tuple[int, int],
) -> Iterator[tuple[int, int]]:
    descriptors: list[int] = []
    try:
        root_fd = os.open(
            evidence_root,
            os.O_RDONLY | _directory_flag() | _no_follow_flag(),
        )
        descriptors.append(root_fd)
        root_details = os.fstat(root_fd)
        if (root_details.st_dev, root_details.st_ino) != root_identity:
            raise PolicyViolation("Wiki governance evidence root identity changed")
        receipts_fd = os.open(
            "receipts",
            os.O_RDONLY | _directory_flag() | _no_follow_flag(),
            dir_fd=root_fd,
        )
        descriptors.append(receipts_fd)
        receipts_details = os.fstat(receipts_fd)
        if (receipts_details.st_dev, receipts_details.st_ino) != receipts_identity:
            raise PolicyViolation("Wiki governance receipts directory identity changed")
        if stat.S_IMODE(root_details.st_mode) != 0o700:
            raise PolicyViolation("Wiki governance evidence root mode changed")
        if stat.S_IMODE(receipts_details.st_mode) != 0o700:
            raise PolicyViolation("Wiki governance receipts directory mode changed")
        yield root_fd, receipts_fd
    except OSError as exc:
        raise PolicyViolation("Wiki governance receipt store layout is unsafe") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _require_exact_receipt_root(root_fd: int) -> None:
    if set(os.listdir(root_fd)) != {".lock", "receipts"}:
        raise PolicyViolation("Wiki governance evidence root contains unexpected content")


def _read_receipt_inventory(receipts_fd: int) -> dict[str, WikiPromotionReceipt]:
    receipts: dict[str, WikiPromotionReceipt] = {}
    for name in sorted(os.listdir(receipts_fd)):
        match = _RECEIPT_NAME.fullmatch(name)
        if match is None:
            raise PolicyViolation("Wiki governance receipts contain unexpected content")
        receipt = _read_receipt_at(receipts_fd, name)
        decision_digest = f"sha256:{match.group('digest')}"
        if receipt.decision_digest != decision_digest:
            raise PolicyViolation("Wiki governance receipt filename does not match its decision")
        if decision_digest in receipts:
            raise PolicyViolation("Wiki governance contains duplicate decision receipts")
        receipts[decision_digest] = receipt
    return receipts


def _read_receipt_at(receipts_fd: int, name: str) -> WikiPromotionReceipt:
    try:
        expected = os.stat(name, dir_fd=receipts_fd, follow_symlinks=False)
    except OSError as exc:
        raise PolicyViolation("Wiki governance receipt cannot be inspected safely") from exc
    if (
        stat.S_ISLNK(expected.st_mode)
        or not stat.S_ISREG(expected.st_mode)
        or stat.S_IMODE(expected.st_mode) != 0o600
    ):
        raise PolicyViolation("Wiki governance receipt must be a private regular file")
    if expected.st_size <= 0 or expected.st_size > MAX_RECEIPT_BYTES:
        raise PolicyViolation("Wiki governance receipt has an invalid size")
    try:
        descriptor = os.open(name, os.O_RDONLY | _no_follow_flag(), dir_fd=receipts_fd)
    except OSError as exc:
        raise PolicyViolation("Wiki governance receipt cannot be opened safely") from exc
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise PolicyViolation("Wiki governance receipt changed while being opened")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        if len(payload) != actual.st_size:
            raise PolicyViolation("Wiki governance receipt changed while being read")
    finally:
        os.close(descriptor)
    try:
        return WikiPromotionReceipt.model_validate(json.loads(payload.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise PolicyViolation("Wiki governance receipt is malformed or invalid") from exc


def _persist_receipt(receipts_fd: int, receipt: WikiPromotionReceipt) -> None:
    digest_hex = receipt.decision_digest.split(":", 1)[1]
    leaf = f"{digest_hex}.json"
    payload = canonical_json_bytes(receipt) + b"\n"
    if len(payload) > MAX_RECEIPT_BYTES:
        raise PolicyViolation("Wiki governance receipt exceeds the size limit")
    if leaf in os.listdir(receipts_fd):
        _read_receipt_at(receipts_fd, leaf)
        raise PolicyViolation("Wiki review decision has already been consumed")

    temporary_name = f".{digest_hex}.{uuid4().hex}.tmp"
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
            0o600,
            dir_fd=receipts_fd,
        )
        try:
            _write_all(temporary_fd, payload)
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)
        try:
            os.link(
                temporary_name,
                leaf,
                src_dir_fd=receipts_fd,
                dst_dir_fd=receipts_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            _read_receipt_at(receipts_fd, leaf)
            raise PolicyViolation("Wiki review decision has already been consumed") from None
        os.fsync(receipts_fd)
        persisted = _read_receipt_at(receipts_fd, leaf)
        if persisted != receipt:
            raise PolicyViolation("persisted Wiki governance receipt does not match its result")
    except OSError as exc:
        raise PolicyViolation("Wiki governance receipt could not be persisted safely") from exc
    finally:
        try:
            os.unlink(temporary_name, dir_fd=receipts_fd)
        except FileNotFoundError:
            pass


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("Wiki governance receipt write did not make progress")
        remaining = remaining[written:]


def _no_follow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as exc:  # pragma: no cover - supported target is POSIX
        raise RuntimeError("Wiki governance evidence requires O_NOFOLLOW") from exc


def _directory_flag() -> int:
    try:
        return os.O_DIRECTORY
    except AttributeError as exc:  # pragma: no cover - supported target is POSIX
        raise RuntimeError("Wiki governance evidence requires O_DIRECTORY") from exc


def _require_git_root(project_root: Path) -> Path:
    candidate = Path(project_root)
    try:
        metadata = candidate.lstat()
    except FileNotFoundError as exc:
        raise PolicyViolation("project root does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PolicyViolation("project root must be a non-symlink directory")
    root = candidate.resolve(strict=True)
    result = _run_git(root, ("rev-parse", "--show-toplevel"), check=True)
    try:
        reported = Path(result.stdout.decode("utf-8").strip()).resolve(strict=True)
    except (OSError, UnicodeError) as exc:
        raise PolicyViolation("Git returned an invalid repository root") from exc
    if reported != root:
        raise PolicyViolation("project root must be the Git repository root")
    return root


def _confined_wiki_root(root: Path, wiki_root: str) -> Path:
    candidate = root.joinpath(*wiki_root.split("/"))
    current = root
    for part in wiki_root.split("/"):
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise PolicyViolation(f"Wiki root component is missing: {part}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PolicyViolation("Wiki root path cannot contain symlinks")
        if not stat.S_ISDIR(metadata.st_mode):
            raise PolicyViolation("Wiki root path must contain only directories")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PolicyViolation("Wiki root escapes the repository") from exc
    return resolved


def _require_versioned_snapshot(
    root: Path,
    wiki_root: str,
    snapshot: TreeSnapshot,
    expected_revision: str,
) -> None:
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", expected_revision):
        raise PolicyViolation("claimed Wiki repository revision is malformed or unversioned")
    head = _git_head(root)
    if head != expected_revision:
        raise PolicyViolation("repository HEAD does not match the claimed Wiki revision")
    if _git_status(root):
        raise PolicyViolation("Wiki review requires a clean repository worktree")
    tracked = _git_tree_files(root, expected_revision, wiki_root)
    snapshot_files = {entry.path: entry.content for entry in snapshot.entries}
    if set(tracked) != set(snapshot_files):
        raise PolicyViolation("Wiki inventory does not match the claimed Git revision")
    expected_directories = {
        PurePosixPath(path).parent.as_posix()
        for path in snapshot_files
        if PurePosixPath(path).parent.as_posix() != "."
    }
    expected_directories |= {
        parent.as_posix()
        for path in tuple(expected_directories)
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    }
    if set(snapshot.directories) != expected_directories:
        raise PolicyViolation("Wiki contains directories not represented by versioned files")
    for path, payload in snapshot_files.items():
        if tracked[path] != payload:
            raise PolicyViolation(f"Wiki file differs from the claimed revision: {path}")
    if _git_head(root) != expected_revision or _git_status(root):
        raise PolicyViolation("repository revision or worktree changed during Wiki review")


def _git_tree_files(root: Path, revision: str, wiki_root: str) -> dict[str, bytes]:
    listing = _run_git(
        root,
        ("ls-tree", "-r", "-z", "--full-tree", revision, "--", wiki_root),
        check=True,
    ).stdout
    files: dict[str, bytes] = {}
    prefix = f"{wiki_root}/"
    for raw_entry in (entry for entry in listing.split(b"\x00") if entry):
        try:
            header, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, object_id = header.split(b" ", 2)
            full_path = raw_path.decode("utf-8")
            decoded_mode = mode.decode("ascii")
            decoded_type = object_type.decode("ascii")
            decoded_id = object_id.decode("ascii")
        except (ValueError, UnicodeError) as exc:
            raise PolicyViolation("Git returned malformed Wiki tree data") from exc
        if not full_path.startswith(prefix):
            raise PolicyViolation("Git returned a Wiki path outside the requested root")
        relative = validate_relative_path(full_path[len(prefix) :])
        if decoded_type != "blob" or decoded_mode not in {"100644", "100755"}:
            raise PolicyViolation(f"Wiki revision contains a non-regular file: {relative}")
        payload = _run_git(root, ("cat-file", "blob", decoded_id), check=True).stdout
        if len(payload) > MAX_GOVERNED_FILE_BYTES:
            raise PolicyViolation(f"Wiki file exceeds the governance size limit: {relative}")
        files[relative] = payload
    if sum(len(payload) for payload in files.values()) > MAX_GOVERNED_TREE_BYTES:
        raise PolicyViolation("Wiki tree exceeds the governance size limit")
    return files


def _git_head(root: Path) -> str | None:
    result = _run_git(root, ("rev-parse", "--verify", "HEAD^{commit}"), check=False)
    if result.returncode != 0:
        return None
    try:
        revision = result.stdout.decode("ascii").strip()
    except UnicodeError as exc:
        raise PolicyViolation("Git returned a non-ASCII HEAD revision") from exc
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision):
        raise PolicyViolation("Git returned a malformed HEAD revision")
    return revision


def _git_status(root: Path) -> bytes:
    return _run_git(
        root,
        ("status", "--porcelain=v1", "--untracked-files=all"),
        check=True,
    ).stdout


def _run_git(
    root: Path,
    args: Sequence[str],
    *,
    check: bool,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            ("git", *args),
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PolicyViolation(f"Git command failed to execute: git {args[0]}") from exc
    if check and completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise PolicyViolation(f"Git command failed: git {args[0]}: {detail[:500]}")
    return completed


def _require_authoritative_urls(values: tuple[AnyHttpUrl, ...]) -> None:
    if not values:
        raise ValueError("Wiki review requires at least one authoritative source URL")
    normalized = tuple(str(value) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError("Wiki authoritative source URLs must be unique")
    for value in values:
        if value.scheme != "https" or value.host is None:
            raise ValueError("Wiki authoritative source URLs must use HTTPS")
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("Wiki authoritative source URLs cannot contain credentials or queries")


def _require_confidential_wiki_evidence(
    wiki: LlmWiki,
    catalog_bytes: bytes,
    index_bytes: bytes,
) -> None:
    bodies = [catalog_bytes, index_bytes]
    bodies.extend(wiki.read_page(record.page_id).encode("utf-8") for record in wiki.catalog.pages)
    redactor = SecretRedactor()
    for payload in bodies:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyViolation("Wiki governance evidence must be valid UTF-8") from exc
        if redactor.redact(text).findings:
            raise PolicyViolation("Wiki governance refuses evidence containing credential material")
        if _ABSOLUTE_USER_PATH.search(text):
            raise PolicyViolation("Wiki governance refuses evidence containing absolute user paths")


def _require_portable_text(value: str, *, role: str) -> None:
    if _ABSOLUTE_USER_PATH.search(value) or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{role} must not contain absolute paths or control lines")
    if SecretRedactor().redact(value).findings:
        raise ValueError(f"{role} must not contain credential material")


def _inventory_digest(
    snapshot: TreeSnapshot,
    *,
    replacements: dict[str, bytes] | None = None,
) -> str:
    replacement_bytes = replacements or {}
    return artifact_digest(
        {
            "directories": snapshot.directories,
            "files": tuple(
                {
                    "path": entry.path,
                    "content_digest": _bytes_digest(
                        replacement_bytes.get(entry.path, entry.content)
                    ),
                    "size_bytes": len(replacement_bytes.get(entry.path, entry.content)),
                    "mode": entry.mode,
                }
                for entry in snapshot.entries
            ),
        }
    )


def _bytes_digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _stable_id(prefix: str, value: object) -> str:
    portable = _portable_hash_value(value)
    return f"{prefix}-{artifact_digest(portable).split(':', 1)[1][:20]}"


def _portable_hash_value(value: object) -> object:
    """Convert the bounded governance ID input to canonical JSON values."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, AnyHttpUrl):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _portable_hash_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_portable_hash_value(item) for item in value)
    raise TypeError(f"unsupported Wiki governance ID input: {type(value).__name__}")


__all__ = [
    "GOVERNANCE_POLICY_VERSION",
    "WikiPromotionLifecycle",
    "WikiPromotionReceipt",
    "WikiPromotionReport",
    "WikiPromotionResult",
    "WikiReviewDecision",
    "WikiReviewRequest",
    "create_wiki_review_request",
    "decide_wiki_review",
]
