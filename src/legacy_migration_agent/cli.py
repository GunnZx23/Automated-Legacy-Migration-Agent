"""Command-line interface for the capstone's local, human-gated workflow."""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from legacy_migration_agent.agent_runtime.ollama_model import (
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    MAX_OLLAMA_TIMEOUT_SECONDS,
    MIN_OLLAMA_TIMEOUT_SECONDS,
)
from legacy_migration_agent.contracts import Platform

if TYPE_CHECKING:
    from legacy_migration_agent.application.agent_run import (
        AgentRunModelClients,
        AgentRunStatus,
    )

CONTROLLED_RUN_COMMANDS = frozenset(
    {
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
    }
)
_MAX_CLI_JSON_BYTES = 32_000_000


def build_parser() -> argparse.ArgumentParser:
    """Build the intentionally small capstone command surface."""

    parser = argparse.ArgumentParser(prog="legacy-migration-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate-manifest", help="validate typed request and manifest JSON"
    )
    validate.add_argument("--request", type=Path, required=True)
    validate.add_argument("--manifest", type=Path, required=True)

    schemas = subparsers.add_parser(
        "export-schemas",
        help="export the versioned artifact JSON Schemas",
    )
    schemas.add_argument("--output-dir", type=Path, required=True)

    wiki = subparsers.add_parser(
        "wiki-search",
        help="run deterministic navigation over the curated LLM Wiki",
    )
    wiki.add_argument("--wiki-root", type=Path, required=True)
    wiki.add_argument("--query", required=True)
    wiki.add_argument("--platform", choices=[platform.value for platform in Platform])
    wiki.add_argument("--source-version")
    wiki.add_argument("--target-version")
    wiki.add_argument("--max-primary-hits", type=int, default=3)
    wiki.add_argument("--as-of", type=date.fromisoformat)
    wiki.add_argument("--max-age-days", type=int, default=365)
    wiki.add_argument("--no-expand-links", action="store_true")

    agents_check = subparsers.add_parser(
        "agents-check",
        help="validate and describe the exact three versioned agent definitions",
    )
    agents_check.add_argument("--project-root", type=Path, default=Path("."))

    agent_request = subparsers.add_parser(
        "agent-request-create",
        help="bind a platform preset request to the current local source bytes",
    )
    agent_request.add_argument("--project-root", type=Path, default=Path("."))
    agent_request.add_argument("--request-id", required=True)
    agent_request.add_argument("--scenario-id", required=True)
    agent_request.add_argument("--requested-at", type=datetime.fromisoformat, required=True)
    agent_request.add_argument("--output", required=True)

    agent_start = subparsers.add_parser(
        "agent-run-start",
        help="start a supported three-agent Salesforce or MuleSoft run",
    )
    _add_agent_run_identity_arguments(agent_start)
    agent_start.add_argument("--scenario-id", required=True)
    agent_start.add_argument("--request", type=Path, required=True)
    _add_live_model_arguments(agent_start, required=True)

    agent_resume = subparsers.add_parser(
        "agent-run-resume",
        help="resume an exact pending manifest approval",
    )
    _add_agent_run_identity_arguments(agent_resume)
    agent_resume.add_argument("--approval", type=Path, required=True)
    agent_resume.add_argument("--request", type=Path)
    _add_live_model_arguments(agent_resume, required=False)

    agent_retry = subparsers.add_parser(
        "agent-run-retry",
        help="authorize the exact existing bounded correction attempt",
    )
    _add_agent_run_identity_arguments(agent_retry)
    agent_retry.add_argument("--approval", type=Path, required=True)
    agent_retry.add_argument("--request", type=Path)
    _add_live_model_arguments(agent_retry, required=True)

    agent_status = subparsers.add_parser(
        "agent-run-status",
        help="read exact-thread run state without invoking a model",
    )
    _add_agent_run_identity_arguments(agent_status)
    agent_status.add_argument("--request", type=Path)

    manifest_decision = subparsers.add_parser(
        "agent-manifest-decision-create",
        help="bind a named human decision to the exact pending manifest interrupt",
    )
    _add_agent_run_identity_arguments(manifest_decision)
    manifest_decision.add_argument(
        "--selection",
        choices=("approve", "reject", "modify"),
        required=True,
    )
    manifest_decision.add_argument("--reviewer", required=True)
    manifest_decision.add_argument("--comment", default="")
    manifest_decision.add_argument("--output", required=True)

    correction_approval = subparsers.add_parser(
        "agent-correction-approval-create",
        help="bind a named human approval to the exact offered second attempt",
    )
    _add_agent_run_identity_arguments(correction_approval)
    correction_approval.add_argument("--reviewer", required=True)
    correction_approval.add_argument("--comment", default="")
    correction_approval.add_argument("--output", required=True)

    final_request = subparsers.add_parser(
        "final-review-request",
        help="request independent review of one exact completed agent run",
    )
    _add_agent_run_identity_arguments(final_request)
    final_request.add_argument("--requester", required=True)
    final_request.add_argument("--reviewer", required=True)
    final_request.add_argument("--requested-at", type=datetime.fromisoformat, required=True)
    final_request.add_argument("--expires-at", type=datetime.fromisoformat, required=True)

    final_decide = subparsers.add_parser(
        "final-review-decide",
        help="consume one exact final-review request without granting external authority",
    )
    _add_agent_run_identity_arguments(final_decide)
    final_decide.add_argument("--reviewer", required=True)
    final_decide.add_argument(
        "--selection",
        choices=("accept", "reject", "request_changes"),
        required=True,
    )
    final_decide.add_argument("--decided-at", type=datetime.fromisoformat, required=True)
    final_decide.add_argument("--comment", default="")

    final_status = subparsers.add_parser(
        "final-review-status",
        help="inspect the provider-free final-review checkpoint",
    )
    _add_agent_run_identity_arguments(final_status)

    graph_evaluate = subparsers.add_parser(
        "graph-evaluate",
        help="evaluate one revision-bound dependency graph against bounded labels",
    )
    graph_evaluate.add_argument("--graph", type=Path, required=True)
    graph_evaluate.add_argument("--labels", type=Path, required=True)
    graph_evaluate.add_argument(
        "--platform",
        choices=[platform.value for platform in Platform],
        required=True,
    )

    evaluation_verify = subparsers.add_parser(
        "evaluation-verify",
        help="verify the compact, predeclared evaluation registry and current results",
    )
    evaluation_verify.add_argument("--registry", type=Path, required=True)
    evaluation_verify.add_argument("--results", type=Path, required=True)

    pilot_run = subparsers.add_parser(
        "evaluation-pilot-run-local",
        help="write the unmeasured two-cell Qwen pilot baseline without invoking a provider",
    )
    pilot_run.add_argument("--project-root", type=Path, default=Path("."))
    pilot_run.add_argument("--registry", type=Path, required=True)
    pilot_run.add_argument("--output-dir", type=Path, required=True)

    pilot_verify = subparsers.add_parser(
        "evaluation-pilot-verify",
        help="verify agent-run receipts and artifact bindings in one pilot snapshot",
    )
    pilot_verify.add_argument("--project-root", type=Path, default=Path("."))
    pilot_verify.add_argument("--registry", type=Path, required=True)
    pilot_verify.add_argument("--snapshot-dir", type=Path, required=True)

    pilot_ingest = subparsers.add_parser(
        "evaluation-pilot-ingest-agent-run",
        help="ingest an existing terminal Qwen run without invoking a provider",
    )
    pilot_ingest.add_argument("--project-root", type=Path, default=Path("."))
    pilot_ingest.add_argument("--registry", type=Path, required=True)
    pilot_ingest.add_argument("--baseline-snapshot-dir", type=Path, required=True)
    pilot_ingest.add_argument("--output-dir", type=Path, required=True)
    pilot_ingest.add_argument("--results-id", required=True)
    pilot_ingest.add_argument("--case-id", required=True)
    pilot_ingest.add_argument("--run-dir", type=Path, required=True)
    pilot_ingest.add_argument("--run-id", required=True)
    pilot_ingest.add_argument("--thread-id", required=True)

    ui = subparsers.add_parser(
        "ui",
        help="run the loopback-only conversational migration interface",
    )
    ui.add_argument("--project-root", type=Path, default=Path("."))
    ui.add_argument("--port", type=_ui_port, default=8765)
    ui.add_argument(
        "--open-browser",
        action="store_true",
        help="open the local agent UI in the system default browser after startup",
    )
    ui.add_argument(
        "--ollama-model",
        type=_ui_ollama_model,
        required=True,
        help=(
            "use the allowlisted local-Ollama provider with this model ID "
            "(for example, qwen3.8:latest)"
        ),
    )
    ui.add_argument(
        "--ollama-timeout-seconds",
        type=_ui_ollama_timeout_seconds,
        default=DEFAULT_OLLAMA_TIMEOUT_SECONDS,
        help=(
            "wall-clock deadline for each local model role call "
            f"({MIN_OLLAMA_TIMEOUT_SECONDS:g}-{MAX_OLLAMA_TIMEOUT_SECONDS:g}; "
            f"default: {DEFAULT_OLLAMA_TIMEOUT_SECONDS:g})"
        ),
    )

    return parser


def _ui_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def _ui_ollama_model(value: str) -> str:
    model_id = value.strip()
    if not model_id or len(model_id) > 300:
        raise argparse.ArgumentTypeError("Ollama model ID must contain 1 to 300 characters")
    if any(character in model_id for character in ("\x00", "\r", "\n")):
        raise argparse.ArgumentTypeError("Ollama model ID contains a forbidden control character")
    return model_id


def _ui_ollama_timeout_seconds(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Ollama timeout must be a number") from exc
    if (
        not math.isfinite(timeout)
        or not MIN_OLLAMA_TIMEOUT_SECONDS <= timeout <= MAX_OLLAMA_TIMEOUT_SECONDS
    ):
        raise argparse.ArgumentTypeError(
            "Ollama timeout must be between "
            f"{MIN_OLLAMA_TIMEOUT_SECONDS:g} and {MAX_OLLAMA_TIMEOUT_SECONDS:g} seconds"
        )
    return timeout


def _add_agent_run_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--thread-id", required=True)


def _add_live_model_arguments(
    parser: argparse.ArgumentParser,
    *,
    required: bool,
) -> None:
    parser.add_argument("--model-id", required=required)
    parser.add_argument("--api-key-env", required=required)
    parser.add_argument("--approved-by", required=required)
    parser.add_argument("--allow-live-api", action="store_true", required=required)
    parser.add_argument(
        "--allow-prompt-data-sharing",
        action="store_true",
        required=required,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse and execute one local capstone command."""

    args = build_parser().parse_args(argv)
    if args.command in CONTROLLED_RUN_COMMANDS:
        return _run_controlled_command_safely(args)
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


def _run_controlled_command_safely(args: argparse.Namespace) -> int:
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


if __name__ == "__main__":
    raise SystemExit(main())
