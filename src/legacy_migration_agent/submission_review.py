"""Additive external review attestations for submission evidence.

This module deliberately does not mutate agent-run evidence or grant workflow
authority.  It loads one reviewer-supplied, digest-bound statement that can be
stored alongside an immutable run after the reviewer has inspected its output.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import (
    Field,
    StrictBool,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from legacy_migration_agent.contracts import (
    ActorIdentifier,
    Identifier,
    Sha256Digest,
    StrictModel,
)
from legacy_migration_agent.core.integrity import artifact_digest

MAX_ATTESTATION_BYTES: Final = 64 * 1024
SUPPLEMENTAL_NOTICE: Final = (
    "This external attestation supplements the immutable in-run review click; "
    "it does not rewrite that click, authenticate reviewer identity, grant controller "
    "authority, or authorize external actions."
)
_DIGEST_ADAPTER = TypeAdapter(Sha256Digest)


class SubmissionReviewError(ValueError):
    """Raised when external candidate-review evidence cannot be safely verified."""


class ExternalCandidateReviewAttestation(StrictModel):
    """One bounded, non-authorizing human review statement for an exact candidate."""

    schema_version: Literal["2.0"] = "2.0"
    attestation_id: Identifier
    run_id: Identifier
    thread_id: Identifier
    request_id: Identifier
    final_review_id: Identifier
    final_review_request_digest: Sha256Digest
    change_set_digest: Sha256Digest
    validation_report_digest: Sha256Digest
    candidate_zip_digest: Sha256Digest
    reviewer_id: ActorIdentifier
    reviewer_relationship_and_expertise: str = Field(min_length=1, max_length=500)
    review_completed_at_raw: str = Field(min_length=1, max_length=200)
    reviewed_at_normalized: datetime
    decision: Literal["accepted", "rejected"]
    reviewer_comment: str = Field(min_length=1, max_length=2000)
    candidate_reviewed: StrictBool
    diff_reviewed: StrictBool
    test_evidence_reviewed: StrictBool
    identity_assurance: Literal["declarative_unverified"] = "declarative_unverified"
    authority_granted: Literal[False] = False
    external_actions_authorized: tuple[()] = ()
    supplemental_notice: Literal[
        "This external attestation supplements the immutable in-run review click; "
        "it does not rewrite that click, authenticate reviewer identity, grant controller "
        "authority, or authorize external actions."
    ] = SUPPLEMENTAL_NOTICE

    @field_validator("reviewed_at_normalized")
    @classmethod
    def require_timezone_aware_review_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("normalized review time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def require_complete_scope_for_acceptance(self) -> ExternalCandidateReviewAttestation:
        if self.decision == "accepted" and not (
            self.candidate_reviewed and self.diff_reviewed and self.test_evidence_reviewed
        ):
            raise ValueError(
                "accepted external review requires confirmed candidate, diff, and test evidence"
            )
        return self


def load_external_candidate_review_attestation(
    path: str | Path,
    *,
    expected_artifact_digest: Sha256Digest | None = None,
) -> ExternalCandidateReviewAttestation:
    """Safely load one strict attestation and optionally verify its canonical digest."""

    location = Path(path)
    raw = _read_bounded_regular_file(location)
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        attestation = ExternalCandidateReviewAttestation.model_validate(payload)
    except SubmissionReviewError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise SubmissionReviewError(
            f"external candidate review attestation is invalid: {location}"
        ) from exc

    if expected_artifact_digest is not None:
        try:
            expected = _DIGEST_ADAPTER.validate_python(expected_artifact_digest, strict=True)
        except ValidationError as exc:
            raise SubmissionReviewError("expected attestation digest is invalid") from exc
        if artifact_digest(attestation) != expected:
            raise SubmissionReviewError("external candidate review attestation digest mismatch")
    return attestation


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SubmissionReviewError(
                f"external candidate review attestation contains duplicate JSON key: {key}"
            )
        value[key] = item
    return value


def _read_bounded_regular_file(path: Path) -> bytes:
    """Read without following a symlink in any path component."""

    absolute = path.absolute()
    parts = absolute.parts
    if not parts or len(parts) == 1:
        raise SubmissionReviewError("external candidate review path must name a file")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _no_follow_flag()
    file_flags = os.O_RDONLY | _no_follow_flag()
    descriptors: list[int] = []
    try:
        directory = os.open(absolute.anchor, directory_flags)
        descriptors.append(directory)
        for part in parts[1:-1]:
            try:
                metadata = os.stat(part, dir_fd=directory, follow_symlinks=False)
            except OSError as exc:
                raise SubmissionReviewError(
                    f"external candidate review path is unsafe: {path}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise SubmissionReviewError(
                    f"external candidate review path contains a symlink or non-directory: {path}"
                )
            try:
                child = os.open(part, directory_flags, dir_fd=directory)
            except OSError as exc:
                raise SubmissionReviewError(
                    f"external candidate review path is unsafe: {path}"
                ) from exc
            descriptors.append(child)
            directory = child

        leaf = parts[-1]
        try:
            metadata = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
        except OSError as exc:
            raise SubmissionReviewError(
                f"external candidate review file is unavailable: {path}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SubmissionReviewError(
                f"external candidate review path must be a regular non-symlink file: {path}"
            )
        if not 1 <= metadata.st_size <= MAX_ATTESTATION_BYTES:
            raise SubmissionReviewError("external candidate review file size is outside the limit")

        try:
            descriptor = os.open(leaf, file_flags, dir_fd=directory)
        except OSError as exc:
            raise SubmissionReviewError(
                f"external candidate review file is unsafe: {path}"
            ) from exc
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (metadata.st_dev, metadata.st_ino):
            raise SubmissionReviewError("external candidate review file changed while opening")

        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, 16 * 1024):
            total += len(chunk)
            if total > MAX_ATTESTATION_BYTES:
                raise SubmissionReviewError(
                    "external candidate review file size is outside the limit"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise SubmissionReviewError("external candidate review file changed while reading")
        return b"".join(chunks)
    except SubmissionReviewError:
        raise
    except OSError as exc:
        raise SubmissionReviewError(
            f"external candidate review file cannot be read: {path}"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _no_follow_flag() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


__all__ = [
    "ExternalCandidateReviewAttestation",
    "MAX_ATTESTATION_BYTES",
    "SUPPLEMENTAL_NOTICE",
    "SubmissionReviewError",
    "load_external_candidate_review_attestation",
]
