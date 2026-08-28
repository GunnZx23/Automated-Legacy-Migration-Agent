import hashlib
import json
import os
import shutil
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

import legacy_migration_agent.knowledge.wiki as wiki_module
from legacy_migration_agent.agent_runtime.model_agents import (  # noqa: PLC2701
    _repair_signal_specs,
)
from legacy_migration_agent.application.migration_scenarios import (
    SALESFORCE_INITIAL_WIKI_EXACT_IDS,
    SALESFORCE_WIKI_QUERY,
)
from legacy_migration_agent.contracts import Platform
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.knowledge.wiki import (
    LlmWiki,
    RetrievalTrace,
    WikiCatalog,
    WikiSource,
    contains_exact_diagnostic_id,
)
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    MULE4_DATAWEAVE,
    MULE4_POM,
    MULE4_TEST,
    MuleSoftLocalCheckCode,
)
from legacy_migration_agent.platforms.mulesoft_runtime import (
    MULESOFT_REPAIR_SIGNALS,
    mulesoft_candidate_diagnostic_id,
)

WIKI_ROOT = Path(__file__).parents[1] / "knowledge" / "wiki"
AS_OF = date(2026, 8, 27)
SALESFORCE_VERSION = "Salesforce API 67.0"
SALESFORCE_ACTIONABLE_REPAIR_SIGNALS = tuple(
    sorted(
        signal_id
        for signal_id, (allowed_paths, _) in _repair_signal_specs(Platform.SALESFORCE).items()
        if allowed_paths
    )
)


def test_project_wiki_is_a_valid_linked_graph():
    wiki = LlmWiki.load(WIKI_ROOT)
    assert len(wiki.catalog.pages) >= 5
    assert all(page.sources for page in wiki.catalog.pages)
    assert wiki.index_digest.startswith("sha256:")


def test_exact_diagnostic_matching_rejects_longer_identifier_substrings() -> None:
    diagnostic_id = "jest_unapproved_module_target"

    assert contains_exact_diagnostic_id(f"Use `{diagnostic_id}` guidance.", diagnostic_id)
    assert not contains_exact_diagnostic_id(
        f"prefix_{diagnostic_id}_suffix",
        diagnostic_id,
    )
    assert not contains_exact_diagnostic_id(f"{diagnostic_id}.suffix", diagnostic_id)


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


@pytest.mark.parametrize(
    ("diagnostic_id", "expected_page", "expected_guidance"),
    (
        (
            "apex_public_interface_annotation_mismatch",
            "salesforce-apex-security",
            "@AuraEnabled(cacheable=true)",
        ),
        (
            "jest_unapproved_module_target",
            "salesforce-validation",
            "@salesforce/apex/AccountContactExplorerController.getAccounts",
        ),
        (
            "jest_globals_import_order",
            "salesforce-validation",
            "first static import",
        ),
        (
            "lwc_template_binding_invalid",
            "salesforce-visualforce-to-lwc",
            "JavaScript getters",
        ),
        (
            "controller_jest_stale_response",
            "salesforce-visualforce-to-lwc",
            "request token",
        ),
        (
            "controller_jest_account_options",
            "salesforce-visualforce-to-lwc",
            "empty-string option",
        ),
        (
            "candidate_jest_execution_failure",
            "salesforce-validation",
            "element.shadowRoot",
        ),
        (
            "controller_jest_execution_failure",
            "salesforce-validation",
            "zero immutable controller",
        ),
    ),
)
def test_salesforce_correction_diagnostics_retrieve_actionable_project_guidance(
    diagnostic_id: str,
    expected_page: str,
    expected_guidance: str,
) -> None:
    trace = LlmWiki.load(WIKI_ROOT).search(
        f"{diagnostic_id} salesforce correction validation",
        platform=Platform.SALESFORCE,
        source_version=SALESFORCE_VERSION,
        target_version=SALESFORCE_VERSION,
        max_primary_hits=1,
        expand_links=True,
        as_of=AS_OF,
        required_exact_ids=(diagnostic_id,),
    )

    assert trace.hits[0].page_id == expected_page
    assert diagnostic_id in trace.hits[0].selected_content
    assert expected_guidance in trace.hits[0].selected_content
    assert trace.expand_links is False


def test_combined_jest_execution_correction_retains_the_actionable_checklist() -> None:
    diagnostic_ids = (
        "candidate_jest_execution_failure",
        "controller_jest_execution_failure",
    )
    trace = LlmWiki.load(WIKI_ROOT).search(
        (
            "candidate_jest_execution_failure controller_jest_execution_failure "
            "salesforce correction validation"
        ),
        platform=Platform.SALESFORCE,
        source_version=SALESFORCE_VERSION,
        target_version=SALESFORCE_VERSION,
        max_primary_hits=1,
        expand_links=True,
        as_of=AS_OF,
        required_exact_ids=diagnostic_ids,
    )

    assert len(trace.hits) == 1
    assert trace.hits[0].page_id == "salesforce-validation"
    selected = trace.hits[0].selected_content
    for critical_rule in (
        "standard plain JavaScript",
        "TypeScript access modifiers",
        "getAccounts",
        "createElement",
        "__esModule: true",
        "{ virtual: true }",
        "enough microtask turns",
        "element.shadowRoot",
        "`Id` named by `key-field`",
        "unapproved `@api`",
    ):
        assert critical_rule in selected


def test_production_salesforce_query_supplies_generation_guidance_to_engineer() -> None:
    trace = LlmWiki.load(WIKI_ROOT).search(
        SALESFORCE_WIKI_QUERY,
        platform=Platform.SALESFORCE,
        source_version=SALESFORCE_VERSION,
        target_version=SALESFORCE_VERSION,
        max_primary_hits=1,
        expand_links=True,
        as_of=AS_OF,
        required_exact_ids=SALESFORCE_INITIAL_WIKI_EXACT_IDS,
    )

    selected = "\n".join(hit.selected_content for hit in trace.hits)
    for required_guidance in (
        "standard plain JavaScript",
        "TypeScript access modifiers",
        "jest.resetAllMocks()",
        "deferred Promise",
        "Internal state needs no access modifier",
        "controller_jest_account_error",
        "a `lightning-combobox` placeholder is not that option",
        "do not bind a positive `canLoadContacts` getter directly",
        "controller_jest_stale_response",
        "controller_jest_empty_state",
        "never before the first explicit Load",
        "no visible `contact-results` hook",
        "public static List<Account> getAccounts()",
        "public static List<Contact> getContacts(Id accountId)",
        "AccountContactExplorerUser",
        "Do not create a second permission set or modify a profile",
        "Do not create `User` records, query",
        "The local controller contract checks safe",
    ):
        assert required_guidance in selected


@pytest.mark.parametrize("diagnostic_id", SALESFORCE_ACTIONABLE_REPAIR_SIGNALS)
def test_every_actionable_salesforce_signal_query_selects_exact_wiki_coverage(
    diagnostic_id: str,
) -> None:
    trace = LlmWiki.load(WIKI_ROOT).search(
        f"{diagnostic_id} salesforce correction validation",
        platform=Platform.SALESFORCE,
        source_version=SALESFORCE_VERSION,
        target_version=SALESFORCE_VERSION,
        max_primary_hits=1,
        expand_links=True,
        as_of=AS_OF,
        required_exact_ids=(diagnostic_id,),
    )

    assert len(trace.hits) == 1
    assert diagnostic_id in trace.hits[0].selected_content
    assert "exact-diagnostic-id" in trace.hits[0].matched_fields


@pytest.mark.parametrize(
    ("diagnostic_id", "expected_guidance"),
    (
        (
            mulesoft_candidate_diagnostic_id(
                MuleSoftLocalCheckCode.DATAWEAVE_CONTRACT,
                MULE4_DATAWEAVE,
            ),
            "empty object",
        ),
        (
            mulesoft_candidate_diagnostic_id(
                MuleSoftLocalCheckCode.MUNIT_CONTRACT,
                MULE4_TEST,
            ),
            "nontrivial assertion",
        ),
        (
            mulesoft_candidate_diagnostic_id(
                MuleSoftLocalCheckCode.POM_CONTRACT,
                MULE4_POM,
            ),
            "approved Mule application packaging",
        ),
    ),
)
def test_mulesoft_correction_diagnostics_retrieve_actionable_project_guidance(
    diagnostic_id: str,
    expected_guidance: str,
) -> None:
    trace = LlmWiki.load(WIKI_ROOT).search(
        f"{diagnostic_id} mulesoft correction validation",
        platform=Platform.MULESOFT,
        source_version="Mule 3.9.5",
        target_version="Mule 4.9.20",
        max_primary_hits=1,
        expand_links=True,
        as_of=AS_OF,
        required_exact_ids=(diagnostic_id,),
    )

    assert trace.hits[0].page_id == "mulesoft-toolchain-validation"
    assert diagnostic_id in trace.hits[0].selected_content
    assert expected_guidance in trace.hits[0].selected_content


@pytest.mark.parametrize("diagnostic_id", tuple(MULESOFT_REPAIR_SIGNALS))
def test_every_actionable_mulesoft_signal_has_targeted_wiki_coverage(
    diagnostic_id: str,
) -> None:
    trace = LlmWiki.load(WIKI_ROOT).search(
        f"{diagnostic_id} mulesoft correction validation",
        platform=Platform.MULESOFT,
        source_version="Mule 3.9.5",
        target_version="Mule 4.9.20",
        max_primary_hits=1,
        expand_links=True,
        as_of=AS_OF,
        required_exact_ids=(diagnostic_id,),
    )

    assert trace.hits[0].page_id == "mulesoft-toolchain-validation"
    assert diagnostic_id in trace.hits[0].selected_content


def test_exact_signal_routing_includes_every_required_page_despite_primary_limit() -> None:
    apex_signal = "apex_public_interface_annotation_mismatch"
    jest_signal = "jest_unapproved_module_target"
    controller_signal = "controller_jest_stale_response"
    trace = LlmWiki.load(WIKI_ROOT).search(
        (f"{apex_signal} {jest_signal} {controller_signal} salesforce correction validation"),
        platform=Platform.SALESFORCE,
        source_version=SALESFORCE_VERSION,
        target_version=SALESFORCE_VERSION,
        max_primary_hits=1,
        expand_links=True,
        as_of=AS_OF,
        required_exact_ids=(apex_signal, jest_signal, controller_signal),
    )

    assert {hit.page_id for hit in trace.hits} == {
        "salesforce-apex-security",
        "salesforce-validation",
        "salesforce-visualforce-to-lwc",
    }
    selected = "\n".join(hit.selected_content for hit in trace.hits)
    assert apex_signal in selected
    assert jest_signal in selected
    assert controller_signal in selected
    assert trace.max_primary_hits == 1
    assert trace.expand_links is False


def test_exact_signal_routing_fails_closed_when_coverage_is_missing() -> None:
    diagnostic_id = "diagnostic_identifier_that_is_not_curated"
    with pytest.raises(PolicyViolation, match="no eligible curated page"):
        LlmWiki.load(WIKI_ROOT).search(
            f"{diagnostic_id} salesforce correction validation",
            platform=Platform.SALESFORCE,
            source_version=SALESFORCE_VERSION,
            target_version=SALESFORCE_VERSION,
            max_primary_hits=1,
            as_of=AS_OF,
            required_exact_ids=(diagnostic_id,),
        )


def test_exact_signal_routing_rejects_an_id_not_bound_into_the_query() -> None:
    with pytest.raises(ValueError, match="must occur verbatim in the query"):
        LlmWiki.load(WIKI_ROOT).search(
            "salesforce correction validation",
            platform=Platform.SALESFORCE,
            source_version=SALESFORCE_VERSION,
            target_version=SALESFORCE_VERSION,
            max_primary_hits=1,
            as_of=AS_OF,
            required_exact_ids=("apex_public_interface_annotation_mismatch",),
        )


def test_exact_signal_routing_preserves_version_scope() -> None:
    diagnostic_id = "apex_public_interface_annotation_mismatch"
    with pytest.raises(PolicyViolation, match="no eligible curated page"):
        LlmWiki.load(WIKI_ROOT).search(
            f"{diagnostic_id} salesforce correction validation",
            platform=Platform.SALESFORCE,
            source_version="Salesforce API 66.0",
            target_version=SALESFORCE_VERSION,
            max_primary_hits=1,
            as_of=AS_OF,
            required_exact_ids=(diagnostic_id,),
        )


def test_exact_signal_routing_fails_closed_when_coverage_is_ambiguous(
    tmp_path: Path,
) -> None:
    diagnostic_id = "apex_public_interface_annotation_mismatch"
    copied = tmp_path / "wiki"
    shutil.copytree(WIKI_ROOT, copied)
    duplicate_page = copied / "pages/salesforce-validation.md"
    duplicate_page.write_text(
        duplicate_page.read_text(encoding="utf-8")
        + f"\nDuplicate diagnostic coverage `{diagnostic_id}`.\n",
        encoding="utf-8",
    )
    catalog = WikiCatalog.model_validate_json((copied / "catalog.json").read_text(encoding="utf-8"))
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

    with pytest.raises(PolicyViolation, match="ambiguous across curated pages"):
        LlmWiki.load(copied).search(
            f"{diagnostic_id} salesforce correction validation",
            platform=Platform.SALESFORCE,
            source_version=SALESFORCE_VERSION,
            target_version=SALESFORCE_VERSION,
            max_primary_hits=1,
            as_of=AS_OF,
            required_exact_ids=(diagnostic_id,),
        )


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

    diagnostic_id = "controller_jest_stale_response"
    with pytest.raises(PolicyViolation, match="no eligible curated page"):
        LlmWiki.load(copied).search(
            f"{diagnostic_id} salesforce correction validation",
            platform=Platform.SALESFORCE,
            source_version=SALESFORCE_VERSION,
            target_version=SALESFORCE_VERSION,
            max_primary_hits=1,
            as_of=AS_OF,
            required_exact_ids=(diagnostic_id,),
        )


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
