"""Deterministic test doubles for Agent UI orchestration tests.

These helpers stand in for approved Ollama and Claude CLI calls only inside the
test suite. They are not imported by product code and make no model-quality
claim.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from mulesoft_candidate_factory import mulesoft_target_outputs
from pydantic import BaseModel
from salesforce_candidate_factory import (
    case_management_candidate_text_outputs,
    salesforce_candidate_text_outputs,
)

from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectConversationContext,
    ArchitectManifestProposal,
    ArchitectModelContext,
    ArchitectSemanticDecision,
    EngineerCorrectionProviderContext,
    EngineerFilePlan,
    EngineerFilePlanOutcome,
    EngineerFileUpdate,
    EngineerModelOutcome,
    EngineerWorkspaceContext,
    ValidatorEvidenceContext,
    ValidatorModelAdvisory,
    _ArchitectConversationClarificationOutput,
    _ArchitectConversationReadyOutput,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelUsageEvidence,
    OutputModel,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.platforms.local_checks import (
    CASE_AGENT_OUTPUT_PATHS,
    SALESFORCE_AGENT_OUTPUT_PATHS,
)
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    MULE3_APP,
)
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    SOURCE_FILES as MULESOFT_SOURCE_FILES,
)
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    TARGET_FILES as MULESOFT_TARGET_FILES,
)
from legacy_migration_agent.platforms.mulesoft_runtime import MULESOFT_VALIDATION_COMMAND_IDS
from legacy_migration_agent.platforms.salesforce_runtime import (
    CASE_SOURCE_ENTRY,
    CASE_TRANSFORMATION_INPUT_PATHS,
    SALESFORCE_SOURCE_ENTRY,
    SALESFORCE_TRANSFORMATION_INPUT_PATHS,
    SALESFORCE_VALIDATION_COMMAND_IDS,
)

LOCAL_MODEL_REVISION = "sha256:" + "a" * 64
CLAUDE_RUNTIME_IDENTITY = "sha256:" + "b" * 64


def _mulesoft_candidate_text_outputs() -> dict[str, str]:
    """Return the MuleSoft candidate as decoded text keyed by approved path."""

    return {
        relative_path: content.decode("utf-8")
        for relative_path, content in mulesoft_target_outputs().items()
    }


# Migration units are disambiguated by the run's fixed source entry path, never
# by platform: the two Salesforce units (account/contact and Case Management
# Console) share ``Platform.SALESFORCE`` yet own distinct approved-output
# inventories, source inputs, validation command ids, and candidate bytes. This
# mirrors how ``salesforce_runtime`` keys its per-unit runtime scopes so the
# recorded double reproduces the exact scope each launch contract pins.
_APPROVED_PATHS = {
    SALESFORCE_SOURCE_ENTRY: SALESFORCE_AGENT_OUTPUT_PATHS,
    CASE_SOURCE_ENTRY: CASE_AGENT_OUTPUT_PATHS,
    MULE3_APP: MULESOFT_TARGET_FILES,
}
_INPUT_PATHS = {
    SALESFORCE_SOURCE_ENTRY: SALESFORCE_TRANSFORMATION_INPUT_PATHS,
    CASE_SOURCE_ENTRY: CASE_TRANSFORMATION_INPUT_PATHS,
    MULE3_APP: MULESOFT_SOURCE_FILES,
}
_VALIDATION_COMMAND_IDS = {
    SALESFORCE_SOURCE_ENTRY: SALESFORCE_VALIDATION_COMMAND_IDS,
    CASE_SOURCE_ENTRY: SALESFORCE_VALIDATION_COMMAND_IDS,
    MULE3_APP: MULESOFT_VALIDATION_COMMAND_IDS,
}
_TEXT_OUTPUTS = {
    SALESFORCE_SOURCE_ENTRY: salesforce_candidate_text_outputs,
    CASE_SOURCE_ENTRY: case_management_candidate_text_outputs,
    MULE3_APP: _mulesoft_candidate_text_outputs,
}


def fixture_model_response(
    project_root: Path,
    *,
    system_prompt: str,
    input_value: BaseModel,
    output_type: type[OutputModel],
) -> OutputModel:
    """Return one schema-valid fixture response for a requested agent role."""

    if not system_prompt.strip():
        raise ValueError("the test double requires a nonempty agent prompt")
    if issubclass(output_type, ArchitectManifestProposal):
        context = ArchitectModelContext.model_validate(input_value)
        evidence_ids = (
            context.dependency_graph.nodes[0].node_id,
            context.wiki_trace.hits[0].page_id,
        )
        return cast(
            OutputModel,
            ArchitectManifestProposal(
                semantic_decisions=(
                    ArchitectSemanticDecision(
                        decision_id="additive-platform-scope",
                        category="target_architecture",
                        summary=(
                            "Use only the additive platform scope and controller-owned checks."
                        ),
                        evidence_ids=evidence_ids,
                    ),
                    ArchitectSemanticDecision(
                        decision_id="external-validation-boundary",
                        category="operational_constraint",
                        summary=(
                            "Keep external runtime validation outside the local Agent UI boundary."
                        ),
                        evidence_ids=evidence_ids,
                    ),
                ),
                cited_graph_nodes=(context.dependency_graph.nodes[0].node_id,),
                cited_wiki_pages=(context.wiki_trace.hits[0].page_id,),
                unresolved_questions=(),
            ),
        )
    if output_type is _ArchitectConversationClarificationOutput:
        context = ArchitectConversationContext.model_validate(input_value)
        if context.selected_platform is not None:
            raise ValueError("the clarification output requires an unselected test scenario")
        return cast(
            OutputModel,
            _ArchitectConversationClarificationOutput(
                status="clarification_needed",
                assistant_message="Select a Salesforce or MuleSoft migration slice.",
                advisory_summary=None,
                missing_information=("Select a migration slice.",),
            ),
        )
    if output_type is _ArchitectConversationReadyOutput:
        context = ArchitectConversationContext.model_validate(input_value)
        if context.selected_platform is None:
            raise ValueError("the ready output requires a selected test scenario")
        if context.scenario_id not in {
            "salesforce-vf-to-lwc",
            "case-management-console",
            "mulesoft-mule3-to-mule4",
        }:
            raise ValueError("the selected test scenario is not supported")
        return cast(
            OutputModel,
            _ArchitectConversationReadyOutput(
                status="ready_to_launch",
                assistant_message=(
                    "The selected scenario is ready for the Controller's canonical launch gate."
                ),
                advisory_summary=(
                    "The selected bounded scenario uses an additive migration and local checks."
                ),
                missing_information=(),
            ),
        )
    if issubclass(output_type, (EngineerModelOutcome, EngineerFilePlanOutcome)):
        engineer_context = (
            EngineerCorrectionProviderContext.model_validate(input_value)
            if isinstance(input_value, EngineerCorrectionProviderContext)
            else EngineerWorkspaceContext.model_validate(input_value)
        )
        entry_path = engineer_context.request.target.entry_path
        approved_paths = _APPROVED_PATHS.get(entry_path)
        if approved_paths is None:
            raise ValueError("the selected test scenario is not supported")
        if tuple(sorted(engineer_context.manifest.approved_paths)) != tuple(sorted(approved_paths)):
            raise ValueError("the test manifest differs from the fixed scenario output scope")
        output_text = _TEXT_OUTPUTS[entry_path]()
        if engineer_context.correction is None:
            updates = tuple(
                EngineerFileUpdate(
                    path=relative_path,
                    content=output_text[relative_path],
                )
                for relative_path in sorted(approved_paths)
            )
        else:
            prior = {
                update.path: update.content
                for update in engineer_context.correction.prior_allowed_updates
            }
            updates = tuple(
                EngineerFileUpdate(
                    path=relative_path,
                    content=prior[relative_path] + "\n",
                )
                for relative_path in engineer_context.correction.allowed_correction_paths
            )
        file_plan = EngineerFilePlan(
            updates=updates,
            assumptions=(
                "The test supplies complete bytes for exactly the approved outputs.",
                "External platform and runtime checks remain outside this test boundary.",
            ),
        )
        if issubclass(output_type, EngineerFilePlanOutcome):
            return cast(
                OutputModel,
                EngineerFilePlanOutcome(kind="file_plan", file_plan=file_plan),
            )
        return cast(OutputModel, EngineerModelOutcome.for_file_plan(file_plan))
    if output_type is ValidatorModelAdvisory:
        context = ValidatorEvidenceContext.model_validate(input_value)
        return cast(
            OutputModel,
            ValidatorModelAdvisory(
                manifest_digest=context.manifest_digest,
                change_set_digest=context.evidence.change_set_digest,
                report_digest=context.evidence.report_digest,
                assessment="supports_report",
                summary=(
                    "The authoritative local report supports inspection of the isolated "
                    "candidate while withholding any external-runtime claim."
                ),
                concerns=(
                    "Salesforce or Mule runtime evidence is still required before readiness.",
                ),
                cited_check_ids=tuple(
                    result.check_id for result in context.evidence.report.results
                ),
                cited_receipt_digests=tuple(
                    artifact_digest(result.receipt)
                    for result in context.evidence.report.results
                    if result.receipt is not None
                ),
                advisory_only=True,
            ),
        )
    raise TypeError(f"unsupported test-double output type: {output_type.__name__}")


def make_ollama_client_test_double(
    project_root: Path,
    *,
    role_calls: list[str] | None = None,
    constructed: list[object] | None = None,
    bound_revisions: list[str] | None = None,
    current_revision: str = LOCAL_MODEL_REVISION,
    expected_timeout_seconds: float = 180.0,
) -> type[object]:
    """Create an Ollama-shaped client class backed by fixture responses."""

    class OllamaClientTestDouble:
        provider = "ollama"
        execution_boundary = "local_loopback"
        live_invocation = False
        store_false_sent = False

        def __init__(
            self,
            model_id: str,
            *,
            approval: LiveModelApproval,
            timeout_seconds: float,
        ) -> None:
            if timeout_seconds != expected_timeout_seconds:
                raise ValueError("unexpected test timeout")
            self.model_id = model_id
            self.live_approval = approval
            self.last_usage: ModelUsageEvidence | None = None
            self.model_revision: str | None = None
            if constructed is not None:
                constructed.append(self)

        def bind_model_revision(self, expected_revision: str) -> None:
            if bound_revisions is not None:
                bound_revisions.append(expected_revision)
            if expected_revision != current_revision:
                raise ModelConfigurationError("local Ollama model revision changed")
            self.model_revision = expected_revision

        def parse(
            self,
            *,
            system_prompt: str,
            input_value: BaseModel,
            output_type: type[OutputModel],
        ) -> OutputModel:
            if role_calls is not None:
                role_calls.append(output_type.__name__)
            result = fixture_model_response(
                project_root,
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )
            self.last_usage = ModelUsageEvidence(
                latency_ms=7,
                provider_usage_reported=True,
                input_tokens=80,
                output_tokens=20,
                total_tokens=100,
            )
            if self.model_revision is None:
                self.model_revision = current_revision
            return result

    return OllamaClientTestDouble


def make_claude_client_test_double(
    project_root: Path,
    *,
    role_calls: list[str] | None = None,
    constructed: list[object] | None = None,
    bound_runtime_identities: list[str] | None = None,
    current_runtime_identity: str = CLAUDE_RUNTIME_IDENTITY,
    expected_timeout_seconds: float = 240.0,
) -> type[object]:
    """Create a truthful Claude CLI-shaped client backed by fixture responses."""

    class ClaudeClientTestDouble:
        provider = "claude-cli"
        execution_boundary = "remote_provider_managed"
        live_invocation = True
        store_false_sent = False

        def __init__(
            self,
            model_id: str,
            *,
            approval: LiveModelApproval,
            timeout_seconds: float,
        ) -> None:
            if timeout_seconds != expected_timeout_seconds:
                raise ValueError("unexpected test timeout")
            self.model_id = model_id
            self.live_approval = approval
            self.last_usage: ModelUsageEvidence | None = None
            self.runtime_identity_digest: str | None = None
            if constructed is not None:
                constructed.append(self)

        @property
        def model_revision(self) -> None:
            """Claude CLI cannot attest to remote model-weight revision."""

            return None

        def _resolve_model_revision(self, *, timeout_seconds: float) -> str:
            if not 0 < timeout_seconds <= expected_timeout_seconds:
                raise ValueError("unexpected test probe timeout")
            return current_runtime_identity

        def bind_runtime_identity(self, expected_identity: str) -> None:
            if bound_runtime_identities is not None:
                bound_runtime_identities.append(expected_identity)
            if expected_identity != current_runtime_identity:
                raise ModelConfigurationError("Claude runtime identity changed")
            if (
                self.runtime_identity_digest is not None
                and self.runtime_identity_digest != expected_identity
            ):
                raise ModelConfigurationError("Claude runtime identity changed")
            self.runtime_identity_digest = expected_identity

        def parse(
            self,
            *,
            system_prompt: str,
            input_value: BaseModel,
            output_type: type[OutputModel],
        ) -> OutputModel:
            if (
                self.runtime_identity_digest is not None
                and self.runtime_identity_digest != current_runtime_identity
            ):
                raise ModelConfigurationError("Claude runtime identity changed")
            if role_calls is not None:
                role_calls.append(output_type.__name__)
            result = fixture_model_response(
                project_root,
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )
            self.last_usage = ModelUsageEvidence(
                latency_ms=11,
                provider_usage_reported=True,
                input_tokens=85,
                output_tokens=25,
                total_tokens=110,
            )
            if self.runtime_identity_digest is None:
                self.runtime_identity_digest = current_runtime_identity
            return result

    return ClaudeClientTestDouble


__all__ = [
    "CLAUDE_RUNTIME_IDENTITY",
    "LOCAL_MODEL_REVISION",
    "fixture_model_response",
    "make_claude_client_test_double",
    "make_ollama_client_test_double",
]
