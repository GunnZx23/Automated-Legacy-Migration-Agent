"""Provider-neutral structured model boundary and optional OpenAI adapter.

The package does not import an SDK at module import time.  A live OpenAI client
is created only after explicit API, credential, and data-sharing consent.  Role
agents expose no model tools; the adapter sends a system prompt plus canonical
JSON and requests a Pydantic structured response.
"""

from __future__ import annotations

import hashlib
import inspect
import re
import time
from typing import Any, Literal, Protocol, TypeAlias, TypeVar, cast

from pydantic import BaseModel, Field, model_validator

from legacy_migration_agent.contracts import Sha256Digest, StrictModel
from legacy_migration_agent.core.integrity import artifact_digest, canonical_json_bytes

OutputModel = TypeVar("OutputModel", bound=BaseModel)
ModelExecutionBoundary: TypeAlias = Literal[
    "offline_recorded",
    "remote_no_store",
    "remote_provider_managed",
    "local_loopback",
]


class ModelRuntimeError(RuntimeError):
    """Base class for configuration, refusal, and malformed-output failures."""


class ModelConfigurationError(ModelRuntimeError):
    """Raised before any call when live model use was not explicitly enabled."""


class ModelRefusalError(ModelRuntimeError):
    """Raised when the provider returns a refusal instead of structured output."""


class ModelOutputError(ModelRuntimeError):
    """Raised when a response is incomplete, missing, or fails its schema."""


class ModelEvidenceError(ModelRuntimeError):
    """Raised when model-call evidence is incomplete or no longer replay-safe."""


class LiveModelApproval(StrictModel):
    """Explicit operator gate for sending capstone context to a live provider."""

    allow_live_api: Literal[True]
    allow_prompt_data_sharing: Literal[True]
    approved_by: str = Field(min_length=1, max_length=160)


class StructuredModelClient(Protocol):
    """Minimal provider-neutral interface used by all three role agents."""

    @property
    def provider(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    @property
    def live_invocation(self) -> bool: ...

    @property
    def store_false_sent(self) -> bool: ...

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[OutputModel],
    ) -> OutputModel: ...


class ModelUsageEvidence(StrictModel):
    """Sanitized provider telemetry for one completed structured response."""

    latency_ms: int = Field(ge=0)
    provider_usage_reported: bool
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_usage_counts(self) -> ModelUsageEvidence:
        counts = (self.input_tokens, self.output_tokens, self.total_tokens)
        if self.provider_usage_reported:
            if any(value is None for value in counts):
                raise ValueError("reported model usage requires complete token counts")
            assert self.input_tokens is not None
            assert self.output_tokens is not None
            assert self.total_tokens is not None
            if self.total_tokens != self.input_tokens + self.output_tokens:
                raise ValueError("model total tokens must equal input plus output tokens")
        elif any(value is not None for value in counts):
            raise ValueError("unreported model usage cannot contain token counts")
        return self


class ModelCallRecord(StrictModel):
    """Digest-only public evidence for one structured model invocation."""

    provider: str = Field(min_length=1, max_length=160)
    model_id: str = Field(min_length=1, max_length=300)
    agent_version: str = Field(min_length=1, max_length=80)
    agent_definition_digest: Sha256Digest
    live_invocation: bool
    store_false_sent: bool
    execution_boundary: ModelExecutionBoundary | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Explicit invocation boundary. Missing only on legacy records, where "
            "live_invocation determines remote versus offline semantics."
        ),
    )
    model_revision: Sha256Digest | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Provider revision observed around the completed call when the configured "
            "model ID is mutable."
        ),
    )
    runtime_identity_digest: Sha256Digest | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Digest binding the configured runtime identity. For a remote CLI this covers "
            "the executable version, model alias, and authenticated provider, not model weights."
        ),
    )
    live_approval: LiveModelApproval | None = None
    system_prompt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    usage: ModelUsageEvidence | None = None

    @model_validator(mode="after")
    def require_live_call_controls(self) -> ModelCallRecord:
        boundary = self.resolved_execution_boundary
        if boundary == "remote_no_store":
            if not self.live_invocation or not self.store_false_sent:
                raise ValueError("remote model records require provider storage control")
            if self.live_approval is None:
                raise ValueError("remote model records require exact approval evidence")
            if self.usage is None:
                raise ValueError("remote model records require measured call telemetry")
        elif boundary == "remote_provider_managed":
            if not self.live_invocation or self.store_false_sent:
                raise ValueError(
                    "provider-managed remote records cannot claim a no-store API control"
                )
            if self.live_approval is None:
                raise ValueError("remote model records require exact approval evidence")
            if self.usage is None:
                raise ValueError("remote model records require measured call telemetry")
            if self.model_revision is not None:
                raise ValueError(
                    "provider-managed remote records cannot claim an observed model revision"
                )
            if self.runtime_identity_digest is None:
                raise ValueError("provider-managed remote records require runtime identity")
        elif boundary == "local_loopback":
            # ``live_invocation`` predates local inference and means a live
            # remote-provider call.  Local execution is recorded explicitly by
            # this boundary instead of pretending ``store=False`` was sent.
            if self.live_invocation or self.store_false_sent:
                raise ValueError("local loopback records cannot claim remote storage controls")
            if self.live_approval is None:
                raise ValueError("local loopback records require exact approval evidence")
            if self.usage is None:
                raise ValueError("local loopback records require measured call telemetry")
            if self.model_revision is None:
                raise ValueError("local loopback records require an observed model revision")
            if (
                self.runtime_identity_digest is not None
                and self.runtime_identity_digest != self.model_revision
            ):
                raise ValueError("local runtime identity must equal its observed model revision")
        else:
            if self.live_invocation:
                raise ValueError("offline model records cannot claim remote invocation")
            if self.live_approval is not None:
                raise ValueError("offline model records cannot carry live approval evidence")
        return self

    @property
    def resolved_runtime_identity_digest(self) -> Sha256Digest | None:
        """Return the explicit runtime identity or the legacy local revision binding."""

        return self.runtime_identity_digest or self.model_revision

    @property
    def resolved_execution_boundary(self) -> ModelExecutionBoundary:
        """Return explicit semantics, inferring only legacy records."""

        if self.execution_boundary is not None:
            return self.execution_boundary
        return "remote_no_store" if self.live_invocation else "offline_recorded"


def model_call_record(
    client: StructuredModelClient,
    *,
    agent_version: str,
    agent_definition_digest: Sha256Digest,
    system_prompt: str,
    input_value: BaseModel,
    output_value: BaseModel,
) -> ModelCallRecord:
    """Build a reproducible invocation record without storing prompt content."""

    boundary = model_execution_boundary(client)
    if boundary == "remote_no_store" and not client.store_false_sent:
        raise ModelEvidenceError(
            "live model invocation did not prove that provider storage was disabled"
        )
    raw_usage = getattr(client, "last_usage", None)
    usage = (
        ModelUsageEvidence.model_validate(raw_usage.model_dump(mode="python"))
        if isinstance(raw_usage, ModelUsageEvidence)
        else None
    )
    if boundary != "offline_recorded" and usage is None:
        raise ModelEvidenceError("model invocation did not retain measured call telemetry")
    raw_approval = getattr(client, "live_approval", None)
    approval = (
        LiveModelApproval.model_validate(raw_approval.model_dump(mode="python"))
        if isinstance(raw_approval, LiveModelApproval)
        else None
    )
    if boundary != "offline_recorded" and approval is None:
        raise ModelEvidenceError("model invocation did not retain its approval evidence")
    if boundary == "offline_recorded" and approval is not None:
        raise ModelEvidenceError("offline model invocation cannot carry live approval evidence")
    raw_revision = getattr(client, "model_revision", None)
    revision: str | None = None
    if raw_revision is not None:
        if (
            not isinstance(raw_revision, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", raw_revision) is None
        ):
            raise ModelEvidenceError("model invocation exposed an invalid observed revision")
        revision = raw_revision
    if boundary == "local_loopback" and revision is None:
        raise ModelEvidenceError("local model invocation did not retain an observed revision")
    raw_runtime_identity = getattr(client, "runtime_identity_digest", None)
    runtime_identity: str | None = None
    if raw_runtime_identity is not None:
        if (
            not isinstance(raw_runtime_identity, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", raw_runtime_identity) is None
        ):
            raise ModelEvidenceError("model invocation exposed an invalid runtime identity")
        runtime_identity = raw_runtime_identity
    if boundary == "local_loopback" and runtime_identity is None:
        runtime_identity = revision
    if boundary == "remote_provider_managed" and runtime_identity is None:
        raise ModelEvidenceError("remote CLI invocation did not retain its runtime identity")
    return ModelCallRecord(
        provider=client.provider,
        model_id=client.model_id,
        agent_version=agent_version,
        agent_definition_digest=agent_definition_digest,
        live_invocation=client.live_invocation,
        store_false_sent=client.store_false_sent,
        execution_boundary=boundary,
        model_revision=revision,
        runtime_identity_digest=runtime_identity,
        live_approval=approval,
        system_prompt_digest=_text_digest(system_prompt),
        input_digest=artifact_digest(input_value),
        output_digest=artifact_digest(output_value),
        usage=usage,
    )


def verify_model_call_record(
    record: ModelCallRecord,
    *,
    agent_version: str,
    agent_definition_digest: Sha256Digest,
    system_prompt: str,
    input_value: BaseModel,
    output_value: BaseModel,
) -> None:
    """Verify a persisted call against the exact prompt, input, and output.

    The provider response itself is deliberately not replayed.  This boundary
    proves that the immutable structured artifact still belongs to the current
    Markdown agent definition and frozen context. The strict OpenAI boundary
    additionally verifies its explicit ``store=False`` control; provider-managed
    remote runtimes retain their separately validated approval and boundary facts.
    """

    mismatches: list[str] = []
    if record.agent_version != agent_version:
        mismatches.append("agent version")
    if record.agent_definition_digest != agent_definition_digest:
        mismatches.append("agent definition")
    if record.system_prompt_digest != _text_digest(system_prompt):
        mismatches.append("system prompt")
    if record.input_digest != artifact_digest(input_value):
        mismatches.append("input")
    if record.output_digest != artifact_digest(output_value):
        mismatches.append("output")
    if record.resolved_execution_boundary == "remote_no_store" and not record.store_false_sent:
        mismatches.append("live provider storage control")
    if mismatches:
        raise ModelEvidenceError(
            "model-call evidence does not match the replay boundary: " + ", ".join(mismatches)
        )


class OpenAIResponsesModelClient:
    """Structured OpenAI Responses API adapter with lazy SDK import.

    Supplying ``client`` with ``live_invocation=False`` is the only offline test
    path.  A real client requires the caller to set ``live_invocation=True``,
    provide a non-empty API key, and supply :class:`LiveModelApproval`.
    """

    provider = "openai"

    def __init__(
        self,
        model_id: str,
        *,
        client: Any | None = None,
        live_invocation: bool = False,
        api_key: str | None = None,
        approval: LiveModelApproval | None = None,
    ) -> None:
        normalized_model = model_id.strip()
        if not normalized_model or len(normalized_model) > 300:
            raise ModelConfigurationError("an explicit model_id is required")
        if any(character in normalized_model for character in ("\x00", "\r", "\n")):
            raise ModelConfigurationError("model_id contains a forbidden control character")
        self._model_id = normalized_model
        self._live_invocation = live_invocation
        self._store_false_sent = False
        self._last_usage: ModelUsageEvidence | None = None
        self._live_approval = (
            LiveModelApproval.model_validate(approval.model_dump(mode="python"))
            if approval is not None
            else None
        )

        if live_invocation:
            if approval is None:
                raise ModelConfigurationError(
                    "live model use requires explicit API and prompt-data-sharing approval"
                )
            if not api_key or not api_key.strip():
                raise ModelConfigurationError("live model use requires an explicit API key")
        elif approval is not None or api_key is not None:
            raise ModelConfigurationError(
                "credentials or live approval cannot be supplied to an offline model client"
            )

        if client is not None:
            self._client = client
        else:
            if not live_invocation:
                raise ModelConfigurationError(
                    "offline use requires an injected test client; live use must be explicitly enabled"
                )
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ModelConfigurationError(
                    "the OpenAI SDK is not installed; install an approved SDK version before live use"
                ) from exc
            self._client = OpenAI(api_key=api_key)

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def live_invocation(self) -> bool:
        return self._live_invocation

    @property
    def store_false_sent(self) -> bool:
        return self._store_false_sent

    @property
    def execution_boundary(self) -> ModelExecutionBoundary:
        return "remote_no_store" if self.live_invocation else "offline_recorded"

    @property
    def last_usage(self) -> ModelUsageEvidence | None:
        return self._last_usage

    @property
    def live_approval(self) -> LiveModelApproval | None:
        return self._live_approval

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

        self._last_usage = None
        parse_method = self._client.responses.parse
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "instructions": system_prompt,
            "input": canonical_json_bytes(input_value).decode("utf-8"),
            "text_format": output_type,
        }
        if _accepts_keyword(parse_method, "store"):
            kwargs["store"] = False
            self._store_false_sent = True
        else:
            self._store_false_sent = False

        # Deliberately no `tools`, `tool_choice`, shell, network, or file handle.
        started_ns = time.perf_counter_ns()
        response = parse_method(**kwargs)
        elapsed_ns = time.perf_counter_ns() - started_ns
        self._last_usage = _model_usage_evidence(response, elapsed_ns)
        refusal = _find_refusal(response)
        if refusal is not None:
            raise ModelRefusalError(f"model refused structured output: {refusal}")
        status = getattr(response, "status", None)
        if status == "incomplete":
            reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
            suffix = f": {reason}" if reason else ""
            raise ModelOutputError("model response was incomplete" + suffix)

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            parsed = _find_parsed_output(response)
        if parsed is None:
            raise ModelOutputError("model response did not contain parsed structured output")
        try:
            if isinstance(parsed, BaseModel):
                return output_type.model_validate(parsed.model_dump(mode="python"))
            return output_type.model_validate(parsed)
        except ValueError as exc:
            raise ModelOutputError("model structured output failed schema validation") from exc


def _accepts_keyword(callable_value: Any, keyword: str) -> bool:
    """Return whether a callable advertises a keyword or arbitrary kwargs."""

    try:
        signature = inspect.signature(callable_value)
    except (TypeError, ValueError):
        # Generated SDK callables may not expose an inspectable signature.  The
        # current Responses API accepts `store`; fail closed if that is untrue.
        return True
    parameters = signature.parameters
    return keyword in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _find_refusal(response: Any) -> str | None:
    for item in getattr(response, "output", ()) or ():
        for content in getattr(item, "content", ()) or ():
            content_type = _value(content, "type")
            refusal = _value(content, "refusal")
            if content_type == "refusal" or refusal:
                text = refusal or _value(content, "text") or "provider refusal"
                return str(text)[:500]
    return None


def _find_parsed_output(response: Any) -> Any | None:
    for item in getattr(response, "output", ()) or ():
        for content in getattr(item, "content", ()) or ():
            parsed = _value(content, "parsed")
            if parsed is not None:
                return parsed
    return None


def _value(value: Any, name: str) -> Any | None:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _model_usage_evidence(response: Any, elapsed_ns: int) -> ModelUsageEvidence:
    latency_ms = max(0, elapsed_ns // 1_000_000)
    usage = getattr(response, "usage", None)
    if usage is None:
        return ModelUsageEvidence(
            latency_ms=latency_ms,
            provider_usage_reported=False,
        )
    input_tokens = _strict_usage_count(usage, "input_tokens")
    output_tokens = _strict_usage_count(usage, "output_tokens")
    total_tokens = _strict_usage_count(usage, "total_tokens")
    return ModelUsageEvidence(
        latency_ms=latency_ms,
        provider_usage_reported=True,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _strict_usage_count(usage: Any, field: str) -> int:
    value = _value(usage, field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelOutputError("model response usage contains an invalid token count")
    return value


def _text_digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def model_execution_boundary(client: StructuredModelClient) -> ModelExecutionBoundary:
    """Resolve boundary semantics while accepting pre-boundary test doubles."""

    value = getattr(client, "execution_boundary", None)
    if value is None:
        return "remote_no_store" if client.live_invocation else "offline_recorded"
    if value not in {
        "offline_recorded",
        "remote_no_store",
        "remote_provider_managed",
        "local_loopback",
    }:
        raise ModelEvidenceError("model client exposed an unknown execution boundary")
    return cast(ModelExecutionBoundary, value)
