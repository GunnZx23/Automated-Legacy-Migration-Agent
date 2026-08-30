from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.submission_review import (
    MAX_ATTESTATION_BYTES,
    SUPPLEMENTAL_NOTICE,
    ExternalCandidateReviewAttestation,
    SubmissionReviewError,
    load_external_candidate_review_attestation,
)


def _digest(seed: int) -> str:
    return f"sha256:{seed:064x}"


def _attestation() -> ExternalCandidateReviewAttestation:
    return ExternalCandidateReviewAttestation(
        attestation_id="external-candidate-review-case-1",
        run_id="ui-run-case-1",
        thread_id="ui-thread-case-1",
        request_id="ui-request-case-1",
        final_review_id="final-review-case-1",
        final_review_request_digest=_digest(1),
        change_set_digest=_digest(2),
        validation_report_digest=_digest(3),
        candidate_zip_digest=_digest(4),
        reviewer_id="BW",
        reviewer_relationship_and_expertise="Manager",
        review_completed_at_raw="11 AM PST 30st August 2026",
        reviewed_at_normalized=datetime(
            2026,
            8,
            30,
            11,
            tzinfo=timezone(-timedelta(hours=8)),
        ),
        decision="accepted",
        reviewer_comment="Approved",
        candidate_reviewed=True,
        diff_reviewed=True,
        test_evidence_reviewed=True,
    )


def _write_attestation(path: Path, value: ExternalCandidateReviewAttestation) -> None:
    path.write_text(
        json.dumps(value.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )


def test_loads_exact_digest_bound_additive_attestation(tmp_path: Path) -> None:
    expected = _attestation()
    path = tmp_path / "external-candidate-review.json"
    _write_attestation(path, expected)

    loaded = load_external_candidate_review_attestation(
        path,
        expected_artifact_digest=artifact_digest(expected),
    )

    assert loaded == expected
    assert loaded.review_completed_at_raw == "11 AM PST 30st August 2026"
    assert loaded.reviewed_at_normalized.utcoffset() == -timedelta(hours=8)
    assert loaded.identity_assurance == "declarative_unverified"
    assert loaded.authority_granted is False
    assert loaded.external_actions_authorized == ()
    assert loaded.supplemental_notice == SUPPLEMENTAL_NOTICE


def test_acceptance_requires_all_three_review_scopes_and_strict_booleans() -> None:
    payload = _attestation().model_dump(mode="python")
    with pytest.raises(ValidationError, match="confirmed candidate, diff, and test evidence"):
        ExternalCandidateReviewAttestation.model_validate(
            payload | {"test_evidence_reviewed": False}
        )
    with pytest.raises(ValidationError):
        ExternalCandidateReviewAttestation.model_validate(payload | {"candidate_reviewed": "true"})


def test_rejects_naive_time_and_any_authority_claim() -> None:
    payload = _attestation().model_dump(mode="python")
    with pytest.raises(ValidationError, match="timezone-aware"):
        ExternalCandidateReviewAttestation.model_validate(
            payload | {"reviewed_at_normalized": datetime(2026, 8, 30, 11)}
        )
    with pytest.raises(ValidationError):
        ExternalCandidateReviewAttestation.model_validate(payload | {"authority_granted": True})
    with pytest.raises(ValidationError):
        ExternalCandidateReviewAttestation.model_validate(
            payload | {"external_actions_authorized": ("deploy",)}
        )
    with pytest.raises(ValidationError):
        ExternalCandidateReviewAttestation.model_validate(
            payload | {"supplemental_notice": "This replaces the in-run review."}
        )


def test_loader_rejects_digest_mismatch_duplicate_keys_and_oversize(tmp_path: Path) -> None:
    path = tmp_path / "external-candidate-review.json"
    _write_attestation(path, _attestation())
    with pytest.raises(SubmissionReviewError, match="digest mismatch"):
        load_external_candidate_review_attestation(path, expected_artifact_digest=_digest(9))

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"2.0","schema_version":"2.0"}', encoding="utf-8")
    with pytest.raises(SubmissionReviewError, match="duplicate JSON key"):
        load_external_candidate_review_attestation(duplicate)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * MAX_ATTESTATION_BYTES + b"}")
    with pytest.raises(SubmissionReviewError, match="size is outside"):
        load_external_candidate_review_attestation(oversized)


def test_loader_rejects_leaf_and_intermediate_symlinks_and_nonregular_files(
    tmp_path: Path,
) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    real = real_directory / "review.json"
    _write_attestation(real, _attestation())

    leaf_link = tmp_path / "review-link.json"
    leaf_link.symlink_to(real)
    with pytest.raises(SubmissionReviewError, match="non-symlink"):
        load_external_candidate_review_attestation(leaf_link)

    directory_link = tmp_path / "directory-link"
    directory_link.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(SubmissionReviewError, match="symlink or non-directory"):
        load_external_candidate_review_attestation(directory_link / "review.json")

    with pytest.raises(SubmissionReviewError, match="regular non-symlink"):
        load_external_candidate_review_attestation(real_directory)


def test_checked_in_schema_matches_contract() -> None:
    schema_path = (
        Path(__file__).parents[1]
        / "schemas"
        / "v2.0"
        / "ExternalCandidateReviewAttestation.schema.json"
    )
    assert json.loads(schema_path.read_text(encoding="utf-8")) == (
        ExternalCandidateReviewAttestation.model_json_schema()
    )
