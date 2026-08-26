from __future__ import annotations

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.knowledge.wiki import WikiCatalog, _LoadedPage, render_wiki_index
from legacy_migration_agent.knowledge.wiki_governance import (
    WikiPromotionLifecycle,
    WikiPromotionResult,
    WikiReviewDecision,
    WikiReviewRequest,
    create_wiki_review_request,
    decide_wiki_review,
)

WIKI_ROOT = "knowledge/wiki"
PAGE_ID = "synthetic-page"
PAGE_PATH = "pages/synthetic-page.md"
TODAY = date.today()


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit_all(repository: Path, message: str = "synthetic Wiki") -> str:
    _git(repository, "add", "--all")
    _git(
        repository,
        "-c",
        "user.name=Wiki Governance Test",
        "-c",
        "user.email=wiki-governance@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        message,
    )
    return _git(repository, "rev-parse", "HEAD")


def _write_catalog(repository: Path, page_updates: dict[str, object] | None = None) -> None:
    wiki_root = repository / WIKI_ROOT
    page: dict[str, object] = {
        "page_id": PAGE_ID,
        "title": "Synthetic governed page",
        "path": PAGE_PATH,
        "summary": "Synthetic evidence used only by the governance test fixture.",
        "platforms": ["salesforce"],
        "source_version": "Salesforce API 67.0",
        "target_version": "Salesforce API 67.0",
        "status": "pilot",
        "owner": "synthetic-owner",
        "last_verified": TODAY.isoformat(),
        "tags": ["synthetic", "governance"],
        "links": [],
        "sources": [
            {
                "title": "Synthetic authoritative documentation",
                "url": "https://example.test/wiki-governance",
                "publisher": "Example",
                "authority": "official",
            }
        ],
    }
    page.update(page_updates or {})
    raw_catalog = {"schema_version": "1.0", "pages": [page]}
    catalog = WikiCatalog.model_validate(raw_catalog)
    catalog_path = wiki_root / "catalog.json"
    catalog_path.write_text(json.dumps(raw_catalog, indent=2) + "\n", encoding="utf-8")
    record = catalog.pages[0]
    body = (wiki_root / record.path).read_text(encoding="utf-8")
    index = render_wiki_index(
        catalog,
        {record.page_id: _LoadedPage(record=record, body=body)},
    )
    (wiki_root / "index.md").write_text(index, encoding="utf-8")


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    page = repository / WIKI_ROOT / PAGE_PATH
    page.parent.mkdir(parents=True)
    page.write_text(
        "# Synthetic governed page\n\nThis page contains bounded migration evidence.\n",
        encoding="utf-8",
    )
    (repository / ".gitignore").write_text(
        "knowledge/wiki/ignored-runtime.txt\n",
        encoding="utf-8",
    )
    _write_catalog(repository)
    _git(repository, "init", "--quiet")
    return repository, _commit_all(repository)


def _request(repository: Path, revision: str) -> WikiReviewRequest:
    return create_wiki_review_request(
        repository,
        WIKI_ROOT,
        page_id=PAGE_ID,
        requester="synthetic-requester",
        designated_reviewer="synthetic-reviewer",
        as_of=TODAY,
        repository_revision=revision,
    )


def _lifecycle(tmp_path: Path) -> WikiPromotionLifecycle:
    return WikiPromotionLifecycle(tmp_path / "wiki-governance-evidence")


def _tamper_result_bytes(
    result: WikiPromotionResult,
    field_name: str,
) -> WikiPromotionResult:
    if field_name == "source_page_bytes":
        return replace(result, source_page_bytes=b"tampered result bytes")
    if field_name == "source_catalog_bytes":
        return replace(result, source_catalog_bytes=b"tampered result bytes")
    if field_name == "source_index_bytes":
        return replace(result, source_index_bytes=b"tampered result bytes")
    if field_name == "proposed_catalog_bytes":
        return replace(result, proposed_catalog_bytes=b"tampered result bytes")
    if field_name == "proposed_index_bytes":
        return replace(result, proposed_index_bytes=b"tampered result bytes")
    raise AssertionError(f"unexpected result byte field: {field_name}")


def test_approved_decision_produces_exact_in_memory_plan_without_mutation(
    tmp_path: Path,
) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(repository, revision)
    decision = decide_wiki_review(
        request,
        reviewer="synthetic-reviewer",
        decision="approve",
        reviewed_on=TODAY,
    )
    catalog_before = (repository / WIKI_ROOT / "catalog.json").read_bytes()
    index_before = (repository / WIKI_ROOT / "index.md").read_bytes()

    result = _lifecycle(tmp_path).finalize(
        repository,
        WIKI_ROOT,
        request,
        decision,
    )

    assert result.report.status == "promotion_planned"
    assert result.report.promotion_applied is False
    assert result.reviewed_page_ids == frozenset((PAGE_ID,))
    assert result.proposed_catalog_bytes is not None
    assert result.proposed_index_bytes is not None
    proposed = WikiCatalog.model_validate_json(result.proposed_catalog_bytes)
    assert proposed.pages[0].status == "reviewed"
    assert proposed.pages[0].last_verified == TODAY
    assert result.report.proposed_catalog_digest == artifact_digest(proposed)
    assert result.report.reviewer_identity_assurance == "declarative_unverified"
    assert WikiReviewDecision.model_validate_json(decision.model_dump_json()) == decision
    assert type(result.report).model_validate_json(result.report.model_dump_json()) == result.report
    assert str(repository) not in result.report.model_dump_json()
    assert "bounded migration evidence" not in result.report.model_dump_json()
    assert (repository / WIKI_ROOT / "catalog.json").read_bytes() == catalog_before
    assert (repository / WIKI_ROOT / "index.md").read_bytes() == index_before
    assert _git(repository, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_rejected_decision_emits_evidence_without_a_promotion(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(repository, revision)
    decision = decide_wiki_review(
        request,
        reviewer="synthetic-reviewer",
        decision="reject",
        reviewed_on=TODAY,
    )

    result = _lifecycle(tmp_path).finalize(repository, WIKI_ROOT, request, decision)

    assert result.report.status == "rejected"
    assert result.report.proposed_page_status is None
    assert result.reviewed_page_ids == frozenset()
    assert result.proposed_catalog_bytes is None
    assert result.proposed_index_bytes is None


def test_request_is_deterministic_and_portably_serializable(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)

    first = _request(repository, revision)
    second = _request(repository, revision)
    serialized = first.model_dump_json()

    assert first == second
    assert WikiReviewRequest.model_validate_json(serialized) == first
    assert str(repository) not in serialized
    assert "bounded migration evidence" not in serialized
    assert "https://example.test/wiki-governance" in serialized
    assert first.requested_action == "promote_to_reviewed"
    assert first.repository_revision == revision


def test_request_rejects_self_review_dirty_tree_and_unversioned_claim(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    with pytest.raises(ValidationError, match="independent designated reviewer"):
        create_wiki_review_request(
            repository,
            WIKI_ROOT,
            page_id=PAGE_ID,
            requester="same-reviewer",
            designated_reviewer="same-reviewer",
            as_of=TODAY,
            repository_revision=revision,
        )

    with pytest.raises(PolicyViolation, match="malformed or unversioned"):
        create_wiki_review_request(
            repository,
            WIKI_ROOT,
            page_id=PAGE_ID,
            requester="synthetic-requester",
            designated_reviewer="synthetic-reviewer",
            as_of=TODAY,
            repository_revision="unversioned",
        )

    (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="clean repository worktree"):
        _request(repository, revision)


@pytest.mark.parametrize("status", ["reviewed", "deprecated"])
def test_request_rejects_non_pilot_page(tmp_path: Path, status: str) -> None:
    repository, _ = _repository(tmp_path)
    _write_catalog(repository, {"status": status})
    revision = _commit_all(repository, status)

    with pytest.raises(PolicyViolation, match="pilot Wiki page"):
        _request(repository, revision)


def test_request_rejects_future_and_stale_evidence_dates(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    with pytest.raises(PolicyViolation, match="as-of date cannot be in the future"):
        create_wiki_review_request(
            repository,
            WIKI_ROOT,
            page_id=PAGE_ID,
            requester="synthetic-requester",
            designated_reviewer="synthetic-reviewer",
            as_of=TODAY + timedelta(days=1),
            repository_revision=revision,
        )

    _write_catalog(
        repository,
        {"last_verified": (TODAY - timedelta(days=366)).isoformat()},
    )
    stale_revision = _commit_all(repository, "stale")
    with pytest.raises(PolicyViolation, match="stale for review"):
        _request(repository, stale_revision)

    _write_catalog(
        repository,
        {"last_verified": (TODAY + timedelta(days=1)).isoformat()},
    )
    future_revision = _commit_all(repository, "future")
    with pytest.raises(PolicyViolation, match="verification date is in the future"):
        _request(repository, future_revision)


def test_request_rejects_symlink_and_oracle_roots(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    page = repository / WIKI_ROOT / PAGE_PATH
    original = repository / "original-page.md"
    page.rename(original)
    page.symlink_to(original)
    with pytest.raises(PolicyViolation, match="symlink"):
        _request(repository, revision)

    oracle_repository = tmp_path / "oracle-repository"
    shutil.copytree(repository, oracle_repository, symlinks=True)
    # Rebuild a clean standalone repository whose Wiki lives under an exact
    # forbidden oracle segment; no real project content is involved.
    shutil.rmtree(oracle_repository / ".git")
    (oracle_repository / WIKI_ROOT / PAGE_PATH).unlink()
    shutil.copy2(oracle_repository / "original-page.md", oracle_repository / WIKI_ROOT / PAGE_PATH)
    oracle_root = oracle_repository / "oracle"
    (oracle_repository / "oracle").mkdir()
    shutil.move(str(oracle_repository / WIKI_ROOT), str(oracle_root / "wiki"))
    _git(oracle_repository, "init", "--quiet")
    oracle_revision = _commit_all(oracle_repository, "oracle")
    with pytest.raises(PolicyViolation, match="oracle"):
        create_wiki_review_request(
            oracle_repository,
            "oracle/wiki",
            page_id=PAGE_ID,
            requester="synthetic-requester",
            designated_reviewer="synthetic-reviewer",
            as_of=TODAY,
            repository_revision=oracle_revision,
        )


def test_decision_rejects_self_review_transfer_and_future_date(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(repository, revision)

    with pytest.raises(ValidationError, match="transferred"):
        decide_wiki_review(
            request,
            reviewer="different-reviewer",
            decision="approve",
            reviewed_on=TODAY,
        )
    with pytest.raises(PolicyViolation, match="date cannot be in the future"):
        decide_wiki_review(
            request,
            reviewer="synthetic-reviewer",
            decision="approve",
            reviewed_on=TODAY + timedelta(days=1),
        )

    self_reviewed = request.model_copy(
        update={"requester": request.designated_reviewer},
    )
    with pytest.raises(ValidationError, match="own promotion"):
        decide_wiki_review(
            self_reviewed,
            reviewer="synthetic-reviewer",
            decision="approve",
            reviewed_on=TODAY,
        )


@pytest.mark.parametrize(
    ("field_name", "replacement", "message"),
    [
        ("requested_action", "publish", "requested_action"),
        ("request_digest", "sha256:" + "0" * 64, "request_digest"),
        ("repository_revision", "f" * 40, "repository_revision"),
        ("page_id", "different-page", "page_id"),
        ("reviewer", "different-reviewer", "reviewer"),
    ],
)
def test_finalization_rejects_wrong_decision_binding(
    tmp_path: Path,
    field_name: str,
    replacement: str,
    message: str,
) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(repository, revision)
    decision = decide_wiki_review(
        request,
        reviewer="synthetic-reviewer",
        decision="approve",
        reviewed_on=TODAY,
    ).model_copy(update={field_name: replacement})

    with pytest.raises(PolicyViolation, match=message):
        _lifecycle(tmp_path).finalize(repository, WIKI_ROOT, request, decision)


def test_decision_is_one_use_and_cannot_replay(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(repository, revision)
    decision = decide_wiki_review(
        request,
        reviewer="synthetic-reviewer",
        decision="approve",
        reviewed_on=TODAY,
    )
    lifecycle = _lifecycle(tmp_path)
    lifecycle.finalize(repository, WIKI_ROOT, request, decision)

    with pytest.raises(PolicyViolation, match="already been consumed"):
        lifecycle.finalize(repository, WIKI_ROOT, request, decision)
    assert lifecycle.consumed_decision_digests == (artifact_digest(decision),)
    resumed = _lifecycle(tmp_path)
    with pytest.raises(PolicyViolation, match="already been consumed"):
        resumed.finalize(repository, WIKI_ROOT, request, decision)


@pytest.mark.parametrize(
    "field_name",
    [
        "source_page_bytes",
        "source_catalog_bytes",
        "source_index_bytes",
        "proposed_catalog_bytes",
        "proposed_index_bytes",
    ],
)
def test_result_rejects_bytes_that_do_not_match_exact_report_digests(
    tmp_path: Path,
    field_name: str,
) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(repository, revision)
    decision = decide_wiki_review(
        request,
        reviewer="synthetic-reviewer",
        decision="approve",
        reviewed_on=TODAY,
    )
    result = _lifecycle(tmp_path).finalize(repository, WIKI_ROOT, request, decision)

    with pytest.raises(ValueError, match="bytes do not match"):
        _tamper_result_bytes(result, field_name)


def test_verification_failure_does_not_consume_decision(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(repository, revision)
    decision = decide_wiki_review(
        request,
        reviewer="synthetic-reviewer",
        decision="approve",
        reviewed_on=TODAY,
    )
    invalid = decision.model_copy(update={"request_digest": "sha256:" + "0" * 64})
    lifecycle = _lifecycle(tmp_path)

    with pytest.raises(PolicyViolation, match="request_digest"):
        lifecycle.finalize(repository, WIKI_ROOT, request, invalid)
    assert lifecycle.consumed_decision_digests == ()
    assert lifecycle.finalize(repository, WIKI_ROOT, request, decision).report.decision == "approve"


def test_persistence_failure_leaves_decision_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(repository, revision)
    decision = decide_wiki_review(
        request,
        reviewer="synthetic-reviewer",
        decision="approve",
        reviewed_on=TODAY,
    )
    lifecycle = _lifecycle(tmp_path)

    def fail_link(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("synthetic durable-storage failure")

    with monkeypatch.context() as context:
        context.setattr("legacy_migration_agent.knowledge.wiki_governance.os.link", fail_link)
        with pytest.raises(PolicyViolation, match="could not be persisted safely"):
            lifecycle.finalize(repository, WIKI_ROOT, request, decision)
    assert lifecycle.consumed_decision_digests == ()

    result = _lifecycle(tmp_path).finalize(repository, WIKI_ROOT, request, decision)
    assert result.report.status == "promotion_planned"


def test_concurrent_lifecycles_fail_closed_on_same_decision(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(repository, revision)
    decision = decide_wiki_review(
        request,
        reviewer="synthetic-reviewer",
        decision="approve",
        reviewed_on=TODAY,
    )
    lifecycles = (_lifecycle(tmp_path), _lifecycle(tmp_path))
    barrier = Barrier(2)

    def finalize(lifecycle: WikiPromotionLifecycle) -> str:
        barrier.wait(timeout=10)
        try:
            lifecycle.finalize(repository, WIKI_ROOT, request, decision)
        except PolicyViolation as exc:
            return str(exc)
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(finalize, lifecycles))

    assert outcomes.count("success") == 1
    assert sum("already been consumed" in outcome for outcome in outcomes) == 1
    assert lifecycles[0].consumed_decision_digests == (artifact_digest(decision),)


@pytest.mark.parametrize("tamper", ["malformed", "symlink", "mode"])
def test_durable_receipt_tampering_fails_closed(tmp_path: Path, tamper: str) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(repository, revision)
    decision = decide_wiki_review(
        request,
        reviewer="synthetic-reviewer",
        decision="approve",
        reviewed_on=TODAY,
    )
    lifecycle = _lifecycle(tmp_path)
    lifecycle.finalize(repository, WIKI_ROOT, request, decision)
    receipt = next((lifecycle.evidence_root / "receipts").iterdir())
    if tamper == "malformed":
        receipt.write_text("{}\n", encoding="utf-8")
    elif tamper == "symlink":
        target = tmp_path / "outside-receipt.json"
        target.write_text("{}\n", encoding="utf-8")
        receipt.unlink()
        receipt.symlink_to(target)
    else:
        receipt.chmod(0o644)

    with pytest.raises(PolicyViolation, match="receipt"):
        _lifecycle(tmp_path).finalize(repository, WIKI_ROOT, request, decision)


def test_replaced_evidence_root_identity_fails_closed(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(repository, revision)
    decision = decide_wiki_review(
        request,
        reviewer="synthetic-reviewer",
        decision="approve",
        reviewed_on=TODAY,
    )
    lifecycle = _lifecycle(tmp_path)
    moved = tmp_path / "moved-wiki-governance-evidence"
    lifecycle.evidence_root.rename(moved)
    lifecycle.evidence_root.mkdir(mode=0o700)

    with pytest.raises(PolicyViolation, match="identity changed"):
        lifecycle.finalize(repository, WIKI_ROOT, request, decision)


@pytest.mark.parametrize("mutation", ["page", "catalog", "inventory", "revision"])
def test_finalization_rejects_changed_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(repository, revision)
    decision = decide_wiki_review(
        request,
        reviewer="synthetic-reviewer",
        decision="approve",
        reviewed_on=TODAY,
    )
    if mutation == "page":
        (repository / WIKI_ROOT / PAGE_PATH).write_text("# Changed page\n", encoding="utf-8")
    elif mutation == "catalog":
        catalog = repository / WIKI_ROOT / "catalog.json"
        catalog.write_text(catalog.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    elif mutation == "inventory":
        (repository / WIKI_ROOT / "ignored-runtime.txt").write_text(
            "ignored but not versioned\n",
            encoding="utf-8",
        )
    else:
        (repository / "later.txt").write_text("later revision\n", encoding="utf-8")
        _commit_all(repository, "later")

    with pytest.raises(PolicyViolation):
        _lifecycle(tmp_path).finalize(repository, WIKI_ROOT, request, decision)


def test_request_rejects_malformed_or_non_authoritative_catalog(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    _write_catalog(
        repository,
        {
            "sources": [
                {
                    "title": "Insecure source",
                    "url": "http://example.test/insecure",
                    "publisher": "Example",
                    "authority": "official",
                }
            ]
        },
    )
    revision = _commit_all(repository, "insecure source")

    with pytest.raises(PolicyViolation, match="HTTPS"):
        _request(repository, revision)

    malformed_request = _request_model_payload()
    malformed_request["extra_field"] = "not allowed"
    with pytest.raises(ValidationError, match="extra"):
        WikiReviewRequest.model_validate(malformed_request)

    malformed_decision = {
        "decision_id": "wiki-review-decision-synthetic",
        "decision": "implicit",
    }
    with pytest.raises(ValidationError):
        WikiReviewDecision.model_validate(malformed_decision)


@pytest.mark.parametrize("field_name", ["sources", "owner", "source_version", "target_version"])
def test_request_rejects_missing_governance_metadata(
    tmp_path: Path,
    field_name: str,
) -> None:
    repository, _ = _repository(tmp_path)
    catalog_path = repository / WIKI_ROOT / "catalog.json"
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    if field_name == "sources":
        del raw["pages"][0][field_name]
    else:
        raw["pages"][0][field_name] = ""
    catalog_path.write_text(json.dumps(raw), encoding="utf-8")
    revision = _commit_all(repository, f"missing {field_name}")

    with pytest.raises(PolicyViolation, match="catalog.json is malformed or invalid"):
        _request(repository, revision)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("# Unsafe page\n\napi_key=synthetic-secret\n", "credential material"),
        ("# Unsafe page\n\n/" + "Users/example/private/file.txt\n", "absolute user paths"),
    ],
)
def test_request_keeps_secret_and_absolute_path_content_out_of_lifecycle(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    repository, _ = _repository(tmp_path)
    (repository / WIKI_ROOT / PAGE_PATH).write_text(content, encoding="utf-8")
    _write_catalog(repository)
    revision = _commit_all(repository, "unsafe evidence")

    with pytest.raises(PolicyViolation, match=message):
        _request(repository, revision)


def _request_model_payload() -> dict[str, object]:
    return {
        "request_id": "wiki-review-request-synthetic",
        "page_id": PAGE_ID,
        "page_path": PAGE_PATH,
        "page_content_digest": "sha256:" + "1" * 64,
        "catalog_digest": "sha256:" + "2" * 64,
        "catalog_content_digest": "sha256:" + "3" * 64,
        "index_content_digest": "sha256:" + "4" * 64,
        "wiki_inventory_digest": "sha256:" + "5" * 64,
        "platform": "salesforce",
        "source_version": "Salesforce API 67.0",
        "target_version": "Salesforce API 67.0",
        "owner": "synthetic-owner",
        "authoritative_source_urls": ["https://example.test/docs"],
        "page_last_verified": TODAY.isoformat(),
        "as_of": TODAY.isoformat(),
        "repository_revision": "a" * 40,
        "requester": "synthetic-requester",
        "designated_reviewer": "synthetic-reviewer",
    }
