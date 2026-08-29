"""Truthful, tool-free structured-output adapter for the local Claude CLI.

The executable runs locally, but inference is remote. The adapter therefore
records ``provider="claude-cli"`` and the ``remote_provider_managed`` execution
boundary. It disables Claude Code session persistence and every optional tool,
hook, plugin, MCP, browser, and project-customization surface that the CLI
exposes. Those controls do not claim that the upstream provider stores nothing;
provider retention remains governed by the operator's configured Claude
account or third-party provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ValidationError

from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelExecutionBoundary,
    ModelOutputError,
    ModelRuntimeError,
    ModelUsageEvidence,
    OutputModel,
)
from legacy_migration_agent.core.integrity import canonical_json_bytes
from legacy_migration_agent.core.observability import lifecycle_event

DEFAULT_CLAUDE_TIMEOUT_SECONDS = 240.0
MIN_CLAUDE_TIMEOUT_SECONDS = 1.0
MAX_CLAUDE_TIMEOUT_SECONDS = 900.0
_MAX_SYSTEM_PROMPT_BYTES = 256 * 1024
_MAX_INPUT_BYTES = 2 * 1024 * 1024
_MAX_USER_PROMPT_BYTES = 4 * 1024 * 1024
_VERSION_PROBE_TIMEOUT_SECONDS = 30.0
_SAFE_IDENTIFIER = re.compile(r"^[^\x00\r\n]{1,300}$")


def _resolve_claude_executable() -> str:
    override = os.environ.get("LEGACY_MIGRATION_CLAUDE_CLI_PATH", "").strip()
    return override or "claude"


def _stream_digest(value: str | None) -> str:
    payload = (value or "").encode("utf-8", errors="replace")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class ClaudeCliStructuredModelClient:
    """Structured-output client backed by one approved Claude CLI identity."""

    provider = "claude-cli"
    live_invocation = True
    store_false_sent = False
    execution_boundary: ModelExecutionBoundary = "remote_provider_managed"

    def __init__(
        self,
        model_id: str,
        *,
        approval: LiveModelApproval,
        timeout_seconds: float = DEFAULT_CLAUDE_TIMEOUT_SECONDS,
    ) -> None:
        normalized_model = model_id.strip()
        if _SAFE_IDENTIFIER.fullmatch(normalized_model) is None:
            raise ModelConfigurationError("an explicit model_id is required")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ModelConfigurationError("Claude timeout must be a number")
        normalized_timeout = float(timeout_seconds)
        if not MIN_CLAUDE_TIMEOUT_SECONDS <= normalized_timeout <= MAX_CLAUDE_TIMEOUT_SECONDS:
            raise ModelConfigurationError("Claude timeout is outside the supported range")
        if not isinstance(approval, LiveModelApproval):
            raise ModelConfigurationError("live Claude use requires explicit approval")
        self._model_id = normalized_model
        self._timeout_seconds = normalized_timeout
        self._executable = _resolve_claude_executable()
        self._workdir = tempfile.gettempdir()
        self._live_approval = LiveModelApproval.model_validate(
            approval.model_dump(mode="python")
        )
        self._last_usage: ModelUsageEvidence | None = None
        self._runtime_identity_digest: str | None = None

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_revision(self) -> str | None:
        """Claude CLI does not expose an observed provider model revision."""

        return None

    @property
    def runtime_identity_digest(self) -> str | None:
        return self._runtime_identity_digest

    @property
    def live_approval(self) -> LiveModelApproval:
        return self._live_approval

    @property
    def last_usage(self) -> ModelUsageEvidence | None:
        return self._last_usage

    def bind_runtime_identity(self, expected_revision: str) -> None:
        """Bind continuation to the same CLI version, alias, and auth provider."""

        if re.fullmatch(r"sha256:[0-9a-f]{64}", expected_revision) is None:
            raise ModelConfigurationError("recorded Claude runtime revision is invalid")
        current = self._resolve_model_revision(timeout_seconds=self._timeout_seconds)
        if current != expected_revision:
            raise ModelConfigurationError("Claude runtime revision changed")
        if (
            self._runtime_identity_digest is not None
            and self._runtime_identity_digest != expected_revision
        ):
            raise ModelConfigurationError("Claude runtime revision changed")
        self._runtime_identity_digest = expected_revision

    def _resolve_model_revision(self, *, timeout_seconds: float) -> str:
        """Prove the CLI and auth seam, returning a stable runtime identity digest."""

        probe_timeout = min(float(timeout_seconds), _VERSION_PROBE_TIMEOUT_SECONDS)
        version = self._run_probe(
            (self._executable, "--version"),
            timeout_seconds=probe_timeout,
            failure="the Claude CLI is unavailable",
        ).stdout.strip()
        auth = self._run_probe(
            (self._executable, "auth", "status", "--json"),
            timeout_seconds=probe_timeout,
            failure="the Claude CLI authentication status is unavailable",
        )
        try:
            auth_payload = json.loads(auth.stdout)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ModelOutputError("Claude CLI authentication status was invalid") from exc
        if not isinstance(auth_payload, dict):
            raise ModelOutputError("Claude CLI authentication status was invalid")
        if auth_payload.get("loggedIn") is not True:
            raise ModelConfigurationError("the Claude CLI is not authenticated")
        provider = auth_payload.get("apiProvider")
        if not isinstance(provider, str) or _SAFE_IDENTIFIER.fullmatch(provider) is None:
            raise ModelOutputError("Claude CLI authentication provider was invalid")
        digest = hashlib.sha256(
            b"claude-cli\x00"
            + self._model_id.encode("utf-8")
            + b"\x00"
            + version.encode("utf-8")
            + b"\x00"
            + provider.encode("utf-8")
        ).hexdigest()
        return "sha256:" + digest

    def _run_probe(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        failure: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=max(timeout_seconds, 1.0),
                cwd=self._workdir,
            )
        except FileNotFoundError as exc:
            raise ModelRuntimeError("the Claude CLI is not installed") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModelRuntimeError(failure) from exc
        if completed.returncode != 0:
            raise ModelRuntimeError(failure)
        return completed

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[OutputModel],
    ) -> OutputModel:
        if not isinstance(input_value, BaseModel):
            raise TypeError("structured model input must be a Pydantic model")
        if not isinstance(output_type, type) or not issubclass(output_type, BaseModel):
            raise TypeError("output_type must be a Pydantic model class")
        if not system_prompt.strip() or "\x00" in system_prompt:
            raise ModelConfigurationError("system prompt must be non-empty text without NUL bytes")
        if len(system_prompt.encode("utf-8")) > _MAX_SYSTEM_PROMPT_BYTES:
            raise ModelConfigurationError("system prompt exceeds the Claude size limit")
        input_bytes = canonical_json_bytes(input_value)
        if len(input_bytes) > _MAX_INPUT_BYTES:
            raise ModelConfigurationError("structured model input exceeds the Claude size limit")
        if self._runtime_identity_digest is None:
            self._runtime_identity_digest = self._resolve_model_revision(
                timeout_seconds=self._timeout_seconds
            )

        schema = output_type.model_json_schema(mode="validation")
        user_prompt = (
            "Produce one structured output for an automated legacy-migration workflow. "
            "The following JSON is the complete authoritative input:\n\n"
            + input_bytes.decode("utf-8")
            + "\n\nReturn exactly one JSON object with no prose or markdown. The object must "
            "validate against this JSON Schema:\n\n"
            + json.dumps(schema, sort_keys=True, separators=(",", ":"))
        )
        if len(user_prompt.encode("utf-8")) > _MAX_USER_PROMPT_BYTES:
            raise ModelConfigurationError("Claude request exceeds the prompt size limit")

        lifecycle_event(
            "claude_cli.generation.started",
            model_id=self._model_id,
            output_type=output_type.__name__,
        )
        started_ns = time.perf_counter_ns()
        envelope = self._invoke(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
        )
        elapsed_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
        structured_output = envelope.get("structured_output")
        if not isinstance(structured_output, dict):
            raise ModelOutputError("Claude response did not contain native structured output")
        self._last_usage = _usage_evidence(envelope, elapsed_ms)
        try:
            parsed = output_type.model_validate(structured_output)
        except ValidationError as exc:
            lifecycle_event(
                "claude_cli.output.rejected",
                phase="schema_validation",
                output_type=output_type.__name__,
                validation_errors=exc.error_count(),
            )
            raise ModelOutputError("Claude structured output failed schema validation") from exc
        lifecycle_event(
            "claude_cli.generation.completed",
            output_type=output_type.__name__,
            elapsed_ms=elapsed_ms,
        )
        return parsed

    def _invoke(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: Mapping[str, object],
    ) -> Mapping[str, object]:
        command = (
            self._executable,
            "--print",
            "--safe-mode",
            "--no-chrome",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--restricted",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "",
            "--system-prompt",
            system_prompt,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, sort_keys=True, separators=(",", ":")),
            "--model",
            self._model_id,
        )
        started_ns = time.perf_counter_ns()
        try:
            completed = subprocess.run(
                command,
                input=user_prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                cwd=self._workdir,
            )
        except FileNotFoundError as exc:
            raise ModelRuntimeError("the Claude CLI is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            lifecycle_event(
                "claude_cli.invoke.timeout",
                model_id=self._model_id,
                timeout_seconds=self._timeout_seconds,
            )
            raise ModelRuntimeError("Claude request exceeded its deadline") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModelRuntimeError("the Claude CLI is unavailable") from exc
        elapsed_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
        if completed.returncode != 0:
            lifecycle_event(
                "claude_cli.invoke.nonzero_exit",
                model_id=self._model_id,
                return_code=completed.returncode,
                elapsed_ms=elapsed_ms,
                stderr_digest=_stream_digest(completed.stderr),
            )
            raise ModelRuntimeError("the Claude CLI request did not complete")
        try:
            envelope = json.loads(completed.stdout)
        except (TypeError, ValueError, UnicodeError) as exc:
            lifecycle_event(
                "claude_cli.invoke.non_json_envelope",
                model_id=self._model_id,
                elapsed_ms=elapsed_ms,
                stdout_digest=_stream_digest(completed.stdout),
            )
            raise ModelOutputError("Claude CLI did not return a JSON envelope") from exc
        if not isinstance(envelope, dict):
            raise ModelOutputError("Claude CLI envelope was not a JSON object")
        if envelope.get("is_error") is True or envelope.get("subtype") != "success":
            lifecycle_event(
                "claude_cli.invoke.unsuccessful",
                model_id=self._model_id,
                elapsed_ms=elapsed_ms,
                subtype_digest=_stream_digest(str(envelope.get("subtype") or "unknown")),
                is_error=envelope.get("is_error") is True,
            )
            raise ModelRuntimeError("Claude did not complete the request successfully")
        return envelope


def _usage_evidence(envelope: Mapping[str, object], elapsed_ms: int) -> ModelUsageEvidence:
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return ModelUsageEvidence(latency_ms=elapsed_ms, provider_usage_reported=False)
    input_tokens = _token_sum(
        usage,
        ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"),
    )
    output_tokens = _token_sum(usage, ("output_tokens",))
    if input_tokens is None and output_tokens is None:
        return ModelUsageEvidence(latency_ms=elapsed_ms, provider_usage_reported=False)
    resolved_input = input_tokens or 0
    resolved_output = output_tokens or 0
    return ModelUsageEvidence(
        latency_ms=elapsed_ms,
        provider_usage_reported=True,
        input_tokens=resolved_input,
        output_tokens=resolved_output,
        total_tokens=resolved_input + resolved_output,
    )


def _token_sum(usage: Mapping[str, object], keys: tuple[str, ...]) -> int | None:
    total = 0
    seen = False
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            total += value
            seen = True
    return total if seen else None


__all__ = [
    "ClaudeCliStructuredModelClient",
    "DEFAULT_CLAUDE_TIMEOUT_SECONDS",
    "MAX_CLAUDE_TIMEOUT_SECONDS",
    "MIN_CLAUDE_TIMEOUT_SECONDS",
]
