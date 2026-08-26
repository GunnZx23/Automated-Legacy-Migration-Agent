import hashlib
import json
import os
import shutil
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

import legacy_migration_agent.knowledge.wiki as wiki_module
from legacy_migration_agent.contracts import Platform
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.knowledge.wiki import (
    LlmWiki,
    RetrievalTrace,
    WikiCatalog,
    WikiSource,
)

WIKI_ROOT = Path(__file__).parents[1] / "knowledge" / "wiki"
AS_OF = date(2026, 8, 26)
SALESFORCE_VERSION = "Salesforce API 67.0"


def test_project_wiki_is_a_valid_linked_graph():
    wiki = LlmWiki.load(WIKI_ROOT)
    assert len(wiki.catalog.pages) >= 5
    assert all(page.sources for page in wiki.catalog.pages)
    assert wiki.index_digest.startswith("sha256:")


def test_research_is_an_explicit_source_authority_without_relaxing_other_values():
    source = WikiSource.model_validate(
        {
            "title": "Peer-reviewed migration research",
            "url": "https://example.org/research",
            "publisher": "Research publisher",
            "authority": "research",
        }
    )

    assert source.authority == "research"
    with pytest.raises(ValidationError, match="authority"):
        WikiSource.model_validate(source.model_dump(mode="json") | {"authority": "unreviewed-blog"})


def test_visualforce_query_ranks_migration_page_and_expands_security_link():
    trace = LlmWiki.load(WIKI_ROOT).search(
        "migrate Visualforce controller rerender to LWC",
        platform=Platform.SALESFORCE,
        source_version=SALESFORCE_VERSION,
        target_version=SALESFORCE_VERSION,
        max_primary_hits=1,
        as_of=AS_OF,
    )

    assert trace.hits[0].page_id == "salesforce-visualforce-to-lwc"
    assert any(
        hit.page_id == "salesforce-apex-security"
        and hit.expanded_from == "salesforce-visualforce-to-lwc"
        for hit in trace.hits
    )


def test_lightning_data_service_get_record_example_is_discoverable():
    trace = LlmWiki.load(WIKI_ROOT).search(
        "Lightning Data Service lightning/uiRecordApi getRecord reactive recordId Account.Name",
        platform=Platform.SALESFORCE,
        source_version=SALESFORCE_VERSION,
        target_version=SALESFORCE_VERSION,
        max_primary_hits=1,
        expand_links=False,
        as_of=AS_OF,
    )

    hit = trace.hits[0]
    assert hit.page_id == "salesforce-lightning-data-service"
    assert "from 'lightning/uiRecordApi'" in hit.selected_content
    assert "@wire(getRecord, { recordId: '$recordId', fields: FIELDS })" in hit.selected_content
    assert any("reference-wire-adapters-record" in str(source.url) for source in hit.sources)


def test_platform_filter_excludes_mulesoft_page():
    trace = LlmWiki.load(WIKI_ROOT).search(
        "tests validation migration",
        platform=Platform.SALESFORCE,
        source_version=SALESFORCE_VERSION,
        target_version=SALESFORCE_VERSION,
        as_of=AS_OF,
    )
    assert "mulesoft-mule3-to-mule4" not in {hit.page_id for hit in trace.hits}


def test_mulesoft_query_uses_the_exact_fixture_versions():
    trace = LlmWiki.load(WIKI_ROOT).search(
        "Mule DataWeave HTTP listener MUnit migration",
        platform=Platform.MULESOFT,
        source_version="Mule 3.9.5",
        target_version="Mule 4.9.20",
        max_primary_hits=1,
        as_of=AS_OF,
    )

    assert trace.hits[0].page_id == "mulesoft-mule3-to-mule4"
    assert trace.hits[0].source_version == "Mule 3.9.5"
    assert trace.hits[0].target_version == "Mule 4.9.20"


def test_version_filter_rejects_mismatched_guidance():
    wiki = LlmWiki.load(WIKI_ROOT)
    matching = wiki.search(
        "Visualforce LWC migration",
        platform=Platform.SALESFORCE,
        source_version="Salesforce API 67.0",
        target_version="Salesforce API 67.0",
        as_of=AS_OF,
    )
    mismatched = wiki.search(
        "Visualforce LWC migration",
        platform=Platform.SALESFORCE,
        source_version="Salesforce API 66.0",
        target_version="Salesforce API 67.0",
        as_of=AS_OF,
    )
    assert matching.hits[0].page_id == "salesforce-visualforce-to-lwc"
    assert not mismatched.hits


def test_as_of_filter_rejects_pages_verified_in_the_future(tmp_path: Path):
    copied = tmp_path / "wiki"
    shutil.copytree(WIKI_ROOT, copied)
    catalog_path = copied / "catalog.json"
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    target = next(
        page for page in raw["pages"] if page["page_id"] == "salesforce-visualforce-to-lwc"
    )
    target["last_verified"] = "2099-01-01"
    catalog_path.write_text(json.dumps(raw), encoding="utf-8")
    catalog = WikiCatalog.model_validate(raw)
    pages = {
        record.page_id: wiki_module._LoadedPage(
            record=record,
            body=(copied / record.path).read_text(encoding="utf-8"),
        )
        for record in catalog.pages
    }
    (copied / "index.md").write_text(
        wiki_module.render_wiki_index(catalog, pages),
        encoding="utf-8",
    )

    trace = LlmWiki.load(copied).search(
        "Visualforce LWC migration",
        platform=Platform.SALESFORCE,
        source_version=SALESFORCE_VERSION,
        target_version=SALESFORCE_VERSION,
        expand_links=False,
        as_of=AS_OF,
    )
    assert "salesforce-visualforce-to-lwc" not in {hit.page_id for hit in trace.hits}


def test_ranking_is_deterministic():
    wiki = LlmWiki.load(WIKI_ROOT)
    first = wiki.search("approval validation evidence", as_of=AS_OF)
    second = wiki.search("approval validation evidence", as_of=AS_OF)
    assert first == second
    assert first.catalog_digest.startswith("sha256:")
    assert all(hit.page_digest.startswith("sha256:") for hit in first.hits)
    assert first.retrieval_strategy == "deterministic_lexical"
    assert first.evidence_bundle_digest.startswith("sha256:")


def test_trace_exposes_concise_content_bound_to_page_evidence():
    trace = LlmWiki.load(WIKI_ROOT).search(
        "Visualforce LWC rerender migration",
        platform=Platform.SALESFORCE,
        source_version=SALESFORCE_VERSION,
        target_version=SALESFORCE_VERSION,
        max_primary_hits=1,
        expand_links=False,
        as_of=AS_OF,
    )

    hit = trace.hits[0]
    assert hit.content_kind == "curated_wiki_evidence"
    assert "Preserve the observable behavior" in hit.selected_content
    assert len(hit.selected_content) <= 1600
    assert hit.selected_content_digest.startswith("sha256:")

    tampered = trace.model_dump(mode="json")
    tampered["hits"][0]["selected_content"] += "\nUnreviewed instruction"
    with pytest.raises(ValidationError, match="selected Wiki content"):
        RetrievalTrace.model_validate(tampered)


def test_trace_bundle_rejects_content_tamper_even_if_hit_digest_is_replaced():
    trace = LlmWiki.load(WIKI_ROOT).search(
        "Visualforce LWC rerender migration",
        platform=Platform.SALESFORCE,
        source_version=SALESFORCE_VERSION,
        target_version=SALESFORCE_VERSION,
        max_primary_hits=1,
        expand_links=False,
        as_of=AS_OF,
    )
    tampered = trace.model_dump(mode="json")
    tampered["hits"][0]["selected_content"] = "# Replaced content"
    # This is the valid SHA-256 of the replacement. The trace-level evidence
    # bundle must still detect that the selected page evidence changed.
    tampered["hits"][0]["selected_content_digest"] = (
        "sha256:"
        + hashlib.sha256(tampered["hits"][0]["selected_content"].encode("utf-8")).hexdigest()
    )
    with pytest.raises(ValidationError, match="evidence bundle"):
        RetrievalTrace.model_validate(tampered)


def test_retrieval_is_bounded_to_at_most_three_selected_pages():
    wiki = LlmWiki.load(WIKI_ROOT)
    trace = wiki.search(
        "migration validation security approval evidence",
        max_primary_hits=3,
        expand_links=True,
        as_of=AS_OF,
    )
    assert 1 <= len(trace.hits) <= 3

    with pytest.raises(ValueError, match="between 1 and 3"):
        wiki.search("migration", max_primary_hits=4, as_of=AS_OF)


def test_platform_retrieval_requires_explicit_versions():
    with pytest.raises(ValueError, match="requires source_version"):
        LlmWiki.load(WIKI_ROOT).search(
            "Visualforce LWC",
            platform=Platform.SALESFORCE,
            as_of=AS_OF,
        )


def test_catalog_rejects_broken_links():
    raw = json.loads((WIKI_ROOT / "catalog.json").read_text(encoding="utf-8"))
    raw["pages"][0]["links"].append("missing-page")
    with pytest.raises(ValidationError, match="unknown pages"):
        WikiCatalog.model_validate(raw)


def test_catalog_rejects_duplicate_page_paths():
    raw = json.loads((WIKI_ROOT / "catalog.json").read_text(encoding="utf-8"))
    raw["pages"][1]["path"] = raw["pages"][0]["path"]
    with pytest.raises(ValidationError, match="page paths must be unique"):
        WikiCatalog.model_validate(raw)


def test_wiki_load_translates_malformed_catalog_to_policy_violation(tmp_path: Path) -> None:
    copied = tmp_path / "wiki"
    shutil.copytree(WIKI_ROOT, copied)
    (copied / "catalog.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(PolicyViolation, match="catalog.json is malformed or invalid"):
        LlmWiki.load(copied)


def test_wiki_rejects_missing_index(tmp_path: Path):
    copied = tmp_path / "wiki"
    shutil.copytree(WIKI_ROOT, copied)
    (copied / "index.md").unlink()

    with pytest.raises(PolicyViolation, match="index.md is missing"):
        LlmWiki.load(copied)


def test_wiki_rejects_tampered_index(tmp_path: Path):
    copied = tmp_path / "wiki"
    shutil.copytree(WIKI_ROOT, copied)
    index = copied / "index.md"
    index.write_text(index.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")

    with pytest.raises(PolicyViolation, match="stale or tampered"):
        LlmWiki.load(copied)


def test_wiki_rejects_stale_index_after_catalog_change(tmp_path: Path):
    copied = tmp_path / "wiki"
    shutil.copytree(WIKI_ROOT, copied)
    catalog_path = copied / "catalog.json"
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    raw["pages"][0]["title"] = "Changed catalog title"
    catalog_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(PolicyViolation, match="stale or tampered"):
        LlmWiki.load(copied)


def test_wiki_rejects_unindexed_markdown_page(tmp_path: Path):
    copied = tmp_path / "wiki"
    shutil.copytree(WIKI_ROOT, copied)
    (copied / "pages" / "unindexed.md").write_text("# Extra page\n", encoding="utf-8")

    with pytest.raises(PolicyViolation, match="unindexed: pages/unindexed.md"):
        LlmWiki.load(copied)


def test_wiki_rejects_oracle_tree_before_opening_regular_file_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied = tmp_path / "wiki"
    shutil.copytree(WIKI_ROOT, copied)
    oracle = copied / "pages" / "oracle"
    oracle.mkdir()
    (oracle / "answer.md").write_text("# answer\n", encoding="utf-8")
    opened: list[str] = []

    def reject_open(path: Path, *args, **kwargs):
        opened.append(str(path))
        raise AssertionError("Wiki bytes must not be opened before oracle preflight")

    monkeypatch.setattr(Path, "open", reject_open)
    with pytest.raises(PolicyViolation, match="expected, golden, or oracle"):
        LlmWiki.load(copied)
    assert opened == []


def test_wiki_rejects_page_symlink_before_opening_regular_file_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied = tmp_path / "wiki"
    shutil.copytree(WIKI_ROOT, copied)
    page = copied / "pages" / "salesforce-visualforce-to-lwc.md"
    target = copied / "outside.md"
    page.replace(target)
    page.symlink_to(target)
    opened: list[str] = []

    def reject_open(path: Path, *args, **kwargs):
        opened.append(str(path))
        raise AssertionError("Wiki bytes must not be opened before symlink preflight")

    monkeypatch.setattr(Path, "open", reject_open)
    with pytest.raises(PolicyViolation, match="symlinks"):
        LlmWiki.load(copied)
    assert opened == []


def test_wiki_descriptor_snapshot_rejects_page_swapped_to_oracle_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied = tmp_path / "wiki"
    shutil.copytree(WIKI_ROOT, copied)
    page = copied / "pages" / "salesforce-visualforce-to-lwc.md"
    oracle = tmp_path / "oracle" / "answer.md"
    oracle.parent.mkdir()
    oracle.write_text("# oracle answer must never be read\n", encoding="utf-8")
    oracle_metadata = oracle.stat()
    actual_open = os.open
    actual_read = os.read
    swapped = False
    oracle_reads: list[int] = []

    def swapping_open(path, *args, **kwargs):
        nonlocal swapped
        rendered = os.fspath(path)
        if rendered == "catalog.json" and not swapped:
            page.unlink()
            page.symlink_to(oracle)
            swapped = True
        return actual_open(path, *args, **kwargs)

    def guarded_read(descriptor: int, size: int) -> bytes:
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == (
            oracle_metadata.st_dev,
            oracle_metadata.st_ino,
        ):
            oracle_reads.append(descriptor)
            raise AssertionError("oracle bytes must never be read through a swapped Wiki path")
        return actual_read(descriptor, size)

    monkeypatch.setattr(os, "open", swapping_open)
    monkeypatch.setattr(os, "read", guarded_read)
    with pytest.raises(PolicyViolation, match="changed|unsafe"):
        LlmWiki.load(copied)
    assert swapped
    assert oracle_reads == []
