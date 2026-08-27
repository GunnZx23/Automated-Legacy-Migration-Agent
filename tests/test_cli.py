from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from legacy_migration_agent.application.migration_scenarios import migration_scenario
from legacy_migration_agent.cli import build_parser, main
from legacy_migration_agent.contracts import (
    DependencyEvidence,
    MigrationManifest,
    MigrationRequest,
    MigrationTarget,
    Platform,
    TransformationStep,
    ValidationCommand,
)
from legacy_migration_agent.core.workspace import content_revision, snapshot_tree
from legacy_migration_agent.graphs.dependency_graph import build_salesforce_dependency_graph
from legacy_migration_agent.schema_compatibility import PUBLIC_SCHEMA_MODELS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_COMMANDS = {
    "validate-manifest",
    "export-schemas",
    "wiki-search",
    "agents-check",
    "agent-request-create",
    "agent-run-start",
    "agent-run-resume",
    "agent-run-retry",
    "agent-run-status",
    "agent-manifest-decision-create",
    "agent-correction-approval-create",
    "final-review-request",
    "final-review-decide",
    "final-review-status",
    "graph-evaluate",
    "evaluation-verify",
    "evaluation-pilot-run-local",
    "evaluation-pilot-verify",
    "evaluation-pilot-ingest-agent-run",
    "ui",
}


def test_parser_exposes_only_the_capstone_command_surface() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )

    assert set(subparsers.choices) == EXPECTED_COMMANDS
    for removed in (
        "phase0-propose",
        "scan-public",
        "benchmark-summarize",
        "release-package-create",
        "readiness-evaluate",
        "go-no-go-evaluate",
    ):
        assert removed not in subparsers.choices


def test_export_schemas_writes_all_public_contracts(tmp_path: Path, capsys) -> None:
    destination = tmp_path / "schemas"

    result = main(["export-schemas", "--output-dir", str(destination)])

    assert result == 0
    summary = json.loads(capsys.readouterr().out)
    assert set(summary["exported"]) == {
        f"{model.__name__}.schema.json" for model in PUBLIC_SCHEMA_MODELS
    }
    for filename in summary["exported"]:
        schema = json.loads((destination / filename).read_text(encoding="utf-8"))
        assert schema["additionalProperties"] is False


def test_validate_manifest_accepts_a_typed_salesforce_request(
    tmp_path: Path,
    capsys,
) -> None:
    output_path = "force-app/main/default/lwc/accountExplorer/accountExplorer.js"
    request = MigrationRequest(
        request_id="cli-salesforce-request-v1",
        platform=Platform.SALESFORCE,
        repository="fixture",
        base_revision="abcdef0",
        target=MigrationTarget(
            entry_path="force-app/main/default/pages/LegacyAccountContactExplorer.page",
            target_runtime="Lightning Web Components",
            source_version="Salesforce API 67.0",
            target_version="Salesforce API 67.0",
            description="Migrate one Visualforce page to LWC.",
        ),
    )
    manifest = MigrationManifest(
        manifest_id="cli-salesforce-manifest-v1",
        request_id=request.request_id,
        platform=request.platform,
        base_revision=request.base_revision,
        approved_paths=(output_path,),
        dependencies=(
            DependencyEvidence(
                path=request.target.entry_path,
                relation="migration source",
                source="request",
            ),
        ),
        transformations=(
            TransformationStep(
                step_id="create-lwc",
                description="Create the bounded LWC JavaScript module.",
                input_paths=(request.target.entry_path,),
                output_paths=(output_path,),
            ),
        ),
        validation_plan=(
            ValidationCommand(
                check_id="lwc-jest",
                command_id="salesforce-lwc-jest",
                purpose="Run LWC Jest tests.",
            ),
        ),
    )
    request_path = tmp_path / "request.json"
    manifest_path = tmp_path / "manifest.json"
    request_path.write_text(request.model_dump_json(), encoding="utf-8")
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")

    result = main(
        [
            "validate-manifest",
            "--request",
            str(request_path),
            "--manifest",
            str(manifest_path),
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "valid": True,
        "request_id": request.request_id,
        "manifest_id": manifest.manifest_id,
    }


def test_wiki_search_returns_a_trace(capsys) -> None:
    result = main(
        [
            "wiki-search",
            "--wiki-root",
            str(PROJECT_ROOT / "knowledge/wiki"),
            "--query",
            "Visualforce rerender LWC",
            "--platform",
            "salesforce",
            "--source-version",
            "Salesforce API 67.0",
            "--target-version",
            "Salesforce API 67.0",
            "--max-primary-hits",
            "1",
            "--as-of",
            "2026-08-27",
        ]
    )

    assert result == 0
    trace = json.loads(capsys.readouterr().out)
    assert trace["hits"][0]["page_id"] == "salesforce-visualforce-to-lwc"
    assert trace["platform"] == "salesforce"


def test_agents_check_exposes_exact_versioned_roles(capsys) -> None:
    result = main(["agents-check", "--project-root", str(PROJECT_ROOT)])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["agent_count"] == 3
    assert [agent["role"] for agent in payload["agents"]] == [
        "architect",
        "engineer",
        "validator",
    ]
    assert all(agent["definition_digest"].startswith("sha256:") for agent in payload["agents"])
    assert payload["agents"][1]["permissions"]["isolated_workspace_write"] is True
    assert payload["agents"][2]["permissions"]["command_execution"] is False


def test_agent_request_create_binds_the_local_source_without_external_action(
    tmp_path: Path,
    capsys,
) -> None:
    scenario = migration_scenario(Platform.SALESFORCE)
    source = tmp_path / scenario.source_root
    shutil.copytree(
        PROJECT_ROOT / "fixtures/salesforce/account-contact-explorer/input",
        source,
    )

    result = main(
        [
            "agent-request-create",
            "--project-root",
            str(tmp_path),
            "--request-id",
            "cli-salesforce-request",
            "--scenario-id",
            scenario.scenario_id,
            "--requested-at",
            "2026-08-25T12:00:00+00:00",
            "--output",
            "request.json",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["external_action_performed"] is False
    assert payload["scenario_id"] == scenario.scenario_id
    assert payload["platform"] == "salesforce"
    request = json.loads((tmp_path / "request.json").read_text(encoding="utf-8"))
    assert request["base_revision"].startswith("sha256:")
    assert request["target"]["target_runtime"] == "Lightning Web Components with Apex"


def test_launch_cli_exposes_scenario_identity_not_free_form_authority() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )

    request_destinations = {
        action.dest for action in subparsers.choices["agent-request-create"]._actions
    }
    start_destinations = {action.dest for action in subparsers.choices["agent-run-start"]._actions}
    assert "scenario_id" in request_destinations
    assert "scenario_id" in start_destinations
    assert not {"platform", "source_root", "description"} & request_destinations
    assert not {"platform", "source_root", "description", "as_of"} & start_destinations


def test_agent_request_create_rejects_unknown_scenario_before_writing_output(
    tmp_path: Path,
    capsys,
) -> None:
    before = snapshot_tree(tmp_path)
    output = tmp_path / "request.json"

    result = main(
        [
            "agent-request-create",
            "--project-root",
            str(tmp_path),
            "--request-id",
            "cli-unknown-scenario",
            "--scenario-id",
            "salesforce-rust-service",
            "--requested-at",
            "2026-08-25T12:00:00+00:00",
            "--output",
            "request.json",
        ]
    )

    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "agent_request_invalid"
    assert not output.exists()
    assert snapshot_tree(tmp_path) == before


def test_run_and_final_review_parsers_require_identity_human_and_live_gates() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["final-review-request", "--run-dir", ".runs/example"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "agent-run-start",
                "--run-dir",
                ".runs/example",
                "--run-id",
                "run-1",
                "--thread-id",
                "thread-1",
                "--scenario-id",
                "salesforce-vf-to-lwc",
                "--request",
                "request.json",
                "--model-id",
                "approved-model",
                "--api-key-env",
                "MODEL_API_KEY",
                "--approved-by",
                "reviewer-1",
            ]
        )

    parsed = parser.parse_args(
        [
            "agent-run-start",
            "--run-dir",
            ".runs/example",
            "--run-id",
            "run-1",
            "--thread-id",
            "thread-1",
            "--scenario-id",
            "salesforce-vf-to-lwc",
            "--request",
            "request.json",
            "--model-id",
            "approved-model",
            "--api-key-env",
            "MODEL_API_KEY",
            "--approved-by",
            "reviewer-1",
            "--allow-live-api",
            "--allow-prompt-data-sharing",
        ]
    )
    assert parsed.command == "agent-run-start"


def test_ui_parser_keeps_the_server_on_a_bounded_local_port() -> None:
    parser = build_parser()

    parsed = parser.parse_args(
        [
            "ui",
            "--port",
            "8765",
            "--open-browser",
            "--ollama-model",
            "qwen3.8:latest",
            "--ollama-timeout-seconds",
            "600",
        ]
    )

    assert parsed.command == "ui"
    assert parsed.port == 8765
    assert parsed.open_browser is True
    assert parsed.ollama_model == "qwen3.8:latest"
    assert parsed.ollama_timeout_seconds == 600.0
    with pytest.raises(SystemExit):
        parser.parse_args(["ui"])
    with pytest.raises(SystemExit):
        parser.parse_args(["ui", "--port", "80"])
    for timeout in ("0", "601", "nan", "inf", "not-a-number"):
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "ui",
                    "--ollama-model",
                    "qwen3.8:latest",
                    "--ollama-timeout-seconds",
                    timeout,
                ]
            )


def test_vscode_launch_profile_opens_only_the_integrated_browser() -> None:
    launch = json.loads((PROJECT_ROOT / ".vscode/launch.json").read_text(encoding="utf-8"))

    assert launch["version"] == "0.2.0"
    assert len(launch["configurations"]) == 1
    profile = launch["configurations"][0]
    assert profile["name"] == "Agent UI: VS Code Integrated Browser"
    assert profile["type"] == "node-terminal"
    assert profile["request"] == "launch"
    assert profile["cwd"] == "${workspaceFolder}"
    assert profile["command"] == (
        "uv run --frozen legacy-migration-agent ui --project-root . "
        "--ollama-model qwen3.8:latest --ollama-timeout-seconds 600"
    )
    assert "--open-browser" not in profile["command"]
    assert profile["serverReadyAction"] == {
        "pattern": r"Agent UI available at (http://127\.0\.0\.1:\d+/)",
        "uriFormat": "%s",
        "action": "openIntegratedBrowser",
    }


def test_ui_command_forwards_only_the_bounded_server_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from legacy_migration_agent.ui import server as server_module

    calls: list[tuple[Path, int, bool, str, float]] = []

    def fake_serve_ui(
        project_root: Path,
        *,
        port: int,
        open_browser: bool,
        ollama_model_id: str,
        ollama_timeout_seconds: float,
    ) -> None:
        calls.append(
            (
                project_root,
                port,
                open_browser,
                ollama_model_id,
                ollama_timeout_seconds,
            )
        )

    monkeypatch.setattr(server_module, "serve_ui", fake_serve_ui)

    result = main(
        [
            "ui",
            "--project-root",
            str(tmp_path),
            "--port",
            "9123",
            "--open-browser",
            "--ollama-model",
            "qwen3.8:latest",
            "--ollama-timeout-seconds",
            "600",
        ]
    )

    assert result == 0
    assert calls == [(tmp_path, 9123, True, "qwen3.8:latest", 600.0)]


def test_ui_command_does_not_import_dormant_cli_capabilities() -> None:
    script = textwrap.dedent(
        """
        import json
        import sys

        from legacy_migration_agent.cli import main

        command_specific_modules = (
            "legacy_migration_agent.agent_runtime.agent_definitions",
            "legacy_migration_agent.agent_runtime.correction",
            "legacy_migration_agent.application.agent_run",
            "legacy_migration_agent.application.final_review",
            "legacy_migration_agent.evaluation",
            "legacy_migration_agent.graphs.graph_evaluation",
            "legacy_migration_agent.knowledge.wiki",
            "legacy_migration_agent.schema_compatibility",
            "legacy_migration_agent.workflow",
        )
        loaded_by_cli_import = [
            module for module in command_specific_modules if module in sys.modules
        ]
        from legacy_migration_agent.ui import server

        calls = []
        server.serve_ui = lambda *args, **kwargs: calls.append((args, kwargs))
        result = main(["ui", "--ollama-model", "qwen3.8:latest"])
        dormant_modules = (
            "legacy_migration_agent.evaluation",
            "legacy_migration_agent.graphs.graph_evaluation",
            "legacy_migration_agent.schema_compatibility",
        )
        print(
            json.dumps(
                {
                    "result": result,
                    "server_calls": len(calls),
                    "loaded_by_cli_import": loaded_by_cli_import,
                    "loaded_dormant_modules": [
                        module for module in dormant_modules if module in sys.modules
                    ],
                }
            )
        )
        """
    )

    completed = subprocess.run(
        (sys.executable, "-c", script),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "result": 0,
        "server_calls": 1,
        "loaded_by_cli_import": [],
        "loaded_dormant_modules": [],
    }


def test_reject_resume_refuses_live_provider_arguments_before_running(
    tmp_path: Path,
    capsys,
) -> None:
    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            {
                "decision_id": "decision-1",
                "request_id": "request-1",
                "manifest_id": "manifest-1",
                "manifest_digest": f"sha256:{'0' * 64}",
                "requested_action": "approve_manifest",
                "selection": "reject",
                "reviewer": "reviewer-1",
                "comment": "Stop this run.",
            }
        ),
        encoding="utf-8",
    )

    result = main(
        [
            "agent-run-resume",
            "--project-root",
            str(tmp_path),
            "--run-dir",
            ".runs/example",
            "--run-id",
            "run-1",
            "--thread-id",
            "thread-1",
            "--approval",
            str(approval),
            "--model-id",
            "model-that-must-not-run",
            "--api-key-env",
            "MODEL_API_KEY",
            "--approved-by",
            "reviewer-1",
            "--allow-live-api",
            "--allow-prompt-data-sharing",
        ]
    )

    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["terminal_disposition"] == "controlled_cli_error"
    assert payload["error"] == {
        "category": "configuration",
        "code": "agent_run_configuration",
        "retry_eligible": False,
        "terminal": True,
    }
    assert "model-that-must-not-run" not in json.dumps(payload)


def test_graph_evaluate_reports_exact_fixture_metrics_without_overclaim(
    tmp_path: Path,
    capsys,
) -> None:
    source_root = PROJECT_ROOT / "fixtures/salesforce/account-contact-explorer/input"
    graph = build_salesforce_dependency_graph(
        source_root,
        ("force-app/main/default/pages/LegacyAccountContactExplorer.page",),
        content_revision(source_root),
    )
    graph_path = tmp_path / "dependency-graph.json"
    graph_path.write_text(graph.model_dump_json(indent=2), encoding="utf-8")
    result = main(
        [
            "graph-evaluate",
            "--graph",
            str(graph_path),
            "--labels",
            str(PROJECT_ROOT / "evaluation/salesforce-account-contact-explorer-source-edges.json"),
            "--platform",
            "salesforce",
        ]
    )

    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["metrics"]["recall"] == 1
    assert report["metrics"]["precision"] == 1
    assert report["claim_scope"] == "exploratory_unreviewed"
    assert report["exit_gate_eligible"] is False


def test_controlled_command_errors_are_sanitized(capsys, tmp_path: Path) -> None:
    missing = tmp_path / "not-present.json"

    result = main(
        [
            "graph-evaluate",
            "--graph",
            str(missing),
            "--labels",
            str(missing),
            "--platform",
            "mulesoft",
        ]
    )

    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "graph_evaluation_invalid"
    assert payload["error"]["terminal"] is True
    assert "not-present" not in json.dumps(payload)


def test_evaluation_verify_accepts_the_complete_unmeasured_design(capsys) -> None:
    result = main(
        [
            "evaluation-verify",
            "--registry",
            str(PROJECT_ROOT / "evaluation/benchmark-v1/registry.json"),
            "--results",
            str(PROJECT_ROOT / "evaluation/results.json"),
        ]
    )

    assert result == 0
    verification = json.loads(capsys.readouterr().out)
    assert verification == {
        "case_count": 6,
        "cell_count": 72,
        "configuration_count": 4,
        "measured_cell_count": 0,
        "registry_id": "legacy-migration-benchmark-v1",
        "repetitions": 3,
        "verified": True,
    }


def test_evaluation_verify_fails_closed_with_sanitized_output(
    capsys,
    tmp_path: Path,
) -> None:
    result = main(
        [
            "evaluation-verify",
            "--registry",
            str(PROJECT_ROOT / "evaluation/benchmark-v1/registry.json"),
            "--results",
            str(tmp_path / "missing-results.json"),
        ]
    )

    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "evaluation_invalid"
    assert payload["error"]["retry_eligible"] is False
    assert "missing-results" not in json.dumps(payload)


def test_evaluation_pilot_run_local_initializes_unperformed_qwen_cells(
    capsys,
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "pilot-snapshot"
    result = main(
        [
            "evaluation-pilot-run-local",
            "--project-root",
            str(PROJECT_ROOT),
            "--registry",
            str(PROJECT_ROOT / "evaluation/pilot-v1/registry.json"),
            "--output-dir",
            str(snapshot),
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["measured_cells"] == 0
    assert payload["summary"]["status_counts"] == {
        "decision_required": 0,
        "failed": 0,
        "not_performed": 2,
        "succeeded": 0,
        "unavailable": 0,
    }
    assert payload["summary"]["model_quality_evaluated"] is False
    assert payload["summary"]["external_platform_evaluated"] is False
    assert payload["verification"]["agent_run_receipts_verified"] == 0
    assert (snapshot / "results.json").is_file()
    assert not tuple((snapshot / "evidence").glob("*.json"))


def test_evaluation_pilot_verify_accepts_checked_in_partial_snapshot(capsys) -> None:
    result = main(
        [
            "evaluation-pilot-verify",
            "--project-root",
            str(PROJECT_ROOT),
            "--registry",
            str(PROJECT_ROOT / "evaluation/pilot-v1/registry.json"),
            "--snapshot-dir",
            str(PROJECT_ROOT / "evaluation/pilot-v1"),
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is True
    assert payload["measured_cells"] == 0
    assert payload["not_performed_cells"] == 2
    assert payload["complete"] is False


def test_evaluation_pilot_verify_fails_closed_without_leaking_path(
    capsys,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "secret-named-missing-snapshot"
    result = main(
        [
            "evaluation-pilot-verify",
            "--project-root",
            str(PROJECT_ROOT),
            "--registry",
            str(PROJECT_ROOT / "evaluation/pilot-v1/registry.json"),
            "--snapshot-dir",
            str(missing),
        ]
    )

    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "evaluation_invalid"
    assert "secret-named-missing-snapshot" not in json.dumps(payload)
