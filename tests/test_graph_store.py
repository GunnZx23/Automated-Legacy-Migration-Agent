from __future__ import annotations

from pathlib import Path

import pytest

from legacy_migration_agent.contracts import Platform
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.workspace import content_revision
from legacy_migration_agent.graphs.dependency_graph import (
    SALESFORCE_ANALYZER_VERSION,
    build_salesforce_dependency_graph,
)
from legacy_migration_agent.graphs.graph_store import GraphSnapshotKey, GraphSnapshotStore

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "salesforce"
    / "account-contact-explorer"
    / "input"
)
PAGE_ENTRY = "force-app/main/default/pages/LegacyAccountContactExplorer.page"
CONTROLLER_ENTRY = "force-app/main/default/classes/LegacyAccountContactExplorerController.cls"


def _graph():
    revision = content_revision(FIXTURE_ROOT)
    return build_salesforce_dependency_graph(FIXTURE_ROOT, (PAGE_ENTRY,), revision)


def test_snapshot_store_round_trips_an_immutable_graph(tmp_path: Path) -> None:
    graph = _graph()
    store = GraphSnapshotStore(tmp_path / "graphs")

    key = store.save(graph, analyzer_version=SALESFORCE_ANALYZER_VERSION)
    loaded = store.load(key)

    assert loaded == graph
    assert loaded is not graph
    assert key == GraphSnapshotKey(
        platform=Platform.SALESFORCE,
        source_revision=graph.base_revision,
        entry_paths=graph.entry_paths,
        analyzer_version=SALESFORCE_ANALYZER_VERSION,
    )
    assert store.save(graph, analyzer_version=SALESFORCE_ANALYZER_VERSION) == key


def test_snapshot_cache_misses_when_any_analysis_input_changes(tmp_path: Path) -> None:
    graph = _graph()
    store = GraphSnapshotStore(tmp_path / "graphs")
    key = store.save(graph, analyzer_version=SALESFORCE_ANALYZER_VERSION)

    changed_revision = key.model_copy(update={"source_revision": "sha256:" + "0" * 64})
    changed_entries = key.model_copy(
        update={"entry_paths": ("force-app/main/default/classes/Other.cls",)}
    )
    changed_analyzer = key.model_copy(update={"analyzer_version": "salesforce-static-v2"})

    assert store.load(changed_revision) is None
    assert store.load(changed_entries) is None
    assert store.load(changed_analyzer) is None


def test_snapshot_key_treats_entry_paths_as_a_canonical_set() -> None:
    revision = "sha256:" + "1" * 64
    first = GraphSnapshotKey(
        platform=Platform.SALESFORCE,
        source_revision=revision,
        entry_paths=("b.cls", "a.cls", "b.cls"),
        analyzer_version=SALESFORCE_ANALYZER_VERSION,
    )
    second = GraphSnapshotKey(
        platform=Platform.SALESFORCE,
        source_revision=revision,
        entry_paths=("a.cls", "b.cls"),
        analyzer_version=SALESFORCE_ANALYZER_VERSION,
    )

    assert first == second
    assert first.cache_digest == second.cache_digest


def test_graph_entry_permutations_have_one_canonical_immutable_snapshot(
    tmp_path: Path,
) -> None:
    revision = content_revision(FIXTURE_ROOT)
    first = build_salesforce_dependency_graph(
        FIXTURE_ROOT,
        (PAGE_ENTRY, CONTROLLER_ENTRY, PAGE_ENTRY),
        revision,
    )
    second = build_salesforce_dependency_graph(
        FIXTURE_ROOT,
        (CONTROLLER_ENTRY, PAGE_ENTRY),
        revision,
    )

    assert first.entry_paths == tuple(sorted((CONTROLLER_ENTRY, PAGE_ENTRY)))
    assert first == second
    assert artifact_digest(first) == artifact_digest(second)

    store = GraphSnapshotStore(tmp_path / "graphs")
    first_key = store.save(first, analyzer_version=SALESFORCE_ANALYZER_VERSION)
    second_key = store.save(second, analyzer_version=SALESFORCE_ANALYZER_VERSION)
    assert first_key == second_key
    assert store.load(first_key) == first


def test_snapshot_path_cannot_be_reused_for_different_graph_content(tmp_path: Path) -> None:
    graph = _graph()
    store = GraphSnapshotStore(tmp_path / "graphs")
    store.save(graph, analyzer_version=SALESFORCE_ANALYZER_VERSION)
    reordered = graph.model_copy(update={"nodes": tuple(reversed(graph.nodes))})

    with pytest.raises(PolicyViolation, match="immutable artifact already exists"):
        store.save(reordered, analyzer_version=SALESFORCE_ANALYZER_VERSION)
