"""Model-client admission, sanitization, and failure classification.

This boundary owns everything that can observe a provider exception.  The
orchestration facade receives only validated model outputs or a fixed,
controller-owned failure classification.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from legacy_migration_agent.agent_runtime.model_workflow import (
    DeterministicValidator,
    SanitizedModelPolicyError,
)
from legacy_migration_agent.agent_runtime.ollama_model import (
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    OllamaStructuredModelClient,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelExecutionBoundary,
    ModelOutputError,
    ModelRefusalError,
    ModelRuntimeError,
    ModelUsageEvidence,
    OpenAIResponsesModelClient,
    OutputModel,
    StructuredModelClient,
    model_execution_boundary,
)
from legacy_migration_agent.application.agent_run_contracts import (
    AgentRunFailureCategory,
    AgentRunFailureReason,
    AgentRunFailureSeam,
)
from legacy_migration_agent.contracts import (
    ChangeSet,
    MigrationManifest,
    MigrationRequest,
    Sha256Digest,
    ValidationReport,
)
from legacy_migration_agent.core.observability import lifecycle_event
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.redaction import SecretRedactor
from legacy_migration_agent.core.workspace import IsolatedWorkspace

_API_KEY_ENVIRONMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,159}$")
_API_KEY_TOKEN = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b")
_PUBLIC_IDENTIFIER = re.compile(r"^[^\x00\r\n]{1,300}$")
_LIVE_AUTHORIZATION = object()


class _ControlledOperationError(RuntimeError):
    """Fixed-message error safe to cross the durable workflow boundary."""

    def __init__(
        self,
        category: AgentRunFailureCategory,
        seam: AgentRunFailureSeam,
        reason_code: AgentRunFailureReason | None = None,
    ) -> None:
        self.category = category
        self.seam = seam
        self.reason_code = reason_code or _default_failure_reason(category, seam)
        super().__init__(f"agent_run_operation_failure:{seam}:{category}")


@dataclass
class _OperationSeamTracker:
    """Remember the active trusted role seam without retaining exception text."""

    seam: AgentRunFailureSeam | None = None
    attempt: int | None = None


@dataclass(frozen=True)
class AgentRunModelClients:
    """One three-role client bundle with a single immutable public identity.

    Use :meth:`offline` only with recorded or non-live test doubles. Runtime
    bundles are created exclusively by one of the approved provider builders.
    """

    architect: StructuredModelClient
    engineer: StructuredModelClient
    validator: StructuredModelClient
    _live_authorization: object | None = None
    _live_approval: LiveModelApproval | None = None
    _provider_id: str = field(init=False, repr=False)
    _model_id: str = field(init=False, repr=False)
    _live_invocation: bool = field(init=False, repr=False)
    _execution_boundary: ModelExecutionBoundary = field(init=False, repr=False)

    def __post_init__(self) -> None:
        clients = (self.architect, self.engineer, self.validator)
        identities = {(client.provider, client.model_id) for client in clients}
        if len(identities) != 1:
            raise ModelConfigurationError(
                "all three role clients must use the same provider and model identity"
            )
        provider, model_id = next(iter(identities))
        _validate_public_model_identifier(provider, role="provider")
        _validate_public_model_identifier(model_id, role="model")
        live_flags = tuple(client.live_invocation for client in clients)
        if any(live_flags) != all(live_flags):
            raise ModelConfigurationError("role clients cannot mix live and offline invocation")
        boundaries = tuple(model_execution_boundary(client) for client in clients)
        if len(set(boundaries)) != 1:
            raise ModelConfigurationError("role clients cannot mix execution boundaries")
        boundary = boundaries[0]
        if boundary == "remote_no_store" and not all(live_flags):
            raise ModelConfigurationError("remote role clients must identify remote invocation")
        if boundary != "remote_no_store" and any(live_flags):
            raise ModelConfigurationError("non-remote role clients cannot claim remote invocation")
        if boundary != "offline_recorded":
            if self._live_authorization is not _LIVE_AUTHORIZATION:
                raise ModelConfigurationError(
                    "model-runtime clients must be created by an approved provider builder"
                )
            if not all(client is clients[0] for client in clients):
                raise ModelConfigurationError("model-runtime roles must share one approved client")
            if self._live_approval is None:
                raise ModelConfigurationError(
                    "model-runtime role clients require approval evidence"
                )
        elif self._live_authorization is not None:
            raise ModelConfigurationError("offline clients cannot carry live authorization")
        elif self._live_approval is not None:
            raise ModelConfigurationError("offline clients cannot carry live approval evidence")
        object.__setattr__(self, "_provider_id", provider)
        object.__setattr__(self, "_model_id", model_id)
        object.__setattr__(self, "_live_invocation", live_flags[0])
        object.__setattr__(self, "_execution_boundary", boundary)

    @classmethod
    def offline(
        cls,
        *,
        architect: StructuredModelClient,
        engineer: StructuredModelClient,
        validator: StructuredModelClient,
    ) -> AgentRunModelClients:
        """Bind provider-neutral, non-live model doubles for local tests."""

        clients = (architect, engineer, validator)
        if any(client.live_invocation for client in clients) or any(
            model_execution_boundary(client) != "offline_recorded" for client in clients
        ):
            raise ModelConfigurationError("offline model injection cannot contain a runtime client")
        return cls(architect=architect, engineer=engineer, validator=validator)

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def live_invocation(self) -> bool:
        return self._live_invocation

    @property
    def execution_boundary(self) -> ModelExecutionBoundary:
        return self._execution_boundary

    @property
    def live_approval(self) -> LiveModelApproval | None:
        return self._live_approval

    def bind_recorded_model_revision(self, revision: Sha256Digest) -> None:
        """Bind a local runtime only to immutable evidence loaded by the controller."""

        if self.execution_boundary != "local_loopback":
            raise ModelConfigurationError("only a local runtime can bind a local model revision")
        binder = getattr(self.architect, "bind_model_revision", None)
        if not callable(binder):
            raise ModelConfigurationError("local runtime cannot bind the recorded model revision")
        binder(revision)


class _SanitizedModelClient:
    """Prevent provider exceptions from crossing into durable state verbatim."""

    def __init__(
        self,
        client: StructuredModelClient,
        *,
        role: Literal["architect", "engineer", "validator"],
        provider_id: str,
        model_id: str,
        live_invocation: bool,
        execution_boundary: ModelExecutionBoundary,
        live_approval: LiveModelApproval | None,
        seam_tracker: _OperationSeamTracker,
    ) -> None:
        self._client = client
        self._role = role
        self._provider_id = provider_id
        self._model_id = model_id
        self._live_invocation = live_invocation
        self._execution_boundary = execution_boundary
        self._live_approval = live_approval
        self._seam_tracker = seam_tracker

    @property
    def provider(self) -> str:
        return self._provider_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def live_invocation(self) -> bool:
        return self._live_invocation

    @property
    def store_false_sent(self) -> bool:
        category: AgentRunFailureCategory | None = None
        value: bool | None = None
        try:
            observed = self._client.store_false_sent
            if not isinstance(observed, bool):
                raise TypeError("model storage evidence must be boolean")
            value = observed
        except Exception as error:
            category = _classify_model_exception(error)
        if category is not None:
            raise _ControlledOperationError(category, self._role)
        if value is None:  # pragma: no cover - defensive type guard
            raise _ControlledOperationError("internal", self._role)
        return value

    @property
    def execution_boundary(self) -> ModelExecutionBoundary:
        return self._execution_boundary

    @property
    def last_usage(self) -> ModelUsageEvidence | None:
        """Expose only a validated copy of the underlying call telemetry."""

        category: AgentRunFailureCategory | None = None
        usage: ModelUsageEvidence | None = None
        try:
            raw = getattr(self._client, "last_usage", None)
            if raw is not None:
                if not isinstance(raw, ModelUsageEvidence):
                    raise TypeError("model usage evidence has an invalid type")
                usage = ModelUsageEvidence.model_validate(raw.model_dump(mode="python"))
        except Exception as error:
            category = _classify_model_exception(error)
        if category is not None:
            raise _ControlledOperationError(category, self._role)
        return usage

    @property
    def model_revision(self) -> str | None:
        """Expose the underlying observed revision without provider prose."""

        category: AgentRunFailureCategory | None = None
        revision: str | None = None
        try:
            raw = getattr(self._client, "model_revision", None)
            if raw is not None and not isinstance(raw, str):
                raise TypeError("model revision evidence has an invalid type")
            revision = raw
        except Exception as error:
            category = _classify_model_exception(error)
        if category is not None:
            raise _ControlledOperationError(category, self._role)
        return revision

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
        self._seam_tracker.seam = self._role
        started_ns = time.perf_counter_ns()
        lifecycle_event(
            "model.call.started",
            role=self._role,
            provider=self._provider_id,
            output_contract=output_type.__name__,
            attempt=self._seam_tracker.attempt,
        )
        category: AgentRunFailureCategory | None = None
        reason_code: AgentRunFailureReason | None = None
        parsed: OutputModel | None = None
        try:
            raw = self._client.parse(
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )
            parsed = output_type.model_validate(raw.model_dump(mode="python"))
        except Exception as error:
            category = _classify_model_exception(error)
            reason_code = _model_failure_reason(error, category)
        if category is not None:
            lifecycle_event(
                "model.call.failed",
                level=logging.WARNING,
                role=self._role,
                category=category,
                reason_code=reason_code,
                attempt=self._seam_tracker.attempt,
                elapsed_ms=_elapsed_milliseconds(started_ns),
            )
            raise _ControlledOperationError(category, self._role, reason_code)
        if parsed is None:  # pragma: no cover - Pydantic outputs cannot be None
            lifecycle_event(
                "model.call.failed",
                level=logging.ERROR,
                role=self._role,
                category="internal",
                reason_code="internal_failure",
                attempt=self._seam_tracker.attempt,
                elapsed_ms=_elapsed_milliseconds(started_ns),
            )
            raise _ControlledOperationError("internal", self._role)
        lifecycle_event(
            "model.call.completed",
            role=self._role,
            attempt=self._seam_tracker.attempt,
            elapsed_ms=_elapsed_milliseconds(started_ns),
        )
        return parsed


class _SanitizedDeterministicValidator:
    """Apply the fixed error boundary to controller validation seams."""

    def __init__(
        self,
        validator: DeterministicValidator,
        seam_tracker: _OperationSeamTracker,
    ) -> None:
        self._validator = validator
        self._seam_tracker = seam_tracker

    def __call__(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        workspace: IsolatedWorkspace,
        attempt: int,
    ) -> ValidationReport:
        self._seam_tracker.seam = "deterministic_validator"
        started_ns = time.perf_counter_ns()
        lifecycle_event(
            "validation.started",
            attempt=attempt,
            planned_checks=len(manifest.validation_plan),
            changed_files=len(change_set.changed_paths),
        )
        failed = False
        error_type: str | None = None
        report: ValidationReport | None = None
        try:
            raw = self._validator(request, manifest, change_set, workspace, attempt)
            report = ValidationReport.model_validate(raw.model_dump(mode="python"))
        except Exception as error:
            failed = True
            error_type = type(error).__name__
        if failed or report is None:
            lifecycle_event(
                "validation.failed",
                level=logging.ERROR,
                attempt=attempt,
                reason_code="deterministic_validation_failed",
                error_type=error_type,
                elapsed_ms=_elapsed_milliseconds(started_ns),
            )
            raise _ControlledOperationError("internal", "deterministic_validator")
        known_validation_ids = {
            identifier
            for result in report.results
            for identifier in (result.check_id, result.command_id)
        }
        for result in report.results:
            diagnostic_ids = ",".join(result.diagnostic_ids) or "none"
            failure_code = _validation_failure_code(result.summary)
            blocked_by = _validation_prerequisite_ids(result.summary, known_validation_ids)
            lifecycle_event(
                "validation.check.completed",
                attempt=attempt,
                check_id=result.check_id,
                command_id=result.command_id,
                required=result.required,
                status=result.status.value,
                exit_code=(result.receipt.exit_code if result.receipt is not None else None),
                duration_ms=_validation_receipt_milliseconds(result),
                failure_code=failure_code,
                diagnostic_ids=diagnostic_ids,
                blocked_by=",".join(blocked_by) or "none",
            )
            for diagnostic_id in result.diagnostic_ids:
                lifecycle_event(
                    "validation.check.diagnostic",
                    attempt=attempt,
                    check_id=result.check_id,
                    diagnostic_id=diagnostic_id,
                )
            for prerequisite_id in blocked_by:
                lifecycle_event(
                    "validation.check.prerequisite_blocked",
                    attempt=attempt,
                    check_id=result.check_id,
                    prerequisite_id=prerequisite_id,
                )
        required_nonpass = tuple(
            result.check_id
            for result in report.results
            if result.required and result.status.value != "passed"
        )
        aggregate_diagnostics = tuple(
            dict.fromkeys(
                diagnostic_id
                for result in report.results
                for diagnostic_id in result.diagnostic_ids
            )
        )
        lifecycle_event(
            "validation.completed",
            attempt=attempt,
            disposition=report.disposition.value,
            checks=len(report.results),
            passed=sum(result.status.value == "passed" for result in report.results),
            failed=sum(result.status.value == "failed" for result in report.results),
            unavailable=sum(result.status.value == "unavailable" for result in report.results),
            nonterminal=sum(result.status.value == "nonterminal" for result in report.results),
            required_nonpass=",".join(required_nonpass) or "none",
            diagnostic_ids=",".join(aggregate_diagnostics) or "none",
            elapsed_ms=_elapsed_milliseconds(started_ns),
        )
        return report


def _classify_model_exception(error: Exception) -> AgentRunFailureCategory:
    if isinstance(error, ModelConfigurationError):
        return "configuration"
    if isinstance(error, ModelRefusalError):
        return "refusal"
    if isinstance(error, ModelOutputError):
        first_argument = error.args[0] if error.args else None
        if isinstance(first_argument, str) and first_argument.startswith(
            "model response was incomplete"
        ):
            return "incomplete"
        return "invalid"
    if isinstance(error, (TypeError, ValueError)):
        return "invalid"
    return "provider_unavailable"


def _default_failure_reason(
    category: AgentRunFailureCategory,
    seam: AgentRunFailureSeam,
) -> AgentRunFailureReason:
    if seam == "deterministic_validator":
        return "deterministic_validation_failed"
    reasons: dict[AgentRunFailureCategory, AgentRunFailureReason] = {
        "configuration": "configuration_invalid",
        "refusal": "provider_refusal",
        "incomplete": "response_incomplete",
        "invalid": "structured_output_invalid",
        "provider_unavailable": "provider_unavailable",
        "internal": "internal_failure",
    }
    return reasons[category]


def _model_failure_reason(
    error: Exception,
    category: AgentRunFailureCategory,
) -> AgentRunFailureReason:
    if (
        category == "provider_unavailable"
        and isinstance(error, ModelRuntimeError)
        and str(error) == "local Ollama request exceeded its deadline"
    ):
        return "provider_timeout"
    if isinstance(error, ModelOutputError):
        message = str(error)
        if message == "local Ollama response attempted an unauthorized tool call":
            return "unauthorized_tool_call"
        if message.startswith("local Ollama inventory"):
            return "model_inventory_invalid"
        if (
            message == "model structured output failed schema validation"
            or message.startswith("local Ollama structured output")
            or message.startswith("local Ollama structured text")
            or message == "local Ollama response did not contain structured text"
        ):
            return "structured_output_invalid"
        if category == "invalid":
            return "provider_response_invalid"
    return _default_failure_reason(category, "architect")


def _operation_failure_reason(
    error: Exception,
    category: AgentRunFailureCategory,
    seam: AgentRunFailureSeam,
) -> AgentRunFailureReason:
    if seam == "deterministic_validator":
        return "deterministic_validation_failed"
    if isinstance(error, SanitizedModelPolicyError):
        return error.reason_code
    if isinstance(error, PolicyViolation):
        if seam == "architect" and str(error).startswith(
            "manifest implementation contract does not match"
        ):
            return "implementation_contract_invalid"
        if seam == "architect" and str(error).startswith(
            "manifest omits scope-required approval actions:"
        ):
            return "required_approval_missing"
        if seam == "architect" and _is_transformation_scope_failure(str(error)):
            return "transformation_scope_invalid"
        return "policy_rejected"
    return _default_failure_reason(category, seam)


def _is_transformation_scope_failure(message: str) -> bool:
    return message.startswith(
        (
            "Architect manifest approved paths must exactly equal transformation outputs",
            "manifest transformation ",
            "manifest transformations ",
            "manifest approved paths do not match the caller-owned output boundary",
            "each approved output must be owned by exactly one transformation",
            "manifest must contain at least one transformation",
        )
    )


def build_live_openai_model_clients(
    *,
    model_id: str,
    api_key_environment: str,
    approval: LiveModelApproval,
    client_factory: Callable[..., StructuredModelClient] = OpenAIResponsesModelClient,
) -> AgentRunModelClients:
    """Create a live, tool-free, ``store=False`` OpenAI bundle after all gates."""

    normalized_model = model_id.strip()
    if not normalized_model:
        raise ModelConfigurationError("an explicit model_id is required")
    variable = api_key_environment.strip()
    if _API_KEY_ENVIRONMENT.fullmatch(variable) is None:
        raise ModelConfigurationError("API key environment variable name is invalid")
    if not approval.approved_by.strip():
        raise ModelConfigurationError("live model approval requires a nonempty approved_by")
    api_key = os.environ.get(variable)
    if api_key is None or not api_key.strip():
        raise ModelConfigurationError(
            "the approved API key environment variable is missing or empty"
        )
    client = client_factory(
        normalized_model,
        live_invocation=True,
        api_key=api_key,
        approval=approval,
    )
    return AgentRunModelClients(
        architect=client,
        engineer=client,
        validator=client,
        _live_authorization=_LIVE_AUTHORIZATION,
        _live_approval=LiveModelApproval.model_validate(approval.model_dump(mode="python")),
    )


def build_local_ollama_model_clients(
    *,
    model_id: str,
    approval: LiveModelApproval,
    timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    client_factory: Callable[..., StructuredModelClient] = OllamaStructuredModelClient,
) -> AgentRunModelClients:
    """Create one approved, tool-free client for loopback Ollama inference."""

    client = client_factory(
        model_id,
        approval=approval,
        timeout_seconds=timeout_seconds,
    )
    return AgentRunModelClients(
        architect=client,
        engineer=client,
        validator=client,
        _live_authorization=_LIVE_AUTHORIZATION,
        _live_approval=LiveModelApproval.model_validate(approval.model_dump(mode="python")),
    )


def _elapsed_milliseconds(started_ns: int) -> int:
    return max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)


def _validation_receipt_milliseconds(result: Any) -> int | None:
    receipt = result.receipt
    if receipt is None:
        return None
    return max(0, int((receipt.ended_at - receipt.started_at).total_seconds() * 1000))


def _validation_failure_code(summary: str) -> str | None:
    match = re.search(r"\bfailure[-_]code=([a-z][a-z0-9_.:-]{0,159})\b", summary)
    return match.group(1) if match is not None else None


def _validation_prerequisite_ids(
    summary: str,
    known_validation_ids: set[str],
) -> tuple[str, ...]:
    marker = "required prerequisite checks did not pass:"
    if marker not in summary:
        return ()
    tail = summary.rsplit(marker, 1)[1].split(".", 1)[0]
    return tuple(
        dict.fromkeys(
            identifier.strip()
            for identifier in tail.split(",")
            if identifier.strip() in known_validation_ids
        )
    )


def _validate_public_model_identifier(value: str, *, role: str) -> None:
    if value != value.strip() or _PUBLIC_IDENTIFIER.fullmatch(value) is None:
        raise ModelConfigurationError(f"{role} identity is blank, oversized, or contains controls")
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", value):
        raise ModelConfigurationError(f"{role} identity cannot contain an absolute path")
    if SecretRedactor().redact(value).changed or _API_KEY_TOKEN.search(value) is not None:
        raise ModelConfigurationError(f"{role} identity cannot contain credential material")


class _PassiveModel:
    """Identity-only client used to deserialize status or reject a manifest."""

    def __init__(self, provider: str, model_id: str) -> None:
        self._provider = provider
        self._model_id = model_id

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def live_invocation(self) -> bool:
        return False

    @property
    def store_false_sent(self) -> bool:
        return False

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[OutputModel],
    ) -> OutputModel:
        del system_prompt, input_value, output_type
        raise ModelConfigurationError("provider-free run inspection cannot invoke a model")


def _passive_models(provider: str, model_id: str) -> AgentRunModelClients:
    client = _PassiveModel(provider, model_id)
    return AgentRunModelClients.offline(
        architect=client,
        engineer=client,
        validator=client,
    )


class _NeverValidate:
    def __call__(self, *args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise PolicyViolation("read-only run inspection cannot invoke validation")


__all__ = [
    "AgentRunModelClients",
    "build_live_openai_model_clients",
    "build_local_ollama_model_clients",
]
