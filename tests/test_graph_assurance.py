from __future__ import annotations

from pathlib import Path

import pytest

from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.workspace import TreeSnapshot, snapshot_tree
from legacy_migration_agent.graphs.dependency_graph import (
    SALESFORCE_ANALYZER_VERSION,
    build_salesforce_dependency_graph,
)
from legacy_migration_agent.graphs.graph_assurance import (
    GraphAssuranceReport,
    GraphAssuranceStatus,
    build_graph_assurance_report,
)
from legacy_migration_agent.graphs.graph_contracts import EdgeKind
from legacy_migration_agent.graphs.mulesoft_dependency_graph import (
    MULESOFT_ANALYZER_VERSION,
    build_mulesoft_dependency_graph,
)
from legacy_migration_agent.platforms.mulesoft_local_checks import MULE3_APP
from legacy_migration_agent.platforms.mulesoft_runtime import MULESOFT_PLATFORM_ADAPTER
from legacy_migration_agent.platforms.salesforce_runtime import (
    CASE_SALESFORCE_PLATFORM_ADAPTER,
    CASE_SOURCE_ENTRY,
    SALESFORCE_PLATFORM_ADAPTER,
    SALESFORCE_SOURCE_ENTRY,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("source_root", "entry_path", "builder", "analyzer_version", "required_paths"),
    (
        (
            PROJECT_ROOT / "fixtures/salesforce/account-contact-explorer/input",
            SALESFORCE_SOURCE_ENTRY,
            build_salesforce_dependency_graph,
            SALESFORCE_ANALYZER_VERSION,
            SALESFORCE_PLATFORM_ADAPTER.scope_policy.required_source_input_paths,
        ),
        (
            PROJECT_ROOT / "fixtures/salesforce/case-management-console/input",
            CASE_SOURCE_ENTRY,
            build_salesforce_dependency_graph,
            SALESFORCE_ANALYZER_VERSION,
            CASE_SALESFORCE_PLATFORM_ADAPTER.scope_policy.required_source_input_paths,
        ),
        (
            PROJECT_ROOT / "fixtures/mulesoft/customer-status-api/input",
            MULE3_APP,
            build_mulesoft_dependency_graph,
            MULESOFT_ANALYZER_VERSION,
            MULESOFT_PLATFORM_ADAPTER.scope_policy.required_source_input_paths,
        ),
    ),
)
def test_supported_capstone_fixtures_receive_assured_reports(
    source_root: Path,
    entry_path: str,
    builder: object,
    analyzer_version: str,
    required_paths: tuple[str, ...],
) -> None:
    snapshot = snapshot_tree(source_root)
    graph = builder(source_root, (entry_path,), snapshot.revision)  # type: ignore[operator]

    report = build_graph_assurance_report(
        snapshot,
        graph,
        analyzer_version=analyzer_version,
        required_source_paths=required_paths,
    )

    assert report.status is GraphAssuranceStatus.ASSURED
    assert report.source_revision == snapshot.revision
    assert report.dependency_graph_digest == artifact_digest(graph)
    assert tuple(item.path for item in report.source_digests) == required_paths
    assert all(item.sha256 is not None for item in report.source_digests)
    assert all(item.complete for item in report.parser_source_coverage)
    assert report.detected_discrepancies == ()
    assert report.unsupported_or_ambiguous_constructs == ()
    assert report.security_sensitive_dependency_coverage.complete


def test_independent_reconciliation_detects_a_silent_graph_edge_omission() -> None:
    source_root = PROJECT_ROOT / "fixtures/salesforce/account-contact-explorer/input"
    snapshot = snapshot_tree(source_root)
    graph = build_salesforce_dependency_graph(
        source_root,
        (SALESFORCE_SOURCE_ENTRY,),
        snapshot.revision,
    )
    payload = graph.model_dump(mode="python")
    payload["edges"] = [
        edge for edge in payload["edges"] if edge["kind"] is not EdgeKind.VF_CONTROLLER
    ]
    omitted = type(graph).model_validate(payload)

    report = build_graph_assurance_report(
        snapshot,
        omitted,
        analyzer_version=SALESFORCE_ANALYZER_VERSION,
        required_source_paths=(
            SALESFORCE_PLATFORM_ADAPTER.scope_policy.required_source_input_paths
        ),
    )

    assert report.status is GraphAssuranceStatus.BLOCKED
    assert any(
        item.category == "reference_count_mismatch"
        and item.expected_count == 1
        and item.observed_count == 0
        for item in report.detected_discrepancies
    )


def test_missing_required_source_is_blocked_even_if_snapshot_revision_is_forged() -> None:
    source_root = PROJECT_ROOT / "fixtures/salesforce/account-contact-explorer/input"
    snapshot = snapshot_tree(source_root)
    graph = build_salesforce_dependency_graph(
        source_root,
        (SALESFORCE_SOURCE_ENTRY,),
        snapshot.revision,
    )
    missing_path = next(
        path
        for path in SALESFORCE_PLATFORM_ADAPTER.scope_policy.required_source_input_paths
        if path.endswith("LegacyAccountContactExplorerController.cls")
    )
    incomplete = TreeSnapshot(
        entries=tuple(item for item in snapshot.entries if item.path != missing_path),
        directories=snapshot.directories,
        revision=snapshot.revision,
    )

    report = build_graph_assurance_report(
        incomplete,
        graph,
        analyzer_version=SALESFORCE_ANALYZER_VERSION,
        required_source_paths=(
            SALESFORCE_PLATFORM_ADAPTER.scope_policy.required_source_input_paths
        ),
    )

    assert report.status is GraphAssuranceStatus.BLOCKED
    assert any(
        item.category == "missing_source" and item.path == missing_path
        for item in report.detected_discrepancies
    )


def test_report_contract_rejects_status_or_digest_tampering() -> None:
    source_root = PROJECT_ROOT / "fixtures/mulesoft/customer-status-api/input"
    snapshot = snapshot_tree(source_root)
    graph = build_mulesoft_dependency_graph(source_root, (MULE3_APP,), snapshot.revision)
    report = build_graph_assurance_report(
        snapshot,
        graph,
        analyzer_version=MULESOFT_ANALYZER_VERSION,
        required_source_paths=MULESOFT_PLATFORM_ADAPTER.scope_policy.required_source_input_paths,
    )

    status_payload = report.model_dump(mode="python")
    status_payload["status"] = GraphAssuranceStatus.BLOCKED
    with pytest.raises(ValueError, match="status does not match"):
        GraphAssuranceReport.model_validate(status_payload)

    digest_payload = report.model_dump(mode="python")
    digest_payload["dependency_graph_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="report ID"):
        GraphAssuranceReport.model_validate(digest_payload)


def test_duplicate_graph_source_bindings_are_an_internal_inconsistency() -> None:
    source_root = PROJECT_ROOT / "fixtures/mulesoft/customer-status-api/input"
    snapshot = snapshot_tree(source_root)
    graph = build_mulesoft_dependency_graph(source_root, (MULE3_APP,), snapshot.revision)
    payload = graph.model_dump(mode="python")
    payload["source_digests"] = (
        *payload["source_digests"],
        payload["source_digests"][0],
    )
    inconsistent = type(graph).model_validate(payload)

    report = build_graph_assurance_report(
        snapshot,
        inconsistent,
        analyzer_version=MULESOFT_ANALYZER_VERSION,
        required_source_paths=MULESOFT_PLATFORM_ADAPTER.scope_policy.required_source_input_paths,
    )

    assert report.status is GraphAssuranceStatus.BLOCKED
    assert any(item.category == "internal_inconsistency" for item in report.detected_discrepancies)
