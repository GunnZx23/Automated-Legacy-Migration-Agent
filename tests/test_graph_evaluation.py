from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacy_migration_agent.contracts import Platform
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.workspace import content_revision
from legacy_migration_agent.graphs.dependency_graph import build_salesforce_dependency_graph
from legacy_migration_agent.graphs.graph_evaluation import (
    evaluate_dependency_graph,
    load_graph_label_set,
)
from legacy_migration_agent.graphs.mulesoft_dependency_graph import build_mulesoft_dependency_graph

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SALESFORCE_ROOT = PROJECT_ROOT / "fixtures/salesforce/account-contact-explorer/input"
SALESFORCE_ENTRY = "force-app/main/default/pages/LegacyAccountContactExplorer.page"
MULESOFT_ROOT = PROJECT_ROOT / "fixtures/mulesoft/customer-status-api/input"
MULESOFT_ENTRY = "legacy-mule3/customer-status-api/src/main/app/customer-status-api.xml"


def test_salesforce_graph_metrics_remain_explicitly_unreviewed() -> None:
    labels = load_graph_label_set(
        PROJECT_ROOT / "evaluation/salesforce-account-contact-explorer-source-edges.json",
        platform=Platform.SALESFORCE,
    )
    graph = build_salesforce_dependency_graph(
        SALESFORCE_ROOT,
        (SALESFORCE_ENTRY,),
        content_revision(SALESFORCE_ROOT),
    )

    report = evaluate_dependency_graph(graph, labels)

    assert report.platform is Platform.SALESFORCE
    assert report.metrics.recall == 1
    assert report.metrics.precision == 1
    assert report.claim_scope == "exploratory_unreviewed"
    assert report.label_review_status == "unreviewed_initial"
    assert report.exit_gate_eligible is False


def test_mulesoft_initial_labels_cover_the_current_fixture_without_overclaim() -> None:
    labels = load_graph_label_set(
        PROJECT_ROOT / "evaluation/mulesoft-customer-status-api-source-edges.json",
        platform=Platform.MULESOFT,
    )
    graph = build_mulesoft_dependency_graph(
        MULESOFT_ROOT,
        (MULESOFT_ENTRY,),
        content_revision(MULESOFT_ROOT),
    )

    report = evaluate_dependency_graph(graph, labels)

    assert report.platform is Platform.MULESOFT
    assert report.metrics.recall == 1
    assert report.metrics.precision == 1
    assert report.unexpected_edges == ()
    assert report.claim_scope == "exploratory_unreviewed"
    assert report.exit_gate_eligible is False


def test_only_an_exact_independently_reviewed_label_set_can_clear_the_gate() -> None:
    labels = load_graph_label_set(
        PROJECT_ROOT / "evaluation/salesforce-account-contact-explorer-source-edges.json",
        platform=Platform.SALESFORCE,
    ).model_copy(update={"review_status": "independently_reviewed"})
    graph = build_salesforce_dependency_graph(
        SALESFORCE_ROOT,
        (SALESFORCE_ENTRY,),
        content_revision(SALESFORCE_ROOT),
    )

    report = evaluate_dependency_graph(graph, labels)

    assert report.claim_scope == "independently_reviewed_fixture"
    assert report.exit_gate_eligible is True


def test_label_loading_rejects_platform_drift_malformed_json_and_symlinks(
    tmp_path: Path,
) -> None:
    source = PROJECT_ROOT / "evaluation/mulesoft-customer-status-api-source-edges.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["platform"] = "salesforce"
    drifted = tmp_path / "drifted.json"
    drifted.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PolicyViolation, match="platform"):
        load_graph_label_set(drifted, platform=Platform.MULESOFT)

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not-json", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="valid bounded JSON"):
        load_graph_label_set(malformed, platform=Platform.MULESOFT)

    link = tmp_path / "labels-link.json"
    link.symlink_to(source)
    with pytest.raises(PolicyViolation, match="non-symlink"):
        load_graph_label_set(link, platform=Platform.MULESOFT)


def test_graph_and_labels_cannot_cross_platform_strata() -> None:
    labels = load_graph_label_set(
        PROJECT_ROOT / "evaluation/mulesoft-customer-status-api-source-edges.json",
        platform=Platform.MULESOFT,
    )
    graph = build_salesforce_dependency_graph(
        SALESFORCE_ROOT,
        (SALESFORCE_ENTRY,),
        content_revision(SALESFORCE_ROOT),
    )

    with pytest.raises(PolicyViolation, match="different platforms"):
        evaluate_dependency_graph(graph, labels)
