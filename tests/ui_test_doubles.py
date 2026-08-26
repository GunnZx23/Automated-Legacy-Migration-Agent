"""Deterministic test doubles for Agent UI orchestration tests.

These helpers stand in for loopback Ollama calls only inside the test suite.
They are not imported by product code and make no model-quality claim.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from pydantic import BaseModel

from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectContext,
    ArchitectConversationContext,
    ArchitectConversationReply,
    ArchitectManifestProposal,
    EngineerFilePlan,
    EngineerFileUpdate,
    EngineerModelOutcome,
    EngineerWorkspaceContext,
    ValidatorAdvisory,
    ValidatorEvidenceContext,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelUsageEvidence,
    OutputModel,
)
from legacy_migration_agent.contracts import (
    ApprovalAction,
    DependencyEvidence,
    EnvironmentKind,
    MigrationManifest,
    Platform,
    TransformationStep,
    ValidationCommand,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.platforms.local_checks import SALESFORCE_AGENT_OUTPUT_PATHS
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    SOURCE_FILES as MULESOFT_SOURCE_FILES,
)
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    TARGET_FILES as MULESOFT_TARGET_FILES,
)
from legacy_migration_agent.platforms.mulesoft_runtime import MULESOFT_VALIDATION_COMMAND_IDS
from legacy_migration_agent.platforms.salesforce_runtime import (
    SALESFORCE_TRANSFORMATION_INPUT_PATHS,
    SALESFORCE_VALIDATION_COMMAND_IDS,
)

LOCAL_MODEL_REVISION = "sha256:" + "a" * 64

_EXPECTED_ROOTS = {
    Platform.SALESFORCE: "fixtures/salesforce/account-contact-explorer/expected",
    Platform.MULESOFT: "fixtures/mulesoft/customer-status-api/expected",
}

_APPROVED_PATHS = {
    Platform.SALESFORCE: SALESFORCE_AGENT_OUTPUT_PATHS,
    Platform.MULESOFT: MULESOFT_TARGET_FILES,
}
_INPUT_PATHS = {
    Platform.SALESFORCE: SALESFORCE_TRANSFORMATION_INPUT_PATHS,
    Platform.MULESOFT: MULESOFT_SOURCE_FILES,
}
_VALIDATION_COMMAND_IDS = {
    Platform.SALESFORCE: SALESFORCE_VALIDATION_COMMAND_IDS,
    Platform.MULESOFT: MULESOFT_VALIDATION_COMMAND_IDS,
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
    if output_type is ArchitectManifestProposal:
        context = ArchitectContext.model_validate(input_value)
        platform = context.request.platform
        dependencies = tuple(
            DependencyEvidence(
                path=edge.provenance[0].path,
                relation=edge.kind.value,
                source=f"dependency-graph:{edge.source_id}->{edge.target_id}",
                resolved=edge.resolved,
            )
            for edge in context.dependency_graph.edges
            if edge.resolved
        )
        if not dependencies:
            raise ValueError("the fixture dependency graph must contain a resolved relationship")
        manifest = MigrationManifest(
            manifest_id=f"ui-test-{context.request.platform.value}-manifest-v1",
            request_id=context.request.request_id,
            platform=context.request.platform,
            base_revision=context.request.base_revision,
            approved_paths=_APPROVED_PATHS[platform],
            dependencies=dependencies,
            transformations=(
                TransformationStep(
                    step_id=f"migrate-{context.request.platform.value}-bounded-slice",
                    description="Create the complete additive target slice within the fixed scope.",
                    input_paths=_INPUT_PATHS[platform],
                    output_paths=_APPROVED_PATHS[platform],
                ),
            ),
            validation_plan=tuple(
                ValidationCommand(
                    check_id=f"check-{command_id}",
                    command_id=command_id,
                    purpose="Run one controller-owned validation check.",
                    environment=EnvironmentKind.LOCAL,
                    required=True,
                )
                for command_id in _VALIDATION_COMMAND_IDS[platform]
            ),
            implementation_contract=(
                context.platform_adapter.scope_policy.required_implementation_contract
            ),
            required_approvals=(ApprovalAction.APPROVE_MANIFEST,),
        )
        return cast(
            OutputModel,
            ArchitectManifestProposal(
                manifest=manifest,
                scope_policy_digest=context.platform_adapter.scope_policy_digest,
                public_decisions=(
                    "Use only the additive platform scope and controller-owned checks.",
                    "Keep external runtime validation outside the local Agent UI boundary.",
                ),
                cited_graph_nodes=(context.dependency_graph.nodes[0].node_id,),
                cited_wiki_pages=(context.wiki_trace.hits[0].page_id,),
                unresolved_questions=(),
            ),
        )
    if output_type is ArchitectConversationReply:
        context = ArchitectConversationContext.model_validate(input_value)
        if context.selected_platform is None:
            return cast(
                OutputModel,
                ArchitectConversationReply(
                    status="clarification_needed",
                    assistant_message="Select a Salesforce or MuleSoft migration slice.",
                    missing_information=("Select a migration slice.",),
                ),
            )
        return cast(
            OutputModel,
            ArchitectConversationReply(
                status="ready_to_launch",
                assistant_message=(
                    "I refined the bounded request. Use Generate migration plan when ready."
                ),
                refined_request=(
                    "Migrate the selected synthetic source fixture additively while preserving "
                    "the legacy entry point and validating the generated candidate locally."
                ),
            ),
        )
    if output_type is EngineerModelOutcome:
        engineer_context = EngineerWorkspaceContext.model_validate(input_value)
        platform = engineer_context.request.platform
        if tuple(sorted(engineer_context.manifest.approved_paths)) != tuple(
            sorted(_APPROVED_PATHS[platform])
        ):
            raise ValueError("the test manifest differs from the fixed scenario output scope")
        expected_root = project_root.joinpath(*_EXPECTED_ROOTS[platform].split("/"))
        updates = tuple(
            EngineerFileUpdate(
                path=path,
                content=expected_root.joinpath(*path.split("/")).read_text(encoding="utf-8"),
            )
            for path in sorted(_APPROVED_PATHS[platform])
        )
        return cast(
            OutputModel,
            EngineerModelOutcome.for_file_plan(
                EngineerFilePlan(
                    updates=updates,
                    assumptions=(
                        "The test supplies complete bytes for exactly the approved outputs.",
                        "External platform and runtime checks remain outside this test boundary.",
                    ),
                )
            ),
        )
    if output_type is ValidatorAdvisory:
        context = ValidatorEvidenceContext.model_validate(input_value)
        return cast(
            OutputModel,
            ValidatorAdvisory(
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


__all__ = [
    "LOCAL_MODEL_REVISION",
    "fixture_model_response",
    "make_ollama_client_test_double",
]
