"""Command execution and shared safety boundaries for the local CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING

from legacy_migration_agent.contracts import Platform

if TYPE_CHECKING:
    from legacy_migration_agent.application.agent_run import (
        AgentRunModelClients,
        AgentRunStatus,
    )

_MAX_CLI_JSON_BYTES = 32_000_000


def dispatch_uncontrolled_command(args: argparse.Namespace) -> int:
    if args.command == "validate-manifest":
        from legacy_migration_agent.contracts import MigrationManifest, MigrationRequest
        from legacy_migration_agent.core.policies import validate_manifest_for_request

        request = MigrationRequest.model_validate(_read_json(args.request))
        manifest = MigrationManifest.model_validate(_read_json(args.manifest))
        validate_manifest_for_request(manifest, request)
        print(
            json.dumps(
                {
                    "valid": True,
                    "request_id": request.request_id,
                    "manifest_id": manifest.manifest_id,
                }
            )
        )
        return 0
    if args.command == "export-schemas":
        from legacy_migration_agent.schema_compatibility import write_schema_snapshots

        exported = [path.name for path in write_schema_snapshots(args.output_dir)]
        print(json.dumps({"exported": exported, "output_dir": str(args.output_dir)}))
        return 0
    if args.command == "wiki-search":
        from legacy_migration_agent.knowledge.wiki import LlmWiki

        trace = LlmWiki.load(args.wiki_root).search(
            args.query,
            platform=Platform(args.platform) if args.platform else None,
            source_version=args.source_version,
            target_version=args.target_version,
            max_primary_hits=args.max_primary_hits,
            expand_links=not args.no_expand_links,
            as_of=args.as_of,
            max_age_days=args.max_age_days,
        )
        print(json.dumps(trace.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0
    if args.command == "agents-check":
        from legacy_migration_agent.agent_runtime.agent_definitions import load_agent_registry

        registry = load_agent_registry(args.project_root / "agents")
        print(
            json.dumps(
                {
                    "valid": True,
                    "agent_count": len(registry.definitions),
                    "agents": [
                        {
                            "role": definition.role.value,
                            "version": definition.version,
                            "definition": f"agents/{definition.relative_path}",
                            "definition_digest": definition.definition_digest,
                            "input_contracts": definition.header.input_contracts,
                            "output_contract": definition.header.output_contract,
                            "permissions": definition.header.permissions.model_dump(mode="json"),
                        }
                        for definition in registry.definitions
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "ui":
        from legacy_migration_agent.ui.server import serve_ui

        serve_ui(
            args.project_root,
            port=args.port,
            open_browser=args.open_browser,
            ollama_model_id=args.ollama_model,
            ollama_timeout_seconds=args.ollama_timeout_seconds,
        )
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


def run_controlled_command_safely(args: argparse.Namespace) -> int:
    try:
        return _dispatch_controlled_command(args)
    except Exception as error:
        model_configuration_error = False
        if args.command.startswith("agent-run"):
            from legacy_migration_agent.agent_runtime.openai_model import (
                ModelConfigurationError,
            )

            model_configuration_error = isinstance(error, ModelConfigurationError)
        policy_violation = False
        if args.command != "evaluation-verify":
            from legacy_migration_agent.core.policies import PolicyViolation

            policy_violation = isinstance(error, PolicyViolation)
        if model_configuration_error:
            category = "configuration"
        elif policy_violation or isinstance(error, (ValueError, OSError)):
            category = "invalid"
        else:
            category = "internal"
    namespace = _controlled_error_namespace(args.command)
    print(
        json.dumps(
            {
                "status": "failed",
                "terminal_disposition": "controlled_cli_error",
                "error": {
                    "code": f"{namespace}_{category}",
                    "category": category,
                    "terminal": True,
                    "retry_eligible": False,
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 2


def _dispatch_controlled_command(args: argparse.Namespace) -> int:
    if args.command == "agent-request-create":
        from legacy_migration_agent.application.agent_run import prepare_agent_run_request
        from legacy_migration_agent.application.migration_scenarios import (
            migration_launch_contract,
        )
        from legacy_migration_agent.core.integrity import ArtifactStore

        try:
            launch_contract = migration_launch_contract(args.scenario_id)
        except KeyError:
            raise ValueError("CLI scenario identity is not supported") from None
        request = prepare_agent_run_request(
            args.project_root,
            request_id=args.request_id,
            launch_contract=launch_contract,
            requested_at=args.requested_at,
        )
        destination = ArtifactStore(args.project_root).write_json(args.output, request)
        print(
            json.dumps(
                {
                    "request_id": request.request_id,
                    "scenario_id": launch_contract.scenario_id,
                    "platform": request.platform.value,
                    "base_revision": request.base_revision,
                    "output": str(destination),
                    "external_action_performed": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "agent-run-start":
        from legacy_migration_agent.application.agent_run import start_agent_run
        from legacy_migration_agent.application.migration_scenarios import (
            migration_launch_contract,
        )
        from legacy_migration_agent.contracts import MigrationRequest

        canonical_request = MigrationRequest.model_validate(_read_json(args.request))
        try:
            launch_contract = migration_launch_contract(args.scenario_id)
        except KeyError:
            raise ValueError("CLI scenario identity is not supported") from None
        models = _live_models_from_args(args, required=True)
        assert models is not None
        result = start_agent_run(
            args.project_root,
            args.run_dir,
            run_id=args.run_id,
            thread_id=args.thread_id,
            launch_contract=launch_contract,
            request=canonical_request,
            models=models,
        )
        return _emit_agent_status(result)
    if args.command == "agent-run-resume":
        from legacy_migration_agent.application.agent_run import resume_agent_run
        from legacy_migration_agent.contracts import MigrationRequest
        from legacy_migration_agent.workflow import ManifestApproval

        manifest_approval = ManifestApproval.model_validate(_read_json(args.approval))
        if manifest_approval.selection == "approve":
            models = _live_models_from_args(args, required=True)
        else:
            _reject_live_arguments(args)
            models = None
        optional_request = (
            MigrationRequest.model_validate(_read_json(args.request))
            if args.request is not None
            else None
        )
        result = resume_agent_run(
            args.project_root,
            args.run_dir,
            run_id=args.run_id,
            thread_id=args.thread_id,
            approval=manifest_approval,
            models=models,
            request=optional_request,
        )
        return _emit_agent_status(result)
    if args.command == "agent-run-retry":
        from legacy_migration_agent.agent_runtime.correction import CorrectionApproval
        from legacy_migration_agent.application.agent_run import retry_agent_run
        from legacy_migration_agent.contracts import MigrationRequest

        correction_approval = CorrectionApproval.model_validate(_read_json(args.approval))
        models = _live_models_from_args(args, required=True)
        assert models is not None
        optional_request = (
            MigrationRequest.model_validate(_read_json(args.request))
            if args.request is not None
            else None
        )
        result = retry_agent_run(
            args.project_root,
            args.run_dir,
            run_id=args.run_id,
            thread_id=args.thread_id,
            approval=correction_approval,
            models=models,
            request=optional_request,
        )
        return _emit_agent_status(result)
    if args.command == "agent-run-status":
        from legacy_migration_agent.application.agent_run import get_agent_run_status
        from legacy_migration_agent.contracts import MigrationRequest

        optional_request = (
            MigrationRequest.model_validate(_read_json(args.request))
            if args.request is not None
            else None
        )
        result = get_agent_run_status(
            args.project_root,
            args.run_dir,
            run_id=args.run_id,
            thread_id=args.thread_id,
            request=optional_request,
        )
        return _emit_agent_status(result)
    if args.command == "agent-manifest-decision-create":
        from legacy_migration_agent.application.agent_run import get_agent_run_status
        from legacy_migration_agent.core.integrity import ArtifactStore
        from legacy_migration_agent.core.policies import PolicyViolation
        from legacy_migration_agent.workflow import ManifestApproval

        status = get_agent_run_status(
            args.project_root,
            args.run_dir,
            run_id=args.run_id,
            thread_id=args.thread_id,
        )
        interrupt = status.interrupt
        if interrupt is None or args.selection not in interrupt.options:
            raise PolicyViolation("run has no matching pending manifest decision")
        decision = ManifestApproval(
            decision_id=interrupt.decision_id,
            request_id=interrupt.request_id,
            manifest_id=interrupt.manifest_id,
            manifest_digest=interrupt.manifest_digest,
            requested_action=interrupt.requested_action,
            selection=args.selection,
            reviewer=args.reviewer,
            comment=args.comment,
        )
        destination = ArtifactStore(args.project_root).write_json(args.output, decision)
        print(
            json.dumps(
                {
                    "decision_id": decision.decision_id,
                    "selection": decision.selection,
                    "reviewer": decision.reviewer,
                    "output": str(destination),
                    "run_resumed": False,
                    "external_action_performed": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "agent-correction-approval-create":
        from legacy_migration_agent.agent_runtime.correction import (
            CorrectionAction,
            CorrectionApproval,
        )
        from legacy_migration_agent.application.agent_run import get_agent_run_status
        from legacy_migration_agent.core.integrity import ArtifactStore
        from legacy_migration_agent.core.policies import PolicyViolation

        status = get_agent_run_status(
            args.project_root,
            args.run_dir,
            run_id=args.run_id,
            thread_id=args.thread_id,
        )
        correction = status.correction
        if (
            correction is None
            or correction.action is not CorrectionAction.RETRY_IMPLEMENTATION
            or correction.authorized_attempt is None
        ):
            raise PolicyViolation("run has no exact bounded correction approval to offer")
        approval = CorrectionApproval(
            correction_id=correction.correction_id,
            request_id=correction.request_id,
            manifest_id=correction.manifest_id,
            manifest_digest=correction.manifest_digest,
            report_id=correction.report_id,
            report_digest=correction.report_digest,
            change_set_digest=correction.change_set_digest,
            base_revision=correction.base_revision,
            completed_attempt=correction.completed_attempt,
            authorized_attempt=correction.authorized_attempt,
            action=correction.action,
            reviewer=args.reviewer,
            comment=args.comment,
        )
        destination = ArtifactStore(args.project_root).write_json(args.output, approval)
        print(
            json.dumps(
                {
                    "correction_id": approval.correction_id,
                    "authorized_attempt": approval.authorized_attempt,
                    "reviewer": approval.reviewer,
                    "output": str(destination),
                    "retry_started": False,
                    "external_action_performed": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "final-review-request":
        from legacy_migration_agent.application.final_review import (
            request_final_review_for_run,
        )

        review_request = request_final_review_for_run(
            args.project_root,
            args.run_dir,
            run_id=args.run_id,
            thread_id=args.thread_id,
            requester=args.requester,
            designated_reviewer=args.reviewer,
            requested_at=args.requested_at,
            expires_at=args.expires_at,
        )
        print(json.dumps(review_request.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0
    if args.command == "final-review-decide":
        from legacy_migration_agent.application.final_review import decide_final_review_for_run

        review_record = decide_final_review_for_run(
            args.project_root,
            args.run_dir,
            run_id=args.run_id,
            thread_id=args.thread_id,
            reviewer=args.reviewer,
            selection=args.selection,
            decided_at=args.decided_at,
            comment=args.comment,
        )
        print(json.dumps(review_record.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0
    if args.command == "final-review-status":
        from legacy_migration_agent.application.final_review import (
            get_final_review_status_for_run,
        )

        review_status = get_final_review_status_for_run(
            args.project_root,
            args.run_dir,
            run_id=args.run_id,
            thread_id=args.thread_id,
        )
        print(json.dumps(review_status.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0
    if args.command == "graph-evaluate":
        from legacy_migration_agent.core.policies import PolicyViolation
        from legacy_migration_agent.graphs.graph_contracts import DependencyGraph
        from legacy_migration_agent.graphs.graph_evaluation import (
            evaluate_dependency_graph,
            load_graph_label_set,
        )

        platform = Platform(args.platform)
        graph = DependencyGraph.model_validate(_read_json(args.graph))
        labels = load_graph_label_set(args.labels, platform=platform)
        if graph.platform is not platform:
            raise PolicyViolation("dependency graph platform differs from selected stratum")
        report = evaluate_dependency_graph(graph, labels)
        print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0 if report.exit_gate_eligible else 1
    if args.command == "evaluation-verify":
        from legacy_migration_agent.evaluation import load_and_verify

        verification = load_and_verify(args.registry, args.results)
        print(json.dumps(verification.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0 if verification.verified else 1
    if args.command == "evaluation-pilot-run-local":
        from legacy_migration_agent.evaluation import (
            load_and_verify_pilot,
            write_local_pilot_snapshot,
        )

        results = write_local_pilot_snapshot(
            args.project_root,
            args.registry,
            args.output_dir,
        )
        pilot_verification = load_and_verify_pilot(
            args.project_root,
            args.registry,
            args.output_dir,
        )
        print(
            json.dumps(
                {
                    "results_id": results.results_id,
                    "summary": results.summary.model_dump(mode="json"),
                    "verification": pilot_verification.model_dump(mode="json"),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "evaluation-pilot-verify":
        from legacy_migration_agent.evaluation import load_and_verify_pilot

        pilot_verification = load_and_verify_pilot(
            args.project_root,
            args.registry,
            args.snapshot_dir,
        )
        print(json.dumps(pilot_verification.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0
    if args.command == "evaluation-pilot-ingest-agent-run":
        from legacy_migration_agent.evaluation import (
            load_and_verify_pilot,
            write_agent_run_pilot_snapshot,
        )

        results = write_agent_run_pilot_snapshot(
            args.project_root,
            args.registry,
            args.baseline_snapshot_dir,
            args.output_dir,
            results_id=args.results_id,
            case_id=args.case_id,
            run_dir=args.run_dir,
            run_id=args.run_id,
            thread_id=args.thread_id,
        )
        pilot_verification = load_and_verify_pilot(
            args.project_root,
            args.registry,
            args.output_dir,
        )
        print(
            json.dumps(
                {
                    "results_id": results.results_id,
                    "summary": results.summary.model_dump(mode="json"),
                    "verification": pilot_verification.model_dump(mode="json"),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    raise AssertionError("unhandled controlled command")


def _read_json(path: Path) -> object:
    if path.is_symlink():
        raise ValueError("CLI JSON input cannot be a symbolic link")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise ValueError("CLI JSON input is unavailable") from exc
    if not resolved.is_file() or metadata.st_size < 1 or metadata.st_size > _MAX_CLI_JSON_BYTES:
        raise ValueError("CLI JSON input must be a nonempty bounded regular file")
    try:
        return json.loads(
            resolved.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("CLI JSON input must be valid UTF-8 JSON") from exc


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"CLI JSON input contains a duplicate key: {key}")
        value[key] = item
    return value


def _controlled_error_namespace(command: str) -> str:
    if command.startswith("agent-run"):
        return "agent_run"
    if command.startswith("agent-request"):
        return "agent_request"
    if command.startswith("agent-manifest"):
        return "agent_decision"
    if command.startswith("agent-correction"):
        return "agent_correction"
    if command.startswith("final-review"):
        return "final_review"
    if command == "graph-evaluate":
        return "graph_evaluation"
    if command.startswith("evaluation-"):
        return "evaluation"
    raise AssertionError(f"unknown controlled command: {command}")


def _emit_agent_status(status: AgentRunStatus) -> int:
    print(json.dumps(status.model_dump(mode="json"), indent=2, sort_keys=True))
    return 2 if status.failure is not None else 0


def _live_models_from_args(
    args: argparse.Namespace,
    *,
    required: bool,
) -> AgentRunModelClients | None:
    from legacy_migration_agent.agent_runtime.openai_model import (
        LiveModelApproval,
        ModelConfigurationError,
    )
    from legacy_migration_agent.application.agent_run import build_live_openai_model_clients

    values = (args.model_id, args.api_key_env, args.approved_by)
    gates = (args.allow_live_api, args.allow_prompt_data_sharing)
    if not any(value is not None for value in values) and not any(gates):
        if required:
            raise ModelConfigurationError("live model configuration is required")
        return None
    if not all(value is not None and str(value).strip() for value in values) or not all(gates):
        raise ModelConfigurationError(
            "live use requires model ID, named API-key environment, both gates, and approver"
        )
    approval = LiveModelApproval(
        allow_live_api=True,
        allow_prompt_data_sharing=True,
        approved_by=args.approved_by,
    )
    return build_live_openai_model_clients(
        model_id=args.model_id,
        api_key_environment=args.api_key_env,
        approval=approval,
    )


def _reject_live_arguments(args: argparse.Namespace) -> None:
    from legacy_migration_agent.agent_runtime.openai_model import ModelConfigurationError

    values = (args.model_id, args.api_key_env, args.approved_by)
    gates = (args.allow_live_api, args.allow_prompt_data_sharing)
    if any(value is not None for value in values) or any(gates):
        raise ModelConfigurationError(
            "reject or modify decisions cannot accept live-provider arguments"
        )
