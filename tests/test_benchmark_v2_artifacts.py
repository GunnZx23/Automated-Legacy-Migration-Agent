from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from legacy_migration_agent.agent_runtime.agent_definitions import (
    AgentRole,
    load_agent_registry,
)
from legacy_migration_agent.application.migration_scenarios import (
    migration_recipe,
    migration_scenario_by_id,
)
from legacy_migration_agent.benchmark_protocol import load_verified_benchmark_protocol
from legacy_migration_agent.contracts import Platform
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.workspace import content_revision
from legacy_migration_agent.graphs.dependency_graph import (
    build_salesforce_dependency_graph,
)
from legacy_migration_agent.graphs.graph_evaluation import (
    evaluate_dependency_graph,
    load_graph_label_set,
)
from legacy_migration_agent.knowledge.wiki import BENCHMARK_RISK_REASONS
from legacy_migration_agent.measured_evaluation import (
    LabelReviewStatus,
    MeasuredEvaluationRegistry,
    WorkflowDisposition,
    canonical_cell_id,
)
from legacy_migration_agent.platforms.mulesoft_runtime import (
    MULESOFT_IMPLEMENTATION_CONTRACT,
)
from legacy_migration_agent.platforms.salesforce_runtime import (
    CASE_IMPLEMENTATION_CONTRACT,
    SALESFORCE_IMPLEMENTATION_CONTRACT,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = PROJECT_ROOT / "evaluation/benchmark-v2"
REGISTRY_PATH = BENCHMARK_ROOT / "registry.json"


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _file_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _registry() -> MeasuredEvaluationRegistry:
    return MeasuredEvaluationRegistry.model_validate_json(REGISTRY_PATH.read_text(encoding="utf-8"))


def test_public_protocol_loader_verifies_the_complete_predeclared_graph() -> None:
    protocol = load_verified_benchmark_protocol(PROJECT_ROOT)

    assert protocol.declaration.registry_id == protocol.registry.registry_id
    assert protocol.declaration_digest == protocol.registry.declaration_digest
    assert protocol.registry_digest == artifact_digest(_json(REGISTRY_PATH))
    assert tuple(source.case_id for source in protocol.source_snapshots.cases) == (
        protocol.declaration.cases
    )
    assert tuple(case.case_id for case in protocol.dependency_labels.cases) == (
        protocol.declaration.cases
    )
    assert protocol.wiki_tree_revision == content_revision(PROJECT_ROOT / "knowledge/wiki")
    assert protocol.risk_seed.case_id == "salesforce-case-management-complex-risk"
    review = protocol.label_review_evidence
    assert review is not None
    assert review.reviewer_id == "bw"
    assert review.reviewer_domain == "Manager"
    assert review.attestation == "Approved"
    assert review.corrections == ()
    assert review.review_subject_digest == protocol.label_review_subject_digest
    assert protocol.label_review_subject_digest == (
        "sha256:fa76b2b5b9f8a9f4dea0637e7c3a1f6d9cddce6fa08ff7fbc4695631ffd29183"
    )


def test_registry_is_the_exact_predeclared_18_cell_matrix() -> None:
    registry = _registry()
    declaration = _json(BENCHMARK_ROOT / "declaration.json")

    assert registry.registry_id == "legacy-migration-benchmark-v2"
    assert registry.declaration_digest == artifact_digest(declaration)
    assert registry.repetitions == 3
    assert declaration["protocol_status"] == "predeclared_not_executed"
    assert declaration["planned_live_cells"] == 18
    assert declaration["result_receipts_status"] == "not_performed"
    assert declaration["result_receipts"] == []

    cell_ids = tuple(
        canonical_cell_id(case.case_id, config.config_id, repetition)
        for case in registry.cases
        for config in registry.configurations
        for repetition in range(1, registry.repetitions + 1)
    )
    assert len(cell_ids) == 18
    assert len(set(cell_ids)) == 18
    assert cell_ids == tuple(
        f"{case_id}--{config_id}--r{repetition}"
        for case_id in declaration["cases"]
        for config_id in declaration["configurations"]
        for repetition in range(1, 4)
    )


def test_case_outcomes_and_independent_review_boundary_are_explicit() -> None:
    registry = _registry()
    cases = {case.case_id: case for case in registry.cases}

    assert cases["mulesoft-customer-status-simple"].expected_disposition is (
        WorkflowDisposition.ENVIRONMENT_UNAVAILABLE
    )
    assert cases["salesforce-account-contact-medium"].expected_disposition is (
        WorkflowDisposition.READY_FOR_HUMAN_REVIEW
    )
    risk_case = cases["salesforce-case-management-complex-risk"]
    assert risk_case.expected_disposition is WorkflowDisposition.DECISION_REQUIRED
    assert risk_case.intervention_expected is True

    assert {case.complexity.value for case in registry.cases} == {
        "simple",
        "medium",
        "complex",
    }
    assert all(
        case.review_status is LabelReviewStatus.INDEPENDENTLY_REVIEWED for case in registry.cases
    )
    assert all(case.reviewer_id == "bw" for case in registry.cases)
    assert all(
        case.review_evidence_digest
        == "sha256:b718d6b3c130d1318f27b9911ec223cde650a39b19af39927ab590ccf3aba5c3"
        for case in registry.cases
    )


def test_independent_review_packet_names_the_canonical_case_hazards() -> None:
    registry = _registry()
    risk_case = next(
        case for case in registry.cases if case.case_id == "salesforce-case-management-complex-risk"
    )
    packet = (BENCHMARK_ROOT / "independent-label-review-packet.md").read_text(encoding="utf-8")
    checklist_item = packet.split("4. ", maxsplit=1)[1].split("\n5. ", maxsplit=1)[0]

    assert tuple(re.findall(r"`([^`]+)`", checklist_item)) == (
        risk_case.expected_intervention_reason_ids
    )


def test_source_snapshots_recompute_from_three_distinct_roots() -> None:
    registry = _registry()
    snapshots = _json(BENCHMARK_ROOT / "source-snapshots.json")["cases"]
    by_case = {case["case_id"]: case for case in snapshots}
    registry_cases = {case.case_id: case for case in registry.cases}
    implementation_contracts = {
        "mulesoft-mule3-to-mule4": MULESOFT_IMPLEMENTATION_CONTRACT,
        "salesforce-vf-to-lwc": SALESFORCE_IMPLEMENTATION_CONTRACT,
        "case-management-console": CASE_IMPLEMENTATION_CONTRACT,
    }

    assert len(by_case) == 3
    assert len({case["source_root"] for case in snapshots}) == 3
    assert len({case["source_revision"] for case in snapshots}) == 3

    for case_id, snapshot in by_case.items():
        source_root = PROJECT_ROOT / snapshot["source_root"]
        scenario = migration_scenario_by_id(snapshot["scenario_id"])
        assert content_revision(source_root) == snapshot["source_revision"]
        assert registry_cases[case_id].source_digest == snapshot["source_revision"]
        assert (
            _file_digest(PROJECT_ROOT / snapshot["fixture_contract_path"])
            == snapshot["fixture_contract_file_digest"]
        )
        assert (
            _file_digest(PROJECT_ROOT / snapshot["source_edge_labels_path"])
            == snapshot["source_edge_labels_file_digest"]
        )
        assert scenario.source_root == snapshot["source_root"]
        assert scenario.definition_digest == snapshot["scenario_definition_digest"]
        assert scenario.scope_policy_digest == snapshot["scope_policy_digest"]
        assert (
            artifact_digest(implementation_contracts[scenario.scenario_id])
            == snapshot["implementation_contract_digest"]
        )


def test_dependency_labels_are_exactly_grounded_and_independently_reviewed() -> None:
    registry = _registry()
    artifact = _json(BENCHMARK_ROOT / "dependency-labels.json")
    registry_cases = {case.case_id: case for case in registry.cases}

    assert artifact["review_status"] == "independently_reviewed"
    assert artifact["reviewer_id"] == "bw"
    assert artifact["review_evidence_path"] == (
        "evaluation/benchmark-v2/label-review-evidence.json"
    )
    assert artifact["review_evidence_digest"] == (
        "sha256:b718d6b3c130d1318f27b9911ec223cde650a39b19af39927ab590ccf3aba5c3"
    )
    impact_policy = artifact["high_impact_definition"]
    assert impact_policy["definition_id"] == "migration-dependency-impact-v1"
    assert impact_policy["basis_outcomes"] == {
        "production_runtime": True,
        "production_data_contract": True,
        "production_security": True,
        "sole_required_validation": True,
        "supporting_test_evidence": False,
    }

    expected_counts = {
        "mulesoft-customer-status-simple": (10, 7),
        "salesforce-account-contact-medium": (22, 17),
        "salesforce-case-management-complex-risk": (33, 27),
    }
    for case_labels in artifact["cases"]:
        case_id = case_labels["case_id"]
        labels = case_labels["labels"]
        source_edges = _json(PROJECT_ROOT / case_labels["source_edge_labels_path"])["edges"]
        registry_dependencies = {
            dependency.dependency_id: dependency
            for dependency in registry_cases[case_id].dependencies
        }
        expected_total, expected_high = expected_counts[case_id]

        assert len(labels) == expected_total
        assert sum(label["high_impact"] for label in labels) == expected_high
        assert len(registry_dependencies) == expected_total
        labeled_edges = [label["edge"] for label in labels]
        assert len({json.dumps(edge, sort_keys=True) for edge in labeled_edges}) == len(
            labeled_edges
        )
        assert {json.dumps(edge, sort_keys=True) for edge in labeled_edges} == {
            json.dumps(edge, sort_keys=True) for edge in source_edges
        }
        for label in labels:
            assert label["edge"] in source_edges
            assert impact_policy["basis_outcomes"][label["impact_basis"]] is label["high_impact"]
            dependency = registry_dependencies[label["dependency_id"]]
            assert dependency.high_impact is label["high_impact"]
            assert dependency.evidence_digest == artifact_digest(label)

    mule, account, case = artifact["cases"]
    assert sum(label["high_impact"] for label in mule["labels"]) == 7
    assert {label["edge"]["kind"] for label in mule["labels"] if not label["high_impact"]} == {
        "munit_flow_reference",
        "munit_variable_reference",
        "munit_suite_test",
    }
    assert {label["edge"]["source"] for label in account["labels"] if not label["high_impact"]} == {
        "LegacyAcctContactExplorerCtrlTest"
    }
    assert {label["edge"]["source"] for label in case["labels"] if not label["high_impact"]} == {
        "LegacyCaseConsoleCtrlTest"
    }


def test_case_source_edge_artifact_matches_the_current_graph_without_claiming_review() -> None:
    label_path = BENCHMARK_ROOT / "salesforce-case-management-console-source-edges.json"
    labels = load_graph_label_set(label_path, platform=Platform.SALESFORCE)
    source_root = PROJECT_ROOT / "fixtures/salesforce/case-management-console/input"
    graph = build_salesforce_dependency_graph(
        source_root,
        ("force-app/main/default/pages/LegacyCaseManagementConsole.page",),
        content_revision(source_root),
    )

    report = evaluate_dependency_graph(graph, labels)

    assert len(labels.edges) == 33
    assert report.metrics.recall == 1
    assert report.metrics.precision == 1
    assert report.missing_edges == ()
    assert report.unexpected_edges == ()
    assert report.label_review_status == "initial_label_set"
    assert report.claim_scope == "exploratory_unreviewed"
    assert report.exit_gate_eligible is False


def test_all_source_edge_artifacts_use_one_unreviewed_vocabulary() -> None:
    snapshots = _json(BENCHMARK_ROOT / "source-snapshots.json")["cases"]

    assert {
        _json(PROJECT_ROOT / snapshot["source_edge_labels_path"])["review_status"]
        for snapshot in snapshots
    } == {"initial_label_set"}


def test_both_configurations_are_bound_to_current_runtime_and_differ_only_by_wiki() -> None:
    registry = _registry()
    bindings = _json(BENCHMARK_ROOT / "runtime-bindings.json")
    agent_registry = load_agent_registry(PROJECT_ROOT / "agents")

    current_agents = {
        role.value: {
            "version": agent_registry.get(role).version,
            "definition_digest": agent_registry.get(role).definition_digest,
        }
        for role in AgentRole
    }
    assert bindings["agent_definitions"] == current_agents
    assert bindings["prompt_policy"]["agent_definition_digests"] == {
        role: values["definition_digest"] for role, values in current_agents.items()
    }
    assert bindings["prompt_policy"]["wiki_catalog_file_digest"] == _file_digest(
        PROJECT_ROOT / "knowledge/wiki/catalog.json"
    )

    validation_scenarios = bindings["validation_policy"]["scenarios"]
    for scenario_id, values in validation_scenarios.items():
        scenario = migration_scenario_by_id(scenario_id)
        recipe = migration_recipe(scenario.recipe_id)
        assert values["definition_digest"] == scenario.definition_digest
        assert values["scope_policy_digest"] == scenario.scope_policy_digest
        assert values["allowed_validation_command_ids"] == list(
            recipe.allowed_validation_command_ids
        )
        assert bindings["prompt_policy"]["scenario_definition_digests"][scenario_id] == (
            scenario.definition_digest
        )

    common = [
        config.model_dump(mode="json", exclude={"config_id", "uses_wiki"})
        for config in registry.configurations
    ]
    assert common[0] == common[1]
    assert {config.config_id: config.uses_wiki for config in registry.configurations} == {
        "full-agent-wiki": True,
        "full-agent-no-wiki": False,
    }
    for config in registry.configurations:
        assert config.provider_id == bindings["provider_id"] == "claude-cli"
        assert config.model_id == bindings["model_id"] == "claude-sonnet-5"
        assert config.agent_definitions_digest == artifact_digest(bindings["agent_definitions"])
        assert config.prompt_policy_digest == artifact_digest(bindings["prompt_policy"])
        assert config.validation_policy_digest == artifact_digest(bindings["validation_policy"])

    assert bindings["environment_conditions"] == [
        {
            "case_id": "mulesoft-customer-status-simple",
            "evidence_path": "tooling/mulesoft-runtime/authority.json",
            "evidence_file_digest": _file_digest(
                PROJECT_ROOT / "tooling/mulesoft-runtime/authority.json"
            ),
            "capability": "mule_runtime_maven_munit",
            "observed_availability": "unavailable",
            "expected_disposition": "environment_unavailable",
        }
    ]


def test_declaration_binds_supporting_artifacts_and_no_result_receipts() -> None:
    declaration = _json(BENCHMARK_ROOT / "declaration.json")
    for path_field, digest_field in (
        ("source_snapshots_path", "source_snapshots_digest"),
        ("runtime_bindings_path", "runtime_bindings_digest"),
        ("dependency_labels_path", "dependency_labels_digest"),
        ("risk_seed_path", "risk_seed_digest"),
    ):
        artifact = _json(PROJECT_ROOT / declaration[path_field])
        assert artifact_digest(artifact) == declaration[digest_field]

    assert not tuple(BENCHMARK_ROOT.glob("*receipt*.json"))
    assert not tuple(BENCHMARK_ROOT.glob("*result*.json"))


def test_risk_seed_is_immutable_inert_text_with_a_decision_required_oracle() -> None:
    declaration = _json(BENCHMARK_ROOT / "declaration.json")
    seed = _json(BENCHMARK_ROOT / "risk-seed.json")
    request = seed["request_text"].lower()

    assert artifact_digest(seed) == declaration["risk_seed_digest"]
    assert seed["seed_id"] == "salesforce-case-security-destructive-scope-v1"
    assert seed["status"] == "inert_not_executed"
    assert seed["synthetic"] is True
    assert seed["normal_ui_exposure"] is False
    assert seed["expected_disposition"] == "decision_required"
    assert seed["expected_intervention_reasons"] == [
        reason.value for reason in BENCHMARK_RISK_REASONS
    ]
    assert all(
        value == "not_performed"
        for value in seed["safe_handling"].values()
        if value != "benchmark_input_only"
    )
    assert "delete the legacy visualforce page" in request
    assert "without sharing" in request
    assert "remove with user_mode" in request
    assert "create, edit, and delete access" in request
    assert "rm -rf" not in request
    assert "git " not in request
    assert "sf project deploy" not in request
    assert "http://" not in request and "https://" not in request
    assert "../" not in request


def test_readme_states_the_small_sample_and_unavailable_evidence_boundaries() -> None:
    readme = (BENCHMARK_ROOT / "README.md").read_text(encoding="utf-8").lower()

    assert "one case per complexity stratum" in readme
    assert "all **18 live model-bearing cells reached a terminal state**" in readme
    assert "initial_label_set` provenance" in readme
    assert "no per-cell `humanreviewrubric`" in readme
    assert "aggregate result is claimed yet" in readme
    assert "attested mule maven/munit runtime remains unavailable" in readme
    assert "cannot support broad statistical" in readme
