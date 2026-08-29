"""Case Management Console Engineer-step comparison (qwen vs. Claude CLI).

Mirror of ``benchmark.py`` for the second Salesforce migration unit. ``prepare``
freezes the current Engineer prompt, the case manifest/wiki-trace/context, and
the Ollama-projected JSON schema. ``run`` sends those exact bytes to either an
installed Ollama alias or the local, already-authenticated ``claude`` CLI,
applies a valid file plan only in a disposable workspace, and invokes the
repository's real local Salesforce validator bound to the case unit.

It never contacts a Salesforce org, never writes application source, and never
constructs an Anthropic API client: the ``claude-cli`` provider shells out to
the local Claude Code CLI in non-interactive structured-output mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from legacy_migration_agent.agent_runtime.agent_definitions import (
    AgentRole,
    load_agent_registry,
)
from legacy_migration_agent.agent_runtime.correction import CorrectionAttemptEvidence
from legacy_migration_agent.agent_runtime.model_agent_correction import (
    EngineerCorrectionAuthority,
    EngineerCorrectionContext,
    correction_wiki_query,
)
from legacy_migration_agent.agent_runtime.model_agents import (
    EngineerAgent,
    EngineerFilePlanOutcome,
    EngineerModelOutcome,
    EngineerRun,
    EngineerWorkspaceContext,
    apply_engineer_file_plan,
)
from legacy_migration_agent.agent_runtime.ollama_model import (
    OllamaStructuredModelClient,
    _project_ollama_schema,
)
from legacy_migration_agent.agent_runtime.openai_model import LiveModelApproval
from legacy_migration_agent.application.migration_scenarios import (
    CASE_WIKI_QUERY,
    _KNOWLEDGE_AS_OF,
)
from legacy_migration_agent.contracts import (
    MigrationManifest,
    MigrationRequest,
    ValidationReport,
)
from legacy_migration_agent.platforms.platform_runtime import _exact_diagnostic_ids
from legacy_migration_agent.core.integrity import artifact_digest, canonical_json_bytes
from legacy_migration_agent.core.policies import validate_manifest_for_request
from legacy_migration_agent.core.run_session import AgentDefinitionDigests, AgentRunSession
from legacy_migration_agent.core.workspace import IsolatedWorkspace
from legacy_migration_agent.knowledge.wiki import LlmWiki, RetrievalTrace
from legacy_migration_agent.platforms.local_checks import (
    CASE_MANAGEMENT_CONSOLE_UNIT_ID,
    CASE_IMPLEMENTATION_CONTRACT,
)
from legacy_migration_agent.platforms.salesforce_runtime import (
    CASE_SALESFORCE_PLATFORM_ADAPTER,
    build_salesforce_local_validator,
)

BENCHMARK_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = BENCHMARK_ROOT.parents[1]
PROTOCOL_ROOT = BENCHMARK_ROOT / "case-protocol"
RESULTS_ROOT = BENCHMARK_ROOT / "case-results"
RUNTIME_ROOT = BENCHMARK_ROOT / "case-runtime"

# The prior live UI run that reached (and failed at) the Engineer step supplies
# the controller-owned scope, manifest, and Architect wiki query for the case.
DEFAULT_SOURCE_RUN = "5326ec30e4c8ddc013a0a270"
GENERATION_SETTINGS = {
    "endpoint": "provider-specific loopback",
    "stream": False,
    "think": False,
    "temperature": 0,
    "timeout_seconds": 600,
}
# The case Architect retrieval pins the four case-specific controller-jest
# diagnostics plus the two shared platform signals, which resolve (via the
# exact-id path) to the Apex-security contract, the validation contract, and
# the dedicated case-management-console behavior page. This mirrors the real
# scenario's ``_retrieve_wiki`` result byte-for-byte after the wiki de-cheat
# moved the case signals out of the shared VF->LWC page into their own page.
EXPECTED_WIKI_PAGE_IDS = (
    "salesforce-apex-security",
    "salesforce-validation",
    "salesforce-case-management-console",
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_model_name(model_id: str) -> str:
    import re

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
    # Substitute the current controller-owned case contract so the frozen
    # comparison reflects the live implementation contract byte-for-byte.
    manifest_value["implementation_contract"] = list(CASE_IMPLEMENTATION_CONTRACT)
    manifest = MigrationManifest.model_validate(manifest_value)
    prior_trace = _read_json(model_root / "wiki-trace.json")
    return request, manifest, prior_trace


def prepare_protocol(source_run: str) -> dict[str, Any]:
    registry, definition_digests = _definition_digests()
    request, manifest, prior_trace = _source_artifacts(source_run)
    validate_manifest_for_request(manifest, request)
    CASE_SALESFORCE_PLATFORM_ADAPTER.validate_manifest(manifest, request)

    # Retrieve exactly as the live case Architect does (platform_runtime
    # ``_retrieve_wiki``): the current de-cheated ``CASE_WIKI_QUERY`` with its
    # exact diagnostic ids forced into the bounded excerpt. The stale
    # ``prior_trace["query"]`` predates the wiki restructuring and no longer
    # resolves the case behavior page, so it must not drive the frozen protocol.
    wiki = LlmWiki.load(PROJECT_ROOT / "knowledge" / "wiki")
    wiki_trace = wiki.search(
        CASE_WIKI_QUERY,
        platform=request.platform,
        source_version=request.target.source_version,
        target_version=request.target.target_version,
        max_primary_hits=1,
        expand_links=True,
        as_of=_KNOWLEDGE_AS_OF,
        max_age_days=365,
        required_exact_ids=_exact_diagnostic_ids(CASE_WIKI_QUERY),
    )
    selected_ids = tuple(hit.page_id for hit in wiki_trace.hits)
    if selected_ids != EXPECTED_WIKI_PAGE_IDS:
        raise RuntimeError(f"current Wiki selection drifted: {selected_ids!r}")

    definition = registry.get(AgentRole.ENGINEER)
    source_root = PROJECT_ROOT / request.repository
    with IsolatedWorkspace(
        source_root,
        manifest.approved_paths,
        expected_revision=request.base_revision,
    ) as workspace:
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
        "unit_id": CASE_MANAGEMENT_CONSOLE_UNIT_ID,
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


class ClaudeCliStructuredModelClient:
    """Structured-output client that shells out to the local Claude Code CLI.

    This satisfies the same ``.parse`` seam as the Ollama/OpenAI clients but
    never builds an Anthropic API client. It invokes ``claude -p`` in
    non-interactive mode with the Engineer system prompt, the exact context as
    the user message, and the projected JSON schema for structured output.
    """

    def __init__(
        self,
        model_id: str,
        *,
        projected_schema: dict[str, Any],
        timeout_seconds: float = 600.0,
    ) -> None:
        self._model_id = model_id
        self._schema = projected_schema
        self._timeout_seconds = timeout_seconds
        self._model_revision: str | None = None
        self._last_usage = None
        self._last_cli_meta: dict[str, Any] | None = None

    @property
    def model_revision(self) -> str | None:
        return self._model_revision

    @property
    def last_usage(self):  # noqa: ANN201 - mirrors the loose benchmark seam
        return self._last_usage

    @property
    def last_cli_meta(self) -> dict[str, Any] | None:
        return self._last_cli_meta

    def bind_model_revision(self, expected_revision: str) -> None:  # pragma: no cover
        self._model_revision = expected_revision

    def parse(self, *, system_prompt: str, input_value: Any, output_type: type) -> Any:
        binary = shutil.which("claude")
        if binary is None:
            raise RuntimeError("claude CLI is not on PATH")
        # Derive the schema from the exact requested output type each call.
        # Attempt one requests EngineerModelOutcome; a correction requests the
        # narrower EngineerFilePlanOutcome. The Ollama projection is reused
        # because it strips the `discriminator` keyword the CLI's strict-mode
        # structured output rejects.
        schema = _project_ollama_schema(output_type.model_json_schema(mode="validation"))
        context_json = canonical_json_bytes(input_value.model_dump(mode="json")).decode("utf-8")
        user_message = (
            "You are being invoked as the Engineer agent for one structured call.\n"
            "The following JSON is the exact `EngineerWorkspaceContext` input. Read it\n"
            "and return exactly one `EngineerModelOutcome` JSON object per your system\n"
            "contract. Output JSON only, with no prose and no code fences.\n\n"
            "<engineer_workspace_context>\n"
            f"{context_json}\n"
            "</engineer_workspace_context>\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Claude Code's `--json-schema` mechanism drives an internal
            # StructuredOutput tool, so it must stay allowed; only filesystem,
            # exec, and network tools are blocked. The system prompt and schema
            # are passed inline (the CLI has no `-file` variants for them), and
            # the workspace context is fed on stdin.
            argv = [
                binary,
                "-p",
                "--output-format",
                "json",
                "--model",
                self._model_id,
                "--system-prompt",
                system_prompt,
                "--exclude-dynamic-system-prompt-sections",
                "--json-schema",
                json.dumps(schema),
                "--disallowed-tools",
                "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit",
            ]
            started = time.monotonic_ns()
            completed = subprocess.run(
                argv,
                input=user_message,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                cwd=str(tmp_path),
            )
            elapsed_ms = (time.monotonic_ns() - started) // 1_000_000
        if completed.returncode != 0:
            raise RuntimeError(
                f"claude CLI exited {completed.returncode}: {completed.stderr.strip()[:1000]}"
            )
        envelope = json.loads(completed.stdout)
        self._model_revision = envelope.get("model") or self._model_id
        self._last_cli_meta = {
            "elapsed_ms": elapsed_ms,
            "cli_model": envelope.get("model"),
            "num_turns": envelope.get("num_turns"),
            "is_error": envelope.get("is_error"),
            "usage": envelope.get("usage"),
            "total_cost_usd": envelope.get("total_cost_usd"),
        }
        result_text = envelope.get("result")
        if not isinstance(result_text, str):
            raise RuntimeError("claude CLI returned no result text")
        payload = _extract_json_object(result_text)
        return output_type.model_validate(payload)


def _extract_json_object(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        newline = stripped.find("\n")
        if newline != -1:
            stripped = stripped[newline + 1 :]
        stripped = stripped.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(stripped[start : end + 1])


def _build_client(provider: str, model_id: str, projected_schema: dict[str, Any]) -> Any:
    if provider == "ollama":
        return OllamaStructuredModelClient(
            model_id,
            approval=LiveModelApproval(
                allow_live_api=True,
                allow_prompt_data_sharing=True,
                approved_by="local-model-benchmark-operator",
            ),
            timeout_seconds=600,
        )
    if provider == "claude-cli":
        return ClaudeCliStructuredModelClient(
            model_id,
            projected_schema=projected_schema,
            timeout_seconds=600,
        )
    raise ValueError(f"unsupported provider: {provider!r}")


def run_model(model_id: str, provider: str) -> dict[str, Any]:
    summary, request, manifest, _wiki_trace, context, prompt = _load_protocol()
    registry, definition_digests = _definition_digests()
    definition = registry.get(AgentRole.ENGINEER)
    if definition.definition_digest != summary["engineer_definition_digest"]:
        raise RuntimeError("current Engineer definition differs from frozen benchmark prompt")

    projected_schema = _read_json(PROTOCOL_ROOT / "projected-schema.json")
    safe_model = _safe_model_name(f"{provider}-{model_id}")
    nonce = uuid4().hex[:12]
    run_id = f"case-benchmark-{safe_model}-{nonce}"
    thread_id = f"case-benchmark-thread-{safe_model}-{nonce}"
    run_dir = RUNTIME_ROOT / run_id
    session = AgentRunSession.initialize(
        PROJECT_ROOT,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        slice_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID,
        source_root=request.repository,
        request_digest=artifact_digest(request),
        agent_definition_digests=definition_digests,
        provider_id=provider,
        model_id=model_id,
    )

    result: dict[str, Any] = {
        "schema_version": "1.0",
        "measured_at": datetime.now(UTC).isoformat(),
        "provider": provider,
        "model_id": model_id,
        "unit_id": CASE_MANAGEMENT_CONSOLE_UNIT_ID,
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
    client = _build_client(provider, model_id, projected_schema)

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
            if provider == "claude-cli":
                result["claude_cli_meta"] = client.last_cli_meta
            else:
                result["usage"] = (
                    client.last_usage.model_dump(mode="json")
                    if client.last_usage is not None
                    else None
                )
            _write_json(
                RESULTS_ROOT / safe_model / f"{nonce}-model-outcome.json",
                outcome.model_dump(mode="json"),
            )
        except BaseException as error:
            result["model_revision"] = getattr(client, "model_revision", None)
            if provider == "claude-cli":
                result["claude_cli_meta"] = getattr(client, "last_cli_meta", None)
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
            validator = build_salesforce_local_validator(session, registry, timeout_seconds=120)
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


def _locate_recoverable_attempt_one(safe_model: str) -> dict[str, Any]:
    """Find the latest attempt-one result that reached a recoverable failure."""

    model_dir = RESULTS_ROOT / safe_model
    candidates = sorted(model_dir.glob("*-result.json"), key=lambda p: p.stat().st_mtime)
    for result_path in reversed(candidates):
        result = _read_json(result_path)
        if (
            result.get("file_plan_applied")
            and result.get("validation_disposition") == "recoverable_failure"
            and result.get("candidate_revision")
        ):
            nonce = result_path.name.split("-", 1)[0]
            return {
                "nonce": nonce,
                "candidate_revision": result["candidate_revision"],
                "change_set_digest": result.get("change_set_digest"),
                "outcome": _read_json(model_dir / f"{nonce}-model-outcome.json"),
                "report": _read_json(model_dir / f"{nonce}-validation-report.json"),
            }
    raise RuntimeError(f"no recoverable attempt-one result found under {model_dir}")


def run_correction(model_id: str, provider: str) -> dict[str, Any]:
    """Run the bounded attempt-two Engineer correction using real controller evidence."""

    summary, request, manifest, wiki_trace, _context, _prompt = _load_protocol()
    registry, definition_digests = _definition_digests()
    definition = registry.get(AgentRole.ENGINEER)
    if definition.definition_digest != summary["engineer_definition_digest"]:
        raise RuntimeError("current Engineer definition differs from frozen benchmark prompt")

    projected_schema = _read_json(PROTOCOL_ROOT / "projected-schema.json")
    safe_model = _safe_model_name(f"{provider}-{model_id}")
    prior = _locate_recoverable_attempt_one(safe_model)
    prior_outcome = EngineerModelOutcome.model_validate(prior["outcome"])
    if not isinstance(prior_outcome.result, EngineerFilePlanOutcome):
        raise RuntimeError("attempt-one artifact is not a file plan")
    prior_file_plan = prior_outcome.result.file_plan
    prior_report = ValidationReport.model_validate(prior["report"])

    nonce = uuid4().hex[:12]
    run_id = f"case-correction-{safe_model}-{nonce}"
    run_dir = RUNTIME_ROOT / run_id
    session = AgentRunSession.initialize(
        PROJECT_ROOT,
        run_dir,
        run_id=run_id,
        thread_id=f"case-correction-thread-{safe_model}-{nonce}",
        slice_id=CASE_MANAGEMENT_CONSOLE_UNIT_ID,
        source_root=request.repository,
        request_digest=artifact_digest(request),
        agent_definition_digests=definition_digests,
        provider_id=provider,
        model_id=model_id,
    )

    result: dict[str, Any] = {
        "schema_version": "1.0",
        "measured_at": datetime.now(UTC).isoformat(),
        "stage": "correction_attempt_two",
        "provider": provider,
        "model_id": model_id,
        "unit_id": CASE_MANAGEMENT_CONSOLE_UNIT_ID,
        "protocol_digest": summary["protocol_digest"],
        "prior_attempt_nonce": prior["nonce"],
        "prior_candidate_revision": prior["candidate_revision"],
        "correction_authority_built": False,
        "structured_output_valid": False,
        "delta_applied": False,
        "local_validation_performed": False,
        "salesforce_org_contacted": False,
    }

    # Re-derive the exact attempt-one ChangeSet deterministically from the frozen
    # prior file plan, then build the real controller correction evidence.
    try:
        with IsolatedWorkspace(
            session.source_root,
            manifest.approved_paths,
            temp_parent=session.workspaces_dir,
            expected_revision=request.base_revision,
        ) as prior_ws:
            prior_change_set, prior_after = apply_engineer_file_plan(
                request, manifest, prior_ws, prior_file_plan
            )
        if prior_after != prior["candidate_revision"]:
            raise RuntimeError("re-derived attempt-one revision differs from persisted evidence")
        if prior["change_set_digest"] and artifact_digest(prior_change_set) != prior["change_set_digest"]:
            raise RuntimeError("re-derived attempt-one ChangeSet digest differs from evidence")

        evidence = CorrectionAttemptEvidence.freeze(manifest, prior_change_set, prior_report)
        repair_signal_ids = EngineerCorrectionContext.require_repair_contract(
            evidence, prior_file_plan
        )
        result["repair_signal_ids"] = list(repair_signal_ids)
        wiki_query = correction_wiki_query(request.platform, repair_signal_ids)
        exact_ids = _exact_diagnostic_ids(wiki_query)
        wiki = LlmWiki.load(PROJECT_ROOT / "knowledge" / "wiki")
        correction_trace = wiki.search(
            wiki_query,
            platform=request.platform,
            source_version=request.target.source_version,
            target_version=request.target.target_version,
            max_primary_hits=3,
            expand_links=True,
            as_of=date.today(),
            max_age_days=365,
            required_exact_ids=exact_ids,
        )
        result["correction_wiki_query"] = wiki_query
        result["correction_wiki_page_ids"] = [hit.page_id for hit in correction_trace.hits]
        authority = EngineerCorrectionAuthority.freeze(
            evidence,
            prior_file_plan,
            prior_candidate_revision=prior["candidate_revision"],
            correction_wiki_trace=correction_trace,
        )
        authority.require_canonical_context(request, manifest)
        result["correction_authority_built"] = True
        result["allowed_correction_paths"] = list(
            authority.model_context.allowed_correction_paths
        )
    except BaseException as error:
        result["failure_stage"] = "correction_authority"
        result["failure"] = _sanitized_error(error)
        _write_json(RESULTS_ROOT / safe_model / f"{nonce}-correction-result.json", result)
        return result

    client = _build_client(provider, model_id, projected_schema)
    engineer_agent = EngineerAgent(registry, client)

    with IsolatedWorkspace(
        session.source_root,
        manifest.approved_paths,
        temp_parent=session.workspaces_dir,
        expected_revision=request.base_revision,
    ) as workspace:
        try:
            context2 = engineer_agent.prepare_context(
                request,
                manifest,
                workspace,
                architect_wiki_trace=wiki_trace,
                attempt=2,
                correction_authority=authority,
            )
            run: EngineerRun = engineer_agent.implement(
                request,
                manifest,
                workspace,
                architect_wiki_trace=wiki_trace,
                attempt=2,
                correction_authority=authority,
                prepared_context=context2,
            )
            result["structured_output_valid"] = True
            result["result_kind"] = run.model_outcome.result.kind
            result["model_revision"] = getattr(client, "model_revision", None)
            if provider == "claude-cli":
                result["claude_cli_meta"] = getattr(client, "last_cli_meta", None)
            _write_json(
                RESULTS_ROOT / safe_model / f"{nonce}-correction-outcome.json",
                run.model_outcome.model_dump(mode="json"),
            )
        except BaseException as error:
            result["model_revision"] = getattr(client, "model_revision", None)
            if provider == "claude-cli":
                result["claude_cli_meta"] = getattr(client, "last_cli_meta", None)
            result["failure_stage"] = "correction_generation"
            result["failure"] = _sanitized_error(error)
            _write_json(RESULTS_ROOT / safe_model / f"{nonce}-correction-result.json", result)
            return result

        if run.change_set is None:
            result["failure_stage"] = "correction_no_change_set"
            _write_json(RESULTS_ROOT / safe_model / f"{nonce}-correction-result.json", result)
            return result
        result["delta_applied"] = True
        result["changed_paths"] = run.change_set.changed_paths
        result["candidate_revision"] = run.workspace_after_revision
        result["change_set_digest"] = artifact_digest(run.change_set)
        if run.effective_file_plan is not None:
            result["delta_changed_paths"] = [u.path for u in run.model_outcome.result.file_plan.updates]

        try:
            validator = build_salesforce_local_validator(session, registry, timeout_seconds=120)
            report = validator(request, manifest, run.change_set, workspace, 2)
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
                RESULTS_ROOT / safe_model / f"{nonce}-correction-validation-report.json",
                report.model_dump(mode="json"),
            )
        except BaseException as error:
            result["failure_stage"] = "local_validation"
            result["failure"] = _sanitized_error(error)

    _write_json(RESULTS_ROOT / safe_model / f"{nonce}-correction-result.json", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--source-run", default=DEFAULT_SOURCE_RUN)
    run = subparsers.add_parser("run")
    run.add_argument("--model", required=True)
    run.add_argument("--provider", required=True, choices=("ollama", "claude-cli"))
    correct = subparsers.add_parser("correct")
    correct.add_argument("--model", required=True)
    correct.add_argument("--provider", required=True, choices=("ollama", "claude-cli"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "prepare":
        value = prepare_protocol(args.source_run)
    elif args.command == "correct":
        value = run_correction(args.model, args.provider)
    else:
        value = run_model(args.model, args.provider)
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
