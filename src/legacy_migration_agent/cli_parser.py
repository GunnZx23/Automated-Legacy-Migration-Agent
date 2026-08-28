"""Argument parser construction for the local capstone CLI."""

from __future__ import annotations

import argparse
import math
from datetime import date, datetime
from pathlib import Path

from legacy_migration_agent import cli_commands as commands
from legacy_migration_agent.agent_runtime.ollama_model import (
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    MAX_OLLAMA_TIMEOUT_SECONDS,
    MIN_OLLAMA_TIMEOUT_SECONDS,
)
from legacy_migration_agent.contracts import Platform


def build_parser() -> argparse.ArgumentParser:
    """Build the intentionally small capstone command surface."""

    parser = argparse.ArgumentParser(prog="legacy-migration-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = _add_command(
        subparsers,
        "validate-manifest",
        "validate typed request and manifest JSON",
    )
    validate.add_argument("--request", type=Path, required=True)
    validate.add_argument("--manifest", type=Path, required=True)

    schemas = _add_command(
        subparsers,
        "export-schemas",
        "export the versioned artifact JSON Schemas",
    )
    schemas.add_argument("--output-dir", type=Path, required=True)

    wiki = _add_command(
        subparsers,
        "wiki-search",
        "run deterministic navigation over the curated LLM Wiki",
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

    agents_check = _add_command(
        subparsers,
        "agents-check",
        "validate and describe the exact three versioned agent definitions",
    )
    agents_check.add_argument("--project-root", type=Path, default=Path("."))

    agent_request = _add_command(
        subparsers,
        "agent-request-create",
        "bind a platform preset request to the current local source bytes",
    )
    agent_request.add_argument("--project-root", type=Path, default=Path("."))
    agent_request.add_argument("--request-id", required=True)
    agent_request.add_argument("--scenario-id", required=True)
    agent_request.add_argument("--requested-at", type=datetime.fromisoformat, required=True)
    agent_request.add_argument("--output", required=True)

    agent_start = _add_command(
        subparsers,
        "agent-run-start",
        "start a supported three-agent Salesforce or MuleSoft run",
    )
    _add_agent_run_identity_arguments(agent_start)
    agent_start.add_argument("--scenario-id", required=True)
    agent_start.add_argument("--request", type=Path, required=True)
    _add_live_model_arguments(agent_start, required=True)

    agent_resume = _add_command(
        subparsers,
        "agent-run-resume",
        "resume an exact pending manifest approval",
    )
    _add_agent_run_identity_arguments(agent_resume)
    agent_resume.add_argument("--approval", type=Path, required=True)
    agent_resume.add_argument("--request", type=Path)
    _add_live_model_arguments(agent_resume, required=False)

    agent_retry = _add_command(
        subparsers,
        "agent-run-retry",
        "authorize the exact existing bounded correction attempt",
    )
    _add_agent_run_identity_arguments(agent_retry)
    agent_retry.add_argument("--approval", type=Path, required=True)
    agent_retry.add_argument("--request", type=Path)
    _add_live_model_arguments(agent_retry, required=True)

    agent_status = _add_command(
        subparsers,
        "agent-run-status",
        "read exact-thread run state without invoking a model",
    )
    _add_agent_run_identity_arguments(agent_status)
    agent_status.add_argument("--request", type=Path)

    manifest_decision = _add_command(
        subparsers,
        "agent-manifest-decision-create",
        "bind a named human decision to the exact pending manifest interrupt",
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

    correction_approval = _add_command(
        subparsers,
        "agent-correction-approval-create",
        "bind a named human approval to the exact offered second attempt",
    )
    _add_agent_run_identity_arguments(correction_approval)
    correction_approval.add_argument("--reviewer", required=True)
    correction_approval.add_argument("--comment", default="")
    correction_approval.add_argument("--output", required=True)

    final_request = _add_command(
        subparsers,
        "final-review-request",
        "request independent review of one exact completed agent run",
    )
    _add_agent_run_identity_arguments(final_request)
    final_request.add_argument("--requester", required=True)
    final_request.add_argument("--reviewer", required=True)
    final_request.add_argument("--requested-at", type=datetime.fromisoformat, required=True)
    final_request.add_argument("--expires-at", type=datetime.fromisoformat, required=True)

    final_decide = _add_command(
        subparsers,
        "final-review-decide",
        "consume one exact final-review request without granting external authority",
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

    final_status = _add_command(
        subparsers,
        "final-review-status",
        "inspect the provider-free final-review checkpoint",
    )
    _add_agent_run_identity_arguments(final_status)

    graph_evaluate = _add_command(
        subparsers,
        "graph-evaluate",
        "evaluate one revision-bound dependency graph against bounded labels",
    )
    graph_evaluate.add_argument("--graph", type=Path, required=True)
    graph_evaluate.add_argument("--labels", type=Path, required=True)
    graph_evaluate.add_argument(
        "--platform",
        choices=[platform.value for platform in Platform],
        required=True,
    )

    evaluation_verify = _add_command(
        subparsers,
        "evaluation-verify",
        "verify the compact, predeclared evaluation registry and current results",
    )
    evaluation_verify.add_argument("--registry", type=Path, required=True)
    evaluation_verify.add_argument("--results", type=Path, required=True)

    pilot_run = _add_command(
        subparsers,
        "evaluation-pilot-run-local",
        "write the unmeasured two-cell Qwen pilot baseline without invoking a provider",
    )
    pilot_run.add_argument("--project-root", type=Path, default=Path("."))
    pilot_run.add_argument("--registry", type=Path, required=True)
    pilot_run.add_argument("--output-dir", type=Path, required=True)

    pilot_verify = _add_command(
        subparsers,
        "evaluation-pilot-verify",
        "verify agent-run receipts and artifact bindings in one pilot snapshot",
    )
    pilot_verify.add_argument("--project-root", type=Path, default=Path("."))
    pilot_verify.add_argument("--registry", type=Path, required=True)
    pilot_verify.add_argument("--snapshot-dir", type=Path, required=True)

    pilot_ingest = _add_command(
        subparsers,
        "evaluation-pilot-ingest-agent-run",
        "ingest an existing terminal Qwen run without invoking a provider",
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

    ui = _add_command(
        subparsers,
        "ui",
        "run the loopback-only conversational migration interface",
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


def _add_command(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name, help=help_text)
    controlled = name == "graph-evaluate" or name.startswith(
        ("agent-", "final-review-", "evaluation-")
    )
    handler = (
        commands.run_controlled_command_safely
        if controlled
        else commands.dispatch_uncontrolled_command
    )
    parser.set_defaults(command_handler=handler)
    return parser
