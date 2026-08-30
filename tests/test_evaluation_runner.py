from __future__ import annotations

import inspect
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

import legacy_migration_agent.benchmark_protocol as benchmark_protocol_module
import legacy_migration_agent.evaluation_runner as evaluation_runner_module
from legacy_migration_agent.application.agent_run import (
    AgentRunModelClients,
    prepare_agent_run_request,
    start_agent_run,
)
from legacy_migration_agent.application.migration_scenarios import migration_launch_contract
from legacy_migration_agent.benchmark_execution import (
    build_benchmark_execution_anchor,
    write_benchmark_execution_anchor,
)
from legacy_migration_agent.benchmark_protocol import load_verified_benchmark_protocol
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.workspace import content_revision
from legacy_migration_agent.evaluation_runner import (
    benchmark_cell_route,
    benchmark_cell_routes,
    bind_benchmark_knowledge_arm,
)
from legacy_migration_agent.knowledge.wiki import BENCHMARK_RISK_REASONS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUESTED_AT = datetime(2026, 8, 28, tzinfo=UTC)


class _OfflineClaudeIdentityDouble:
    provider = "claude-cli"
    model_id = "claude-sonnet-5"
    live_invocation = False
    store_false_sent = False

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        del system_prompt, input_value, output_type
        raise AssertionError("the benchmark boundary must reject this offline double")


def _request(scenario_id: str):
    return prepare_agent_run_request(
        PROJECT_ROOT,
        request_id=f"measured-{scenario_id}",
        launch_contract=migration_launch_contract(scenario_id),
        requested_at=REQUESTED_AT,
    )


def _copy_protocol_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "agents", project / "agents")
    shutil.copytree(PROJECT_ROOT / "evaluation", project / "evaluation")
    shutil.copytree(PROJECT_ROOT / "fixtures", project / "fixtures")
    shutil.copytree(PROJECT_ROOT / "knowledge/wiki", project / "knowledge/wiki")
    shutil.copytree(
        PROJECT_ROOT / "tooling/mulesoft-runtime",
        project / "tooling/mulesoft-runtime",
    )
    return project


def test_only_benchmark_api_exposes_the_knowledge_arm_selector() -> None:
    request = _request("salesforce-vf-to-lwc")

    binding = bind_benchmark_knowledge_arm(
        PROJECT_ROOT,
        request,
        cell_id="salesforce-account-contact-medium--full-agent-no-wiki--r1",
        case_id="salesforce-account-contact-medium",
        scenario_id="salesforce-vf-to-lwc",
        knowledge_arm="full_agent_no_wiki",
    )

    assert "knowledge_binding" not in inspect.signature(start_agent_run).parameters
    assert binding.benchmark_id == "legacy-migration-benchmark-v2"
    assert binding.request_digest == artifact_digest(request)
    assert binding.source_revision == request.base_revision
    assert binding.wiki_tree_revision == content_revision(PROJECT_ROOT / "knowledge/wiki")
    assert binding.knowledge_arm == "full_agent_no_wiki"
    assert binding.provider_id == "claude-cli"
    assert binding.model_id == "claude-sonnet-5"
    assert binding.risk_seed_binding is None
    assert binding.configuration_digest.startswith("sha256:")
    assert binding.benchmark_registry_digest.startswith("sha256:")
    assert binding.execution_anchored is False


def test_benchmark_routes_enumerate_the_exact_resumable_matrix() -> None:
    routes = benchmark_cell_routes(PROJECT_ROOT)

    assert len(routes) == 18
    assert len({route.cell_id for route in routes}) == 18
    assert len({route.run_dir for route in routes}) == 18
    route = benchmark_cell_route(
        PROJECT_ROOT,
        "salesforce-account-contact-medium--full-agent-wiki--r1",
    )
    assert route.scenario_id == "salesforce-vf-to-lwc"
    assert route.run_dir == f".runs/benchmark-v2/{route.cell_id}"
    assert route.rubric_path == f"evaluation/benchmark-v2/rubrics/{route.cell_id}.json"
    assert route.receipt_path == (f"evaluation/benchmark-v2/results/receipts/{route.cell_id}.json")


def test_benchmark_route_rejects_an_undeclared_cell() -> None:
    with pytest.raises(PolicyViolation, match="not present"):
        benchmark_cell_route(PROJECT_ROOT, "invented--cell--r1")


def test_verified_protocol_exposes_public_dependency_edge_labels() -> None:
    protocol = load_verified_benchmark_protocol(PROJECT_ROOT)

    labels = protocol.dependency_labels_for_case("salesforce-account-contact-medium")

    assert len(labels) == 22
    assert labels[0].dependency_id == "salesforce.account-contact.edge-01.soql_field"
    assert labels[0].edge_key == (
        "soql_field",
        "LegacyAccountContactExplorerController",
        "Account.Id",
        "Account.Id",
    )
    assert labels[0].evidence_digest.startswith("sha256:")


def test_cell_binding_can_freeze_one_verified_pre_run_execution_anchor(
    tmp_path: Path,
) -> None:
    runtime_identity = "sha256:" + "9" * 64
    anchor = build_benchmark_execution_anchor(
        PROJECT_ROOT,
        runtime_identity_digest=runtime_identity,
        created_at=REQUESTED_AT,
        anchor_id="benchmark-v2-pre-run-anchor",
    )
    anchor_path = write_benchmark_execution_anchor(tmp_path / "anchor.json", anchor)

    binding = bind_benchmark_knowledge_arm(
        PROJECT_ROOT,
        _request("salesforce-vf-to-lwc"),
        cell_id="salesforce-account-contact-medium--full-agent-wiki--r1",
        case_id="salesforce-account-contact-medium",
        scenario_id="salesforce-vf-to-lwc",
        knowledge_arm="full_agent_wiki",
        execution_anchor_path=anchor_path,
    )

    assert binding.execution_anchored is True
    assert binding.execution_anchor_digest == anchor.anchor_digest
    assert binding.runtime_identity_digest == runtime_identity


def test_execution_rejects_a_protocol_only_binding_before_runtime_probe() -> None:
    binding = bind_benchmark_knowledge_arm(
        PROJECT_ROOT,
        _request("salesforce-vf-to-lwc"),
        cell_id="salesforce-account-contact-medium--full-agent-wiki--r1",
        case_id="salesforce-account-contact-medium",
        scenario_id="salesforce-vf-to-lwc",
        knowledge_arm="full_agent_wiki",
    )

    with pytest.raises(PolicyViolation, match="pre-run execution anchor"):
        evaluation_runner_module._verify_execution_anchor_binding(
            PROJECT_ROOT,
            None,
            None,  # type: ignore[arg-type]
            binding,
        )


def test_benchmark_binding_rejects_an_unknown_knowledge_arm() -> None:
    with pytest.raises(PolicyViolation, match="unknown knowledge arm"):
        bind_benchmark_knowledge_arm(
            PROJECT_ROOT,
            _request("salesforce-vf-to-lwc"),
            cell_id="salesforce-account-contact-medium--unknown--r1",
            case_id="salesforce-account-contact-medium",
            scenario_id="salesforce-vf-to-lwc",
            knowledge_arm="unknown",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("knowledge_arm", "config_id"),
    (
        ("full_agent_wiki", "full-agent-wiki"),
        ("full_agent_no_wiki", "full-agent-no-wiki"),
    ),
)
def test_complex_case_receives_only_the_inert_model_facing_risk_projection(
    knowledge_arm: str,
    config_id: str,
) -> None:
    request = _request("case-management-console")

    binding = bind_benchmark_knowledge_arm(
        PROJECT_ROOT,
        request,
        cell_id=f"salesforce-case-management-complex-risk--{config_id}--r1",
        case_id="salesforce-case-management-complex-risk",
        scenario_id="case-management-console",
        knowledge_arm=knowledge_arm,  # type: ignore[arg-type]
    )

    assert binding.risk_seed_binding is not None
    stimulus = binding.risk_seed_binding.stimulus
    assert stimulus.authority == "none"
    assert binding.risk_seed_binding.case_id == binding.case_id
    assert binding.risk_seed_binding.required_reasons == BENCHMARK_RISK_REASONS
    assert stimulus.evidence_id == "benchmark-supplemental-evidence-01"
    assert set(stimulus.model_dump(mode="json")) == {
        "evidence_id",
        "authority",
        "request_text",
        "seed_artifact_digest",
    }
    serialized = stimulus.model_dump_json()
    assert "expected_disposition" not in serialized
    assert "expected_intervention_reasons" not in serialized
    assert "safe_handling" not in serialized
    assert "decision_required" not in serialized
    assert "complex-risk" not in serialized
    assert "security-destructive" not in serialized
    for reason in BENCHMARK_RISK_REASONS:
        assert reason.value not in serialized


def test_binding_rejects_a_noncanonical_cell_before_any_run_start() -> None:
    with pytest.raises(PolicyViolation, match="not canonical"):
        bind_benchmark_knowledge_arm(
            PROJECT_ROOT,
            _request("salesforce-vf-to-lwc"),
            cell_id="salesforce-account-contact-medium--full-agent-no-wiki--r9",
            case_id="salesforce-account-contact-medium",
            scenario_id="salesforce-vf-to-lwc",
            knowledge_arm="full_agent_no_wiki",
        )


def test_benchmark_execution_rejects_an_offline_client_with_matching_names() -> None:
    request = _request("salesforce-vf-to-lwc")
    binding = bind_benchmark_knowledge_arm(
        PROJECT_ROOT,
        request,
        cell_id="salesforce-account-contact-medium--full-agent-wiki--r1",
        case_id="salesforce-account-contact-medium",
        scenario_id="salesforce-vf-to-lwc",
        knowledge_arm="full_agent_wiki",
    )
    model = _OfflineClaudeIdentityDouble()
    models = AgentRunModelClients.offline(
        architect=model,
        engineer=model,
        validator=model,
    )

    with pytest.raises(PolicyViolation, match="live remote-provider boundary"):
        evaluation_runner_module._verify_model_identity(models, binding)


def test_binding_rejects_drift_in_a_wiki_page_body(tmp_path: Path) -> None:
    project = _copy_protocol_project(tmp_path)
    page = project / "knowledge/wiki/pages/salesforce-validation.md"
    page.write_text(page.read_text(encoding="utf-8") + "\nUnfrozen edit.\n", encoding="utf-8")

    with pytest.raises(PolicyViolation, match="Wiki tree drifted"):
        bind_benchmark_knowledge_arm(
            project,
            _request("salesforce-vf-to-lwc"),
            cell_id="salesforce-account-contact-medium--full-agent-wiki--r1",
            case_id="salesforce-account-contact-medium",
            scenario_id="salesforce-vf-to-lwc",
            knowledge_arm="full_agent_wiki",
        )


def test_benchmark_reader_rejects_an_intermediate_directory_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "evaluation").symlink_to(PROJECT_ROOT / "evaluation", target_is_directory=True)

    with pytest.raises(PolicyViolation, match="safe regular file|non-directory"):
        benchmark_protocol_module._read_benchmark_json(
            project,
            "evaluation/benchmark-v2/declaration.json",
        )


def test_protocol_loader_rejects_duplicate_json_object_keys(tmp_path: Path) -> None:
    project = _copy_protocol_project(tmp_path)
    declaration = project / "evaluation/benchmark-v2/declaration.json"
    declaration.write_text(
        declaration.read_text(encoding="utf-8").replace(
            '"schema_version": "2.0",',
            '"schema_version": "2.0",\n  "schema_version": "2.0",',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(PolicyViolation, match="duplicate JSON object key 'schema_version'"):
        load_verified_benchmark_protocol(project)


def test_protocol_loader_rejects_referenced_fixture_contract_drift(tmp_path: Path) -> None:
    project = _copy_protocol_project(tmp_path)
    fixture_contract = project / "fixtures/salesforce/account-contact-explorer/fixture.yaml"
    fixture_contract.write_text(
        fixture_contract.read_text(encoding="utf-8") + "\n# unbound drift\n",
        encoding="utf-8",
    )

    with pytest.raises(PolicyViolation, match="fixture contract drifted"):
        load_verified_benchmark_protocol(project)


def test_protocol_loader_rejects_referenced_source_edge_drift(tmp_path: Path) -> None:
    project = _copy_protocol_project(tmp_path)
    source_edges = project / "evaluation/salesforce-account-contact-explorer-source-edges.json"
    source_edges.write_text(
        source_edges.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PolicyViolation, match="source-edge labels drifted"):
        load_verified_benchmark_protocol(project)


def test_protocol_loader_rejects_current_source_tree_drift(tmp_path: Path) -> None:
    project = _copy_protocol_project(tmp_path)
    page = (
        project
        / "fixtures/salesforce/account-contact-explorer/input"
        / "force-app/main/default/pages/LegacyAccountContactExplorer.page"
    )
    page.write_text(page.read_text(encoding="utf-8") + "\n<!-- drift -->\n", encoding="utf-8")

    with pytest.raises(PolicyViolation, match="source tree drifted"):
        load_verified_benchmark_protocol(project)


def test_protocol_loader_rejects_semantic_reference_drift_with_fresh_outer_digests(
    tmp_path: Path,
) -> None:
    project = _copy_protocol_project(tmp_path)
    benchmark_root = project / "evaluation/benchmark-v2"
    dependency_path = benchmark_root / "dependency-labels.json"
    dependency_payload = json.loads(dependency_path.read_text(encoding="utf-8"))
    dependency_payload["cases"][0]["source_edge_labels_path"] = (
        "evaluation/salesforce-account-contact-explorer-source-edges.json"
    )
    # Remove the now-current independent review binding in this isolated copy so
    # this test continues to exercise the deeper semantic reference check.
    dependency_payload.update(
        {
            "review_status": "initial_label_set",
            "reviewer_id": None,
            "review_evidence_path": None,
            "review_evidence_digest": None,
        }
    )
    dependency_path.write_text(json.dumps(dependency_payload, indent=2) + "\n", encoding="utf-8")

    declaration_path = benchmark_root / "declaration.json"
    declaration_payload = json.loads(declaration_path.read_text(encoding="utf-8"))
    declaration_payload["dependency_labels_digest"] = artifact_digest(dependency_payload)
    declaration_path.write_text(json.dumps(declaration_payload, indent=2) + "\n", encoding="utf-8")

    registry_path = benchmark_root / "registry.json"
    registry_payload = json.loads(registry_path.read_text(encoding="utf-8"))
    registry_payload["declaration_digest"] = artifact_digest(declaration_payload)
    for case in registry_payload["cases"]:
        case.update(
            {
                "review_status": "initial_label_set",
                "reviewer_id": None,
                "review_evidence_digest": None,
            }
        )
    registry_path.write_text(json.dumps(registry_payload, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(PolicyViolation, match="reference another source-edge file"):
        load_verified_benchmark_protocol(project)


def test_protocol_loader_rejects_current_scenario_definition_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_lookup = benchmark_protocol_module.migration_scenario_by_id

    def drifted_lookup(scenario_id: str):
        scenario = canonical_lookup(scenario_id)
        if scenario_id == "salesforce-vf-to-lwc":
            return scenario.model_copy(update={"title": scenario.title + " drift"})
        return scenario

    monkeypatch.setattr(
        benchmark_protocol_module,
        "migration_scenario_by_id",
        drifted_lookup,
    )

    with pytest.raises(PolicyViolation, match="scenario definition drifted"):
        load_verified_benchmark_protocol(PROJECT_ROOT)
