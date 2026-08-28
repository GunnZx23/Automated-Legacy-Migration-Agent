"""Frozen local-model comparison for the Salesforce Engineer role.

This runner deliberately bypasses the conversational UI and Architect model
generation.  ``prepare`` freezes one exact, current Engineer prompt, model
context, and Ollama-projected JSON schema.  ``run`` sends those same bytes to
one installed Ollama alias, applies a valid file plan only in a disposable
workspace, and invokes the repository's real local Salesforce validator.

It never contacts a Salesforce org and never writes application source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from legacy_migration_agent.agent_runtime.agent_definitions import (
    AgentRole,
    load_agent_registry,
)
from legacy_migration_agent.agent_runtime.model_agents import (
    EngineerAgent,
    EngineerFilePlanOutcome,
    EngineerModelOutcome,
    EngineerWorkspaceContext,
    apply_engineer_file_plan,
)
from legacy_migration_agent.agent_runtime.ollama_model import (
    OllamaStructuredModelClient,
    _project_ollama_schema,
)
from legacy_migration_agent.agent_runtime.openai_model import LiveModelApproval
from legacy_migration_agent.contracts import MigrationManifest, MigrationRequest
from legacy_migration_agent.core.integrity import artifact_digest, canonical_json_bytes
from legacy_migration_agent.core.policies import validate_manifest_for_request
from legacy_migration_agent.core.run_session import (
    AgentDefinitionDigests,
    AgentRunSession,
)
from legacy_migration_agent.core.workspace import IsolatedWorkspace
from legacy_migration_agent.knowledge.wiki import LlmWiki, RetrievalTrace
from legacy_migration_agent.platforms.local_checks import (
    SALESFORCE_IMPLEMENTATION_CONTRACT,
)
from legacy_migration_agent.platforms.salesforce_runtime import (
    SALESFORCE_PLATFORM_ADAPTER,
    build_salesforce_local_validator,
)

BENCHMARK_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = BENCHMARK_ROOT.parents[1]
PROTOCOL_ROOT = BENCHMARK_ROOT / "protocol"
RESULTS_ROOT = BENCHMARK_ROOT / "results"
RUNTIME_ROOT = BENCHMARK_ROOT / "runtime"

DEFAULT_SOURCE_RUN = "3cd384f951d0cb4199dd3f07"
GENERATION_SETTINGS = {
    "endpoint": "Ollama /api/chat on 127.0.0.1:11434",
    "stream": False,
    "think": False,
    "temperature": 0,
    "timeout_seconds": 600,
}


class _MetadataCaptureTransport:
    """Delegate unchanged while retaining only sanitized top-level metadata."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.chat_metadata: dict[str, Any] | None = None

    def request(self, **kwargs: Any) -> bytes:
        payload = self.delegate.request(**kwargs)
        if kwargs.get("path") == "/api/chat":
            response = json.loads(payload)
            message = response.get("message")
            self.chat_metadata = {
                "model": response.get("model"),
                "error": response.get("error"),
                "done": response.get("done"),
                "done_reason": response.get("done_reason"),
                "prompt_eval_count": response.get("prompt_eval_count"),
                "eval_count": response.get("eval_count"),
                "message_role": message.get("role") if isinstance(message, dict) else None,
                "tool_call_count": (
                    len(message.get("tool_calls") or ()) if isinstance(message, dict) else None
                ),
            }
        return payload


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_model_name(model_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model_id).strip("._-")


def _definition_digests() -> tuple[Any, AgentDefinitionDigests]:
    registry = load_agent_registry(PROJECT_ROOT / "agents")
    digests = AgentDefinitionDigests(
        architect=registry.get(AgentRole.ARCHITECT).definition_digest,
        engineer=registry.get(AgentRole.ENGINEER).definition_digest,
        validator=registry.get(AgentRole.VALIDATOR).definition_digest,
    )
    return registry, digests


def _source_artifacts(
    source_run: str,
) -> tuple[MigrationRequest, MigrationManifest, dict[str, Any]]:
    run_root = PROJECT_ROOT / ".runs" / "agent-ui" / source_run / "evidence"
    request = MigrationRequest.model_validate(_read_json(run_root / "request.json"))
    model_root = run_root / "model-runs" / request.request_id
    architect = _read_json(model_root / "architect.json")
    manifest_value = architect["proposal"]["manifest"]
    # The old run supplies the controller-owned scope and semantic decisions.
    # The current implementation contract is substituted explicitly so the
    # frozen comparison includes the corrected Jest import-order requirement.
    manifest_value["implementation_contract"] = list(SALESFORCE_IMPLEMENTATION_CONTRACT)
    manifest = MigrationManifest.model_validate(manifest_value)
    prior_trace = _read_json(model_root / "wiki-trace.json")
    return request, manifest, prior_trace


def prepare_protocol(source_run: str) -> dict[str, Any]:
    registry, definition_digests = _definition_digests()
    request, manifest, prior_trace = _source_artifacts(source_run)
    validate_manifest_for_request(manifest, request)
    SALESFORCE_PLATFORM_ADAPTER.validate_manifest(manifest, request)

    wiki = LlmWiki.load(PROJECT_ROOT / "knowledge" / "wiki")
    wiki_trace = wiki.search(
        str(prior_trace["query"]),
        platform=request.platform,
        source_version=request.target.source_version,
        target_version=request.target.target_version,
        # The persisted Architect handoff contains one lexical primary plus
        # its two controller-selected linked pages. Re-expand that same
        # topology so current page bytes (including the Jest fix) are frozen.
        max_primary_hits=1,
        expand_links=True,
        as_of=date.today(),
        max_age_days=int(prior_trace["max_age_days"]),
    )
    selected_ids = tuple(hit.page_id for hit in wiki_trace.hits)
    expected_ids = (
        "salesforce-visualforce-to-lwc",
        "salesforce-apex-security",
        "salesforce-validation",
    )
    if selected_ids != expected_ids:
        raise RuntimeError(f"current Wiki selection drifted: {selected_ids!r}")

    definition = registry.get(AgentRole.ENGINEER)
    source_root = PROJECT_ROOT / request.repository
    with IsolatedWorkspace(
        source_root,
        manifest.approved_paths,
        expected_revision=request.base_revision,
    ) as workspace:
        # prepare_context does not dispatch the supplied model object.
        agent = EngineerAgent(registry, object())  # type: ignore[arg-type]
        context = agent.prepare_context(
            request,
            manifest,
            workspace,
            architect_wiki_trace=wiki_trace,
            attempt=1,
        )

    projected_schema = _project_ollama_schema(
        EngineerModelOutcome.model_json_schema(mode="validation")
    )
    protocol_payload = {
        "system_prompt": definition.system_prompt,
        "context": context.model_dump(mode="json"),
        "projected_schema": projected_schema,
        "generation_settings": GENERATION_SETTINGS,
    }
    protocol_digest = artifact_digest(protocol_payload)
    summary = {
        "schema_version": "1.0",
        "prepared_at": datetime.now(UTC).isoformat(),
        "source_run": source_run,
        "source_revision": request.base_revision,
        "request_digest": artifact_digest(request),
        "manifest_digest": artifact_digest(manifest),
        "wiki_trace_digest": artifact_digest(wiki_trace),
        "wiki_page_ids": selected_ids,
        "engineer_version": definition.version,
        "engineer_definition_digest": definition.definition_digest,
        "system_prompt_digest": _text_digest(definition.system_prompt),
        "context_digest": artifact_digest(context),
        "projected_schema_digest": artifact_digest(projected_schema),
        "protocol_digest": protocol_digest,
        "generation_settings": GENERATION_SETTINGS,
        "agent_definition_digests": definition_digests.model_dump(mode="json"),
        "input_characters": len(canonical_json_bytes(context)),
    }
    _write_json(PROTOCOL_ROOT / "context.json", context.model_dump(mode="json"))
    _write_json(PROTOCOL_ROOT / "projected-schema.json", projected_schema)
    _write_json(PROTOCOL_ROOT / "request.json", request.model_dump(mode="json"))
    _write_json(PROTOCOL_ROOT / "manifest.json", manifest.model_dump(mode="json"))
    _write_json(PROTOCOL_ROOT / "wiki-trace.json", wiki_trace.model_dump(mode="json"))
    (PROTOCOL_ROOT / "engineer-system-prompt.md").write_text(
        definition.system_prompt,
        encoding="utf-8",
    )
    _write_json(PROTOCOL_ROOT / "protocol.json", summary)
    return summary


def _load_protocol() -> tuple[
    dict[str, Any],
    MigrationRequest,
    MigrationManifest,
    RetrievalTrace,
    EngineerWorkspaceContext,
    str,
]:
    summary = _read_json(PROTOCOL_ROOT / "protocol.json")
    request = MigrationRequest.model_validate(_read_json(PROTOCOL_ROOT / "request.json"))
    manifest = MigrationManifest.model_validate(_read_json(PROTOCOL_ROOT / "manifest.json"))
    wiki_trace = RetrievalTrace.model_validate(_read_json(PROTOCOL_ROOT / "wiki-trace.json"))
    context = EngineerWorkspaceContext.model_validate(_read_json(PROTOCOL_ROOT / "context.json"))
    prompt = (PROTOCOL_ROOT / "engineer-system-prompt.md").read_text(encoding="utf-8")
    projected_schema = _read_json(PROTOCOL_ROOT / "projected-schema.json")
    current_payload = {
        "system_prompt": prompt,
        "context": context.model_dump(mode="json"),
        "projected_schema": projected_schema,
        "generation_settings": summary["generation_settings"],
    }
    if artifact_digest(current_payload) != summary["protocol_digest"]:
        raise RuntimeError("frozen benchmark protocol digest mismatch")
    if (
        _project_ollama_schema(EngineerModelOutcome.model_json_schema(mode="validation"))
        != projected_schema
    ):
        raise RuntimeError("runtime Engineer schema differs from the frozen schema")
    return summary, request, manifest, wiki_trace, context, prompt


def _sanitized_error(error: BaseException) -> dict[str, str]:
    message = str(error).replace(str(PROJECT_ROOT), "<project-root>")
    return {"type": type(error).__name__, "message": message[:2000]}


def run_model(model_id: str) -> dict[str, Any]:
    summary, request, manifest, _wiki_trace, context, prompt = _load_protocol()
    registry, definition_digests = _definition_digests()
    definition = registry.get(AgentRole.ENGINEER)
    if definition.definition_digest != summary["engineer_definition_digest"]:
        raise RuntimeError("current Engineer definition differs from frozen benchmark prompt")

    safe_model = _safe_model_name(model_id)
    nonce = uuid4().hex[:12]
    run_id = f"benchmark-{safe_model}-{nonce}"
    thread_id = f"benchmark-thread-{safe_model}-{nonce}"
    run_dir = RUNTIME_ROOT / run_id
    session = AgentRunSession.initialize(
        PROJECT_ROOT,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        slice_id="salesforce-vf-to-lwc-model-comparison",
        source_root=request.repository,
        request_digest=artifact_digest(request),
        agent_definition_digests=definition_digests,
        provider_id="ollama",
        model_id=model_id,
    )

    result: dict[str, Any] = {
        "schema_version": "1.0",
        "measured_at": datetime.now(UTC).isoformat(),
        "model_id": model_id,
        "protocol_digest": summary["protocol_digest"],
        "system_prompt_digest": summary["system_prompt_digest"],
        "context_digest": summary["context_digest"],
        "projected_schema_digest": summary["projected_schema_digest"],
        "generation_settings": summary["generation_settings"],
        "structured_output_valid": False,
        "file_plan_applied": False,
        "local_validation_performed": False,
        "salesforce_org_contacted": False,
    }
    client = OllamaStructuredModelClient(
        model_id,
        approval=LiveModelApproval(
            allow_live_api=True,
            allow_prompt_data_sharing=True,
            approved_by="local-model-benchmark-operator",
        ),
        timeout_seconds=600,
    )
    capture = _MetadataCaptureTransport(client._transport)  # type: ignore[attr-defined]
    client._transport = capture  # type: ignore[attr-defined]

    with IsolatedWorkspace(
        session.source_root,
        manifest.approved_paths,
        temp_parent=session.workspaces_dir,
        expected_revision=request.base_revision,
    ) as workspace:
        try:
            raw = client.parse(
                system_prompt=prompt,
                input_value=context,
                output_type=EngineerModelOutcome,
            )
            outcome = EngineerModelOutcome.model_validate(raw.model_dump(mode="python"))
            result["structured_output_valid"] = True
            result["result_kind"] = outcome.result.kind
            result["model_revision"] = client.model_revision
            result["ollama_response_metadata"] = capture.chat_metadata
            result["usage"] = (
                client.last_usage.model_dump(mode="json") if client.last_usage is not None else None
            )
            _write_json(
                RESULTS_ROOT / safe_model / f"{nonce}-model-outcome.json",
                outcome.model_dump(mode="json"),
            )
        except BaseException as error:
            result["model_revision"] = client.model_revision
            result["ollama_response_metadata"] = capture.chat_metadata
            result["usage"] = (
                client.last_usage.model_dump(mode="json") if client.last_usage is not None else None
            )
            result["failure_stage"] = "structured_generation"
            result["failure"] = _sanitized_error(error)
            _write_json(RESULTS_ROOT / safe_model / f"{nonce}-result.json", result)
            return result

        if not isinstance(outcome.result, EngineerFilePlanOutcome):
            result["failure_stage"] = "decision_required"
            result["intervention"] = outcome.result.intervention.model_dump(mode="json")
            _write_json(RESULTS_ROOT / safe_model / f"{nonce}-result.json", result)
            return result

        try:
            change_set, after_revision = apply_engineer_file_plan(
                request,
                manifest,
                workspace,
                outcome.result.file_plan,
            )
            result["file_plan_applied"] = True
            result["changed_paths"] = change_set.changed_paths
            result["candidate_revision"] = after_revision
            result["change_set_digest"] = artifact_digest(change_set)
        except BaseException as error:
            result["failure_stage"] = "file_plan_application"
            result["failure"] = _sanitized_error(error)
            _write_json(RESULTS_ROOT / safe_model / f"{nonce}-result.json", result)
            return result

        try:
            validator = build_salesforce_local_validator(
                session,
                registry,
                timeout_seconds=120,
            )
            report = validator(request, manifest, change_set, workspace, 1)
            result["local_validation_performed"] = True
            result["validation_disposition"] = report.disposition.value
            result["validation_results"] = [
                {
                    "check_id": item.check_id,
                    "status": item.status.value,
                    "diagnostic_ids": item.diagnostic_ids,
                    "summary": item.summary,
                }
                for item in report.results
            ]
            result["checks_passed"] = sum(item.status.value == "passed" for item in report.results)
            result["checks_total"] = len(report.results)
            _write_json(
                RESULTS_ROOT / safe_model / f"{nonce}-validation-report.json",
                report.model_dump(mode="json"),
            )
        except BaseException as error:
            result["failure_stage"] = "local_validation"
            result["failure"] = _sanitized_error(error)

    _write_json(RESULTS_ROOT / safe_model / f"{nonce}-result.json", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--source-run", default=DEFAULT_SOURCE_RUN)
    run = subparsers.add_parser("run")
    run.add_argument("--model", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "prepare":
        value = prepare_protocol(args.source_run)
    else:
        value = run_model(args.model)
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
