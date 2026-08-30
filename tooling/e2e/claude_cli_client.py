"""Deprecated Claude/Ollama compatibility client retained for import safety.

This historical client is intentionally fail-closed. It must not invoke Claude
because it presents a remote provider as ``ollama`` / ``local_loopback`` and
would therefore emit false provider provenance. Use the truthful first-class
``legacy-migration-agent ui --claude-model ...`` path instead.

The former implementation remains below solely to preserve import compatibility
for private helpers during the transition. Every provider-capable method has an
unconditional deprecation guard before any validation, state access, or process
execution; there is no compatibility mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import NoReturn

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
from legacy_migration_agent.core.observability import lifecycle_event

# Matches OllamaStructuredModelClient's bounds so the same UI timeout config
# behaves predictably; a live Claude role call can be slow, so the ceiling is
# generous.
MIN_CLAUDE_TIMEOUT_SECONDS = 1.0
MAX_CLAUDE_TIMEOUT_SECONDS = 900.0
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 240.0
_MAX_SYSTEM_PROMPT_BYTES = 256 * 1024
_VERSION_PROBE_TIMEOUT_SECONDS = 30.0

DEPRECATION_MESSAGE = (
    "this historical Claude/Ollama compatibility shim is disabled because it would "
    "record false provider provenance; use the truthful first-class command "
    "`legacy-migration-agent ui --claude-model ...` instead"
)


def _raise_deprecated() -> NoReturn:
    """Reject every provider-capable path before it can invoke a subprocess."""

    raise ModelConfigurationError(DEPRECATION_MESSAGE)


def _resolve_claude_executable() -> str:
    """Return the ``claude`` executable path, honoring an explicit override."""

    override = os.environ.get("LIVE_CLAUDE_CLI_PATH", "").strip()
    if override:
        return override
    return "claude"


def _stderr_tail(stderr: str | None, *, limit: int = 600) -> str:
    """Return the tail of a CLI stderr stream for legible failure diagnostics."""

    text = (stderr or "").strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


class ClaudeCliStructuredModelClient:
    """Inert compatibility type whose provider-capable methods always reject."""

    # The inert identity cannot be mistaken for the historical Ollama seam. No
    # instance can be constructed and every provider-capable method fails closed.
    provider = "disabled-deprecated-shim"
    live_invocation = False
    store_false_sent = False
    execution_boundary: ModelExecutionBoundary = "offline_recorded"

    def __init__(
        self,
        model_id: str,
        *,
        approval: LiveModelApproval,
        timeout_seconds: float = DEFAULT_CLAUDE_TIMEOUT_SECONDS,
    ) -> None:
        _raise_deprecated()
        normalized_model = model_id.strip()
        if not normalized_model or len(normalized_model) > 300:
            raise ModelConfigurationError("an explicit model_id is required")
        if any(character in normalized_model for character in ("\x00", "\r", "\n")):
            raise ModelConfigurationError("model_id contains a forbidden control character")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ModelConfigurationError("claude timeout must be a number")
        normalized_timeout = float(timeout_seconds)
        if not MIN_CLAUDE_TIMEOUT_SECONDS <= normalized_timeout <= MAX_CLAUDE_TIMEOUT_SECONDS:
            raise ModelConfigurationError("claude timeout is outside the supported range")
        if not isinstance(approval, LiveModelApproval):
            raise ModelConfigurationError("live model use requires explicit approval")
        self._model_id = normalized_model
        self._timeout_seconds = normalized_timeout
        self._executable = _resolve_claude_executable()
        # Run the nested CLI from a neutral directory so it does not auto-discover
        # this repository's CLAUDE.md / .claude hooks into the completion.
        self._workdir = tempfile.gettempdir()
        self._live_approval = LiveModelApproval.model_validate(approval.model_dump(mode="python"))
        self._last_usage: ModelUsageEvidence | None = None
        self._model_revision: str | None = None
        self._cli_version: str | None = None

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_revision(self) -> str | None:
        return self._model_revision

    @property
    def live_approval(self) -> LiveModelApproval:
        return self._live_approval

    @property
    def last_usage(self) -> ModelUsageEvidence | None:
        return self._last_usage

    def bind_model_revision(self, expected_revision: str) -> None:
        """Bind a recorded revision, mirroring the Ollama client's contract."""

        _raise_deprecated()
        if len(expected_revision) != 71 or not expected_revision.startswith("sha256:"):
            raise ModelConfigurationError("recorded model revision is invalid")
        current = self._resolve_model_revision(timeout_seconds=self._timeout_seconds)
        if current != expected_revision:
            raise ModelConfigurationError("live claude model revision changed")
        if self._model_revision is not None and self._model_revision != expected_revision:
            raise ModelConfigurationError("live claude model revision changed")
        self._model_revision = expected_revision

    def _resolve_model_revision(self, *, timeout_seconds: float) -> str:
        """Prove the CLI is runnable and derive a stable identity digest.

        There is no weight digest for a remote model, so the "revision" is a
        deterministic identity over the model id and the CLI version. Probing
        ``claude --version`` doubles as the liveness check the UI's readiness
        endpoint relies on: if the CLI cannot run, readiness reports the runtime
        unreachable and the browser keeps Send/Start disabled.
        """

        _raise_deprecated()
        probe_timeout = min(float(timeout_seconds), _VERSION_PROBE_TIMEOUT_SECONDS)
        try:
            completed = subprocess.run(
                [self._executable, "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=max(probe_timeout, 1.0),
                cwd=self._workdir,
            )
        except FileNotFoundError as exc:
            raise ModelRuntimeError("the local claude CLI is not installed") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModelRuntimeError("the local claude CLI is unavailable") from exc
        if completed.returncode != 0:
            raise ModelRuntimeError("the local claude CLI is unavailable")
        version = completed.stdout.strip() or "unknown"
        self._cli_version = version
        digest = hashlib.sha256(
            b"claude-cli\x00" + self._model_id.encode("utf-8") + b"\x00" + version.encode("utf-8")
        ).hexdigest()
        return "sha256:" + digest

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[OutputModel],
    ) -> OutputModel:
        _raise_deprecated()
        if not isinstance(input_value, BaseModel):
            raise TypeError("structured model input must be a Pydantic model")
        if not isinstance(output_type, type) or not issubclass(output_type, BaseModel):
            raise TypeError("output_type must be a Pydantic model class")
        if not system_prompt.strip() or "\x00" in system_prompt:
            raise ModelConfigurationError("system prompt must be non-empty text without NUL bytes")
        if len(system_prompt.encode("utf-8")) > _MAX_SYSTEM_PROMPT_BYTES:
            raise ModelConfigurationError("system prompt exceeds the size limit")

        # Liveness + revision before spending a generation attempt (mirrors the
        # Ollama client's before-generation inventory probe).
        if self._model_revision is None:
            self._model_revision = self._resolve_model_revision(
                timeout_seconds=self._timeout_seconds
            )

        schema = output_type.model_json_schema(mode="validation")
        user_prompt = (
            "You are producing one structured output for an automated legacy-migration "
            "workflow. The following JSON is your complete, authoritative input:\n\n"
            f"{input_value.model_dump_json(indent=2)}\n\n"
            "Respond with a SINGLE JSON object and nothing else — no prose, no explanation, "
            "no markdown code fences. Populate every required field. The object MUST validate "
            "against this JSON Schema:\n\n"
            f"{json.dumps(schema, indent=2)}"
        )

        lifecycle_event(
            "claude_cli.generation.started",
            model_id=self._model_id,
            output_type=output_type.__name__,
        )
        started_ns = time.perf_counter_ns()
        envelope = self._invoke(system_prompt=system_prompt, user_prompt=user_prompt)
        elapsed_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)

        result_text = envelope.get("result")
        if not isinstance(result_text, str) or not result_text.strip():
            raise ModelOutputError("live claude response did not contain structured text")
        json_text = _extract_json_object(result_text)
        self._last_usage = _usage_evidence(envelope, elapsed_ms)
        try:
            model = output_type.model_validate_json(json_text)
        except ValidationError as exc:
            lifecycle_event(
                "claude_cli.output.rejected",
                phase="schema_validation",
                output_type=output_type.__name__,
                validation_errors=exc.error_count(),
            )
            raise ModelOutputError(
                "live claude structured output failed schema validation"
            ) from exc
        lifecycle_event(
            "claude_cli.generation.completed",
            output_type=output_type.__name__,
            elapsed_ms=elapsed_ms,
        )
        return model

    def _invoke(self, *, system_prompt: str, user_prompt: str) -> dict[str, object]:
        _raise_deprecated()
        command = [
            self._executable,
            "-p",
            "--system-prompt",
            system_prompt,
            "--output-format",
            "json",
            "--model",
            self._model_id,
            "--strict-mcp-config",
            "--permission-mode",
            "default",
            "--allowed-tools",
            "",
        ]
        started_ns = time.perf_counter_ns()
        try:
            completed = subprocess.run(
                command,
                input=user_prompt,
                stdin=None,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                cwd=self._workdir,
            )
        except FileNotFoundError as exc:
            raise ModelRuntimeError("the local claude CLI is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            lifecycle_event(
                "claude_cli.invoke.timeout",
                model_id=self._model_id,
                timeout_seconds=self._timeout_seconds,
            )
            raise ModelRuntimeError(
                f"live claude request exceeded its {self._timeout_seconds:g}s deadline"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModelRuntimeError("the local claude CLI is unavailable") from exc
        elapsed_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
        if completed.returncode != 0:
            lifecycle_event(
                "claude_cli.invoke.nonzero_exit",
                model_id=self._model_id,
                return_code=completed.returncode,
                elapsed_ms=elapsed_ms,
                stderr=_stderr_tail(completed.stderr),
            )
            raise ModelRuntimeError(
                f"the local claude CLI exited with status {completed.returncode}"
            )
        try:
            envelope = json.loads(completed.stdout)
        except (ValueError, UnicodeError) as exc:
            lifecycle_event(
                "claude_cli.invoke.non_json_envelope",
                model_id=self._model_id,
                elapsed_ms=elapsed_ms,
                stdout_preview=(completed.stdout or "")[:400],
                stderr=_stderr_tail(completed.stderr),
            )
            raise ModelOutputError("live claude did not return a JSON envelope") from exc
        if not isinstance(envelope, dict):
            raise ModelOutputError("live claude envelope was not a JSON object")
        if envelope.get("is_error") is True or envelope.get("subtype") != "success":
            lifecycle_event(
                "claude_cli.invoke.unsuccessful",
                model_id=self._model_id,
                elapsed_ms=elapsed_ms,
                subtype=envelope.get("subtype"),
                is_error=envelope.get("is_error"),
                num_turns=envelope.get("num_turns"),
                result_preview=(str(envelope.get("result") or ""))[:400],
            )
            raise ModelRuntimeError(
                "live claude did not complete the request successfully "
                f"(subtype={envelope.get('subtype')!r}, is_error={envelope.get('is_error')!r})"
            )
        return envelope


def _extract_json_object(text: str) -> str:
    """Return the outermost JSON object from a model reply, tolerating fences."""

    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop an opening ```json / ``` fence and any trailing fence.
        newline = stripped.find("\n")
        if newline != -1:
            stripped = stripped[newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
        stripped = stripped.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ModelOutputError("live claude response did not contain a JSON object")
    return stripped[start : end + 1]


def _usage_evidence(envelope: dict[str, object], elapsed_ms: int) -> ModelUsageEvidence:
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


def _token_sum(usage: dict[str, object], keys: tuple[str, ...]) -> int | None:
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


def main(argv: list[str] | None = None) -> int:
    """Reject direct execution with the same fail-closed migration guidance."""

    del argv
    print(f"[claude_cli_client] Disabled: {DEPRECATION_MESSAGE}", file=sys.stderr, flush=True)
    return 2


__all__ = [
    "ClaudeCliStructuredModelClient",
    "DEPRECATION_MESSAGE",
    "DEFAULT_CLAUDE_TIMEOUT_SECONDS",
]


if __name__ == "__main__":
    raise SystemExit(main())
