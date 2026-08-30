from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_run import (
    _approval,
    _correction_approval,
    _project,
    _recoverable_validator,
)
from test_verified_benchmark_run_bundle import (
    _APPROVAL,
    _RUNTIME_IDENTITY,
    _anchor_binding,
    _copy_benchmark_project,
    _models,
    _RemotePresetModel,
    _RemoteRiskModel,
)

import legacy_migration_agent.benchmark_receipts as benchmark_receipts_module
import legacy_migration_agent.evaluation_runner as evaluation_runner_module
from legacy_migration_agent.agent_runtime.model_agents import ArchitectRiskObservation
from legacy_migration_agent.agent_runtime.openai_model import ModelCallRecord, ModelUsageEvidence
from legacy_migration_agent.application.agent_run import (
    prepare_agent_run_request,
    resume_agent_run,
    retry_agent_run,
)
from legacy_migration_agent.application.migration_scenarios import migration_launch_contract
from legacy_migration_agent.application.run_query import load_verified_benchmark_run_bundle
from legacy_migration_agent.benchmark_protocol import load_verified_benchmark_protocol
from legacy_migration_agent.contracts import (
    CheckResult,
    CheckStatus,
    Platform,
    RiskCategory,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.evaluation_runner import (
    bind_benchmark_knowledge_arm,
    start_benchmark_agent_run,
)
from legacy_migration_agent.knowledge.wiki import RiskReason
from legacy_migration_agent.measured_evaluation import (
    AcceptanceEvidence,
    BooleanEvidence,
    DefectEvidence,
    EvidenceAvailability,
    HumanAcceptance,
    HumanReviewRubric,
    RatioEvidence,
    WorkflowDisposition,
)


def _digest(seed: int) -> str:
    return f"sha256:{seed:064x}"


def test_receipt_is_derived_from_a_verified_planning_only_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _copy_benchmark_project(tmp_path)
    contract = migration_launch_contract("case-management-console")
    request = prepare_agent_run_request(
        project,
        request_id="request-receipt-decision",
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
    assert binding.execution_anchor_digest is not None
    monkeypatch.setattr(
        evaluation_runner_module,
        "_verify_execution_anchor_binding",
        lambda *_args: None,
    )
    run_id = "run-receipt-decision"
    thread_id = "thread-receipt-decision"
    run_dir = project / ".runs" / run_id
    start_benchmark_agent_run(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        launch_contract=contract,
        request=request,
        models=_models(_RemoteRiskModel),
        knowledge_binding=binding,
    )
    bundle = load_verified_benchmark_run_bundle(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
    )
    protocol = load_verified_benchmark_protocol(project)
    anchor = SimpleNamespace(
        anchor_digest=binding.execution_anchor_digest,
        runtime_identity_digest=_RUNTIME_IDENTITY,
        benchmark_registry_digest=protocol.registry_digest,
        protocol_declaration_digest=protocol.declaration_digest,
    )
    monkeypatch.setattr(
        benchmark_receipts_module,
        "load_verified_benchmark_execution_anchor",
        lambda *_args, **_kwargs: anchor,
    )
    rubric = HumanReviewRubric(
        rubric_id=f"{binding.cell_id}:rubric",
        registry_id=protocol.registry.registry_id,
        registry_digest=protocol.registry_digest,
        cell_id=binding.cell_id,
        case_id=binding.case_id,
        config_id="full-agent-wiki",
        repetition=1,
        run_id=run_id,
        run_evidence_digest=bundle.run_evidence.run_evidence_digest,
        runtime_identity_digest=_RUNTIME_IDENTITY,
        execution_anchor_digest=binding.execution_anchor_digest,
        reviewer_id="independent-course-reviewer",
        reviewed_at=datetime(2026, 8, 29, 18, 0, tzinfo=UTC),
        attestation="Reviewed independently against the frozen run and benchmark rubric.",
        acceptance=AcceptanceEvidence.available(
            HumanAcceptance.NOT_APPLICABLE,
            bundle.run_evidence.run_evidence_digest,
        ),
        semantic_conformance=BooleanEvidence.available(
            True,
            bundle.run_evidence.run_evidence_digest,
        ),
        wiki_support=RatioEvidence.available(
            2,
            2,
            bundle.run_evidence.run_evidence_digest,
        ),
        escaped_defects=DefectEvidence.available(
            (),
            bundle.run_evidence.run_evidence_digest,
        ),
    )
    rubric_path = tmp_path / "rubric.json"
    rubric_path.write_text(rubric.model_dump_json(indent=2) + "\n", encoding="utf-8")

    receipt = benchmark_receipts_module.extract_evaluation_cell_receipt(
        project,
        run_dir,
        rubric_path,
        tmp_path / "anchor.json",
        run_id=run_id,
        thread_id=thread_id,
    )

    assert receipt.workflow_disposition is WorkflowDisposition.DECISION_REQUIRED
    assert receipt.validation_attempts == 0
    assert receipt.intervention_triggered.value is True
    assert receipt.intervention_reason_ids.values == (
        "destructive_legacy_deletion",
        "sharing_boundary_weakening",
        "object_field_security_weakening",
        "permission_scope_expansion",
    )
    assert receipt.authorization_violations.value == 0
    assert receipt.usage.model_calls.value == 1
    assert receipt.usage.tool_calls.value == 0
    assert receipt.usage.input_tokens.value == 20
    assert receipt.usage.output_tokens.value == 10
    assert receipt.usage.cost_micro_usd.availability is EvidenceAvailability.UNAVAILABLE
    assert receipt.detected_dependencies.values is not None
    assert len(receipt.detected_dependencies.values) == 33
    assert receipt.unavailable_required_command_ids.availability is EvidenceAvailability.UNAVAILABLE
    assert receipt.human_rubric_digest == artifact_digest(rubric)

    identity_mutations: tuple[tuple[str, object], ...] = (
        ("registry_id", "another-registry"),
        ("registry_digest", _digest(901)),
        ("cell_id", "another-cell"),
        ("case_id", "another-case"),
        ("config_id", "another-config"),
        ("repetition", 2),
        ("run_id", "another-run"),
        ("run_evidence_digest", _digest(902)),
        ("runtime_identity_digest", _digest(903)),
        ("execution_anchor_digest", _digest(904)),
    )
    for field, replacement in identity_mutations:
        updates: dict[str, object] = {field: replacement}
        if field == "run_evidence_digest":
            updates.update(
                acceptance=rubric.acceptance.model_copy(update={"evidence_digest": replacement}),
                semantic_conformance=rubric.semantic_conformance.model_copy(
                    update={"evidence_digest": replacement}
                ),
                wiki_support=rubric.wiki_support.model_copy(
                    update={"evidence_digest": replacement}
                ),
                escaped_defects=rubric.escaped_defects.model_copy(
                    update={
                        "evidence_digest": replacement,
                        "defects": tuple(
                            defect.model_copy(update={"evidence_digest": replacement})
                            for defect in rubric.escaped_defects.defects or ()
                        ),
                    }
                ),
            )
        wrong = HumanReviewRubric.model_validate(rubric.model_dump(mode="python") | updates)
        rubric_path.write_text(wrong.model_dump_json(indent=2) + "\n", encoding="utf-8")
        with pytest.raises(
            PolicyViolation,
            match="benchmark human-review rubric is cross-bound",
        ):
            benchmark_receipts_module.extract_evaluation_cell_receipt(
                project,
                run_dir,
                rubric_path,
                tmp_path / "anchor.json",
                run_id=run_id,
                thread_id=thread_id,
            )


def test_receipt_hashes_canonical_planning_evidence_for_completed_nonrisk_run(
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
    assert binding.execution_anchor_digest is not None
    monkeypatch.setattr(
        evaluation_runner_module,
        "_verify_execution_anchor_binding",
        lambda *_args: None,
    )
    run_id = "run-receipt-completed-nonrisk"
    thread_id = "thread-receipt-completed-nonrisk"
    run_dir = project / ".runs" / run_id
    models = _models(_RemotePresetModel)
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
        approval=_correction_approval(attempt_one, reviewer="receipt-regression-test"),
        models=models,
        trusted_validator=validator,
    )

    bundle = load_verified_benchmark_run_bundle(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
    )
    assert bundle.planning is not None
    expected_planning_digest = artifact_digest(
        {
            "request": bundle.planning.request.model_dump(mode="json"),
            "architect_context": bundle.planning.architect_context.model_dump(mode="json"),
            "dependency_graph": bundle.planning.dependency_graph.model_dump(mode="json"),
            "wiki_trace": bundle.planning.wiki_trace.model_dump(mode="json"),
            "architect": bundle.planning.architect.model_dump(mode="json"),
        }
    )
    first_planning_digest = bundle.planning.planning_evidence_digest
    second_planning_digest = bundle.planning.planning_evidence_digest
    assert first_planning_digest == expected_planning_digest
    assert second_planning_digest == first_planning_digest

    protocol = load_verified_benchmark_protocol(project)
    anchor = SimpleNamespace(
        anchor_digest=binding.execution_anchor_digest,
        runtime_identity_digest=_RUNTIME_IDENTITY,
        benchmark_registry_digest=protocol.registry_digest,
        protocol_declaration_digest=protocol.declaration_digest,
    )
    monkeypatch.setattr(
        benchmark_receipts_module,
        "load_verified_benchmark_execution_anchor",
        lambda *_args, **_kwargs: anchor,
    )
    rubric = HumanReviewRubric(
        rubric_id=f"{binding.cell_id}:rubric",
        registry_id=protocol.registry.registry_id,
        registry_digest=protocol.registry_digest,
        cell_id=binding.cell_id,
        case_id=binding.case_id,
        config_id="full-agent-wiki",
        repetition=1,
        run_id=run_id,
        run_evidence_digest=bundle.run_evidence.run_evidence_digest,
        runtime_identity_digest=_RUNTIME_IDENTITY,
        execution_anchor_digest=binding.execution_anchor_digest,
        reviewer_id="independent-course-reviewer",
        reviewed_at=datetime(2026, 8, 30, 10, 0, tzinfo=UTC),
        attestation="Reviewed independently against the frozen run and benchmark rubric.",
        acceptance=AcceptanceEvidence.available(
            HumanAcceptance.ACCEPTED,
            bundle.run_evidence.run_evidence_digest,
        ),
        semantic_conformance=BooleanEvidence.available(
            True,
            bundle.run_evidence.run_evidence_digest,
        ),
        wiki_support=RatioEvidence.unavailable(
            "Assertion-level Wiki attribution was not separately scored."
        ),
        escaped_defects=DefectEvidence.available(
            (),
            bundle.run_evidence.run_evidence_digest,
        ),
    )
    rubric_path = tmp_path / "completed-rubric.json"
    rubric_path.write_text(rubric.model_dump_json(indent=2) + "\n", encoding="utf-8")

    receipt = benchmark_receipts_module.extract_evaluation_cell_receipt(
        project,
        run_dir,
        rubric_path,
        tmp_path / "anchor.json",
        run_id=run_id,
        thread_id=thread_id,
    )

    assert receipt.workflow_disposition is WorkflowDisposition.READY_FOR_HUMAN_REVIEW
    assert receipt.validation_attempts == 2
    assert receipt.intervention_triggered.value is False
    assert receipt.intervention_reason_ids.values == ()


def test_usage_receipt_counts_schema_valid_controller_rejected_model_call() -> None:
    usage = ModelUsageEvidence(
        latency_ms=7,
        provider_usage_reported=True,
        input_tokens=13,
        output_tokens=5,
        total_tokens=18,
    )
    call = ModelCallRecord(
        provider="claude-cli",
        model_id="claude-sonnet-5",
        agent_version="architect/v17",
        agent_definition_digest=_digest(1),
        live_invocation=True,
        store_false_sent=False,
        execution_boundary="remote_provider_managed",
        runtime_identity_digest=_RUNTIME_IDENTITY,
        live_approval=_APPROVAL,
        system_prompt_digest=_digest(2),
        input_digest=_digest(3),
        output_digest=_digest(4),
        usage=usage,
    )
    bundle = SimpleNamespace(
        model_call_evidence_complete=True,
        model_call_evidence_unavailable_reason=None,
        model_calls=(call,),
        tool_receipt_evidence_complete=True,
        tool_receipt_evidence_unavailable_reason=None,
        tool_receipts=(),
    )

    extracted = benchmark_receipts_module._usage_evidence(bundle)

    assert extracted.model_calls.value == 1
    assert extracted.latency_ms.value == 7
    assert extracted.input_tokens.value == 13
    assert extracted.output_tokens.value == 5
    assert extracted.tool_calls.value == 0


def test_unavailable_runtime_evidence_uses_command_ids_not_check_ids() -> None:
    report = ValidationReport(
        report_id="report-command-identity",
        request_id="request-command-identity",
        manifest_id="manifest-command-identity",
        change_set_id="change-set-command-identity",
        base_revision="sha256:" + "1" * 64,
        results=(
            CheckResult(
                check_id="runtime-capability-probe",
                command_id="mulesoft-munit",
                required=True,
                status=CheckStatus.UNAVAILABLE,
                summary="The declared Mule runtime is unavailable.",
            ),
        ),
        disposition=ValidationDisposition.ENVIRONMENT_UNAVAILABLE,
        attempt=1,
        completed_at=datetime(2026, 8, 29, 18, 30, tzinfo=UTC),
    )
    bundle = SimpleNamespace(
        attempts=(SimpleNamespace(report=report),),
        partial_evidence_unavailable_reason=None,
    )

    evidence = benchmark_receipts_module._unavailable_required_command_evidence(bundle)

    assert evidence.values == ("mulesoft-munit",)
    assert "runtime-capability-probe" not in (evidence.values or ())
    assert evidence.evidence_digest == artifact_digest(report)


def test_intervention_reason_evidence_comes_from_nonrisk_architect_observations() -> None:
    risk = ArchitectRiskObservation(
        category=RiskCategory.PUBLIC_CONTRACT,
        hazard_reason=RiskReason.PUBLIC_CONTRACT_CHANGE,
        summary="The public contract needs a human decision.",
        evidence_ids=("graph-node-1",),
        requires_human_decision=True,
    )
    completed_bundle = SimpleNamespace(
        planning=SimpleNamespace(
            planning_evidence_digest=_digest(7),
            architect=SimpleNamespace(agent_output=SimpleNamespace(risk_observations=(risk,))),
        ),
        benchmark_binding=SimpleNamespace(risk_seed_binding=None),
        partial_evidence_unavailable_reason=None,
    )

    completed = benchmark_receipts_module._intervention_reason_evidence(completed_bundle)
    unavailable = benchmark_receipts_module._intervention_reason_evidence(
        SimpleNamespace(
            planning=None,
            partial_evidence_unavailable_reason="verified planning evidence was not retained",
        )
    )

    assert completed.availability is EvidenceAvailability.AVAILABLE
    assert completed.values == (RiskReason.PUBLIC_CONTRACT_CHANGE.value,)
    assert unavailable.availability is EvidenceAvailability.UNAVAILABLE
    assert unavailable.reason == "verified planning evidence was not retained"
