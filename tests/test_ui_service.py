from __future__ import annotations

import io
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from ui_test_doubles import (
    LOCAL_MODEL_REVISION,
    fixture_model_response,
    make_ollama_client_test_double,
)

from legacy_migration_agent.agent_runtime.correction import CorrectionAction
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectManifestProposal,
    EngineerFilePlan,
    EngineerFileUpdate,
    EngineerInterventionOutcome,
    EngineerModelOutcome,
    EngineerWorkspaceContext,
    ValidatorAdvisory,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelOutputError,
    ModelRuntimeError,
    ModelUsageEvidence,
)
from legacy_migration_agent.contracts import (
    ApprovalAction,
    CheckResult,
    CheckStatus,
    EnvironmentKind,
    ImplementationIntervention,
    ImplementationInterventionEvidence,
    ManifestStatus,
    MigrationManifest,
    PlanningInterventionOption,
    ToolReceipt,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.observability import terminal_lifecycle_logging
from legacy_migration_agent.core.workspace import snapshot_tree
from legacy_migration_agent.platforms.local_checks import (
    SALESFORCE_AGENT_OUTPUT_PATHS,
    SALESFORCE_IMPLEMENTATION_CONTRACT,
)
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    MULESOFT_IMPLEMENTATION_CONTRACT,
)
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    TARGET_FILES as MULESOFT_TARGET_FILES,
)
from legacy_migration_agent.ui.service import (
    AgentPlanningDecisionView,
    AgentUiError,
    AgentUiService,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "qwen3.6:latest"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "agents", project / "agents")
    shutil.copytree(PROJECT_ROOT / "knowledge/wiki", project / "knowledge/wiki")
    shutil.copytree(PROJECT_ROOT / "fixtures", project / "fixtures")
    return project


def _stub_ollama(
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    *,
    role_calls: list[str] | None = None,
    constructed: list[object] | None = None,
    expected_timeout_seconds: float = 180.0,
) -> None:
    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        make_ollama_client_test_double(
            project,
            role_calls=role_calls,
            constructed=constructed,
            expected_timeout_seconds=expected_timeout_seconds,
        ),
    )


def _recoverable_validator(
    run_id: str,
    *,
    failed_diagnostic_ids: tuple[str, ...] = ("jest_mock_not_reset",),
):
    def validate(request, manifest, change_set, workspace, attempt):
        del workspace
        now = datetime(2026, 8, 25, tzinfo=UTC)
        results = []
        for index, check in enumerate(manifest.validation_plan):
            status = CheckStatus.FAILED if attempt == 1 and index == 0 else CheckStatus.PASSED
            results.append(
                CheckResult(
                    check_id=check.check_id,
                    command_id=check.command_id,
                    required=True,
                    status=status,
                    receipt=ToolReceipt(
                        receipt_id=f"ui-receipt-{attempt}-{index}",
                        tool_id=check.command_id,
                        request_id=request.request_id,
                        run_id=run_id,
                        attempt=attempt,
                        base_revision=manifest.base_revision,
                        environment=EnvironmentKind.LOCAL,
                        input_artifact_digest=artifact_digest(change_set),
                        operation="controller-owned UI retry test validation",
                        working_directory=".",
                        started_at=now,
                        ended_at=now,
                        exit_code=1 if status is CheckStatus.FAILED else 0,
                        terminal=True,
                    ),
                    summary="Typed deterministic UI retry test result.",
                    diagnostic_ids=(failed_diagnostic_ids if status is CheckStatus.FAILED else ()),
                )
            )
        return ValidationReport(
            report_id=f"ui-report-{attempt}",
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


def test_scenarios_expose_only_fixed_browser_safe_metadata(tmp_path: Path) -> None:
    service = AgentUiService(_project(tmp_path), ollama_model_id=MODEL_ID)

    scenarios = service.scenarios()

    assert tuple(item["platform"] for item in scenarios) == ("salesforce", "mulesoft")
    assert tuple(item["title"] for item in scenarios) == (
        "Visualforce to Lightning Web Component",
        "Mule 3 to Mule 4",
    )
    assert all(set(item) == {"platform", "title", "prompt"} for item in scenarios)
    assert all("fixture" not in str(item).casefold() for item in scenarios)


def test_server_owned_ollama_configuration_is_required_and_browser_safe(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)

    with pytest.raises(ValueError, match="must be a string"):
        AgentUiService(project, ollama_model_id=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="1 to 300"):
        AgentUiService(project, ollama_model_id="   ")
    for timeout in (True, 0, 601, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="ollama_timeout_seconds"):
            AgentUiService(
                project,
                ollama_model_id=MODEL_ID,
                ollama_timeout_seconds=timeout,  # type: ignore[arg-type]
            )

    service = AgentUiService(project, ollama_model_id=" qwen3.6:latest ")

    assert service.model_configuration() == {
        "provider": "ollama",
        "model_id": MODEL_ID,
        "execution_boundary": "local_loopback",
    }


@pytest.mark.parametrize(
    ("outcome", "expected_status", "reachable", "installed"),
    [
        (LOCAL_MODEL_REVISION, "ready", True, True),
        (
            ModelRuntimeError("secret=/Users/private/provider failure"),
            "ollama_unreachable",
            False,
            None,
        ),
        (
            ModelConfigurationError("secret=/Users/private/model missing"),
            "model_unavailable",
            True,
            False,
        ),
        (
            ModelOutputError("secret=/Users/private/bad inventory"),
            "inventory_unverified",
            True,
            None,
        ),
    ],
)
def test_runtime_readiness_reuses_inventory_probe_and_returns_only_safe_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str | Exception,
    expected_status: str,
    reachable: bool,
    installed: bool | None,
) -> None:
    service = AgentUiService(
        _project(tmp_path),
        ollama_model_id=MODEL_ID,
        ollama_timeout_seconds=600,
    )
    observed_timeouts: list[float] = []

    class InventoryProbe:
        def _resolve_model_revision(self, *, timeout_seconds: float) -> str:
            observed_timeouts.append(timeout_seconds)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(
        service,
        "_local_models",
        lambda _model_id: SimpleNamespace(architect=InventoryProbe()),
    )

    readiness = service.runtime_readiness()

    assert readiness == {
        "provider": "ollama",
        "model_id": MODEL_ID,
        "configured": True,
        "ollama_reachable": reachable,
        "model_installed": installed,
        "status": expected_status,
    }
    assert observed_timeouts == [3.0]
    assert "secret" not in str(readiness)
    assert "/Users/" not in str(readiness)


def test_planning_decision_view_allows_unresolved_only_terminal_plan() -> None:
    view = AgentPlanningDecisionView(
        manifest_id="decision-required-manifest",
        manifest_digest="sha256:" + "c" * 64,
        unresolved_questions=("Confirm the unresolved runtime dependency.",),
        required_approvals=(),
        summary="The approve or reject manifest gate did not open.",
    )

    assert view.unresolved_questions == ("Confirm the unresolved runtime dependency.",)
    assert view.required_approvals == ()


def test_salesforce_start_invokes_only_architect_and_awaits_exact_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    source = project / "fixtures/salesforce/account-contact-explorer/input"
    before = snapshot_tree(source)
    role_calls: list[str] = []
    _stub_ollama(monkeypatch, project, role_calls=role_calls)
    service = AgentUiService(project, ollama_model_id=MODEL_ID)

    started = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice to additive LWC and Apex artifacts.",
    )

    assert started.platform == "salesforce"
    assert started.status == "awaiting_approval"
    assert started.terminal_disposition is None
    assert started.boundaries.mode == "local_ollama"
    assert started.boundaries.provider_attempted is True
    assert started.boundaries.provider_invoked is True
    assert started.boundaries.model_call_record_persisted is True
    assert started.boundaries.structured_response_accepted is True
    assert started.boundaries.provider_id == "ollama"
    assert started.boundaries.model_id == MODEL_ID
    assert started.boundaries.model_revision == LOCAL_MODEL_REVISION
    assert started.boundaries.execution_boundary == "local_loopback"
    assert started.boundaries.external_platform_invoked is False
    assert started.boundaries.source_mutated is False
    assert started.boundaries.deployment_performed is False
    assert "Real structured-output inference" in started.boundaries.notice
    assert started.manifest is not None
    assert started.manifest.approval_required is True
    assert started.manifest.status == "planned"
    assert started.manifest.manifest_digest.startswith("sha256:")
    assert started.manifest.public_decisions == (
        "Use only the additive platform scope and controller-owned checks.",
        "Keep external runtime validation outside the local Agent UI boundary.",
    )
    assert started.manifest.implementation_contract == SALESFORCE_IMPLEMENTATION_CONTRACT
    assert started.manifest.cited_graph_nodes
    assert started.manifest.cited_wiki_pages
    assert started.manifest.unresolved_questions == ()
    assert started.manifest.required_approvals == ("approve_manifest",)
    assert started.manifest.risks == ()
    assert started.manifest.transformations[0].step_id == ("migrate-salesforce-bounded-slice")
    assert started.manifest.transformations[0].input_paths
    assert started.manifest.transformations[0].output_paths
    assert all(command.check_id for command in started.manifest.validation_commands)
    assert all(command.command_id for command in started.manifest.validation_commands)
    assert all(command.environment == "local" for command in started.manifest.validation_commands)
    assert all(command.required is True for command in started.manifest.validation_commands)
    assert started.candidate is None
    assert started.validation is None
    assert started.human_decision is None
    assert len(started.model_calls) == 1
    assert started.model_calls[0].role == "architect"
    assert started.model_calls[0].latency_ms == 7
    assert started.model_calls[0].input_tokens == 80
    assert started.model_calls[0].output_tokens == 20
    assert started.model_calls[0].total_tokens == 100
    assert started.model_calls[0].input_digest.startswith("sha256:")
    assert started.model_calls[0].output_digest.startswith("sha256:")
    assert tuple(stage.state for stage in started.stages) == (
        "complete",
        "active",
        "pending",
        "pending",
    )
    run_dir = project / ".runs/agent-ui" / started.handle
    assert not tuple(run_dir.glob("evidence/model-runs/*/engineer-attempt-*.json"))
    assert not tuple(run_dir.glob("evidence/model-runs/*/validator-attempt-*.json"))
    assert snapshot_tree(source) == before
    assert service.get(started.handle) == started
    assert role_calls == ["ArchitectManifestProposal"]


def test_latest_recovers_the_newest_verifiable_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    _stub_ollama(monkeypatch, project)
    service = AgentUiService(project, ollama_model_id=MODEL_ID)

    assert service.latest() is None
    started = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice to additive LWC and Apex artifacts.",
    )

    recovered = service.latest()
    assert recovered is not None
    assert recovered.handle == started.handle
    assert recovered.status == "awaiting_approval"

    completed = service.decide(
        started.handle,
        selection="approve",
        reviewer="course-reviewer",
    )
    recovered_terminal = service.latest()
    assert recovered_terminal is not None
    assert recovered_terminal.handle == completed.handle
    assert recovered_terminal.status == "completed"


def test_salesforce_approval_projects_persisted_candidate_and_advisory_and_safe_zip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    source = project / "fixtures/salesforce/account-contact-explorer/input"
    before = snapshot_tree(source)
    _stub_ollama(monkeypatch, project)
    service = AgentUiService(project, ollama_model_id=MODEL_ID)
    terminal_output = io.StringIO()
    with terminal_lifecycle_logging(stream=terminal_output):
        started = service.start(
            "salesforce",
            prompt="Migrate this bounded Visualforce slice to additive LWC and Apex artifacts.",
        )

        completed = service.decide(
            started.handle,
            selection="approve",
            reviewer="course-reviewer",
            comment="Approve this bounded synthetic-fixture candidate.",
        )

    assert completed.status == "completed"
    assert completed.terminal_disposition == "environment_unavailable"
    assert completed.candidate is not None
    assert completed.candidate.available is True
    assert set(completed.candidate.changed_paths) == set(SALESFORCE_AGENT_OUTPUT_PATHS)
    assert {item.path for item in completed.candidate.files} == set(SALESFORCE_AGENT_OUTPUT_PATHS)
    assert completed.candidate.unified_diff.startswith("diff --git")
    assert completed.metrics.changed_files == len(SALESFORCE_AGENT_OUTPUT_PATHS)
    assert completed.validation is not None
    assert completed.validation.disposition == "environment_unavailable"
    assert completed.validation.final_review_enabled is False
    assert completed.validation.advisory_assessment == "supports_report"
    assert completed.validation.deterministic_report_controls_disposition is True
    assert completed.validation.validator_summary
    assert completed.validation.concerns
    assert completed.human_decision is not None
    assert completed.human_decision.selection == "approve"
    assert completed.human_decision.reviewer == "course-reviewer"
    assert completed.human_decision.comment == ("Approve this bounded synthetic-fixture candidate.")
    assert completed.manifest is not None
    assert completed.human_decision.manifest_digest == completed.manifest.manifest_digest
    assert tuple(call.role for call in completed.model_calls) == (
        "architect",
        "engineer",
        "validator",
    )
    assert {item.status for item in completed.validation.results} >= {
        "passed",
        "unavailable",
    }
    assert tuple(stage.state for stage in completed.stages) == (
        "complete",
        "complete",
        "complete",
        "complete",
    )
    assert snapshot_tree(source) == before
    assert service.get(started.handle) == completed

    lifecycle_log = terminal_output.getvalue()
    ordered_events = (
        "event=ui.run.created",
        'event=model.call.started role="architect"',
        "event=ui.manifest.awaiting_approval",
        "event=ui.manifest.decision.received",
        'event=model.call.started role="engineer"',
        "event=validation.started",
        "event=validation.check.completed",
        "event=validation.completed",
        'event=model.call.started role="validator"',
        "event=ui.run.terminal",
    )
    positions = [lifecycle_log.index(event) for event in ordered_events]
    assert positions == sorted(positions)
    assert 'disposition="environment_unavailable"' in lifecycle_log
    assert "Migrate this bounded Visualforce" not in lifecycle_log
    assert "course-reviewer" not in lifecycle_log
    assert "Approve this bounded synthetic-fixture candidate" not in lifecycle_log
    assert "/Users/" not in lifecycle_log

    payload = service.candidate_zip(started.handle)
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        assert set(bundle.namelist()) == set(SALESFORCE_AGENT_OUTPUT_PATHS)
        assert all(
            not path.startswith("/") and ".." not in path.split("/") for path in bundle.namelist()
        )
        for path in SALESFORCE_AGENT_OUTPUT_PATHS:
            expected = (
                project / "fixtures/salesforce/account-contact-explorer/expected" / path
            ).read_bytes()
            assert bundle.read(path) == expected

    exported = service.export_candidate(started.handle)
    assert exported.platform == "salesforce"
    assert exported.handle == started.handle
    assert exported.attempt == 1
    assert exported.validation_disposition == "environment_unavailable"
    assert exported.ready_for_human_review is False
    assert exported.file_count == len(SALESFORCE_AGENT_OUTPUT_PATHS)
    assert (project / exported.archive_path).read_bytes() == payload
    exported_root = project / exported.candidate_path
    for path in SALESFORCE_AGENT_OUTPUT_PATHS:
        assert (
            exported_root.joinpath(*path.split("/")).read_bytes()
            == (
                project / "fixtures/salesforce/account-contact-explorer/expected" / path
            ).read_bytes()
        )
    receipt = (project / exported.receipt_path).read_text(encoding="utf-8")
    assert '"validation_disposition":"environment_unavailable"' in receipt
    assert "Migrate this bounded Visualforce" not in receipt
    assert str(project) not in receipt


def test_recoverable_attempt_requires_exact_human_retry_and_projects_attempt_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    source = project / "fixtures/salesforce/account-contact-explorer/input"
    before = snapshot_tree(source)
    _stub_ollama(monkeypatch, project)
    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.build_salesforce_local_validator",
        lambda session, registry, timeout_seconds: _recoverable_validator(session.context.run_id),
    )
    service = AgentUiService(project, ollama_model_id=MODEL_ID)
    started = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice to additive LWC and Apex artifacts.",
    )
    attempt_one = service.decide(
        started.handle,
        selection="approve",
        reviewer="course-reviewer",
    )

    assert attempt_one.status == "completed"
    assert attempt_one.execution_attempt == 1
    assert attempt_one.terminal_disposition == "recoverable_failure"
    assert attempt_one.validation is not None
    assert attempt_one.validation.attempt == 1
    assert attempt_one.validation.results[0].diagnostic_ids == ("jest_mock_not_reset",)
    assert attempt_one.correction is not None
    assert attempt_one.correction.retry_available is True
    assert attempt_one.correction.approval is None
    assert attempt_one.correction.completed_attempt == 1
    assert attempt_one.correction.authorized_attempt == 2
    assert attempt_one.correction.failed_check_ids == (
        attempt_one.validation.results[0].check_id,
        "jest_mock_not_reset",
    )
    assert attempt_one.attempt_history[0].attempt == 1
    assert attempt_one.attempt_history[0].diagnostic_ids == ("jest_mock_not_reset",)
    first_export = service.export_candidate(started.handle)
    assert first_export.attempt == 1
    assert first_export.ready_for_human_review is False

    with pytest.raises(AgentUiError) as wrong_correction:
        service.retry(
            started.handle,
            correction_id="correction-wrong",
            reviewer="course-reviewer",
        )
    assert wrong_correction.value.code == "invalid_correction"
    with pytest.raises(AgentUiError) as invalid_reviewer:
        service.retry(
            started.handle,
            correction_id=attempt_one.correction.correction_id,
            reviewer="../../reviewer",
        )
    assert invalid_reviewer.value.code == "invalid_reviewer"

    completed = service.retry(
        started.handle,
        correction_id=attempt_one.correction.correction_id,
        reviewer="course-reviewer",
        comment="Authorize the exact typed correction.",
    )

    assert completed.status == "completed"
    assert completed.execution_attempt == 2
    assert completed.terminal_disposition == "ready_for_human_review"
    assert completed.validation is not None
    assert completed.validation.attempt == 2
    assert completed.correction is not None
    assert completed.correction.retry_available is False
    assert completed.correction.completed_attempt == 2
    assert completed.correction.authorized_attempt is None
    assert completed.correction.action is CorrectionAction.COMPLETE
    assert completed.correction.approval is not None
    assert completed.correction.approval.reviewer == "course-reviewer"
    assert completed.correction.approval.comment == "Authorize the exact typed correction."
    assert tuple(item.attempt for item in completed.attempt_history) == (1, 2)
    assert tuple(call.attempt for call in completed.model_calls) == (1, 1, 1, 2, 2)
    assert snapshot_tree(source) == before
    assert service.get(started.handle) == completed
    second_export = service.export_candidate(started.handle)
    assert second_export.attempt == 2
    assert second_export.ready_for_human_review is True
    assert second_export.export_root != first_export.export_root
    assert (project / first_export.receipt_path).is_file()
    assert (project / second_export.receipt_path).is_file()

    with pytest.raises(AgentUiError) as repeated:
        service.retry(
            started.handle,
            correction_id=attempt_one.correction.correction_id,
            reviewer="course-reviewer",
        )
    assert repeated.value.code == "retry_already_recorded"

    unrelated = service.start(
        "salesforce",
        prompt="Migrate another bounded Visualforce slice to additive LWC and Apex artifacts.",
    )
    with pytest.raises(AgentUiError) as unavailable:
        service.retry(
            unrelated.handle,
            correction_id=attempt_one.correction.correction_id,
            reviewer="course-reviewer",
        )
    assert unavailable.value.code == "retry_unavailable"


def test_attempt_two_recoverable_failure_projects_terminal_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    _stub_ollama(monkeypatch, project)

    def always_recoverable(run_id: str):
        base_validator = _recoverable_validator(run_id)

        def validate(request, manifest, change_set, workspace, attempt):
            report = base_validator(request, manifest, change_set, workspace, attempt)
            first = report.results[0]
            failed_first = first.model_copy(
                update={
                    "status": CheckStatus.FAILED,
                    "receipt": first.receipt.model_copy(update={"exit_code": 1}),
                    "diagnostic_ids": ("jest_mock_not_reset",),
                }
            )
            return report.model_copy(
                update={
                    "results": (failed_first, *report.results[1:]),
                    "disposition": ValidationDisposition.RECOVERABLE_FAILURE,
                }
            )

        return validate

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.build_salesforce_local_validator",
        lambda session, registry, timeout_seconds: always_recoverable(session.context.run_id),
    )
    service = AgentUiService(project, ollama_model_id=MODEL_ID)
    started = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice to additive LWC and Apex artifacts.",
    )
    attempt_one = service.decide(
        started.handle,
        selection="approve",
        reviewer="course-reviewer",
    )
    assert attempt_one.correction is not None

    exhausted = service.retry(
        started.handle,
        correction_id=attempt_one.correction.correction_id,
        reviewer="course-reviewer",
        comment="Authorize the exact typed correction.",
    )

    assert exhausted.execution_attempt == 2
    assert exhausted.terminal_disposition == "recoverable_failure"
    assert exhausted.correction is not None
    assert exhausted.correction.completed_attempt == 2
    assert exhausted.correction.authorized_attempt is None
    assert exhausted.correction.action is CorrectionAction.STOP_EXHAUSTED
    assert exhausted.correction.retry_available is False
    assert exhausted.correction.approval is not None
    assert exhausted.correction.failed_check_ids == (
        exhausted.validation.results[0].check_id,
        "jest_mock_not_reset",
    )
    assert service.get(started.handle) == exhausted


def test_attempt_two_engineer_intervention_retains_exact_retry_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []
    base_client = make_ollama_client_test_double(project, role_calls=role_calls)

    class AttemptTwoInterventionClient(base_client):
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
            context = EngineerWorkspaceContext.model_validate(input_value)
            if context.attempt == 1:
                return super().parse(
                    system_prompt=system_prompt,
                    input_value=input_value,
                    output_type=output_type,
                )
            role_calls.append(output_type.__name__)
            output_path = context.manifest.approved_paths[0]
            affected_paths = (output_path, context.request.target.entry_path)
            self.last_usage = ModelUsageEvidence(
                latency_ms=5,
                provider_usage_reported=True,
                input_tokens=75,
                output_tokens=25,
                total_tokens=100,
            )
            return EngineerModelOutcome(
                result=EngineerInterventionOutcome(
                    kind="decision_required",
                    intervention=ImplementationIntervention(
                        intervention_id="ui-attempt-two-engineer-stop",
                        request_id=context.request.request_id,
                        request_digest=context.request_digest,
                        manifest_id=context.manifest.manifest_id,
                        manifest_digest=context.manifest_digest,
                        base_revision=context.workspace_base_revision,
                        agent_version=context.agent_version,
                        agent_definition_digest=context.agent_definition_digest,
                        input_evidence_digest=context.input_evidence_digest,
                        reason="The corrective attempt still lacks a required public contract.",
                        requested_action=ApprovalAction.EXPAND_SCOPE,
                        affected_paths=affected_paths,
                        public_concerns=(
                            "A same-manifest retry cannot safely invent the missing contract.",
                        ),
                        evidence=(
                            ImplementationInterventionEvidence(
                                source="engineer_input",
                                source_digest=context.input_evidence_digest,
                                summary="The exact correction evidence does not resolve the gap.",
                                affected_paths=affected_paths,
                            ),
                        ),
                        options=(
                            PlanningInterventionOption.EXPAND_SCOPE_AND_REPLAN,
                            PlanningInterventionOption.STOP_REQUEST,
                        ),
                        recommendation="Stop and approve a new manifest if scope changes.",
                    ),
                )
            )

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        AttemptTwoInterventionClient,
    )
    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.build_salesforce_local_validator",
        lambda session, registry, timeout_seconds: _recoverable_validator(
            session.context.run_id,
            failed_diagnostic_ids=(),
        ),
    )
    service = AgentUiService(project, ollama_model_id=MODEL_ID)
    started = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice to additive LWC and Apex artifacts.",
    )
    attempt_one = service.decide(
        started.handle,
        selection="approve",
        reviewer="course-reviewer",
    )
    assert attempt_one.correction is not None

    stopped = service.retry(
        started.handle,
        correction_id=attempt_one.correction.correction_id,
        reviewer="course-reviewer",
        comment="Authorize the exact typed correction.",
    )

    assert stopped.status == "decision_required"
    assert stopped.execution_attempt == 2
    assert stopped.intervention is not None
    assert stopped.candidate is not None
    assert stopped.candidate.attempt == 1
    assert stopped.candidate.download_available is False
    assert stopped.correction is not None
    assert stopped.correction.correction_id == attempt_one.correction.correction_id
    assert stopped.correction.completed_attempt == 1
    assert stopped.correction.authorized_attempt == 2
    assert stopped.correction.action is CorrectionAction.RETRY_IMPLEMENTATION
    assert stopped.correction.retry_available is False
    assert stopped.correction.approval is not None
    assert service.get(started.handle) == stopped
    assert role_calls == [
        "ArchitectManifestProposal",
        "EngineerModelOutcome",
        "ValidatorAdvisory",
        "EngineerModelOutcome",
    ]


def test_attempt_two_engineer_failure_retains_prior_candidate_for_debugging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []
    base_client = make_ollama_client_test_double(project, role_calls=role_calls)

    class AttemptTwoInvalidEngineerClient(base_client):
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
            context = EngineerWorkspaceContext.model_validate(input_value)
            if context.attempt == 1:
                return super().parse(
                    system_prompt=system_prompt,
                    input_value=input_value,
                    output_type=output_type,
                )
            role_calls.append(output_type.__name__)
            self.last_usage = ModelUsageEvidence(
                latency_ms=5,
                provider_usage_reported=True,
                input_tokens=75,
                output_tokens=25,
                total_tokens=100,
            )
            return EngineerModelOutcome.for_file_plan(
                EngineerFilePlan(
                    updates=(
                        EngineerFileUpdate(
                            path="force-app/main/default/lwc/unapproved/unapproved.js",
                            content="export default class Unapproved {}\n",
                        ),
                    )
                )
            )

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        AttemptTwoInvalidEngineerClient,
    )
    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.build_salesforce_local_validator",
        lambda session, registry, timeout_seconds: _recoverable_validator(session.context.run_id),
    )
    service = AgentUiService(project, ollama_model_id=MODEL_ID)
    started = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice to additive LWC and Apex artifacts.",
    )
    attempt_one = service.decide(
        started.handle,
        selection="approve",
        reviewer="course-reviewer",
    )
    assert attempt_one.correction is not None

    failed = service.retry(
        started.handle,
        correction_id=attempt_one.correction.correction_id,
        reviewer="course-reviewer",
        comment="Authorize the exact typed correction.",
    )

    assert failed.status == "failed"
    assert failed.execution_attempt == 2
    assert failed.failure is not None
    assert failed.failure.seam == "engineer"
    assert failed.candidate is not None
    assert failed.candidate.attempt == 1
    assert failed.candidate.download_available is False
    assert failed.validation is None
    assert failed.correction is not None
    assert failed.correction.retry_available is False
    assert service.get(started.handle) == failed
    with pytest.raises(AgentUiError) as unavailable:
        service.candidate_zip(started.handle)
    assert unavailable.value.code == "candidate_unavailable"
    with pytest.raises(AgentUiError) as export_unavailable:
        service.export_candidate(started.handle)
    assert export_unavailable.value.code == "candidate_unavailable"
    assert not (project / "output").exists()
    assert role_calls == [
        "ArchitectManifestProposal",
        "EngineerModelOutcome",
        "ValidatorAdvisory",
        "EngineerModelOutcome",
    ]


def test_retry_rechecks_full_source_snapshot_when_execution_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    _stub_ollama(monkeypatch, project)
    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.build_salesforce_local_validator",
        lambda session, registry, timeout_seconds: _recoverable_validator(session.context.run_id),
    )
    service = AgentUiService(project, ollama_model_id=MODEL_ID)
    started = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice to additive LWC and Apex artifacts.",
    )
    attempt_one = service.decide(
        started.handle,
        selection="approve",
        reviewer="course-reviewer",
    )
    assert attempt_one.correction is not None

    source = project / "fixtures/salesforce/account-contact-explorer/input"
    source_file = next(path for path in source.rglob("*") if path.is_file())
    original_mode = source_file.stat().st_mode
    real_snapshot_tree = snapshot_tree
    source_snapshot_calls = 0

    def tracked_snapshot_tree(root):
        nonlocal source_snapshot_calls
        if Path(root) == source:
            source_snapshot_calls += 1
        return real_snapshot_tree(root)

    def mutating_failure(*args, **kwargs):
        del args, kwargs
        source_file.chmod(original_mode ^ 0o100)
        raise RuntimeError("simulated execution failure after a mode-only source mutation")

    monkeypatch.setattr(
        "legacy_migration_agent.ui.service.snapshot_tree",
        tracked_snapshot_tree,
    )
    monkeypatch.setattr(
        "legacy_migration_agent.ui.service.retry_agent_run",
        mutating_failure,
    )
    try:
        with pytest.raises(AgentUiError) as failed:
            service.retry(
                started.handle,
                correction_id=attempt_one.correction.correction_id,
                reviewer="course-reviewer",
            )
        assert failed.value.code == "run_unavailable"
        assert source_snapshot_calls == 2
    finally:
        source_file.chmod(original_mode)


def test_retry_resumes_only_identical_durable_approval_after_authorization_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    source = project / "fixtures/salesforce/account-contact-explorer/input"
    before = snapshot_tree(source)
    role_calls: list[str] = []
    _stub_ollama(monkeypatch, project, role_calls=role_calls)
    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.build_salesforce_local_validator",
        lambda session, registry, timeout_seconds: _recoverable_validator(session.context.run_id),
    )
    service = AgentUiService(project, ollama_model_id=MODEL_ID)
    started = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice to additive LWC and Apex artifacts.",
    )
    attempt_one = service.decide(
        started.handle,
        selection="approve",
        reviewer="course-reviewer",
    )
    assert attempt_one.correction is not None
    calls_before_retry = list(role_calls)
    interrupted = False

    def interrupt_once() -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise RuntimeError("simulated post-authorization interruption")

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run._after_authorization_boundary",
        interrupt_once,
    )
    retry_arguments = {
        "correction_id": attempt_one.correction.correction_id,
        "reviewer": "course-reviewer",
        "comment": "Authorize this exact correction once.",
    }

    with pytest.raises(AgentUiError) as first_call:
        service.retry(started.handle, **retry_arguments)
    assert first_call.value.code == "run_unavailable"
    assert role_calls == calls_before_retry
    assert snapshot_tree(source) == before

    recoverable = service.get(started.handle)
    assert recoverable.execution_attempt == 1
    assert recoverable.correction is not None
    assert recoverable.correction.retry_available is True
    assert recoverable.correction.approval is not None
    assert recoverable.correction.approval.reviewer == retry_arguments["reviewer"]
    assert recoverable.correction.approval.comment == retry_arguments["comment"]

    with pytest.raises(AgentUiError) as changed_approval:
        service.retry(
            started.handle,
            correction_id=retry_arguments["correction_id"],
            reviewer=retry_arguments["reviewer"],
            comment="A different comment must not replace durable approval.",
        )
    assert changed_approval.value.code == "retry_already_recorded"
    assert role_calls == calls_before_retry

    completed = service.retry(started.handle, **retry_arguments)
    assert completed.execution_attempt == 2
    assert completed.terminal_disposition == "ready_for_human_review"
    assert role_calls == [
        *calls_before_retry,
        "EngineerModelOutcome",
        "ValidatorAdvisory",
    ]
    assert snapshot_tree(source) == before

    with pytest.raises(AgentUiError) as third_call:
        service.retry(started.handle, **retry_arguments)
    assert third_call.value.code == "retry_already_recorded"
    assert role_calls == [
        *calls_before_retry,
        "EngineerModelOutcome",
        "ValidatorAdvisory",
    ]


def test_reject_stops_before_engineer_and_repeat_or_unknown_decisions_are_controlled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    _stub_ollama(monkeypatch, project)
    service = AgentUiService(project, ollama_model_id=MODEL_ID)
    started = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice to additive LWC and Apex artifacts.",
    )

    rejected = service.decide(
        started.handle,
        selection="reject",
        reviewer="course-reviewer",
    )

    assert rejected.status == "rejected"
    assert rejected.terminal_disposition == "plan_invalid"
    assert rejected.candidate is None
    assert rejected.validation is None
    assert rejected.human_decision is not None
    assert rejected.human_decision.selection == "reject"
    assert rejected.human_decision.reviewer == "course-reviewer"
    run_dir = project / ".runs/agent-ui" / started.handle
    assert not tuple(run_dir.glob("evidence/model-runs/*/engineer-attempt-*.json"))
    assert not tuple(run_dir.glob("evidence/model-runs/*/validator-attempt-*.json"))

    # A new service instance proves that durable run state, not an in-memory
    # decision flag, rejects a repeated/stale browser action.
    restarted_service = AgentUiService(project, ollama_model_id=MODEL_ID)
    assert restarted_service.get(started.handle).human_decision == rejected.human_decision
    with pytest.raises(AgentUiError) as repeated:
        restarted_service.decide(
            started.handle,
            selection="approve",
            reviewer="course-reviewer",
        )
    assert repeated.value.code == "decision_already_recorded"

    with pytest.raises(AgentUiError) as missing:
        service.get("0" * 24)
    assert missing.value.code == "unknown_run"
    with pytest.raises(AgentUiError) as unavailable:
        service.candidate_zip(started.handle)
    assert unavailable.value.code == "candidate_unavailable"


def test_run_capacity_counts_active_runs_but_releases_rejected_and_completed_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    _stub_ollama(monkeypatch, project)
    service = AgentUiService(project, ollama_model_id=MODEL_ID, max_runs=1)
    prompt = "Migrate this bounded Visualforce slice to additive LWC and Apex artifacts."

    first = service.start("salesforce", prompt=prompt)
    with pytest.raises(AgentUiError) as active_capacity:
        service.start("salesforce", prompt=prompt)
    assert active_capacity.value.code == "run_capacity_reached"

    rejected = service.decide(
        first.handle,
        selection="reject",
        reviewer="course-reviewer",
    )
    assert rejected.status == "rejected"

    second = service.start("salesforce", prompt=prompt)
    completed = service.decide(
        second.handle,
        selection="approve",
        reviewer="course-reviewer",
    )
    assert completed.status == "completed"

    third = service.start("salesforce", prompt=prompt)
    assert third.status == "awaiting_approval"


def test_corrupt_owned_run_directory_counts_toward_capacity(tmp_path: Path) -> None:
    project = _project(tmp_path)
    corrupt = project / ".runs/agent-ui" / ("a" * 24)
    corrupt.mkdir(parents=True)
    service = AgentUiService(project, ollama_model_id=MODEL_ID, max_runs=1)

    with pytest.raises(AgentUiError) as capacity:
        service.start(
            "salesforce",
            prompt="Migrate this bounded Salesforce fixture safely.",
        )

    assert capacity.value.code == "run_capacity_reached"


def test_invalid_browser_inputs_have_fixed_non_leaking_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    _stub_ollama(monkeypatch, project)
    service = AgentUiService(project, ollama_model_id=MODEL_ID)

    with pytest.raises(AgentUiError) as invalid_platform:
        service.start("python", prompt="Migrate this bounded fixture safely.")
    assert invalid_platform.value.code == "invalid_platform"

    with pytest.raises(AgentUiError) as invalid_prompt:
        service.start("salesforce", prompt="short")
    assert invalid_prompt.value.code == "invalid_prompt"

    with pytest.raises(AgentUiError) as nul_prompt:
        service.start("salesforce", prompt="Migrate this bounded\x00 fixture safely.")
    assert nul_prompt.value.code == "invalid_prompt"

    started = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice to additive LWC and Apex artifacts.",
    )
    with pytest.raises(AgentUiError) as invalid_selection:
        service.decide(
            started.handle,
            selection="modify",
            reviewer="course-reviewer",
        )
    assert invalid_selection.value.code == "invalid_decision"

    with pytest.raises(AgentUiError) as invalid_reviewer:
        service.decide(started.handle, selection="approve", reviewer="../../reviewer")
    assert invalid_reviewer.value.code == "invalid_reviewer"
    assert str(tmp_path) not in str(invalid_reviewer.value)
    assert tmp_path.exists()


def test_mulesoft_path_has_graph_wiki_candidate_and_validation_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    source = project / "fixtures/mulesoft/customer-status-api/input"
    before = snapshot_tree(source)
    _stub_ollama(monkeypatch, project)
    service = AgentUiService(project, ollama_model_id=MODEL_ID)

    started = service.start(
        "mulesoft",
        prompt="Migrate this bounded Mule 3 customer status API to an additive Mule 4 application.",
    )
    completed = service.decide(
        started.handle,
        selection="approve",
        reviewer="course-reviewer",
    )

    assert started.metrics.graph_nodes > 0
    assert started.metrics.graph_edges > 0
    assert started.metrics.wiki_hits > 0
    assert started.evidence.wiki_hits
    assert started.manifest is not None
    assert started.manifest.implementation_contract == MULESOFT_IMPLEMENTATION_CONTRACT
    assert completed.candidate is not None
    assert set(completed.candidate.changed_paths) == set(MULESOFT_TARGET_FILES)
    assert completed.validation is not None
    assert completed.validation.results
    assert completed.terminal_disposition == "environment_unavailable"
    assert snapshot_tree(source) == before


def test_local_ollama_uses_real_role_boundary_and_free_form_prompt_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    source = project / "fixtures/salesforce/account-contact-explorer/input"
    before = snapshot_tree(source)
    constructed: list[object] = []
    _stub_ollama(
        monkeypatch,
        project,
        constructed=constructed,
        expected_timeout_seconds=600.0,
    )
    service = AgentUiService(
        project,
        ollama_model_id=MODEL_ID,
        ollama_timeout_seconds=600.0,
    )
    prompt = (
        "Please migrate this Visualforce account explorer to an additive LWC and Apex "
        "implementation while keeping the existing source unchanged."
    )

    started = service.start(
        "salesforce",
        prompt=prompt,
    )

    assert started.prompt == prompt
    assert started.status == "awaiting_approval"
    assert started.boundaries.provider_invoked is True
    assert started.boundaries.provider_id == "ollama"
    assert started.boundaries.model_id == MODEL_ID
    assert started.boundaries.model_revision == LOCAL_MODEL_REVISION
    assert started.boundaries.execution_boundary == "local_loopback"
    assert started.boundaries.mode == "local_ollama"
    assert "Real structured-output inference" in started.boundaries.notice
    assert snapshot_tree(source) == before

    wrong_model_service = AgentUiService(project, ollama_model_id="qwen3.6:other")
    with pytest.raises(AgentUiError) as wrong_identity:
        wrong_model_service.decide(
            started.handle,
            selection="approve",
            reviewer="course-reviewer",
        )
    assert wrong_identity.value.code == "run_unavailable"

    completed = service.decide(
        started.handle,
        selection="approve",
        reviewer="course-reviewer",
    )

    assert completed.status == "completed"
    assert completed.boundaries.provider_invoked is True
    assert completed.boundaries.execution_boundary == "local_loopback"
    assert completed.candidate is not None
    assert completed.validation is not None
    assert len(constructed) == 2
    assert snapshot_tree(source) == before


def test_local_engineer_decision_required_is_a_safe_terminal_view_without_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []

    class DecisionRequiredLocalClient:
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
            del timeout_seconds
            self.model_id = model_id
            self.live_approval = approval
            self.last_usage: ModelUsageEvidence | None = None
            self.model_revision: str | None = None

        def bind_model_revision(self, expected_revision: str) -> None:
            self.model_revision = expected_revision

        def parse(
            self,
            *,
            system_prompt: str,
            input_value: BaseModel,
            output_type: type[BaseModel],
        ) -> BaseModel:
            role_calls.append(output_type.__name__)
            if output_type is EngineerModelOutcome:
                context = EngineerWorkspaceContext.model_validate(input_value)
                output_path = context.manifest.approved_paths[0]
                affected_paths = (output_path, context.request.target.entry_path)
                result: BaseModel = EngineerModelOutcome(
                    result=EngineerInterventionOutcome(
                        kind="decision_required",
                        intervention=ImplementationIntervention(
                            intervention_id="ui-local-engineer-stop",
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
                            public_concerns=(
                                "Generating candidate bytes would require an unsupported guess.",
                            ),
                            evidence=(
                                ImplementationInterventionEvidence(
                                    source="engineer_input",
                                    source_digest=context.input_evidence_digest,
                                    summary="The bounded context lacks the required contract.",
                                    affected_paths=affected_paths,
                                ),
                            ),
                            options=(
                                PlanningInterventionOption.EXPAND_SCOPE_AND_REPLAN,
                                PlanningInterventionOption.STOP_REQUEST,
                            ),
                            recommendation="Resolve the evidence gap and approve a new manifest.",
                        ),
                    )
                )
            else:
                result = fixture_model_response(
                    project,
                    system_prompt=system_prompt,
                    input_value=input_value,
                    output_type=output_type,
                )
            self.last_usage = ModelUsageEvidence(
                latency_ms=5,
                provider_usage_reported=True,
                input_tokens=75,
                output_tokens=25,
                total_tokens=100,
            )
            if self.model_revision is None:
                self.model_revision = LOCAL_MODEL_REVISION
            return result

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        DecisionRequiredLocalClient,
    )
    service = AgentUiService(project, ollama_model_id=MODEL_ID)
    started = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice, but stop if a required contract is absent.",
    )

    stopped = service.decide(
        started.handle,
        selection="approve",
        reviewer="course-reviewer",
    )

    assert stopped.status == "decision_required", stopped.failure
    assert stopped.terminal_disposition == "decision_required"
    assert stopped.candidate is None
    assert stopped.validation is None
    assert stopped.failure is None
    assert stopped.intervention is not None
    assert stopped.intervention.requested_action == "expand_scope"
    assert "required public implementation contract" in stopped.intervention.reason
    assert stopped.stages[2].state == "blocked"
    assert stopped.stages[3].state == "blocked"
    assert role_calls == ["ArchitectManifestProposal", "EngineerModelOutcome"]


def test_local_validator_failure_retains_read_only_candidate_and_deterministic_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []

    class ValidatorFailureLocalClient:
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
            del timeout_seconds
            self.model_id = model_id
            self.live_approval = approval
            self.last_usage: ModelUsageEvidence | None = None
            self.model_revision: str | None = None

        def bind_model_revision(self, expected_revision: str) -> None:
            self.model_revision = expected_revision

        def parse(
            self,
            *,
            system_prompt: str,
            input_value: BaseModel,
            output_type: type[BaseModel],
        ) -> BaseModel:
            role_calls.append(output_type.__name__)
            if output_type is ValidatorAdvisory:
                raise RuntimeError("secret=/Users/example/private-project")
            result = fixture_model_response(
                project,
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )
            self.last_usage = ModelUsageEvidence(
                latency_ms=9,
                provider_usage_reported=True,
                input_tokens=90,
                output_tokens=30,
                total_tokens=120,
            )
            if self.model_revision is None:
                self.model_revision = LOCAL_MODEL_REVISION
            return result

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        ValidatorFailureLocalClient,
    )
    service = AgentUiService(project, ollama_model_id=MODEL_ID)
    started = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice to additive LWC and Apex artifacts.",
    )

    failed = service.decide(
        started.handle,
        selection="approve",
        reviewer="course-reviewer",
    )

    assert failed.status == "failed"
    assert failed.failure is not None
    assert failed.failure.seam == "validator"
    assert failed.failure.reason_code == "provider_unavailable"
    assert failed.candidate is not None
    assert failed.candidate.download_available is False
    assert failed.candidate.files
    assert failed.validation is not None
    assert failed.validation.validator_completed is False
    assert failed.validation.advisory_assessment is None
    assert failed.validation.results
    assert "deterministic checks completed" in failed.validation.validator_summary
    assert [call.role for call in failed.model_calls] == ["architect", "engineer"]
    assert failed.stages[2].state == "complete"
    assert "deterministic checks completed" in failed.stages[3].detail
    serialized = failed.model_dump_json()
    assert "secret=/Users/example/private-project" not in serialized
    assert "/Users/example/private-project" not in serialized
    with pytest.raises(AgentUiError) as unavailable:
        service.candidate_zip(started.handle)
    assert unavailable.value.code == "candidate_unavailable"
    assert role_calls == [
        "ArchitectManifestProposal",
        "EngineerModelOutcome",
        "ValidatorAdvisory",
    ]


def test_local_architect_decision_required_never_opens_manifest_approval_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []

    class ArchitectDecisionLocalClient:
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
            del timeout_seconds
            self.model_id = model_id
            self.live_approval = approval
            self.last_usage: ModelUsageEvidence | None = None
            self.model_revision: str | None = None

        def bind_model_revision(self, expected_revision: str) -> None:
            self.model_revision = expected_revision

        def parse(
            self,
            *,
            system_prompt: str,
            input_value: BaseModel,
            output_type: type[BaseModel],
        ) -> BaseModel:
            role_calls.append(output_type.__name__)
            proposal = ArchitectManifestProposal.model_validate(
                fixture_model_response(
                    project,
                    system_prompt=system_prompt,
                    input_value=input_value,
                    output_type=output_type,
                )
            )
            dependencies = list(proposal.manifest.dependencies)
            dependencies[0] = dependencies[0].model_copy(update={"resolved": False})
            manifest = MigrationManifest.model_validate(
                {
                    **proposal.manifest.model_dump(mode="python"),
                    "dependencies": tuple(dependencies),
                    "required_approvals": (ApprovalAction.APPROVE_MANIFEST,),
                    "status": ManifestStatus.DECISION_REQUIRED,
                }
            )
            result = ArchitectManifestProposal.model_validate(
                {
                    **proposal.model_dump(mode="python"),
                    "manifest": manifest,
                    "public_decisions": (
                        "Keep the additive LWC and Apex output boundary unchanged.",
                        "Stop before approval until the unresolved dependency is confirmed.",
                    ),
                    "unresolved_questions": (
                        "Confirm the unresolved runtime dependency before implementation.",
                    ),
                }
            )
            self.last_usage = ModelUsageEvidence(
                latency_ms=6,
                provider_usage_reported=True,
                input_tokens=70,
                output_tokens=30,
                total_tokens=100,
            )
            self.model_revision = LOCAL_MODEL_REVISION
            return result

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        ArchitectDecisionLocalClient,
    )
    service = AgentUiService(project, ollama_model_id=MODEL_ID)

    stopped = service.start(
        "salesforce",
        prompt="Plan this bounded Visualforce migration and stop on unresolved dependencies.",
    )

    assert stopped.status == "decision_required", stopped.failure
    assert stopped.terminal_disposition == "decision_required"
    assert stopped.manifest is not None
    assert stopped.manifest.status == "decision_required"
    assert stopped.manifest.approval_required is False
    assert stopped.manifest.required_approvals == ("approve_manifest",)
    assert stopped.planning_decision is not None
    assert stopped.planning_decision.required_approvals == ("approve_manifest",)
    assert stopped.planning_decision.unresolved_questions == (
        "Confirm the unresolved runtime dependency before implementation.",
    )
    assert stopped.intervention is None
    assert stopped.candidate is None
    assert stopped.validation is None
    assert tuple(stage.state for stage in stopped.stages) == (
        "complete",
        "blocked",
        "blocked",
        "blocked",
    )
    assert role_calls == ["ArchitectManifestProposal"]
    run_dir = project / ".runs/agent-ui" / stopped.handle
    assert not tuple(run_dir.glob("evidence/model-runs/*/engineer-attempt-*.json"))
    assert not tuple(run_dir.glob("evidence/model-runs/*/validator-attempt-*.json"))

    with pytest.raises(AgentUiError) as no_approval_gate:
        service.decide(
            stopped.handle,
            selection="approve",
            reviewer="course-reviewer",
        )
    assert no_approval_gate.value.code == "decision_already_recorded"


def test_local_architect_provider_failure_returns_sanitized_durable_error_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)

    class FailingLocalClient:
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
            del timeout_seconds
            self.model_id = model_id
            self.live_approval = approval
            self.last_usage = None

        def parse(
            self,
            *,
            system_prompt: str,
            input_value: BaseModel,
            output_type: type[BaseModel],
        ) -> BaseModel:
            del system_prompt, input_value, output_type
            raise RuntimeError("secret=/Users/example/private-project")

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        FailingLocalClient,
    )
    service = AgentUiService(project, ollama_model_id=MODEL_ID, max_runs=1)
    terminal_output = io.StringIO()
    with terminal_lifecycle_logging(stream=terminal_output):
        failed = service.start(
            "salesforce",
            prompt="Migrate this bounded Visualforce slice using the local model.",
        )

    assert failed.status == "failed"
    assert failed.terminal_disposition == "controlled_failure"
    assert failed.manifest is None
    assert failed.candidate is None
    assert failed.validation is None
    assert failed.intervention is None
    assert failed.failure is not None
    assert failed.failure.seam == "architect"
    assert failed.failure.category == "provider_unavailable"
    assert failed.failure.reason_code == "provider_unavailable"
    assert failed.failure.phase == "model_inference"
    assert failed.failure.response_received is None
    assert failed.failure.schema_valid is None
    assert failed.failure.policy_valid is None
    assert "Ollama" in failed.failure.guidance
    assert failed.failure.terminal is True
    assert failed.failure.retry_eligible is False
    assert failed.boundaries.provider_attempted is True
    assert failed.boundaries.provider_invoked is None
    assert failed.boundaries.model_call_record_persisted is False
    assert failed.boundaries.structured_response_accepted is False
    assert failed.boundaries.model_revision is None
    assert failed.model_calls == ()
    assert "attempted" in failed.boundaries.notice
    serialized = failed.model_dump_json()
    assert "secret" not in serialized
    assert "/Users/" not in serialized
    lifecycle_log = terminal_output.getvalue()
    assert "event=model.call.failed" in lifecycle_log
    assert 'role="architect"' in lifecycle_log
    assert 'category="provider_unavailable"' in lifecycle_log
    assert 'reason_code="provider_unavailable"' in lifecycle_log
    assert "event=workflow.operation.failed" in lifecycle_log
    assert "secret" not in lifecycle_log
    assert "/Users/" not in lifecycle_log
    assert "Migrate this bounded Visualforce" not in lifecycle_log

    _stub_ollama(monkeypatch, project)
    replacement = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice after the failed run.",
    )
    assert replacement.status == "awaiting_approval"


@pytest.mark.parametrize(
    ("failure_kind", "reason_code"),
    (
        ("transformation", "transformation_scope_invalid"),
        ("implementation_contract", "implementation_contract_invalid"),
    ),
)
def test_local_architect_policy_failure_distinguishes_invocation_from_model_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    reason_code: str,
) -> None:
    project = _project(tmp_path)

    class ArchitectPolicyFailureLocalClient:
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
            del timeout_seconds
            self.model_id = model_id
            self.live_approval = approval
            self.last_usage: ModelUsageEvidence | None = None
            self.model_revision: str | None = None

        def bind_model_revision(self, expected_revision: str) -> None:
            self.model_revision = expected_revision

        def parse(
            self,
            *,
            system_prompt: str,
            input_value: BaseModel,
            output_type: type[BaseModel],
        ) -> BaseModel:
            proposal = ArchitectManifestProposal.model_validate(
                fixture_model_response(
                    project,
                    system_prompt=system_prompt,
                    input_value=input_value,
                    output_type=output_type,
                )
            )
            if failure_kind == "transformation":
                step = proposal.manifest.transformations[0]
                chained = step.model_copy(
                    update={
                        "input_paths": (
                            *step.input_paths,
                            proposal.manifest.approved_paths[0],
                        )
                    }
                )
                manifest = proposal.manifest.model_copy(update={"transformations": (chained,)})
            else:
                manifest = proposal.manifest.model_copy(update={"implementation_contract": ()})
            self.last_usage = ModelUsageEvidence(
                latency_ms=8,
                provider_usage_reported=True,
                input_tokens=80,
                output_tokens=20,
                total_tokens=100,
            )
            self.model_revision = LOCAL_MODEL_REVISION
            return proposal.model_copy(update={"manifest": manifest})

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        ArchitectPolicyFailureLocalClient,
    )
    service = AgentUiService(project, ollama_model_id=MODEL_ID)

    failed = service.start(
        "salesforce",
        prompt="Migrate this bounded Visualforce slice to additive LWC and Apex artifacts.",
    )

    assert failed.status == "failed"
    assert failed.failure is not None
    assert failed.failure.reason_code == reason_code
    assert failed.failure.phase == "policy_validation"
    if failure_kind == "implementation_contract":
        assert "controller-owned implementation contract" in failed.failure.summary
    assert failed.boundaries.provider_attempted is True
    assert failed.boundaries.provider_invoked is True
    assert failed.boundaries.model_call_record_persisted is False
    assert failed.boundaries.structured_response_accepted is True
    assert failed.boundaries.model_revision is None
    assert failed.model_calls == ()
    assert "No model-call record" in failed.boundaries.notice


def test_symlinked_run_parent_is_rejected_before_creating_external_content(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    external = tmp_path / "external-runs"
    external.mkdir()
    (project / ".runs").symlink_to(external, target_is_directory=True)
    service = AgentUiService(project, ollama_model_id=MODEL_ID)

    with pytest.raises(AgentUiError) as unavailable:
        service.start("salesforce", prompt="Migrate this bounded Salesforce fixture safely.")

    assert unavailable.value.code == "run_unavailable"
    assert not (external / "agent-ui").exists()
