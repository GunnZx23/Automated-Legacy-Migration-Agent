from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from pydantic import BaseModel

import legacy_migration_agent.application.agent_run as agent_run_module
import legacy_migration_agent.core.run_session as run_session_module
from legacy_migration_agent.agent_runtime.checkpointing import (
    durable_migration_workflow,
    strict_checkpoint_serializer,
)
from legacy_migration_agent.agent_runtime.correction import (
    CorrectionAction,
    CorrectionApproval,
    CorrectionRequest,
)
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectContext,
    ArchitectManifestProposal,
    EngineerFilePlan,
    EngineerFileUpdate,
    EngineerInterventionOutcome,
    EngineerModelOutcome,
    EngineerWorkspaceContext,
    ValidatorAdvisory,
    ValidatorEvidenceContext,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelOutputError,
    ModelRefusalError,
    ModelRuntimeError,
    ModelUsageEvidence,
)
from legacy_migration_agent.application.agent_run import (
    AgentRunModelClients,
    build_live_openai_model_clients,
    build_local_ollama_model_clients,
    get_agent_run_status,
    prepare_agent_run_request,
    resume_agent_run,
    retry_agent_run,
    start_agent_run,
)
from legacy_migration_agent.application.final_review import (
    decide_final_review_for_run,
    get_final_review_status_for_run,
    request_final_review_for_run,
)
from legacy_migration_agent.contracts import (
    ApprovalAction,
    CheckResult,
    CheckStatus,
    DecisionRequest,
    EnvironmentKind,
    ImplementationIntervention,
    ImplementationInterventionEvidence,
    MigrationManifest,
    MigrationRequest,
    MigrationTarget,
    PlanningInterventionOption,
    Platform,
    ToolReceipt,
    TransformationStep,
    ValidationCommand,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.workspace import content_revision, snapshot_tree
from legacy_migration_agent.knowledge.wiki import RetrievalTrace
from legacy_migration_agent.platforms.local_checks import (
    SALESFORCE_AGENT_OUTPUT_PATHS,
    SALESFORCE_IMPLEMENTATION_CONTRACT,
)
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    MULE3_APP,
    MULESOFT_IMPLEMENTATION_CONTRACT,
    SOURCE_FILES,
    TARGET_FILES,
)
from legacy_migration_agent.platforms.mulesoft_runtime import (
    MULESOFT_SOURCE_VERSION,
    MULESOFT_TARGET_RUNTIME,
    MULESOFT_TARGET_VERSION,
    MULESOFT_VALIDATION_COMMAND_IDS,
)
from legacy_migration_agent.platforms.salesforce_runtime import (
    SALESFORCE_API_RUNTIME,
    SALESFORCE_SOURCE_ENTRY,
    SALESFORCE_TARGET_RUNTIME,
    SALESFORCE_TRANSFORMATION_INPUT_PATHS,
    SALESFORCE_VALIDATION_COMMAND_IDS,
)
from legacy_migration_agent.workflow import (
    ManifestApproval,
    manifest_decision_request,
    manifest_digest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 8, 26)
LOCAL_MODEL_REVISION_A = "sha256:" + "a" * 64
LOCAL_MODEL_REVISION_B = "sha256:" + "b" * 64

# These evaluator-owned target bytes are deliberately frozen before any
# oracle-free project/run is constructed.  Product code receives only model
# output and never a path to either evaluator tree.
SF_FROZEN_OUTPUTS = {
    path: (PROJECT_ROOT / "fixtures/salesforce/account-contact-explorer/expected" / path).read_text(
        encoding="utf-8"
    )
    for path in SALESFORCE_AGENT_OUTPUT_PATHS
}
MULE_FROZEN_OUTPUTS = {
    path: (PROJECT_ROOT / "fixtures/mulesoft/customer-status-api/expected" / path).read_text(
        encoding="utf-8"
    )
    for path in TARGET_FILES
}
SENSITIVE_FAILURE = "sk-provider-error-secret-123456789 /private/tmp/oracle"


class PresetStructuredModel:
    provider = "offline-agent-run-test"
    model_id = "three-markdown-agents/v1"
    live_invocation = False
    store_false_sent = False

    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs
        self.calls: list[str] = []
        self.api_key = "sk-agent-run-secret-123456789"

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        assert system_prompt
        self.calls.append(output_type.__name__)
        if output_type is ArchitectManifestProposal:
            context = ArchitectContext.model_validate(input_value)
            manifest = _manifest(context.request)
            return ArchitectManifestProposal(
                manifest=manifest,
                scope_policy_digest=context.platform_adapter.scope_policy_digest,
                public_decisions=(
                    "Use the exact shipped additive scope and controller-owned checks.",
                ),
                cited_graph_nodes=(context.dependency_graph.nodes[0].node_id,),
                cited_wiki_pages=(context.wiki_trace.hits[0].page_id,),
            )
        if output_type is EngineerModelOutcome:
            return EngineerModelOutcome.for_file_plan(
                EngineerFilePlan(
                    updates=tuple(
                        EngineerFileUpdate(path=path, content=self.outputs[path])
                        for path in sorted(self.outputs)
                    ),
                    assumptions=("Only the manifest-approved additive files are changed.",),
                )
            )
        if output_type is ValidatorAdvisory:
            context = ValidatorEvidenceContext.model_validate(input_value)
            return ValidatorAdvisory(
                manifest_digest=context.manifest_digest,
                change_set_digest=context.evidence.change_set_digest,
                report_digest=context.evidence.report_digest,
                assessment="supports_report",
                summary="The deterministic report remains authoritative.",
                cited_check_ids=(context.evidence.report.results[0].check_id,),
                advisory_only=True,
            )
        raise AssertionError(f"unexpected structured output type: {output_type}")


class EngineerStopStructuredModel(PresetStructuredModel):
    def __init__(self, outputs: dict[str, str], *, stop_on_occurrence: int = 1) -> None:
        super().__init__(outputs)
        self.stop_on_occurrence = stop_on_occurrence

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        if output_type is not EngineerModelOutcome:
            return super().parse(
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )
        occurrence = self.calls.count(EngineerModelOutcome.__name__) + 1
        if occurrence != self.stop_on_occurrence:
            return super().parse(
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )
        self.calls.append(output_type.__name__)
        context = EngineerWorkspaceContext.model_validate(input_value)
        output_path = context.manifest.approved_paths[0]
        affected_paths = (output_path, context.request.target.entry_path)
        intervention = ImplementationIntervention(
            intervention_id="implementation-stop-agent-run",
            request_id=context.request.request_id,
            request_digest=context.request_digest,
            manifest_id=context.manifest.manifest_id,
            manifest_digest=context.manifest_digest,
            base_revision=context.workspace_base_revision,
            agent_version=context.agent_version,
            agent_definition_digest=context.agent_definition_digest,
            input_evidence_digest=context.input_evidence_digest,
            reason="A required public implementation contract is absent.",
            requested_action=ApprovalAction.EXPAND_SCOPE,
            affected_paths=affected_paths,
            public_concerns=("Generating code would require an unsupported guess.",),
            evidence=(
                ImplementationInterventionEvidence(
                    source="engineer_input",
                    source_digest=context.input_evidence_digest,
                    summary="The frozen implementation context lacks the contract.",
                    affected_paths=affected_paths,
                ),
            ),
            options=(
                PlanningInterventionOption.EXPAND_SCOPE_AND_REPLAN,
                PlanningInterventionOption.STOP_REQUEST,
            ),
            recommendation="Resolve the evidence gap and approve a revised manifest.",
        )
        return EngineerModelOutcome(
            result=EngineerInterventionOutcome(
                kind="decision_required",
                intervention=intervention,
            )
        )


class ExplodingStructuredModel(PresetStructuredModel):
    def __init__(
        self,
        outputs: dict[str, str],
        fail_output_type: type[BaseModel],
        *,
        failure: Exception | None = None,
    ) -> None:
        super().__init__(outputs)
        self.fail_output_type = fail_output_type
        self.failure = failure or RuntimeError(SENSITIVE_FAILURE)

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        if output_type is self.fail_output_type:
            self.calls.append(output_type.__name__)
            raise self.failure
        return super().parse(
            system_prompt=system_prompt,
            input_value=input_value,
            output_type=output_type,
        )


class InterruptOnceStructuredModel(PresetStructuredModel):
    """Simulate process loss after one role lease but before its artifact."""

    def __init__(
        self,
        outputs: dict[str, str],
        interrupt_output_type: type[BaseModel],
        *,
        interrupt_on_occurrence: int,
    ) -> None:
        super().__init__(outputs)
        self.interrupt_output_type = interrupt_output_type
        self.interrupt_on_occurrence = interrupt_on_occurrence
        self.role_occurrences = 0

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        if output_type is self.interrupt_output_type:
            self.role_occurrences += 1
            if self.role_occurrences == self.interrupt_on_occurrence:
                self.calls.append(output_type.__name__)
                raise KeyboardInterrupt("simulated process interruption after lease")
        return super().parse(
            system_prompt=system_prompt,
            input_value=input_value,
            output_type=output_type,
        )


class SemanticallyInvalidStructuredModel(PresetStructuredModel):
    """Return schema-valid output that violates one frozen role boundary."""

    def __init__(self, outputs: dict[str, str], invalid_output: type[BaseModel]) -> None:
        super().__init__(outputs)
        self.invalid_output = invalid_output

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        if output_type is not self.invalid_output:
            return super().parse(
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )
        if output_type is ArchitectManifestProposal:
            valid = ArchitectManifestProposal.model_validate(
                super().parse(
                    system_prompt=system_prompt,
                    input_value=input_value,
                    output_type=output_type,
                )
            )
            return valid.model_copy(update={"cited_wiki_pages": ("foreign-wiki-page",)})
        if output_type is EngineerModelOutcome:
            self.calls.append(output_type.__name__)
            context = EngineerWorkspaceContext.model_validate(input_value)
            first_path = context.manifest.approved_paths[0]
            return EngineerModelOutcome.for_file_plan(
                EngineerFilePlan(
                    updates=(
                        EngineerFileUpdate(path=first_path, content=self.outputs[first_path]),
                    ),
                    assumptions=("Return an intentionally incomplete but schema-valid plan.",),
                )
            )
        if output_type is ValidatorAdvisory:
            valid = ValidatorAdvisory.model_validate(
                super().parse(
                    system_prompt=system_prompt,
                    input_value=input_value,
                    output_type=output_type,
                )
            )
            return valid.model_copy(update={"cited_check_ids": ("foreign-check",)})
        raise AssertionError(f"unsupported invalid output type: {output_type}")


def _models(model: PresetStructuredModel) -> AgentRunModelClients:
    return AgentRunModelClients.offline(
        architect=model,
        engineer=model,
        validator=model,
    )


def _project(tmp_path: Path, platform: Platform) -> tuple[Path, MigrationRequest]:
    project = tmp_path / f"project-{platform.value}"
    fixture = (
        PROJECT_ROOT / "fixtures/salesforce/account-contact-explorer/input"
        if platform is Platform.SALESFORCE
        else PROJECT_ROOT / "fixtures/mulesoft/customer-status-api/input"
    )
    shutil.copytree(fixture, project / "source")
    shutil.copytree(PROJECT_ROOT / "agents", project / "agents")
    shutil.copytree(PROJECT_ROOT / "knowledge/wiki", project / "knowledge/wiki")
    if platform is Platform.SALESFORCE:
        target = MigrationTarget(
            entry_path=SALESFORCE_SOURCE_ENTRY,
            target_runtime=SALESFORCE_TARGET_RUNTIME,
            source_version=SALESFORCE_API_RUNTIME,
            target_version=SALESFORCE_API_RUNTIME,
            description="Migrate the bounded Visualforce explorer to additive LWC and Apex.",
        )
    else:
        target = MigrationTarget(
            entry_path=MULE3_APP,
            target_runtime=MULESOFT_TARGET_RUNTIME,
            source_version=MULESOFT_SOURCE_VERSION,
            target_version=MULESOFT_TARGET_VERSION,
            description="Migrate the bounded Mule 3 customer API to Mule 4.",
        )
    request = MigrationRequest(
        request_id=f"request-{platform.value}-agent-run",
        platform=platform,
        repository="source",
        base_revision=content_revision(project / "source"),
        target=target,
        allowed_environment=EnvironmentKind.LOCAL,
        requested_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    return project, request


@pytest.mark.parametrize("platform", (Platform.SALESFORCE, Platform.MULESOFT))
def test_prepare_request_binds_current_source_and_exact_platform_preset(
    tmp_path: Path,
    platform: Platform,
) -> None:
    project, expected = _project(tmp_path, platform)

    prepared = prepare_agent_run_request(
        project,
        request_id=f"prepared-{platform.value}",
        platform=platform,
        source_root="source",
        description="Prepare an exact local request without invoking a provider.",
        requested_at=datetime(2026, 8, 24, tzinfo=UTC),
    )

    assert prepared.repository == "source"
    assert prepared.base_revision == content_revision(project / "source")
    assert prepared.target.model_copy(update={"description": expected.target.description}) == (
        expected.target
    )
    assert prepared.allowed_environment is EnvironmentKind.LOCAL


def test_prepare_mulesoft_request_accepts_the_shipped_nested_input_path() -> None:
    source_root = "fixtures/mulesoft/customer-status-api/input"

    prepared = prepare_agent_run_request(
        PROJECT_ROOT,
        request_id="prepared-mulesoft-shipped-fixture",
        platform=Platform.MULESOFT,
        source_root=source_root,
        description="Prepare the shipped Mule 3-to-Mule 4 migration request.",
        requested_at=datetime(2026, 8, 24, tzinfo=UTC),
    )

    assert prepared.repository == source_root
    assert prepared.base_revision == content_revision(PROJECT_ROOT / source_root)
    assert prepared.target.entry_path == MULE3_APP


def test_request_preparation_rejects_oracle_source_paths() -> None:
    with pytest.raises(PolicyViolation, match="expected, golden, or oracle"):
        prepare_agent_run_request(
            PROJECT_ROOT,
            request_id="prepared-forbidden-oracle",
            platform=Platform.MULESOFT,
            source_root="fixtures/mulesoft/customer-status-api/expected",
            description="An oracle must never become agent input.",
            requested_at=datetime(2026, 8, 24, tzinfo=UTC),
        )


def test_mulesoft_agent_run_starts_from_a_nested_repository_relative_source(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path, Platform.MULESOFT)
    nested_source = project / "fixtures/mulesoft/customer-status-api/input"
    nested_source.parent.mkdir(parents=True)
    (project / "source").rename(nested_source)
    source_root = nested_source.relative_to(project).as_posix()
    request = prepare_agent_run_request(
        project,
        request_id="request-mulesoft-nested-run",
        platform=Platform.MULESOFT,
        source_root=source_root,
        description="Run the shipped nested Mule source through Architect.",
        requested_at=datetime(2026, 8, 24, tzinfo=UTC),
    )

    started = start_agent_run(
        project,
        project / ".runs/nested-mulesoft",
        run_id="run-mulesoft-nested",
        thread_id="thread-mulesoft-nested",
        source_root=source_root,
        request=request,
        models=_models(PresetStructuredModel(MULE_FROZEN_OUTPUTS)),
        wiki_as_of=AS_OF,
    )

    assert started.status == "awaiting_approval"
    assert started.platform is Platform.MULESOFT
    assert started.interrupt is not None


def _manifest(request: MigrationRequest) -> MigrationManifest:
    if request.platform is Platform.SALESFORCE:
        outputs = SALESFORCE_AGENT_OUTPUT_PATHS
        inputs = SALESFORCE_TRANSFORMATION_INPUT_PATHS
        commands = SALESFORCE_VALIDATION_COMMAND_IDS
        implementation_contract = SALESFORCE_IMPLEMENTATION_CONTRACT
    else:
        outputs = TARGET_FILES
        inputs = SOURCE_FILES
        commands = MULESOFT_VALIDATION_COMMAND_IDS
        implementation_contract = MULESOFT_IMPLEMENTATION_CONTRACT
    return MigrationManifest(
        manifest_id=f"manifest-{request.platform.value}-agent-run",
        request_id=request.request_id,
        platform=request.platform,
        base_revision=request.base_revision,
        approved_paths=outputs,
        dependencies=(),
        transformations=(
            TransformationStep(
                step_id=f"migrate-{request.platform.value}-bounded-slice",
                description="Create the exact shipped target file set.",
                input_paths=inputs,
                output_paths=outputs,
            ),
        ),
        validation_plan=tuple(
            ValidationCommand(
                check_id=f"check-{command}",
                command_id=command,
                purpose="Run one controller-owned local validation check.",
                environment=EnvironmentKind.LOCAL,
                required=True,
            )
            for command in commands
        ),
        implementation_contract=implementation_contract,
        required_approvals=(ApprovalAction.APPROVE_MANIFEST,),
    )


def _approval(status, selection: str = "approve") -> ManifestApproval:
    assert status.interrupt is not None
    return ManifestApproval(
        decision_id=status.interrupt.decision_id,
        request_id=status.interrupt.request_id,
        manifest_id=status.interrupt.manifest_id,
        manifest_digest=status.interrupt.manifest_digest,
        requested_action=status.interrupt.requested_action,
        selection=selection,
        reviewer="agent-run-reviewer",
        comment="Explicit test decision.",
    )


def _synthetic_correction_approval(request: MigrationRequest) -> CorrectionApproval:
    digest = "sha256:" + "1" * 64
    return CorrectionApproval(
        correction_id="correction-terminal-failure",
        request_id=request.request_id,
        manifest_id=f"manifest-{request.platform.value}-agent-run",
        manifest_digest=digest,
        report_id="report-terminal-failure",
        report_digest=digest,
        change_set_digest=digest,
        base_revision=request.base_revision,
        completed_attempt=1,
        authorized_attempt=2,
        action=CorrectionAction.RETRY_IMPLEMENTATION,
        reviewer="failure-state-reviewer",
    )


def _correction_approval(status, *, reviewer: str) -> CorrectionApproval:
    correction = status.correction
    assert correction is not None
    return CorrectionApproval(
        correction_id=correction.correction_id,
        request_id=correction.request_id,
        manifest_id=correction.manifest_id,
        manifest_digest=correction.manifest_digest,
        report_id=correction.report_id,
        report_digest=correction.report_digest,
        change_set_digest=correction.change_set_digest,
        base_revision=correction.base_revision,
        completed_attempt=correction.completed_attempt,
        authorized_attempt=2,
        action=correction.action,
        reviewer=reviewer,
    )


def _assert_failure_tree_is_sanitized(run_dir: Path) -> None:
    forbidden = (
        SENSITIVE_FAILURE.encode(),
        b"sk-provider-error-secret-123456789",
        b"/private/tmp/oracle",
        b"RuntimeError",
        b"ModelConfigurationError",
        b"ModelRefusalError",
        b"ModelOutputError",
        b"Traceback (most recent call last)",
    )
    files = [path for path in run_dir.rglob("*") if path.is_file()]
    assert files
    for path in files:
        payload = path.read_bytes()
        for value in forbidden:
            assert value not in payload, f"unsafe failure bytes persisted in {path}"


def _rewrite_latest_checkpoint_request_description(
    database: Path,
    *,
    thread_id: str,
    description: str,
) -> None:
    """Mutate only the request description in the exact latest checkpoint row."""

    serializer = strict_checkpoint_serializer()
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """
            SELECT checkpoint_ns, checkpoint_id, type, checkpoint
            FROM checkpoints
            WHERE thread_id = ?
            ORDER BY checkpoint_id DESC
            LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
        assert row is not None
        checkpoint_ns, checkpoint_id, payload_type, payload = row
        checkpoint = serializer.loads_typed((payload_type, payload))
        assert isinstance(checkpoint, dict)
        channel_values = checkpoint["channel_values"]
        assert isinstance(channel_values, dict)
        request = MigrationRequest.model_validate(channel_values["request"])
        channel_values["request"] = request.model_copy(
            update={"target": request.target.model_copy(update={"description": description})}
        )
        updated_type, updated_payload = serializer.dumps_typed(checkpoint)
        changed = connection.execute(
            """
            UPDATE checkpoints
            SET type = ?, checkpoint = ?
            WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
            """,
            (
                updated_type,
                updated_payload,
                thread_id,
                checkpoint_ns,
                checkpoint_id,
            ),
        ).rowcount
        assert changed == 1


def _replace_portable_json(path: Path, value: BaseModel | dict[str, object]) -> None:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _recompute_partial_index_artifact(
    index_path: Path, artifact_path: str, value: BaseModel
) -> None:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    artifacts = index["artifacts"]
    assert isinstance(artifacts, list)
    for artifact in artifacts:
        if artifact["path"] == artifact_path:
            artifact["canonical_sha256"] = artifact_digest(value)
            break
    else:
        raise AssertionError(f"partial index omitted {artifact_path}")
    _replace_portable_json(index_path, index)


def test_planned_lifecycle_anchors_the_exact_pending_manifest_request(
    tmp_path: Path,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / ".runs/run-pending-request"
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-pending-request",
        thread_id="thread-pending-request",
        source_root="source",
        request=request,
        models=_models(PresetStructuredModel(SF_FROZEN_OUTPUTS)),
        wiki_as_of=AS_OF,
    )

    assert started.interrupt is not None
    decision_path = (
        run_dir / "evidence/control" / f"decision-request-{started.interrupt.decision_id}.json"
    )
    persisted = DecisionRequest.model_validate_json(decision_path.read_text(encoding="utf-8"))
    manifest = _manifest(request)
    assert persisted == manifest_decision_request(
        request,
        manifest,
        manifest_digest(manifest),
    )
    planned_index = json.loads(
        (run_dir / "evidence/indexes/agent-run-planned.json").read_text(encoding="utf-8")
    )
    assert decision_path.relative_to(run_dir / "evidence").as_posix() in {
        artifact["path"] for artifact in planned_index["artifacts"]
    }
    checkpoint_path = run_dir / "evidence/status/agent-run-planned-checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert MigrationRequest.model_validate(checkpoint["request"]) == request
    assert {item["name"] for item in checkpoint["values"]} == {
        "manifest",
        "manifest_digest",
        "request",
        "status",
    }
    assert checkpoint["next"] == ["approval_gate"]
    assert len(checkpoint["tasks"]) == 1
    assert len(checkpoint["tasks"][0]["interrupts"]) == 1
    assert checkpoint_path.relative_to(run_dir / "evidence").as_posix() in {
        artifact["path"] for artifact in planned_index["artifacts"]
    }
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-pending-request",
            thread_id="thread-pending-request",
        )
        == started
    )


def test_planned_lifecycle_rejects_request_only_checkpoint_rewrite_before_engineer(
    tmp_path: Path,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    run_dir = project / ".runs/run-planned-request-rewrite"
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-planned-request-rewrite",
        thread_id="thread-planned-request-rewrite",
        source_root="source",
        request=request,
        models=_models(model),
        wiki_as_of=AS_OF,
    )

    _rewrite_latest_checkpoint_request_description(
        run_dir / "state/checkpoints.sqlite3",
        thread_id="thread-planned-request-rewrite",
        description="Mutated after the immutable plan was frozen.",
    )

    with pytest.raises(PolicyViolation, match="checkpoint request differs"):
        resume_agent_run(
            project,
            run_dir,
            run_id="run-planned-request-rewrite",
            thread_id="thread-planned-request-rewrite",
            approval=_approval(started),
            models=_models(model),
        )
    assert model.calls == ["ArchitectManifestProposal"]


@pytest.mark.parametrize(
    ("platform", "outputs"),
    (
        (Platform.SALESFORCE, SF_FROZEN_OUTPUTS),
        (Platform.MULESOFT, MULE_FROZEN_OUTPUTS),
    ),
)
def test_real_three_agent_run_reloads_exact_sqlite_thread_and_stops_unavailable(
    tmp_path: Path,
    platform: Platform,
    outputs: dict[str, str],
) -> None:
    project, request = _project(tmp_path, platform)
    run_dir = project / ".runs" / "run-1"
    architect_model = PresetStructuredModel(outputs)
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-1",
        thread_id="thread-1",
        source_root="source",
        request=request,
        models=_models(architect_model),
        wiki_as_of=AS_OF,
    )

    assert started.status == "awaiting_approval"
    assert started.pending_nodes == ("approval_gate",)
    assert architect_model.calls == ["ArchitectManifestProposal"]
    preset = agent_run_module._preset_for(platform)
    wiki_trace = RetrievalTrace.model_validate_json(
        (run_dir / f"evidence/model-runs/{request.request_id}/wiki-trace.json").read_text(
            encoding="utf-8"
        )
    )
    assert wiki_trace.query == preset.wiki_query
    assert wiki_trace.platform is platform
    assert wiki_trace.source_version == preset.source_version == request.target.source_version
    assert wiki_trace.target_version == preset.target_version == request.target.target_version
    assert wiki_trace.max_primary_hits == preset.wiki_max_primary_hits == 1
    assert wiki_trace.expand_links is True
    primary = wiki_trace.hits[0]
    assert primary.expanded_from is None
    assert "linked-page" not in primary.matched_fields
    if platform is Platform.SALESFORCE:
        assert tuple(hit.page_id for hit in wiki_trace.hits) == (
            "salesforce-visualforce-to-lwc",
            "salesforce-apex-security",
            "salesforce-validation",
        )
        linked = wiki_trace.hits[1:]
        assert all(hit.expanded_from == "salesforce-visualforce-to-lwc" for hit in linked)
    else:
        assert primary.page_id == "mulesoft-mule3-to-mule4"
        toolchain = next(
            hit for hit in wiki_trace.hits if hit.page_id == "mulesoft-toolchain-validation"
        )
        assert toolchain.expanded_from == "mulesoft-mule3-to-mule4"
        linked = tuple(hit for hit in wiki_trace.hits if hit.expanded_from is not None)
    assert linked
    assert all(hit.matched_fields == ("linked-page",) for hit in linked)
    assert all(hit.score == 0.25 for hit in linked)

    # Simulate a process restart with new client objects but the same immutable
    # public provider/model binding and the same SQLite database.
    resumed_model = PresetStructuredModel(outputs)
    completed = resume_agent_run(
        project,
        run_dir,
        run_id="run-1",
        thread_id="thread-1",
        approval=_approval(started),
        models=_models(resumed_model),
    )
    assert completed.status == "completed"
    assert completed.terminal_disposition == "environment_unavailable"
    assert completed.correction is not None
    assert completed.correction.action is CorrectionAction.STOP_ENVIRONMENT
    assert resumed_model.calls == ["EngineerModelOutcome", "ValidatorAdvisory"]
    control_root = run_dir / "evidence/control"
    persisted_manifest_approval = json.loads(
        (control_root / "manifest-approval.json").read_text(encoding="utf-8")
    )
    assert started.interrupt is not None
    assert persisted_manifest_approval["decision_id"] == started.interrupt.decision_id
    persisted_correction = CorrectionRequest.model_validate_json(
        (control_root / "correction-request-attempt-1.json").read_text(encoding="utf-8")
    )
    assert persisted_correction.action is CorrectionAction.STOP_ENVIRONMENT
    assert (
        control_root / f"decision-request-{persisted_manifest_approval['decision_id']}.json"
    ).is_file()

    before = list(resumed_model.calls)
    evidence_before = snapshot_tree(run_dir / "evidence").revision
    checkpoint_before = (run_dir / "state/checkpoints.sqlite3").read_bytes()
    inspected = get_agent_run_status(
        project,
        run_dir,
        run_id="run-1",
        thread_id="thread-1",
    )
    assert inspected == completed
    assert resumed_model.calls == before
    assert snapshot_tree(run_dir / "evidence").revision == evidence_before
    assert (run_dir / "state/checkpoints.sqlite3").read_bytes() == checkpoint_before

    original_oracle = (
        PROJECT_ROOT
        / "fixtures"
        / platform.value
        / ("account-contact-explorer" if platform is Platform.SALESFORCE else "customer-status-api")
        / "expected"
    )
    raw_architect_definition = (project / "agents/architect.md").read_bytes()
    for path in run_dir.rglob("*"):
        assert path.name.casefold() not in {"expected", "golden", "oracle"}
        if path.is_file():
            payload = path.read_bytes()
            assert str(original_oracle).encode() not in payload
            assert architect_model.api_key.encode() not in payload
            assert raw_architect_definition not in payload
    portable = json.dumps(inspected.model_dump(mode="json"), sort_keys=True)
    assert str(project) not in portable


@pytest.mark.parametrize(
    ("role", "failure_output", "operation"),
    (
        ("architect", ArchitectManifestProposal, "start"),
        ("engineer", EngineerModelOutcome, "resume"),
        ("validator", ValidatorAdvisory, "resume"),
    ),
)
def test_model_failures_are_sanitized_terminal_and_reloadable(
    tmp_path: Path,
    role: str,
    failure_output: type[BaseModel],
    operation: str,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / ".runs/run-provider-failure"
    exploding = ExplodingStructuredModel(SF_FROZEN_OUTPUTS, failure_output)
    if role == "architect":
        failed = start_agent_run(
            project,
            run_dir,
            run_id="run-provider-failure",
            thread_id="thread-provider-failure",
            source_root="source",
            request=request,
            models=_models(exploding),
            wiki_as_of=AS_OF,
        )
    else:
        architect = PresetStructuredModel(SF_FROZEN_OUTPUTS)
        started = start_agent_run(
            project,
            run_dir,
            run_id="run-provider-failure",
            thread_id="thread-provider-failure",
            source_root="source",
            request=request,
            models=_models(architect),
            wiki_as_of=AS_OF,
        )
        failed = resume_agent_run(
            project,
            run_dir,
            run_id="run-provider-failure",
            thread_id="thread-provider-failure",
            approval=_approval(started),
            models=_models(exploding),
        )

    assert failed.status == "failed"
    assert failed.terminal_disposition == "controlled_failure"
    assert failed.pending_nodes == ()
    assert failed.interrupt is None
    assert failed.correction is None
    assert failed.task_failed is True
    assert failed.failure is not None
    assert failed.failure.operation == operation
    assert failed.failure.seam == role
    assert failed.failure.category == "provider_unavailable"
    assert failed.failure.reason_code == "provider_unavailable"
    assert failed.failure.attempt == 1
    assert failed.failure.terminal is True
    assert failed.failure.retry_eligible is False
    assert (run_dir / "evidence/agent-run-failure.json").is_file()
    assert (run_dir / "evidence/indexes/agent-run-failed.json").is_file()

    calls = list(exploding.calls)
    reloaded = get_agent_run_status(
        project,
        run_dir,
        run_id="run-provider-failure",
        thread_id="thread-provider-failure",
    )
    assert reloaded == failed
    assert exploding.calls == calls
    _assert_failure_tree_is_sanitized(run_dir)

    with pytest.raises(PolicyViolation, match="terminally failed"):
        resume_agent_run(
            project,
            run_dir,
            run_id="run-provider-failure",
            thread_id="thread-provider-failure",
            approval=ManifestApproval(
                decision_id="decision-terminal-failure",
                request_id=request.request_id,
                manifest_id=f"manifest-{request.platform.value}-agent-run",
                manifest_digest="sha256:" + "1" * 64,
                requested_action=ApprovalAction.APPROVE_MANIFEST,
                selection="reject",
                reviewer="failure-state-reviewer",
            ),
            models=None,
        )
    with pytest.raises(PolicyViolation, match="terminally failed"):
        retry_agent_run(
            project,
            run_dir,
            run_id="run-provider-failure",
            thread_id="thread-provider-failure",
            approval=_synthetic_correction_approval(request),
            models=_models(PresetStructuredModel(SF_FROZEN_OUTPUTS)),
        )
    with pytest.raises((FileExistsError, PolicyViolation)):
        start_agent_run(
            project,
            run_dir,
            run_id="run-provider-failure",
            thread_id="thread-provider-failure",
            source_root="source",
            request=request,
            models=_models(PresetStructuredModel(SF_FROZEN_OUTPUTS)),
            wiki_as_of=AS_OF,
        )
    _assert_failure_tree_is_sanitized(run_dir)


@pytest.mark.parametrize(
    ("role", "output_type"),
    (
        ("engineer", EngineerModelOutcome),
        ("validator", ValidatorAdvisory),
    ),
)
@pytest.mark.parametrize("attempt", (1, 2))
def test_interrupted_role_invocation_is_not_redispatched_after_reload(
    tmp_path: Path,
    role: str,
    output_type: type[BaseModel],
    attempt: int,
) -> None:
    suffix = f"{role}-attempt-{attempt}"
    run_id = f"run-interrupted-{suffix}"
    thread_id = f"thread-interrupted-{suffix}"
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / f".runs/{run_id}"
    model = InterruptOnceStructuredModel(
        SF_FROZEN_OUTPUTS,
        output_type,
        interrupt_on_occurrence=attempt,
    )
    models = _models(model)
    trusted_validator = _recoverable_validator(run_id)
    started = start_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=trusted_validator,
    )
    manifest_approval = _approval(started)

    if attempt == 1:
        operation = partial(
            resume_agent_run,
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=manifest_approval,
            models=models,
            trusted_validator=trusted_validator,
        )
        operation_name = "resume"
        authorization_kind = "agent-run-manifest-authorized"
    else:
        attempt_one = resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=manifest_approval,
            models=models,
            trusted_validator=trusted_validator,
        )
        correction_approval = _correction_approval(
            attempt_one,
            reviewer=f"interrupted-{suffix}-reviewer",
        )
        operation = partial(
            retry_agent_run,
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=correction_approval,
            models=models,
            trusted_validator=trusted_validator,
        )
        operation_name = "retry"
        authorization_kind = "agent-run-correction-authorized"

    with pytest.raises(KeyboardInterrupt, match="process interruption after lease"):
        operation()

    role_output = output_type.__name__
    assert model.calls.count(role_output) == attempt
    lease_relative = (
        f"model-runs/{request.request_id}/{role}-invocation-lease-attempt-{attempt}.json"
    )
    runtime_lease = run_dir / "state/inflight-model-runs" / lease_relative
    portable_lease = run_dir / "evidence" / lease_relative
    assert runtime_lease.is_file()
    assert not portable_lease.exists()
    authorized_index = json.loads(
        (run_dir / f"evidence/indexes/{authorization_kind}.json").read_text(encoding="utf-8")
    )
    assert lease_relative not in {artifact["path"] for artifact in authorized_index["artifacts"]}

    failed = operation()

    assert model.calls.count(role_output) == attempt
    assert failed.status == "failed"
    assert failed.terminal_disposition == "controlled_failure"
    assert failed.failure is not None
    assert failed.failure.operation == operation_name
    assert failed.failure.seam == role
    assert failed.failure.category == "invalid"
    assert failed.failure.reason_code == "policy_rejected"
    assert failed.failure.attempt == attempt
    assert runtime_lease.is_file()
    assert portable_lease.is_file()
    failure_index = json.loads(
        (run_dir / "evidence/indexes/agent-run-failed.json").read_text(encoding="utf-8")
    )
    assert lease_relative in {artifact["path"] for artifact in failure_index["artifacts"]}
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
        )
        == failed
    )


@pytest.mark.parametrize(
    ("role", "artifact_name", "role_output"),
    (
        ("engineer", "engineer-attempt-1.json", "EngineerModelOutcome"),
        ("validator", "validator-attempt-1.json", "ValidatorAdvisory"),
    ),
)
def test_persisted_role_artifact_replays_after_pre_lifecycle_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    artifact_name: str,
    role_output: str,
) -> None:
    run_id = f"run-post-artifact-{role}"
    thread_id = f"thread-post-artifact-{role}"
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / f".runs/{run_id}"
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    models = _models(model)
    trusted_validator = _recoverable_validator(run_id)
    started = start_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=trusted_validator,
    )
    approval = _approval(started)
    original_write = run_session_module._WorkflowArtifactStore.write_json
    interrupted = False

    def interrupt_after_role_artifact(self, relative_path, value):
        nonlocal interrupted
        result = original_write(self, relative_path, value)
        if relative_path.endswith(artifact_name) and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("simulated interruption after role artifact")
        return result

    monkeypatch.setattr(
        run_session_module._WorkflowArtifactStore,
        "write_json",
        interrupt_after_role_artifact,
    )
    with pytest.raises(KeyboardInterrupt, match="interruption after role artifact"):
        resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=approval,
            models=models,
            trusted_validator=trusted_validator,
        )

    calls_before_reload = model.calls.count(role_output)
    assert calls_before_reload == 1
    completed = resume_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        approval=approval,
        models=models,
        trusted_validator=trusted_validator,
    )

    assert completed.terminal_disposition == "recoverable_failure"
    assert model.calls.count(role_output) == calls_before_reload
    artifact_relative = f"model-runs/{request.request_id}/{artifact_name}"
    assert (run_dir / "state/inflight-model-runs" / artifact_relative).is_file()
    assert (run_dir / "evidence" / artifact_relative).is_file()
    completed_index = json.loads(
        (run_dir / "evidence/indexes/agent-run-resumed.json").read_text(encoding="utf-8")
    )
    assert artifact_relative in {artifact["path"] for artifact in completed_index["artifacts"]}


@pytest.mark.parametrize("attempt", (1, 2))
@pytest.mark.parametrize("crash_point", ("before_control", "after_control", "after_promotion"))
def test_terminal_checkpoint_recovers_without_role_redispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attempt: int,
    crash_point: str,
) -> None:
    suffix = f"attempt-{attempt}-{crash_point.replace('_', '-')}"
    run_id = f"run-terminal-recovery-{suffix}"
    thread_id = f"thread-terminal-recovery-{suffix}"
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / f".runs/{run_id}"
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    models = _models(model)
    validator = _recoverable_validator(run_id)
    started = start_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=validator,
    )
    manifest_approval = _approval(started)
    if attempt == 1:
        operation = partial(
            resume_agent_run,
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=manifest_approval,
            models=models,
            trusted_validator=validator,
        )
        success_kind = "agent-run-resumed"
    else:
        attempt_one = resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=manifest_approval,
            models=models,
            trusted_validator=validator,
        )
        correction_approval = _correction_approval(
            attempt_one,
            reviewer=f"terminal-recovery-{suffix}-reviewer",
        )
        operation = partial(
            retry_agent_run,
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=correction_approval,
            models=models,
            trusted_validator=validator,
        )
        success_kind = "agent-run-retried"

    interrupted = False
    original_control = agent_run_module._persist_checkpoint_control_evidence
    original_freeze = agent_run_module._freeze_operation_evidence

    if crash_point in {"before_control", "after_control"}:

        def interrupt_control(components):
            nonlocal interrupted
            if interrupted:
                return original_control(components)
            interrupted = True
            if crash_point == "after_control":
                original_control(components)
            raise KeyboardInterrupt(f"terminal crash {crash_point}")

        monkeypatch.setattr(
            agent_run_module, "_persist_checkpoint_control_evidence", interrupt_control
        )
    else:

        def interrupt_after_promotion(session, kind, status, workflow_status, checkpoint):
            nonlocal interrupted
            if kind == success_kind and not interrupted:
                interrupted = True
                session.promote_model_operation_artifacts()
                raise KeyboardInterrupt("terminal crash after_promotion")
            return original_freeze(session, kind, status, workflow_status, checkpoint)

        monkeypatch.setattr(
            agent_run_module, "_freeze_operation_evidence", interrupt_after_promotion
        )

    with pytest.raises(KeyboardInterrupt, match="terminal crash"):
        operation()
    calls_before_recovery = list(model.calls)
    assert calls_before_recovery.count("EngineerModelOutcome") == attempt
    assert calls_before_recovery.count("ValidatorAdvisory") == attempt

    completed = operation()

    assert list(model.calls) == calls_before_recovery
    assert completed.status == "completed"
    assert completed.execution_attempt == attempt
    assert completed.terminal_disposition == (
        "recoverable_failure" if attempt == 1 else "ready_for_human_review"
    )
    model_root = f"model-runs/{request.request_id}"
    required = {
        f"{model_root}/engineer-invocation-lease-attempt-{attempt}.json",
        f"{model_root}/engineer-attempt-{attempt}.json",
        f"{model_root}/report-attempt-{attempt}.json",
        f"{model_root}/validator-invocation-lease-attempt-{attempt}.json",
        f"{model_root}/validator-attempt-{attempt}.json",
        f"control/correction-request-attempt-{attempt}.json",
    }
    if attempt == 2:
        required.add(f"{model_root}/engineer-correction-attempt-2.json")
    index = json.loads(
        (run_dir / f"evidence/indexes/{success_kind}.json").read_text(encoding="utf-8")
    )
    assert required <= {artifact["path"] for artifact in index["artifacts"]}
    assert all((run_dir / "evidence" / path).is_file() for path in required)


@pytest.mark.parametrize("crash_point", ("before_control", "after_control"))
@pytest.mark.parametrize("attempt", (1, 2))
def test_terminal_engineer_intervention_recovers_without_redispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
    attempt: int,
) -> None:
    suffix = f"attempt-{attempt}-{crash_point.replace('_', '-')}"
    run_id = f"run-terminal-intervention-{suffix}"
    thread_id = f"thread-terminal-intervention-{suffix}"
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / f".runs/{run_id}"
    model = EngineerStopStructuredModel(SF_FROZEN_OUTPUTS, stop_on_occurrence=attempt)
    models = _models(model)
    validator = _recoverable_validator(run_id)
    started = start_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=validator,
    )
    manifest_approval = _approval(started)
    if attempt == 1:
        operation = partial(
            resume_agent_run,
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=manifest_approval,
            models=models,
            trusted_validator=validator,
        )
        success_kind = "agent-run-resumed"
    else:
        attempt_one = resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=manifest_approval,
            models=models,
            trusted_validator=validator,
        )
        operation = partial(
            retry_agent_run,
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=_correction_approval(
                attempt_one,
                reviewer=f"terminal-intervention-{suffix}-reviewer",
            ),
            models=models,
            trusted_validator=validator,
        )
        success_kind = "agent-run-retried"
    original_control = agent_run_module._persist_checkpoint_control_evidence
    interrupted = False

    def interrupt_control(components):
        nonlocal interrupted
        if interrupted:
            return original_control(components)
        interrupted = True
        if crash_point == "after_control":
            original_control(components)
        raise KeyboardInterrupt(f"terminal intervention crash {crash_point}")

    monkeypatch.setattr(agent_run_module, "_persist_checkpoint_control_evidence", interrupt_control)
    with pytest.raises(KeyboardInterrupt, match="terminal intervention crash"):
        operation()
    calls_before_recovery = list(model.calls)

    completed = operation()

    assert model.calls == calls_before_recovery
    assert completed.status == "decision_required"
    assert completed.terminal_disposition == "decision_required"
    model_root = f"model-runs/{request.request_id}"
    index = json.loads(
        (run_dir / f"evidence/indexes/{success_kind}.json").read_text(encoding="utf-8")
    )
    paths = {artifact["path"] for artifact in index["artifacts"]}
    assert f"{model_root}/engineer-invocation-lease-attempt-{attempt}.json" in paths
    assert f"{model_root}/engineer-attempt-{attempt}.json" in paths
    assert f"{model_root}/validator-invocation-lease-attempt-{attempt}.json" not in paths
    assert f"{model_root}/validator-attempt-{attempt}.json" not in paths
    assert f"{model_root}/report-attempt-{attempt}.json" not in paths


def test_validation_terminal_rejects_injected_decision_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-terminal-injected-decision"
    thread_id = "thread-terminal-injected-decision"
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / f".runs/{run_id}"
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    models = _models(model)
    validator = _recoverable_validator(run_id)
    started = start_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=validator,
    )
    approval = _approval(started)
    interrupted = False
    original_control = agent_run_module._persist_checkpoint_control_evidence

    def interrupt_once(components):
        nonlocal interrupted
        if interrupted:
            return original_control(components)
        interrupted = True
        raise KeyboardInterrupt("pause validation terminal before control evidence")

    monkeypatch.setattr(agent_run_module, "_persist_checkpoint_control_evidence", interrupt_once)
    with pytest.raises(KeyboardInterrupt, match="pause validation terminal"):
        resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=approval,
            models=models,
            trusted_validator=validator,
        )
    calls_before = list(model.calls)
    _replace_portable_json(
        run_dir / "evidence/control/decision-request-injected.json",
        {"injected": True},
    )

    with pytest.raises(
        PolicyViolation,
        match="validation terminal cannot add decision-request evidence",
    ):
        resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=approval,
            models=models,
            trusted_validator=validator,
        )
    assert model.calls == calls_before


def test_completed_run_retains_exact_inventory_with_runtime_replay_copies(
    tmp_path: Path,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / ".runs/run-completed-extra-evidence"
    models = _models(PresetStructuredModel(SF_FROZEN_OUTPUTS))
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-completed-extra-evidence",
        thread_id="thread-completed-extra-evidence",
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=_recoverable_validator("run-completed-extra-evidence"),
    )
    resume_agent_run(
        project,
        run_dir,
        run_id="run-completed-extra-evidence",
        thread_id="thread-completed-extra-evidence",
        approval=_approval(started),
        models=models,
        trusted_validator=_recoverable_validator("run-completed-extra-evidence"),
    )
    _replace_portable_json(run_dir / "evidence/unexpected.json", {"unexpected": True})

    with pytest.raises(PolicyViolation, match="does not contain the exact artifact set"):
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-completed-extra-evidence",
            thread_id="thread-completed-extra-evidence",
        )


def test_concurrent_exact_resumes_dispatch_once_and_keep_one_terminal_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-concurrent-resume"
    thread_id = "thread-concurrent-resume"
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / f".runs/{run_id}"
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    models = _models(model)
    validator = _recoverable_validator(run_id)
    started = start_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=validator,
    )
    approval = _approval(started)
    authorization_barrier = threading.Barrier(2)

    def synchronize_authorized_callers() -> None:
        authorization_barrier.wait(timeout=10)

    monkeypatch.setattr(
        agent_run_module,
        "_after_authorization_boundary",
        synchronize_authorized_callers,
    )

    def resume() -> object:
        return resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=approval,
            models=models,
            trusted_validator=validator,
        )

    outcomes: list[object] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(resume), executor.submit(resume))
        for future in futures:
            try:
                outcomes.append(future.result(timeout=20))
            except Exception as exc:  # noqa: BLE001 - result is asserted below
                outcomes.append(exc)

    statuses = [item for item in outcomes if not isinstance(item, Exception)]
    errors = [item for item in outcomes if isinstance(item, Exception)]
    assert len(statuses) == 1, repr(outcomes)
    assert len(errors) == 1
    assert isinstance(errors[0], PolicyViolation)
    assert str(errors[0]) == "agent run operation is already in progress at the engineer seam"
    assert model.calls.count("EngineerModelOutcome") == 1
    assert model.calls.count("ValidatorAdvisory") == 1
    assert not (run_dir / "evidence/indexes/agent-run-failed.json").exists()
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
        )
        == statuses[0]
    )


@pytest.mark.parametrize("tamper", ("digest", "binding"))
def test_interrupted_role_invocation_rejects_tampered_runtime_lease(
    tmp_path: Path,
    tamper: str,
) -> None:
    run_id = f"run-tampered-lease-{tamper}"
    thread_id = f"thread-tampered-lease-{tamper}"
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / f".runs/{run_id}"
    model = InterruptOnceStructuredModel(
        SF_FROZEN_OUTPUTS,
        EngineerModelOutcome,
        interrupt_on_occurrence=1,
    )
    models = _models(model)
    started = start_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=_recoverable_validator(run_id),
    )
    approval = _approval(started)
    with pytest.raises(KeyboardInterrupt):
        resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=approval,
            models=models,
            trusted_validator=_recoverable_validator(run_id),
        )

    lease_path = (
        run_dir
        / "state/inflight-model-runs/model-runs"
        / request.request_id
        / "engineer-invocation-lease-attempt-1.json"
    )
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    if tamper == "digest":
        lease["lease_digest"] = "sha256:" + "f" * 64
        expected_error = "runtime invocation lease is malformed"
    else:
        lease["binding"]["input_evidence_digest"] = "sha256:" + "e" * 64
        lease["lease_digest"] = artifact_digest(
            {
                "invocation_id": lease["invocation_id"],
                "binding": lease["binding"],
            }
        )
        expected_error = "persisted role invocation lease differs from exact provider input"
    _replace_portable_json(lease_path, lease)
    calls_before_reload = list(model.calls)

    if tamper == "digest":
        with pytest.raises(PolicyViolation, match=expected_error):
            get_agent_run_status(
                project,
                run_dir,
                run_id=run_id,
                thread_id=thread_id,
            )
    else:
        failed = resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=approval,
            models=models,
            trusted_validator=_recoverable_validator(run_id),
        )
        assert failed.failure is not None
        assert failed.failure.seam == "engineer"
        assert failed.failure.reason_code == "policy_rejected"
    assert model.calls == calls_before_reload


@pytest.mark.parametrize(
    ("role", "invalid_output", "operation"),
    (
        ("architect", ArchitectManifestProposal, "start"),
        ("engineer", EngineerModelOutcome, "resume"),
        ("validator", ValidatorAdvisory, "resume"),
    ),
)
def test_schema_valid_semantic_role_failures_become_durable_invalid_failures(
    tmp_path: Path,
    role: str,
    invalid_output: type[BaseModel],
    operation: str,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / ".runs/run-semantic-failure"
    invalid = SemanticallyInvalidStructuredModel(SF_FROZEN_OUTPUTS, invalid_output)
    if role == "architect":
        failed = start_agent_run(
            project,
            run_dir,
            run_id="run-semantic-failure",
            thread_id="thread-semantic-failure",
            source_root="source",
            request=request,
            models=_models(invalid),
            wiki_as_of=AS_OF,
        )
    else:
        started = start_agent_run(
            project,
            run_dir,
            run_id="run-semantic-failure",
            thread_id="thread-semantic-failure",
            source_root="source",
            request=request,
            models=_models(PresetStructuredModel(SF_FROZEN_OUTPUTS)),
            wiki_as_of=AS_OF,
        )
        failed = resume_agent_run(
            project,
            run_dir,
            run_id="run-semantic-failure",
            thread_id="thread-semantic-failure",
            approval=_approval(started),
            models=_models(invalid),
        )

    assert failed.status == "failed"
    assert failed.failure is not None
    assert failed.failure.operation == operation
    assert failed.failure.seam == role
    assert failed.failure.category == "invalid"
    assert failed.failure.reason_code in {
        "policy_rejected",
        "structured_output_invalid",
    }
    assert failed.failure.terminal is True
    assert failed.failure.retry_eligible is False
    assert (run_dir / "evidence/agent-run-failure.json").is_file()
    assert (run_dir / "evidence/indexes/agent-run-failed.json").is_file()
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-semantic-failure",
            thread_id="thread-semantic-failure",
        )
        == failed
    )


def test_architect_generated_output_chaining_has_typed_transformation_scope_failure(
    tmp_path: Path,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / ".runs/run-transformation-scope-failure"

    class GeneratedOutputChainingModel(PresetStructuredModel):
        def parse(
            self,
            *,
            system_prompt: str,
            input_value: BaseModel,
            output_type: type[BaseModel],
        ) -> BaseModel:
            proposal = ArchitectManifestProposal.model_validate(
                super().parse(
                    system_prompt=system_prompt,
                    input_value=input_value,
                    output_type=output_type,
                )
            )
            transformations = list(proposal.manifest.transformations)
            first = transformations[0]
            transformations[0] = first.model_copy(
                update={
                    "input_paths": (
                        *first.input_paths,
                        proposal.manifest.approved_paths[0],
                    )
                }
            )
            manifest = proposal.manifest.model_copy(
                update={"transformations": tuple(transformations)}
            )
            return proposal.model_copy(update={"manifest": manifest})

    failed = start_agent_run(
        project,
        run_dir,
        run_id="run-transformation-scope-failure",
        thread_id="thread-transformation-scope-failure",
        source_root="source",
        request=request,
        models=_models(GeneratedOutputChainingModel(SF_FROZEN_OUTPUTS)),
        wiki_as_of=AS_OF,
    )

    assert failed.status == "failed"
    assert failed.failure is not None
    assert failed.failure.seam == "architect"
    assert failed.failure.category == "invalid"
    assert failed.failure.reason_code == "transformation_scope_invalid"
    assert not tuple(run_dir.glob("evidence/model-runs/*/architect-attempt-*.json"))
    public_failure = (run_dir / "evidence/agent-run-failure.json").read_text(encoding="utf-8")
    assert SALESFORCE_AGENT_OUTPUT_PATHS[0] not in public_failure
    _assert_failure_tree_is_sanitized(run_dir)


def test_architect_implementation_contract_drift_has_typed_failure(
    tmp_path: Path,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / ".runs/run-implementation-contract-failure"

    class MissingImplementationContractModel(PresetStructuredModel):
        def parse(
            self,
            *,
            system_prompt: str,
            input_value: BaseModel,
            output_type: type[BaseModel],
        ) -> BaseModel:
            proposal = ArchitectManifestProposal.model_validate(
                super().parse(
                    system_prompt=system_prompt,
                    input_value=input_value,
                    output_type=output_type,
                )
            )
            manifest = proposal.manifest.model_copy(update={"implementation_contract": ()})
            return proposal.model_copy(update={"manifest": manifest})

    failed = start_agent_run(
        project,
        run_dir,
        run_id="run-implementation-contract-failure",
        thread_id="thread-implementation-contract-failure",
        source_root="source",
        request=request,
        models=_models(MissingImplementationContractModel(SF_FROZEN_OUTPUTS)),
        wiki_as_of=AS_OF,
    )

    assert failed.status == "failed"
    assert failed.failure is not None
    assert failed.failure.seam == "architect"
    assert failed.failure.category == "invalid"
    assert failed.failure.reason_code == "implementation_contract_invalid"
    assert not tuple(run_dir.glob("evidence/model-runs/*/architect-attempt-*.json"))
    _assert_failure_tree_is_sanitized(run_dir)


@pytest.mark.parametrize(
    ("exception", "category", "reason_code"),
    (
        (
            ModelConfigurationError(SENSITIVE_FAILURE),
            "configuration",
            "configuration_invalid",
        ),
        (ModelRefusalError(SENSITIVE_FAILURE), "refusal", "provider_refusal"),
        (
            ModelOutputError("model response was incomplete: " + SENSITIVE_FAILURE),
            "incomplete",
            "response_incomplete",
        ),
        (ModelOutputError(SENSITIVE_FAILURE), "invalid", "provider_response_invalid"),
        (
            ModelOutputError("model structured output failed schema validation"),
            "invalid",
            "structured_output_invalid",
        ),
        (
            ModelOutputError("local Ollama response attempted an unauthorized tool call"),
            "invalid",
            "unauthorized_tool_call",
        ),
        (
            ModelOutputError("local Ollama inventory contained an invalid model digest"),
            "invalid",
            "model_inventory_invalid",
        ),
        (
            ModelRuntimeError("local Ollama request exceeded its deadline"),
            "provider_unavailable",
            "provider_timeout",
        ),
    ),
)
def test_model_failure_categories_are_typed_without_original_error_bytes(
    tmp_path: Path,
    exception: Exception,
    category: str,
    reason_code: str,
) -> None:
    project, request = _project(tmp_path / category, Platform.SALESFORCE)
    run_dir = project / ".runs/run-typed-failure"
    model = ExplodingStructuredModel(
        SF_FROZEN_OUTPUTS,
        ArchitectManifestProposal,
        failure=exception,
    )
    failed = start_agent_run(
        project,
        run_dir,
        run_id="run-typed-failure",
        thread_id="thread-typed-failure",
        source_root="source",
        request=request,
        models=_models(model),
        wiki_as_of=AS_OF,
    )
    assert failed.failure is not None
    assert failed.failure.category == category
    assert failed.failure.reason_code == reason_code
    _assert_failure_tree_is_sanitized(run_dir)


def test_deterministic_validator_failure_is_sanitized_and_terminal(tmp_path: Path) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / ".runs/run-validator-failure"
    architect = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-validator-failure",
        thread_id="thread-validator-failure",
        source_root="source",
        request=request,
        models=_models(architect),
        wiki_as_of=AS_OF,
    )

    def exploding_validator(*args: object, **kwargs: object) -> ValidationReport:
        del args, kwargs
        raise RuntimeError(SENSITIVE_FAILURE)

    resumed_model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    failed = resume_agent_run(
        project,
        run_dir,
        run_id="run-validator-failure",
        thread_id="thread-validator-failure",
        approval=_approval(started),
        models=_models(resumed_model),
        trusted_validator=exploding_validator,
    )
    assert resumed_model.calls == ["EngineerModelOutcome"]
    assert failed.failure is not None
    assert failed.failure.operation == "resume"
    assert failed.failure.seam == "deterministic_validator"
    assert failed.failure.category == "internal"
    assert failed.failure.reason_code == "deterministic_validation_failed"
    assert failed.failure.retry_eligible is False
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-validator-failure",
            thread_id="thread-validator-failure",
        )
        == failed
    )
    _assert_failure_tree_is_sanitized(run_dir)


@pytest.mark.parametrize(
    ("seam", "failure_output"),
    (
        ("architect", ArchitectManifestProposal),
        ("engineer", EngineerModelOutcome),
        ("validator", ValidatorAdvisory),
        ("deterministic_validator", None),
    ),
)
def test_each_controlled_failure_rejects_request_only_checkpoint_rewrite(
    tmp_path: Path,
    seam: str,
    failure_output: type[BaseModel] | None,
) -> None:
    project, request = _project(tmp_path / seam, Platform.SALESFORCE)
    run_id = f"run-{seam}-request-rewrite"
    thread_id = f"thread-{seam}-request-rewrite"
    run_dir = project / f".runs/{run_id}"
    if seam == "architect":
        assert failure_output is not None
        failed = start_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            source_root="source",
            request=request,
            models=_models(ExplodingStructuredModel(SF_FROZEN_OUTPUTS, failure_output)),
            wiki_as_of=AS_OF,
        )
    else:
        started = start_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            source_root="source",
            request=request,
            models=_models(PresetStructuredModel(SF_FROZEN_OUTPUTS)),
            wiki_as_of=AS_OF,
        )
        if seam == "deterministic_validator":

            def exploding_validator(*args: object, **kwargs: object) -> ValidationReport:
                del args, kwargs
                raise RuntimeError(SENSITIVE_FAILURE)

            failed = resume_agent_run(
                project,
                run_dir,
                run_id=run_id,
                thread_id=thread_id,
                approval=_approval(started),
                models=_models(PresetStructuredModel(SF_FROZEN_OUTPUTS)),
                trusted_validator=exploding_validator,
            )
        else:
            assert failure_output is not None
            failed = resume_agent_run(
                project,
                run_dir,
                run_id=run_id,
                thread_id=thread_id,
                approval=_approval(started),
                models=_models(ExplodingStructuredModel(SF_FROZEN_OUTPUTS, failure_output)),
            )
    assert failed.failure is not None
    assert failed.failure.seam == seam

    _rewrite_latest_checkpoint_request_description(
        run_dir / "state/checkpoints.sqlite3",
        thread_id=thread_id,
        description=f"Substituted checkpoint request for {seam}.",
    )

    with pytest.raises(PolicyViolation, match="checkpoint request differs"):
        get_agent_run_status(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
        )


def test_reject_and_modify_do_not_need_or_call_engineer_or_validator(tmp_path: Path) -> None:
    for selection in ("reject", "modify"):
        project, request = _project(tmp_path / selection, Platform.SALESFORCE)
        model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
        started = start_agent_run(
            project,
            project / ".runs/run-1",
            run_id="run-1",
            thread_id="thread-1",
            source_root="source",
            request=request,
            models=_models(model),
            wiki_as_of=AS_OF,
        )
        terminal = resume_agent_run(
            project,
            project / ".runs/run-1",
            run_id="run-1",
            thread_id="thread-1",
            approval=_approval(started, selection),
            models=None,
        )
        assert terminal.status == (
            "rejected" if selection == "reject" else "modification_requested"
        )
        assert model.calls == ["ArchitectManifestProposal"]


def test_nonportable_manifest_approval_is_rejected_before_checkpoint_mutation(
    tmp_path: Path,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    run_dir = project / ".runs/run-unsafe-approval"
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-unsafe-approval",
        thread_id="thread-unsafe-approval",
        source_root="source",
        request=request,
        models=_models(model),
        wiki_as_of=AS_OF,
    )
    unsafe = _approval(started).model_copy(update={"comment": "sk-approval-secret-123456789"})
    checkpoint = run_dir / "state/checkpoints.sqlite3"
    before = checkpoint.read_bytes()
    evidence_before = snapshot_tree(run_dir / "evidence").revision

    with pytest.raises(PolicyViolation):
        resume_agent_run(
            project,
            run_dir,
            run_id="run-unsafe-approval",
            thread_id="thread-unsafe-approval",
            approval=unsafe,
            models=_models(model),
        )

    assert checkpoint.read_bytes() == before
    assert snapshot_tree(run_dir / "evidence").revision == evidence_before
    assert not (run_dir / "evidence/control/manifest-approval.json").exists()
    assert not (run_dir / "evidence/indexes/agent-run-manifest-authorized.json").exists()
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-unsafe-approval",
            thread_id="thread-unsafe-approval",
        ).status
        == "awaiting_approval"
    )


def test_manifest_authorization_recovers_after_pre_execution_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    run_dir = project / ".runs/run-manifest-authorization"
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-manifest-authorization",
        thread_id="thread-manifest-authorization",
        source_root="source",
        request=request,
        models=_models(model),
        wiki_as_of=AS_OF,
    )
    exact = _approval(started)
    interrupted = False

    def interrupt_once() -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run._after_authorization_boundary",
        interrupt_once,
    )
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        resume_agent_run(
            project,
            run_dir,
            run_id="run-manifest-authorization",
            thread_id="thread-manifest-authorization",
            approval=exact,
            models=_models(model),
        )

    assert model.calls == ["ArchitectManifestProposal"]
    control_root = run_dir / "evidence/control"
    assert (
        ManifestApproval.model_validate_json(
            (control_root / "manifest-approval.json").read_text(encoding="utf-8")
        )
        == exact
    )
    authorized_index = json.loads(
        (run_dir / "evidence/indexes/agent-run-manifest-authorized.json").read_text(
            encoding="utf-8"
        )
    )
    indexed = {artifact["path"] for artifact in authorized_index["artifacts"]}
    assert "control/manifest-approval.json" in indexed
    assert f"control/decision-request-{exact.decision_id}.json" in indexed
    assert "status/agent-run-manifest-authorized.json" in indexed
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-manifest-authorization",
            thread_id="thread-manifest-authorization",
        )
        == started
    )

    different = exact.model_copy(update={"reviewer": "different-reviewer"})
    with pytest.raises(PolicyViolation, match="immutable authorization intent"):
        resume_agent_run(
            project,
            run_dir,
            run_id="run-manifest-authorization",
            thread_id="thread-manifest-authorization",
            approval=different,
            models=_models(model),
        )
    assert model.calls == ["ArchitectManifestProposal"]

    completed = resume_agent_run(
        project,
        run_dir,
        run_id="run-manifest-authorization",
        thread_id="thread-manifest-authorization",
        approval=exact,
        models=_models(model),
    )
    assert completed.terminal_disposition == "environment_unavailable"
    assert model.calls == [
        "ArchitectManifestProposal",
        "EngineerModelOutcome",
        "ValidatorAdvisory",
    ]


@pytest.mark.parametrize(
    "interrupted_step",
    ("approval", "status", "workflow_status", "checkpoint", "index", "anchor"),
)
def test_manifest_authorization_recovers_from_each_partial_freeze_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted_step: str,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    run_dir = project / f".runs/run-partial-manifest-{interrupted_step}"
    run_id = f"run-partial-manifest-{interrupted_step}"
    thread_id = f"thread-partial-manifest-{interrupted_step}"
    started = start_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        source_root="source",
        request=request,
        models=_models(model),
        wiki_as_of=AS_OF,
    )
    exact = _approval(started)
    interrupted = False

    def interrupt_partial_freeze(kind: str, step: str) -> None:
        nonlocal interrupted
        if kind == "agent-run-manifest-authorized" and step == interrupted_step and not interrupted:
            interrupted = True
            raise RuntimeError(f"interrupted after {step}")

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run._after_authorization_write",
        interrupt_partial_freeze,
    )
    with pytest.raises(RuntimeError, match=f"interrupted after {interrupted_step}"):
        resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=exact,
            models=_models(model),
        )

    assert model.calls == ["ArchitectManifestProposal"]
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
        )
        == started
    )
    substituted = exact.model_copy(update={"reviewer": "substituted-reviewer"})
    mismatch = (
        "immutable authorization intent"
        if interrupted_step == "anchor"
        else "differs from the partial authorization"
    )
    with pytest.raises(PolicyViolation, match=mismatch):
        resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=substituted,
            models=_models(model),
        )
    assert model.calls == ["ArchitectManifestProposal"]

    completed = resume_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        approval=exact,
        models=_models(model),
    )
    assert completed.terminal_disposition == "environment_unavailable"
    assert model.calls == [
        "ArchitectManifestProposal",
        "EngineerModelOutcome",
        "ValidatorAdvisory",
    ]


def test_manifest_authorization_intent_survives_crash_before_portable_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    run_id = "run-manifest-intent-only"
    thread_id = "thread-manifest-intent-only"
    run_dir = project / f".runs/{run_id}"
    started = start_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        source_root="source",
        request=request,
        models=_models(model),
        wiki_as_of=AS_OF,
    )
    exact = _approval(started)
    interrupted = False

    def interrupt_after_intent(kind: str, step: str) -> None:
        nonlocal interrupted
        if kind == "agent-run-manifest-authorized" and step == "intent" and not interrupted:
            interrupted = True
            raise RuntimeError("interrupted after authorization intent")

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run._after_authorization_write",
        interrupt_after_intent,
    )
    with pytest.raises(RuntimeError, match="interrupted after authorization intent"):
        resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=exact,
            models=_models(model),
        )

    assert (run_dir / "state/anchors/agent-run-manifest-authorization-intent.json").is_file()
    assert not (run_dir / "evidence/control/manifest-approval.json").exists()
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
        )
        == started
    )

    substituted = exact.model_copy(
        update={"selection": "reject", "reviewer": "substituted-intent-reviewer"}
    )
    with pytest.raises(PolicyViolation, match="immutable authorization intent"):
        resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=substituted,
            models=_models(model),
        )
    assert model.calls == ["ArchitectManifestProposal"]

    completed = resume_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        approval=exact,
        models=_models(model),
    )
    assert completed.terminal_disposition == "environment_unavailable"


@pytest.mark.parametrize("interrupted_step", ("approval", "index"))
def test_manifest_authorization_rejects_replaced_approval_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted_step: str,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    run_id = f"run-manifest-replaced-{interrupted_step}"
    thread_id = f"thread-manifest-replaced-{interrupted_step}"
    run_dir = project / f".runs/{run_id}"
    started = start_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        source_root="source",
        request=request,
        models=_models(model),
        wiki_as_of=AS_OF,
    )
    exact = _approval(started)
    interrupted = False

    def interrupt_partial(kind: str, step: str) -> None:
        nonlocal interrupted
        if kind == "agent-run-manifest-authorized" and step == interrupted_step and not interrupted:
            interrupted = True
            raise RuntimeError(f"interrupted manifest replacement at {step}")

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run._after_authorization_write",
        interrupt_partial,
    )
    with pytest.raises(RuntimeError, match="interrupted manifest replacement"):
        resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=exact,
            models=_models(model),
        )

    substituted = exact.model_copy(
        update={"selection": "reject", "reviewer": "replacement-reviewer"}
    )
    approval_path = run_dir / "evidence/control/manifest-approval.json"
    _replace_portable_json(approval_path, substituted)
    if interrupted_step == "index":
        _recompute_partial_index_artifact(
            run_dir / "evidence/indexes/agent-run-manifest-authorized.json",
            "control/manifest-approval.json",
            substituted,
        )

    with pytest.raises(PolicyViolation, match="immutable authorization intent"):
        resume_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=substituted,
            models=_models(model),
        )
    assert model.calls == ["ArchitectManifestProposal"]


def test_engineer_decision_required_stops_before_validator(tmp_path: Path) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = EngineerStopStructuredModel(SF_FROZEN_OUTPUTS)
    run_dir = project / ".runs/run-stop"
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-stop",
        thread_id="thread-stop",
        source_root="source",
        request=request,
        models=_models(model),
        wiki_as_of=AS_OF,
    )
    stopped = resume_agent_run(
        project,
        run_dir,
        run_id="run-stop",
        thread_id="thread-stop",
        approval=_approval(started),
        models=_models(model),
    )

    assert stopped.status == "decision_required"
    assert stopped.terminal_disposition == "decision_required"
    assert model.calls == ["ArchitectManifestProposal", "EngineerModelOutcome"]
    assert not tuple((run_dir / "evidence/model-runs").glob("validator-*.json"))


def test_unresolved_graph_stops_before_any_model_call(tmp_path: Path) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    controller = (
        project / "source/force-app/main/default/classes/LegacyAccountContactExplorerController.cls"
    )
    source = controller.read_text(encoding="utf-8")
    prefix, closing = source.rsplit("}", 1)
    controller.write_text(
        prefix
        + "\n    public static void unresolvedQuery() {\n"
        + "        Database.query('SELECT Id FROM Account');\n"
        + "    }\n}\n"
        + closing,
        encoding="utf-8",
    )
    request = request.model_copy(update={"base_revision": content_revision(project / "source")})
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    run_dir = project / ".runs/run-unresolved"
    stopped = start_agent_run(
        project,
        run_dir,
        run_id="run-unresolved",
        thread_id="thread-unresolved",
        source_root="source",
        request=request,
        models=_models(model),
        wiki_as_of=AS_OF,
    )

    assert stopped.status == "decision_required"
    assert stopped.terminal_disposition == "decision_required"
    assert model.calls == []
    model_root = run_dir / "evidence/model-runs" / request.request_id
    assert (model_root / "architect-preflight.json").is_file()
    assert not (model_root / "architect.json").exists()


def test_start_and_reload_reject_wrong_binding_before_checkpoint_use(tmp_path: Path) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    run_dir = project / ".runs/run-1"
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-1",
        thread_id="thread-1",
        source_root="source",
        request=request,
        models=_models(model),
        wiki_as_of=AS_OF,
    )
    with pytest.raises(PolicyViolation, match="run_id"):
        get_agent_run_status(project, run_dir, run_id="wrong", thread_id="thread-1")
    with pytest.raises(PolicyViolation, match="thread_id"):
        get_agent_run_status(project, run_dir, run_id="run-1", thread_id="wrong")
    wrong_request = request.model_copy(update={"request_id": "request-wrong"})
    with pytest.raises(PolicyViolation, match="caller request"):
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-1",
            thread_id="thread-1",
            request=wrong_request,
        )

    wrong_model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    wrong_model.model_id = "different-model/v1"  # type: ignore[misc]
    with pytest.raises(PolicyViolation, match="model provider or model ID"):
        resume_agent_run(
            project,
            run_dir,
            run_id="run-1",
            thread_id="thread-1",
            approval=_approval(started),
            models=_models(wrong_model),
        )
    assert wrong_model.calls == []

    with pytest.raises((FileExistsError, PolicyViolation)):
        start_agent_run(
            project,
            run_dir,
            run_id="run-2",
            thread_id="thread-2",
            source_root="source",
            request=request,
            models=_models(model),
            wiki_as_of=AS_OF,
        )

    wrong_approval = _approval(started, "reject").model_copy(
        update={"decision_id": "manifest-approval-wrong"}
    )
    with pytest.raises(PolicyViolation, match="different decision"):
        resume_agent_run(
            project,
            run_dir,
            run_id="run-1",
            thread_id="thread-1",
            approval=wrong_approval,
            models=None,
        )
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-1",
            thread_id="thread-1",
        ).status
        == "awaiting_approval"
    )

    source_file = project / "source/sfdx-project.json"
    original_source = source_file.read_bytes()
    source_file.write_bytes(original_source + b"\n")
    with pytest.raises(PolicyViolation, match="source content revision changed"):
        get_agent_run_status(project, run_dir, run_id="run-1", thread_id="thread-1")
    source_file.write_bytes(original_source)

    with (project / "agents/engineer.md").open("a", encoding="utf-8") as handle:
        handle.write("\nBinding-changing test sentence.\n")
    with pytest.raises(PolicyViolation, match="agent definitions"):
        get_agent_run_status(project, run_dir, run_id="run-1", thread_id="thread-1")


def test_invalid_preset_and_secret_identity_fail_before_run_creation(tmp_path: Path) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    invalid = request.model_copy(
        update={
            "target": request.target.model_copy(update={"target_runtime": "Unapproved runtime"})
        }
    )
    run_dir = project / ".runs/run-invalid"
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    with pytest.raises(PolicyViolation, match="shipped platform preset"):
        start_agent_run(
            project,
            run_dir,
            run_id="run-invalid",
            thread_id="thread-invalid",
            source_root="source",
            request=invalid,
            models=_models(model),
            wiki_as_of=AS_OF,
        )
    assert not run_dir.exists()

    model.provider = "api_key=do-not-store-this"  # type: ignore[misc]
    with pytest.raises(ModelConfigurationError, match="credential material"):
        _models(model)
    assert not run_dir.exists()

    with pytest.raises(PolicyViolation, match="cannot overlap"):
        start_agent_run(
            project,
            project / "agents/run-state",
            run_id="run-overlap",
            thread_id="thread-overlap",
            source_root="source",
            request=request,
            models=_models(PresetStructuredModel(SF_FROZEN_OUTPUTS)),
            wiki_as_of=AS_OF,
        )
    assert not (project / "agents/run-state").exists()

    with pytest.raises(PolicyViolation, match="expected, golden, or oracle"):
        start_agent_run(
            project,
            project / ".runs/oracle/run-state",
            run_id="run-oracle",
            thread_id="thread-oracle",
            source_root="source",
            request=request,
            models=_models(PresetStructuredModel(SF_FROZEN_OUTPUTS)),
            wiki_as_of=AS_OF,
        )
    assert not (project / ".runs/oracle/run-state").exists()


def test_status_rejects_fully_recomputed_portable_evidence_chain(tmp_path: Path) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / ".runs/run-tamper"
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    start_agent_run(
        project,
        run_dir,
        run_id="run-tamper",
        thread_id="thread-tamper",
        source_root="source",
        request=request,
        models=_models(model),
        wiki_as_of=AS_OF,
    )
    architect_relative = f"model-runs/{request.request_id}/architect.json"
    architect_path = run_dir / "evidence" / architect_relative
    architect_payload = json.loads(architect_path.read_text(encoding="utf-8"))
    architect_payload["model_call"]["model_id"] = "tampered-model/v1"
    architect_path.write_text(
        json.dumps(architect_payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    index_path = run_dir / "evidence/indexes/agent-run-planned.json"
    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    for artifact in index_payload["artifacts"]:
        if artifact["path"] == architect_relative:
            artifact["canonical_sha256"] = artifact_digest(architect_payload)
            break
    else:  # pragma: no cover - lifecycle construction invariant
        raise AssertionError("planned lifecycle omitted the Architect artifact")
    index_path.write_text(
        json.dumps(index_payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PolicyViolation, match="runtime evidence anchor digest mismatch"):
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-tamper",
            thread_id="thread-tamper",
        )


def test_status_rejects_sqlite_only_terminal_projection_tampering(tmp_path: Path) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / ".runs/run-state-tamper"
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-state-tamper",
        thread_id="thread-state-tamper",
        source_root="source",
        request=request,
        models=_models(model),
        wiki_as_of=AS_OF,
    )
    completed = resume_agent_run(
        project,
        run_dir,
        run_id="run-state-tamper",
        thread_id="thread-state-tamper",
        approval=_approval(started),
        models=_models(model),
    )
    assert completed.terminal_disposition == "environment_unavailable"
    evidence_before = snapshot_tree(run_dir / "evidence").revision

    def forbidden_role(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("checkpoint tampering must not invoke a workflow role")

    with durable_migration_workflow(
        run_dir / "state/checkpoints.sqlite3",
        forbidden_role,
        forbidden_role,
        forbidden_role,
    ) as workflow:
        workflow.graph.update_state(
            {"configurable": {"thread_id": "thread-state-tamper"}},
            {
                "status": "completed",
                "terminal_disposition": ValidationDisposition.READY_FOR_HUMAN_REVIEW,
            },
            as_node="validator",
        )

    assert snapshot_tree(run_dir / "evidence").revision == evidence_before
    with pytest.raises(
        PolicyViolation,
        match="workflow state projection differs from immutable lifecycle evidence",
    ):
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-state-tamper",
            thread_id="thread-state-tamper",
        )


def test_failed_status_rejects_sqlite_only_workflow_projection_tampering(
    tmp_path: Path,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / ".runs/run-failed-state-tamper"
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-failed-state-tamper",
        thread_id="thread-failed-state-tamper",
        source_root="source",
        request=request,
        models=_models(PresetStructuredModel(SF_FROZEN_OUTPUTS)),
        wiki_as_of=AS_OF,
    )
    failed = resume_agent_run(
        project,
        run_dir,
        run_id="run-failed-state-tamper",
        thread_id="thread-failed-state-tamper",
        approval=_approval(started),
        models=_models(ExplodingStructuredModel(SF_FROZEN_OUTPUTS, EngineerModelOutcome)),
    )
    assert failed.status == "failed"
    evidence_before = snapshot_tree(run_dir / "evidence").revision

    def forbidden_role(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("checkpoint tampering must not invoke a workflow role")

    with durable_migration_workflow(
        run_dir / "state/checkpoints.sqlite3",
        forbidden_role,
        forbidden_role,
        forbidden_role,
    ) as workflow:
        workflow.graph.update_state(
            {"configurable": {"thread_id": "thread-failed-state-tamper"}},
            {
                "status": "decision_required",
                "terminal_disposition": ValidationDisposition.DECISION_REQUIRED,
            },
            as_node="decision_required",
        )

    assert snapshot_tree(run_dir / "evidence").revision == evidence_before
    with pytest.raises(
        PolicyViolation,
        match="underlying workflow projection differs from immutable lifecycle evidence",
    ):
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-failed-state-tamper",
            thread_id="thread-failed-state-tamper",
        )


def _recoverable_validator(run_id: str):
    def validate(request, manifest, change_set, workspace, attempt):
        del workspace
        now = datetime(2026, 8, 24, tzinfo=UTC)
        results = []
        for index, check in enumerate(manifest.validation_plan):
            status = CheckStatus.FAILED if attempt == 1 and index == 0 else CheckStatus.PASSED
            receipt = ToolReceipt(
                receipt_id=f"receipt-{attempt}-{index}",
                tool_id=check.command_id,
                request_id=request.request_id,
                run_id=run_id,
                attempt=attempt,
                base_revision=manifest.base_revision,
                environment=EnvironmentKind.LOCAL,
                input_artifact_digest=artifact_digest(change_set),
                operation="trusted deterministic test validation",
                working_directory=".",
                started_at=now,
                ended_at=now,
                exit_code=1 if status is CheckStatus.FAILED else 0,
                terminal=True,
            )
            results.append(
                CheckResult(
                    check_id=check.check_id,
                    command_id=check.command_id,
                    required=True,
                    status=status,
                    receipt=receipt,
                    summary="Bounded deterministic test result.",
                )
            )
        return ValidationReport(
            report_id=f"report-{attempt}",
            request_id=request.request_id,
            manifest_id=manifest.manifest_id,
            change_set_id=change_set.change_set_id,
            base_revision=manifest.base_revision,
            results=tuple(results),
            disposition=(
                ValidationDisposition.RECOVERABLE_FAILURE
                if attempt == 1
                else ValidationDisposition.READY_FOR_HUMAN_REVIEW
            ),
            attempt=attempt,
        )

    return validate


def test_exact_correction_approval_runs_attempt_two_once(tmp_path: Path) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    models = _models(model)
    run_dir = project / ".runs/run-retry"
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-retry",
        thread_id="thread-retry",
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=_recoverable_validator("run-retry"),
    )
    failed = resume_agent_run(
        project,
        run_dir,
        run_id="run-retry",
        thread_id="thread-retry",
        approval=_approval(started),
        models=models,
        trusted_validator=_recoverable_validator("run-retry"),
    )
    correction = failed.correction
    assert correction is not None
    assert correction.action is CorrectionAction.RETRY_IMPLEMENTATION
    assert correction.authorized_attempt == 2
    exact = CorrectionApproval(
        correction_id=correction.correction_id,
        request_id=correction.request_id,
        manifest_id=correction.manifest_id,
        manifest_digest=correction.manifest_digest,
        report_id=correction.report_id,
        report_digest=correction.report_digest,
        change_set_digest=correction.change_set_digest,
        base_revision=correction.base_revision,
        completed_attempt=correction.completed_attempt,
        authorized_attempt=2,
        action=correction.action,
        reviewer="retry-reviewer",
    )
    completed = retry_agent_run(
        project,
        run_dir,
        run_id="run-retry",
        thread_id="thread-retry",
        approval=exact,
        models=models,
        trusted_validator=_recoverable_validator("run-retry"),
    )
    assert completed.terminal_disposition == "ready_for_human_review"
    control_root = run_dir / "evidence/control"
    assert (
        CorrectionApproval.model_validate_json(
            (control_root / "correction-approval-attempt-2.json").read_text(encoding="utf-8")
        )
        == exact
    )
    terminal_correction = CorrectionRequest.model_validate_json(
        (control_root / "correction-request-attempt-2.json").read_text(encoding="utf-8")
    )
    assert terminal_correction.action is CorrectionAction.COMPLETE
    calls = list(model.calls)
    with pytest.raises(PolicyViolation):
        retry_agent_run(
            project,
            run_dir,
            run_id="run-retry",
            thread_id="thread-retry",
            approval=exact,
            models=models,
            trusted_validator=_recoverable_validator("run-retry"),
        )
    assert model.calls == calls


def test_correction_authorization_recovers_after_pre_execution_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    models = _models(model)
    run_dir = project / ".runs/run-correction-authorization"
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-correction-authorization",
        thread_id="thread-correction-authorization",
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=_recoverable_validator("run-correction-authorization"),
    )
    attempt_one = resume_agent_run(
        project,
        run_dir,
        run_id="run-correction-authorization",
        thread_id="thread-correction-authorization",
        approval=_approval(started),
        models=models,
        trusted_validator=_recoverable_validator("run-correction-authorization"),
    )
    correction = attempt_one.correction
    assert correction is not None
    exact = CorrectionApproval(
        correction_id=correction.correction_id,
        request_id=correction.request_id,
        manifest_id=correction.manifest_id,
        manifest_digest=correction.manifest_digest,
        report_id=correction.report_id,
        report_digest=correction.report_digest,
        change_set_digest=correction.change_set_digest,
        base_revision=correction.base_revision,
        completed_attempt=correction.completed_attempt,
        authorized_attempt=2,
        action=correction.action,
        reviewer="correction-authorization-reviewer",
    )
    calls_before = list(model.calls)
    interrupted = False

    def interrupt_once() -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise RuntimeError("simulated correction interruption")

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run._after_authorization_boundary",
        interrupt_once,
    )
    with pytest.raises(RuntimeError, match="simulated correction interruption"):
        retry_agent_run(
            project,
            run_dir,
            run_id="run-correction-authorization",
            thread_id="thread-correction-authorization",
            approval=exact,
            models=models,
            trusted_validator=_recoverable_validator("run-correction-authorization"),
        )

    assert model.calls == calls_before
    assert (
        CorrectionApproval.model_validate_json(
            (run_dir / "evidence/control/correction-approval-attempt-2.json").read_text(
                encoding="utf-8"
            )
        )
        == exact
    )
    authorized_index = json.loads(
        (run_dir / "evidence/indexes/agent-run-correction-authorized.json").read_text(
            encoding="utf-8"
        )
    )
    indexed = {artifact["path"] for artifact in authorized_index["artifacts"]}
    assert "control/correction-request-attempt-1.json" in indexed
    assert "control/correction-approval-attempt-2.json" in indexed
    assert "status/agent-run-correction-authorized.json" in indexed
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-correction-authorization",
            thread_id="thread-correction-authorization",
        )
        == attempt_one
    )

    completed = retry_agent_run(
        project,
        run_dir,
        run_id="run-correction-authorization",
        thread_id="thread-correction-authorization",
        approval=exact,
        models=models,
        trusted_validator=_recoverable_validator("run-correction-authorization"),
    )
    assert completed.terminal_disposition == "ready_for_human_review"
    assert model.calls == [*calls_before, "EngineerModelOutcome", "ValidatorAdvisory"]


def test_correction_authorization_rejects_reviewer_and_comment_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    models = _models(model)
    run_id = "run-correction-replaced-approval"
    thread_id = "thread-correction-replaced-approval"
    run_dir = project / f".runs/{run_id}"
    started = start_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=_recoverable_validator(run_id),
    )
    attempt_one = resume_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        approval=_approval(started),
        models=models,
        trusted_validator=_recoverable_validator(run_id),
    )
    correction = attempt_one.correction
    assert correction is not None
    exact = CorrectionApproval(
        correction_id=correction.correction_id,
        request_id=correction.request_id,
        manifest_id=correction.manifest_id,
        manifest_digest=correction.manifest_digest,
        report_id=correction.report_id,
        report_digest=correction.report_digest,
        change_set_digest=correction.change_set_digest,
        base_revision=correction.base_revision,
        completed_attempt=correction.completed_attempt,
        authorized_attempt=2,
        action=correction.action,
        reviewer="original-correction-reviewer",
        comment="Original bounded correction authorization.",
    )
    calls_before = list(model.calls)
    interrupted = False

    def interrupt_after_approval(kind: str, step: str) -> None:
        nonlocal interrupted
        if kind == "agent-run-correction-authorized" and step == "approval" and not interrupted:
            interrupted = True
            raise RuntimeError("interrupted after correction approval")

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run._after_authorization_write",
        interrupt_after_approval,
    )
    with pytest.raises(RuntimeError, match="interrupted after correction approval"):
        retry_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=exact,
            models=models,
            trusted_validator=_recoverable_validator(run_id),
        )

    substituted = exact.model_copy(
        update={
            "reviewer": "substituted-correction-reviewer",
            "comment": "Substituted authorization text.",
        }
    )
    _replace_portable_json(
        run_dir / "evidence/control/correction-approval-attempt-2.json",
        substituted,
    )
    with pytest.raises(PolicyViolation, match="immutable authorization intent"):
        retry_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=substituted,
            models=models,
            trusted_validator=_recoverable_validator(run_id),
        )
    assert model.calls == calls_before


def test_correction_authorization_intent_survives_crash_before_portable_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    models = _models(model)
    run_id = "run-correction-intent-only"
    thread_id = "thread-correction-intent-only"
    run_dir = project / f".runs/{run_id}"
    started = start_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=_recoverable_validator(run_id),
    )
    attempt_one = resume_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        approval=_approval(started),
        models=models,
        trusted_validator=_recoverable_validator(run_id),
    )
    correction = attempt_one.correction
    assert correction is not None
    exact = CorrectionApproval(
        correction_id=correction.correction_id,
        request_id=correction.request_id,
        manifest_id=correction.manifest_id,
        manifest_digest=correction.manifest_digest,
        report_id=correction.report_id,
        report_digest=correction.report_digest,
        change_set_digest=correction.change_set_digest,
        base_revision=correction.base_revision,
        completed_attempt=correction.completed_attempt,
        authorized_attempt=2,
        action=correction.action,
        reviewer="correction-intent-reviewer",
        comment="Exact correction intent.",
    )
    calls_before = list(model.calls)
    interrupted = False

    def interrupt_after_intent(kind: str, step: str) -> None:
        nonlocal interrupted
        if kind == "agent-run-correction-authorized" and step == "intent" and not interrupted:
            interrupted = True
            raise RuntimeError("interrupted after correction intent")

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run._after_authorization_write",
        interrupt_after_intent,
    )
    with pytest.raises(RuntimeError, match="interrupted after correction intent"):
        retry_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=exact,
            models=models,
            trusted_validator=_recoverable_validator(run_id),
        )

    assert (run_dir / "state/anchors/agent-run-correction-authorization-intent.json").is_file()
    assert not (run_dir / "evidence/control/correction-approval-attempt-2.json").exists()
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
        )
        == attempt_one
    )

    substituted = exact.model_copy(update={"comment": "Substituted correction intent."})
    with pytest.raises(PolicyViolation, match="immutable authorization intent"):
        retry_agent_run(
            project,
            run_dir,
            run_id=run_id,
            thread_id=thread_id,
            approval=substituted,
            models=models,
            trusted_validator=_recoverable_validator(run_id),
        )
    assert model.calls == calls_before

    completed = retry_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        approval=exact,
        models=models,
        trusted_validator=_recoverable_validator(run_id),
    )
    assert completed.terminal_disposition == "ready_for_human_review"
    assert model.calls == [*calls_before, "EngineerModelOutcome", "ValidatorAdvisory"]


def test_correction_authorization_recovers_from_partial_index_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    models = _models(model)
    run_dir = project / ".runs/run-partial-correction-index"
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-partial-correction-index",
        thread_id="thread-partial-correction-index",
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=_recoverable_validator("run-partial-correction-index"),
    )
    attempt_one = resume_agent_run(
        project,
        run_dir,
        run_id="run-partial-correction-index",
        thread_id="thread-partial-correction-index",
        approval=_approval(started),
        models=models,
        trusted_validator=_recoverable_validator("run-partial-correction-index"),
    )
    correction = attempt_one.correction
    assert correction is not None
    exact = CorrectionApproval(
        correction_id=correction.correction_id,
        request_id=correction.request_id,
        manifest_id=correction.manifest_id,
        manifest_digest=correction.manifest_digest,
        report_id=correction.report_id,
        report_digest=correction.report_digest,
        change_set_digest=correction.change_set_digest,
        base_revision=correction.base_revision,
        completed_attempt=correction.completed_attempt,
        authorized_attempt=2,
        action=correction.action,
        reviewer="partial-correction-reviewer",
    )
    interrupted = False

    def interrupt_partial_index(kind: str, step: str) -> None:
        nonlocal interrupted
        if kind == "agent-run-correction-authorized" and step == "index" and not interrupted:
            interrupted = True
            raise RuntimeError("interrupted after correction index")

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run._after_authorization_write",
        interrupt_partial_index,
    )
    with pytest.raises(RuntimeError, match="interrupted after correction index"):
        retry_agent_run(
            project,
            run_dir,
            run_id="run-partial-correction-index",
            thread_id="thread-partial-correction-index",
            approval=exact,
            models=models,
            trusted_validator=_recoverable_validator("run-partial-correction-index"),
        )

    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-partial-correction-index",
            thread_id="thread-partial-correction-index",
        )
        == attempt_one
    )
    substituted = exact.model_copy(update={"reviewer": "substituted-correction-reviewer"})
    with pytest.raises(PolicyViolation, match="differs from the partial authorization"):
        retry_agent_run(
            project,
            run_dir,
            run_id="run-partial-correction-index",
            thread_id="thread-partial-correction-index",
            approval=substituted,
            models=models,
            trusted_validator=_recoverable_validator("run-partial-correction-index"),
        )

    completed = retry_agent_run(
        project,
        run_dir,
        run_id="run-partial-correction-index",
        thread_id="thread-partial-correction-index",
        approval=exact,
        models=models,
        trusted_validator=_recoverable_validator("run-partial-correction-index"),
    )
    assert completed.terminal_disposition == "ready_for_human_review"


def test_provider_free_agent_status_survives_governed_final_review(tmp_path: Path) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    models = _models(model)
    run_dir = project / ".runs/run-final-review-status"
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-final-review-status",
        thread_id="thread-final-review-status",
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
        trusted_validator=_recoverable_validator("run-final-review-status"),
    )
    attempt_one = resume_agent_run(
        project,
        run_dir,
        run_id="run-final-review-status",
        thread_id="thread-final-review-status",
        approval=_approval(started),
        models=models,
        trusted_validator=_recoverable_validator("run-final-review-status"),
    )
    correction = attempt_one.correction
    assert correction is not None
    completed = retry_agent_run(
        project,
        run_dir,
        run_id="run-final-review-status",
        thread_id="thread-final-review-status",
        approval=CorrectionApproval(
            correction_id=correction.correction_id,
            request_id=correction.request_id,
            manifest_id=correction.manifest_id,
            manifest_digest=correction.manifest_digest,
            report_id=correction.report_id,
            report_digest=correction.report_digest,
            change_set_digest=correction.change_set_digest,
            base_revision=correction.base_revision,
            completed_attempt=correction.completed_attempt,
            authorized_attempt=2,
            action=correction.action,
            reviewer="retry-reviewer",
        ),
        models=models,
        trusted_validator=_recoverable_validator("run-final-review-status"),
    )
    assert completed.terminal_disposition == "ready_for_human_review"

    now = datetime.now(UTC)
    request_final_review_for_run(
        project,
        run_dir,
        run_id="run-final-review-status",
        thread_id="thread-final-review-status",
        requester="migration-owner",
        designated_reviewer="independent-reviewer",
        requested_at=now,
        expires_at=now + timedelta(days=2),
    )
    assert (
        get_final_review_status_for_run(
            project,
            run_dir,
            run_id="run-final-review-status",
            thread_id="thread-final-review-status",
        ).status
        == "awaiting_final_review"
    )
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-final-review-status",
            thread_id="thread-final-review-status",
        ).terminal_disposition
        == "ready_for_human_review"
    )

    decide_final_review_for_run(
        project,
        run_dir,
        run_id="run-final-review-status",
        thread_id="thread-final-review-status",
        reviewer="independent-reviewer",
        selection="accept",
        decided_at=datetime.now(UTC),
    )
    assert (
        get_final_review_status_for_run(
            project,
            run_dir,
            run_id="run-final-review-status",
            thread_id="thread-final-review-status",
        ).status
        == "accepted"
    )
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-final-review-status",
            thread_id="thread-final-review-status",
        ).terminal_disposition
        == "ready_for_human_review"
    )


def test_retry_model_failure_freezes_terminal_attempt_two_status(tmp_path: Path) -> None:
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / ".runs/run-retry-failure"
    initial_model = PresetStructuredModel(SF_FROZEN_OUTPUTS)
    started = start_agent_run(
        project,
        run_dir,
        run_id="run-retry-failure",
        thread_id="thread-retry-failure",
        source_root="source",
        request=request,
        models=_models(initial_model),
        wiki_as_of=AS_OF,
        trusted_validator=_recoverable_validator("run-retry-failure"),
    )
    attempt_one = resume_agent_run(
        project,
        run_dir,
        run_id="run-retry-failure",
        thread_id="thread-retry-failure",
        approval=_approval(started),
        models=_models(initial_model),
        trusted_validator=_recoverable_validator("run-retry-failure"),
    )
    correction = attempt_one.correction
    assert correction is not None
    exact = CorrectionApproval(
        correction_id=correction.correction_id,
        request_id=correction.request_id,
        manifest_id=correction.manifest_id,
        manifest_digest=correction.manifest_digest,
        report_id=correction.report_id,
        report_digest=correction.report_digest,
        change_set_digest=correction.change_set_digest,
        base_revision=correction.base_revision,
        completed_attempt=correction.completed_attempt,
        authorized_attempt=2,
        action=correction.action,
        reviewer="retry-failure-reviewer",
    )
    exploding = ExplodingStructuredModel(SF_FROZEN_OUTPUTS, EngineerModelOutcome)
    failed = retry_agent_run(
        project,
        run_dir,
        run_id="run-retry-failure",
        thread_id="thread-retry-failure",
        approval=exact,
        models=_models(exploding),
        trusted_validator=_recoverable_validator("run-retry-failure"),
    )
    assert failed.failure is not None
    assert failed.failure.operation == "retry"
    assert failed.failure.seam == "engineer"
    assert failed.failure.attempt == 2
    assert failed.execution_attempt == 2
    assert failed.correction is None
    assert (
        get_agent_run_status(
            project,
            run_dir,
            run_id="run-retry-failure",
            thread_id="thread-retry-failure",
        )
        == failed
    )
    _assert_failure_tree_is_sanitized(run_dir)


def test_live_model_gates_fail_without_sdk_construction_or_run_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CAPSTONE_OPENAI_KEY", raising=False)
    with pytest.raises(ModelConfigurationError, match="missing or empty"):
        build_live_openai_model_clients(
            model_id="gpt-approved",
            api_key_environment="CAPSTONE_OPENAI_KEY",
            approval=LiveModelApproval(
                allow_live_api=True,
                allow_prompt_data_sharing=True,
                approved_by="reviewer",
            ),
        )
    assert not (tmp_path / ".runs").exists()


def test_local_ollama_builder_is_approved_shared_and_loopback_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoCallOllamaClient:
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
            self.model_id = model_id
            self.live_approval = approval
            self.timeout_seconds = timeout_seconds
            self.last_usage = None

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        NoCallOllamaClient,
    )
    exact_approval = LiveModelApproval(
        allow_live_api=True,
        allow_prompt_data_sharing=True,
        approved_by="local-reviewer",
    )

    models = build_local_ollama_model_clients(
        model_id="qwen3.6:latest",
        approval=exact_approval,
        timeout_seconds=90,
    )

    assert models.provider_id == "ollama"
    assert models.model_id == "qwen3.6:latest"
    assert models.execution_boundary == "local_loopback"
    assert models.live_invocation is False
    assert models.live_approval == exact_approval
    assert models.architect is models.engineer is models.validator
    assert models.architect.timeout_seconds == 90  # type: ignore[attr-defined]


def test_local_runtime_client_cannot_bypass_the_approved_builder() -> None:
    class LocalClient(PresetStructuredModel):
        provider = "ollama"
        execution_boundary = "local_loopback"

    local = LocalClient(SF_FROZEN_OUTPUTS)
    with pytest.raises(ModelConfigurationError, match="approved provider builder"):
        AgentRunModelClients(
            architect=local,
            engineer=local,
            validator=local,
        )


def test_live_environment_secret_is_never_persisted_when_preflight_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoCallOpenAIClient:
        provider = "openai"
        live_invocation = True
        store_false_sent = False

        def __init__(
            self,
            model_id: str,
            *,
            live_invocation: bool,
            api_key: str,
            approval: LiveModelApproval,
        ) -> None:
            assert live_invocation is True
            assert approval.approved_by == "live-reviewer"
            self.model_id = model_id
            self.api_key = api_key
            self.calls = 0

        def parse(self, **kwargs):
            del kwargs
            self.calls += 1
            raise AssertionError("unresolved graph must not call the live provider")

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OpenAIResponsesModelClient",
        NoCallOpenAIClient,
    )
    secret = "sk-live-agent-run-secret-123456789"
    monkeypatch.setenv("CAPSTONE_LIVE_KEY", secret)
    models = build_live_openai_model_clients(
        model_id="gpt-approved",
        api_key_environment="CAPSTONE_LIVE_KEY",
        approval=LiveModelApproval(
            allow_live_api=True,
            allow_prompt_data_sharing=True,
            approved_by="live-reviewer",
        ),
    )
    project, request = _project(tmp_path, Platform.SALESFORCE)
    controller = (
        project / "source/force-app/main/default/classes/LegacyAccountContactExplorerController.cls"
    )
    original = controller.read_text(encoding="utf-8")
    prefix, suffix = original.rsplit("}", 1)
    controller.write_text(
        prefix + "\nDatabase.query('SELECT Id FROM Account');\n}\n" + suffix,
        encoding="utf-8",
    )
    request = request.model_copy(update={"base_revision": content_revision(project / "source")})
    run_dir = project / ".runs/run-live-preflight"

    stopped = start_agent_run(
        project,
        run_dir,
        run_id="run-live-preflight",
        thread_id="thread-live-preflight",
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
    )

    assert stopped.status == "decision_required"
    assert models.architect.calls == 0  # type: ignore[attr-defined]
    for path in run_dir.rglob("*"):
        if path.is_file():
            payload = path.read_bytes()
            assert secret.encode() not in payload
            assert b"CAPSTONE_LIVE_KEY" not in payload


def test_live_agent_run_preserves_sanitized_usage_through_the_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MeasuredLiveOpenAIClient(PresetStructuredModel):
        provider = "openai"
        live_invocation = True
        store_false_sent = True

        def __init__(
            self,
            model_id: str,
            *,
            live_invocation: bool,
            api_key: str,
            approval: LiveModelApproval,
        ) -> None:
            assert live_invocation is True
            assert api_key
            assert approval.approved_by == "live-telemetry-reviewer"
            super().__init__(SF_FROZEN_OUTPUTS)
            self.model_id = model_id
            self.last_usage: ModelUsageEvidence | None = None

        def parse(
            self,
            *,
            system_prompt: str,
            input_value: BaseModel,
            output_type: type[BaseModel],
        ) -> BaseModel:
            parsed = super().parse(
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )
            self.last_usage = ModelUsageEvidence(
                latency_ms=7,
                provider_usage_reported=True,
                input_tokens=101,
                output_tokens=29,
                total_tokens=130,
            )
            return parsed

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OpenAIResponsesModelClient",
        MeasuredLiveOpenAIClient,
    )
    secret = "sk-live-telemetry-secret-123456789"
    monkeypatch.setenv("CAPSTONE_LIVE_TELEMETRY_KEY", secret)
    models = build_live_openai_model_clients(
        model_id="gpt-approved-telemetry",
        api_key_environment="CAPSTONE_LIVE_TELEMETRY_KEY",
        approval=LiveModelApproval(
            allow_live_api=True,
            allow_prompt_data_sharing=True,
            approved_by="live-telemetry-reviewer",
        ),
    )
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / ".runs/run-live-telemetry"

    started = start_agent_run(
        project,
        run_dir,
        run_id="run-live-telemetry",
        thread_id="thread-live-telemetry",
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
    )

    assert started.status == "awaiting_approval"
    architect = json.loads(
        (run_dir / f"evidence/model-runs/{request.request_id}/architect.json").read_text(
            encoding="utf-8"
        )
    )
    assert architect["model_call"]["live_invocation"] is True
    assert architect["model_call"]["store_false_sent"] is True
    assert architect["model_call"]["live_approval"] == {
        "allow_live_api": True,
        "allow_prompt_data_sharing": True,
        "approved_by": "live-telemetry-reviewer",
    }
    assert architect["model_call"]["usage"] == {
        "input_tokens": 101,
        "latency_ms": 7,
        "output_tokens": 29,
        "provider_usage_reported": True,
        "total_tokens": 130,
    }
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


def test_local_ollama_run_records_loopback_boundary_without_remote_storage_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MeasuredLocalOllamaClient(PresetStructuredModel):
        provider = "ollama"
        execution_boundary = "local_loopback"
        live_invocation = False
        store_false_sent = False
        current_revision = LOCAL_MODEL_REVISION_A

        def __init__(
            self,
            model_id: str,
            *,
            approval: LiveModelApproval,
            timeout_seconds: float,
        ) -> None:
            assert approval.approved_by == "local-evidence-reviewer"
            assert timeout_seconds == 75
            super().__init__(SF_FROZEN_OUTPUTS)
            self.model_id = model_id
            self.live_approval = approval
            self.last_usage: ModelUsageEvidence | None = None
            self._model_revision: str | None = None

        @property
        def model_revision(self) -> str | None:
            return self._model_revision

        def bind_model_revision(self, expected_revision: str) -> None:
            if expected_revision != self.current_revision:
                raise ModelConfigurationError("local Ollama model revision changed")
            self._model_revision = expected_revision

        def parse(
            self,
            *,
            system_prompt: str,
            input_value: BaseModel,
            output_type: type[BaseModel],
        ) -> BaseModel:
            if self._model_revision is None:
                self._model_revision = self.current_revision
            elif self._model_revision != self.current_revision:
                raise ModelConfigurationError("local Ollama model revision changed")
            parsed = super().parse(
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )
            self.last_usage = ModelUsageEvidence(
                latency_ms=19,
                provider_usage_reported=True,
                input_tokens=83,
                output_tokens=17,
                total_tokens=100,
            )
            return parsed

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        MeasuredLocalOllamaClient,
    )
    models = build_local_ollama_model_clients(
        model_id="qwen3.6:latest",
        approval=LiveModelApproval(
            allow_live_api=True,
            allow_prompt_data_sharing=True,
            approved_by="local-evidence-reviewer",
        ),
        timeout_seconds=75,
    )
    project, request = _project(tmp_path, Platform.SALESFORCE)
    run_dir = project / ".runs/run-local-ollama-evidence"

    started = start_agent_run(
        project,
        run_dir,
        run_id="run-local-ollama-evidence",
        thread_id="thread-local-ollama-evidence",
        source_root="source",
        request=request,
        models=models,
        wiki_as_of=AS_OF,
    )

    assert started.status == "awaiting_approval"
    architect = json.loads(
        (run_dir / f"evidence/model-runs/{request.request_id}/architect.json").read_text(
            encoding="utf-8"
        )
    )
    assert architect["model_call"]["provider"] == "ollama"
    assert architect["model_call"]["model_id"] == "qwen3.6:latest"
    assert architect["model_call"]["execution_boundary"] == "local_loopback"
    assert architect["model_call"]["model_revision"] == LOCAL_MODEL_REVISION_A
    assert architect["model_call"]["live_invocation"] is False
    assert architect["model_call"]["store_false_sent"] is False
    assert architect["model_call"]["live_approval"]["approved_by"] == ("local-evidence-reviewer")
    assert architect["model_call"]["usage"] == {
        "input_tokens": 83,
        "latency_ms": 19,
        "output_tokens": 17,
        "provider_usage_reported": True,
        "total_tokens": 100,
    }

    class OfflineOllamaImpostor(PresetStructuredModel):
        provider = "ollama"
        model_id = "qwen3.6:latest"

    impostor = OfflineOllamaImpostor(SF_FROZEN_OUTPUTS)
    with pytest.raises(PolicyViolation, match="execution boundary differs"):
        resume_agent_run(
            project,
            run_dir,
            run_id="run-local-ollama-evidence",
            thread_id="thread-local-ollama-evidence",
            approval=_approval(started),
            models=AgentRunModelClients.offline(
                architect=impostor,
                engineer=impostor,
                validator=impostor,
            ),
        )

    MeasuredLocalOllamaClient.current_revision = LOCAL_MODEL_REVISION_B
    drifted_models = build_local_ollama_model_clients(
        model_id="qwen3.6:latest",
        approval=LiveModelApproval(
            allow_live_api=True,
            allow_prompt_data_sharing=True,
            approved_by="local-evidence-reviewer",
        ),
        timeout_seconds=75,
    )
    with pytest.raises(PolicyViolation, match="revision differs"):
        resume_agent_run(
            project,
            run_dir,
            run_id="run-local-ollama-evidence",
            thread_id="thread-local-ollama-evidence",
            approval=_approval(started),
            models=drifted_models,
        )
