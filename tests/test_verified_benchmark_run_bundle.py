from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel
from test_agent_run import (
    SF_FROZEN_OUTPUTS,
    BenchmarkRiskAwareModel,
    PresetStructuredModel,
    _approval,
    _correction_approval,
    _project,
    _recoverable_validator,
)

import legacy_migration_agent.application.run_query as run_query_module
import legacy_migration_agent.evaluation_runner as evaluation_runner_module
from legacy_migration_agent.agent_runtime.agent_definitions import AgentRole, load_agent_registry
from legacy_migration_agent.agent_runtime.model_agents import ArchitectManifestProposal
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelUsageEvidence,
)
from legacy_migration_agent.application.agent_run import (
    prepare_agent_run_request,
    resume_agent_run,
    retry_agent_run,
)
from legacy_migration_agent.application.agent_run_models import (
    AgentRunModelClients,
    build_claude_cli_model_clients,
)
from legacy_migration_agent.application.migration_scenarios import migration_launch_contract
from legacy_migration_agent.application.run_query import load_verified_benchmark_run_bundle
from legacy_migration_agent.contracts import Platform
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.run_session import AgentRunSession
from legacy_migration_agent.evaluation_runner import (
    bind_benchmark_knowledge_arm,
    start_benchmark_agent_run,
)
from legacy_migration_agent.knowledge.wiki import BenchmarkKnowledgeBinding

_RUNTIME_IDENTITY = "sha256:" + "7" * 64
_ZERO_DIGEST = "sha256:" + "0" * 64
_APPROVAL = LiveModelApproval(
    allow_live_api=True,
    allow_prompt_data_sharing=True,
    approved_by="verified-bundle-test",
    approved_remote_provider_id="bedrock",
)


class _RemoteEvidenceMixin:
    """Provider-free double with truthful remote-boundary evidence fields."""

    provider = "claude-cli"
    live_invocation = True
    store_false_sent = False
    execution_boundary = "remote_provider_managed"

    def __init__(
        self,
        model_id: str,
        *,
        approval: LiveModelApproval,
        timeout_seconds: float,
    ) -> None:
        del timeout_seconds
        super().__init__(SF_FROZEN_OUTPUTS)
        self.model_id = model_id
        self.live_approval = approval
        self.last_usage: ModelUsageEvidence | None = None
        self.runtime_identity_digest = _RUNTIME_IDENTITY

    def resolve_runtime_identity(self) -> str:
        return self.runtime_identity_digest

    def bind_runtime_identity(self, expected: str) -> None:
        if expected != self.runtime_identity_digest:
            raise ModelConfigurationError("test runtime identity changed")

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        result = super().parse(
            system_prompt=system_prompt,
            input_value=input_value,
            output_type=output_type,
        )
        self.last_usage = ModelUsageEvidence(
            latency_ms=3,
            provider_usage_reported=True,
            input_tokens=20,
            output_tokens=10,
            total_tokens=30,
        )
        return result


class _RemotePresetModel(_RemoteEvidenceMixin, PresetStructuredModel):
    pass


class _RemoteRiskModel(_RemoteEvidenceMixin, BenchmarkRiskAwareModel):
    pass


class _RemoteRejectedArchitectModel(_RemotePresetModel):
    """Return schema-valid Architect output that controller policy rejects."""

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        output = super().parse(
            system_prompt=system_prompt,
            input_value=input_value,
            output_type=output_type,
        )
        if issubclass(output_type, ArchitectManifestProposal):
            proposal = ArchitectManifestProposal.model_validate(output)
            return proposal.model_copy(
                update={"cited_wiki_pages": ("unbound-authored-evidence-marker",)}
            )
        return output


def _models(client_factory: type[_RemoteEvidenceMixin]) -> AgentRunModelClients:
    return build_claude_cli_model_clients(
        model_id="claude-sonnet-5",
        approval=_APPROVAL,
        timeout_seconds=5,
        client_factory=client_factory,
    )


def _anchor_binding(binding: BenchmarkKnowledgeBinding) -> BenchmarkKnowledgeBinding:
    return BenchmarkKnowledgeBinding.model_validate(
        binding.model_copy(
            update={
                "execution_anchor_digest": "sha256:" + "8" * 64,
                "runtime_identity_digest": _RUNTIME_IDENTITY,
            }
        ).model_dump(mode="python")
    )


def _copy_benchmark_project(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    shutil.copytree(source / "fixtures", project / "fixtures")
    shutil.copytree(source / "agents", project / "agents")
    shutil.copytree(source / "knowledge/wiki", project / "knowledge/wiki")
    shutil.copytree(source / "evaluation", project / "evaluation")
    shutil.copytree(
        source / "tooling/mulesoft-runtime",
        project / "tooling/mulesoft-runtime",
    )
    return project


def test_bundle_supports_planning_only_decision_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _copy_benchmark_project(tmp_path)
    contract = migration_launch_contract("case-management-console")
    request = prepare_agent_run_request(
        project,
        request_id="request-bundle-decision",
        launch_contract=contract,
        requested_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    binding = _anchor_binding(
        bind_benchmark_knowledge_arm(
            project,
            request,
            cell_id="salesforce-case-management-complex-risk--full-agent-wiki--r1",
            case_id="salesforce-case-management-complex-risk",
            scenario_id=contract.scenario_id,
            knowledge_arm="full_agent_wiki",
        )
    )
    models = _models(_RemoteRiskModel)
    monkeypatch.setattr(
        evaluation_runner_module,
        "_verify_execution_anchor_binding",
        lambda *_args: None,
    )
    run_id = "run-bundle-decision"
    thread_id = "thread-bundle-decision"
    run_dir = project / ".runs" / run_id

    status = start_benchmark_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        launch_contract=contract,
        request=request,
        models=models,
        knowledge_binding=binding,
    )
    bundle = load_verified_benchmark_run_bundle(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
    )

    assert status.status == "decision_required"
    assert bundle.completed_attempts == ()
    assert bundle.planning is not None
    assert len(bundle.model_calls) == 1
    assert bundle.model_calls[0].agent_version == (
        load_agent_registry(project / "agents").get(AgentRole.ARCHITECT).version
    )
    assert bundle.tool_receipts == ()
    assert bundle.model_call_evidence_complete is True
    assert bundle.partial_evidence_unavailable is False


def test_bundle_counts_schema_valid_architect_call_rejected_by_controller_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    contract = migration_launch_contract("salesforce-vf-to-lwc")
    binding = _anchor_binding(
        bind_benchmark_knowledge_arm(
            project,
            request,
            cell_id="salesforce-account-contact-medium--full-agent-wiki--r1",
            case_id="salesforce-account-contact-medium",
            scenario_id=contract.scenario_id,
            knowledge_arm="full_agent_wiki",
        )
    )
    monkeypatch.setattr(
        evaluation_runner_module,
        "_verify_execution_anchor_binding",
        lambda *_args: None,
    )
    run_id = "run-bundle-architect-policy-rejection"
    thread_id = "thread-bundle-architect-policy-rejection"
    run_dir = project / ".runs" / run_id

    status = start_benchmark_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        launch_contract=contract,
        request=request,
        models=_models(_RemoteRejectedArchitectModel),
        knowledge_binding=binding,
    )
    bundle = load_verified_benchmark_run_bundle(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
    )

    assert status.status == "failed"
    assert status.failure is not None
    assert status.failure.seam == "architect"
    assert status.failure.reason_code == "policy_rejected"
    assert bundle.planning is None
    assert bundle.attempts == ()
    assert len(bundle.model_calls) == 1
    assert bundle.model_calls[0].usage == ModelUsageEvidence(
        latency_ms=3,
        provider_usage_reported=True,
        input_tokens=20,
        output_tokens=10,
        total_tokens=30,
    )
    assert bundle.runtime_identity_digest == _RUNTIME_IDENTITY
    assert bundle.model_call_evidence_complete is True
    assert bundle.model_call_evidence_unavailable_reason is None
    assert bundle.tool_receipt_evidence_complete is True
    assert bundle.partial_evidence_unavailable is False


def test_bundle_verifies_two_attempts_and_rejects_tampered_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    contract = migration_launch_contract("salesforce-vf-to-lwc")
    binding = _anchor_binding(
        bind_benchmark_knowledge_arm(
            project,
            request,
            cell_id="salesforce-account-contact-medium--full-agent-wiki--r2",
            case_id="salesforce-account-contact-medium",
            scenario_id=contract.scenario_id,
            knowledge_arm="full_agent_wiki",
        )
    )
    models = _models(_RemotePresetModel)
    monkeypatch.setattr(
        evaluation_runner_module,
        "_verify_execution_anchor_binding",
        lambda *_args: None,
    )
    run_id = "run-bundle-completed"
    thread_id = "thread-bundle-completed"
    run_dir = project / ".runs" / run_id
    validator = _recoverable_validator(run_id)
    started = start_benchmark_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        launch_contract=contract,
        request=request,
        models=models,
        knowledge_binding=binding,
        trusted_validator=validator,
    )
    attempt_one = resume_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        approval=_approval(started),
        models=models,
        trusted_validator=validator,
    )
    retry_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        approval=_correction_approval(attempt_one, reviewer="verified-bundle-test"),
        models=models,
        trusted_validator=validator,
    )

    bundle = load_verified_benchmark_run_bundle(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
    )
    assert bundle.completed_attempts == (1, 2)
    assert bundle.model_call_evidence_complete is True
    assert bundle.runtime_identity_digest == _RUNTIME_IDENTITY
    assert bundle.tool_receipts
    assert {receipt.run_id for receipt in bundle.tool_receipts} == {run_id}

    assert bundle.planning is not None
    session = AgentRunSession.load(project, run_dir)
    registry = load_agent_registry(project / "agents")
    architect_definition = registry.get(AgentRole.ARCHITECT)
    architect_call = bundle.planning.architect.model_call
    tampered_prompt = architect_definition.model_copy(
        update={"prompt": architect_definition.prompt + "\nTampered prompt."}
    )
    with pytest.raises(PolicyViolation, match="replay-safe"):
        run_query_module._verify_complete_model_call(
            architect_call,
            definition=tampered_prompt,
            session=session,
            input_value=bundle.planning.architect_context.model_context,
            output_value=bundle.planning.architect.agent_output,
            role="Architect",
        )
    with pytest.raises(PolicyViolation, match="replay-safe"):
        run_query_module._verify_complete_model_call(
            architect_call.model_copy(update={"agent_version": "architect/v99"}),
            definition=architect_definition,
            session=session,
            input_value=bundle.planning.architect_context.model_context,
            output_value=bundle.planning.architect.agent_output,
            role="Architect",
        )

    snapshot = bundle.attempts[0]
    with pytest.raises(PolicyViolation, match="exact model input"):
        run_query_module._verify_role_invocation_lease(
            session,
            bundle.planning,
            attempt=1,
            role="engineer",
            definition=registry.get(AgentRole.ENGINEER),
            manifest=snapshot.manifest,
            change_set=snapshot.change_set,
            report=snapshot.report,
            expected_input_digest=_ZERO_DIGEST,
        )
    monkeypatch.setattr(
        AgentRunSession,
        "is_model_operation_artifact_promoted",
        lambda *_args: False,
    )
    with pytest.raises(PolicyViolation, match="not lifecycle-promoted"):
        run_query_module._verify_role_invocation_lease(
            session,
            bundle.planning,
            attempt=1,
            role="engineer",
            definition=registry.get(AgentRole.ENGINEER),
            manifest=snapshot.manifest,
            change_set=snapshot.change_set,
            report=snapshot.report,
            expected_input_digest=snapshot.engineer.model_call.input_digest,
        )

    receipt_index = next(
        index for index, result in enumerate(snapshot.report.results) if result.receipt is not None
    )
    result = snapshot.report.results[receipt_index]
    assert result.receipt is not None
    bad_result = result.model_copy(
        update={"receipt": result.receipt.model_copy(update={"run_id": "foreign-run"})}
    )
    bad_results = list(snapshot.report.results)
    bad_results[receipt_index] = bad_result
    bad_report = snapshot.report.model_copy(update={"results": tuple(bad_results)})
    with pytest.raises(PolicyViolation, match="exact benchmark run"):
        run_query_module._verify_tool_receipt_bindings(
            session,
            snapshot.change_set,
            bad_report,
        )
