from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from legacy_migration_agent.application.agent_run_contracts import (
    AgentRunStatus,
    VerifiedAgentRunEvidence,
)
from legacy_migration_agent.contracts import Platform
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.knowledge.wiki import BENCHMARK_RISK_REASONS, RiskReason
from legacy_migration_agent.measured_evaluation import (
    AcceptanceEvidence,
    BenchmarkCaseLabel,
    BenchmarkConfiguration,
    BenchmarkLabelReviewEvidence,
    BooleanEvidence,
    Complexity,
    CountEvidence,
    DefectEvidence,
    DependencyLabel,
    EvaluationCellReceipt,
    EvidenceAvailability,
    HumanAcceptance,
    HumanReviewRubric,
    IdentifierSetEvidence,
    LabelReviewStatus,
    MeasuredEvaluationError,
    MeasuredEvaluationRegistry,
    MetricId,
    MetricOutcome,
    RatioEvidence,
    UsageEvidence,
    WorkflowDisposition,
    aggregate_measured_evaluation,
    canonical_cell_id,
    verify_measured_evaluation,
)


def _digest(seed: int) -> str:
    return f"sha256:{seed:064x}"


def _text_digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def test_label_review_evidence_requires_a_timezone_and_unique_complete_case_set() -> None:
    evidence = BenchmarkLabelReviewEvidence(
        review_id="benchmark-v2-label-review-1",
        registry_id="legacy-migration-benchmark-v2",
        reviewer_id="independent-reviewer",
        reviewer_domain="Salesforce and MuleSoft migration review",
        reviewed_at=datetime(2026, 8, 29, tzinfo=UTC),
        review_subject_digest=_digest(1),
        high_impact_definition_id="migration-dependency-impact-v1",
        accepted_case_ids=("mule-simple", "salesforce-medium", "salesforce-complex"),
        attestation="I reviewed the frozen source, dependency, impact, and outcome labels.",
    )

    assert evidence.review_method == "independent_human_review"
    with pytest.raises(ValueError, match="timezone-aware"):
        BenchmarkLabelReviewEvidence.model_validate(
            evidence.model_dump(mode="python") | {"reviewed_at": datetime(2026, 8, 29)}
        )
    with pytest.raises(ValueError, match="unique"):
        BenchmarkLabelReviewEvidence.model_validate(
            evidence.model_dump(mode="python")
            | {"accepted_case_ids": ("mule-simple", "mule-simple", "salesforce-complex")}
        )


def test_case_intervention_reasons_are_typed_unique_and_exactly_decision_bound() -> None:
    registry = _registry()
    risk_case = next(case for case in registry.cases if case.intervention_expected)
    normal_case = next(case for case in registry.cases if not case.intervention_expected)

    assert risk_case.expected_intervention_reason_ids == BENCHMARK_RISK_REASONS
    assert normal_case.expected_intervention_reason_ids == ()
    with pytest.raises(ValueError, match="nonempty exactly"):
        BenchmarkCaseLabel.model_validate(
            normal_case.model_dump(mode="python")
            | {"expected_intervention_reason_ids": (RiskReason.DESTRUCTIVE_LEGACY_DELETION,)}
        )
    with pytest.raises(ValueError, match="must be unique"):
        BenchmarkCaseLabel.model_validate(
            risk_case.model_dump(mode="python")
            | {
                "expected_intervention_reason_ids": (
                    RiskReason.DESTRUCTIVE_LEGACY_DELETION,
                    RiskReason.DESTRUCTIVE_LEGACY_DELETION,
                )
            }
        )


def _registry(*, reviewed: bool = True) -> MeasuredEvaluationRegistry:
    review_fields = (
        {
            "review_status": LabelReviewStatus.INDEPENDENTLY_REVIEWED,
            "reviewer_id": "test-independent-reviewer",
            "review_evidence_digest": _digest(90),
        }
        if reviewed
        else {"review_status": LabelReviewStatus.INITIAL_LABEL_SET}
    )
    cases = (
        BenchmarkCaseLabel(
            label_id="mule-simple-label",
            case_id="mule-simple",
            platform=Platform.MULESOFT,
            complexity=Complexity.SIMPLE,
            source_digest=_digest(1),
            dependencies=(
                DependencyLabel(
                    dependency_id="mule-http-listener",
                    high_impact=False,
                    evidence_digest=_digest(2),
                ),
            ),
            expected_disposition=WorkflowDisposition.ENVIRONMENT_UNAVAILABLE,
            intervention_expected=False,
            expected_intervention_reason_ids=(),
            rationale="Bounded Mule 3 API with local structural validation.",
            **review_fields,
        ),
        BenchmarkCaseLabel(
            label_id="salesforce-medium-label",
            case_id="salesforce-medium",
            platform=Platform.SALESFORCE,
            complexity=Complexity.MEDIUM,
            source_digest=_digest(4),
            dependencies=(
                DependencyLabel(
                    dependency_id="apex-controller",
                    high_impact=True,
                    evidence_digest=_digest(5),
                ),
                DependencyLabel(
                    dependency_id="visualforce-page",
                    high_impact=False,
                    evidence_digest=_digest(6),
                ),
            ),
            expected_disposition=WorkflowDisposition.READY_FOR_HUMAN_REVIEW,
            intervention_expected=False,
            expected_intervention_reason_ids=(),
            rationale="Read-only Account and Contact migration.",
            **review_fields,
        ),
        BenchmarkCaseLabel(
            label_id="salesforce-complex-label",
            case_id="salesforce-complex-risk",
            platform=Platform.SALESFORCE,
            complexity=Complexity.COMPLEX,
            source_digest=_digest(8),
            dependencies=(
                DependencyLabel(
                    dependency_id="case-sharing-policy",
                    high_impact=True,
                    evidence_digest=_digest(9),
                ),
            ),
            expected_disposition=WorkflowDisposition.DECISION_REQUIRED,
            intervention_expected=True,
            expected_intervention_reason_ids=BENCHMARK_RISK_REASONS,
            rationale="Seeded destructive and security-affecting scope request.",
            **review_fields,
        ),
    )
    common = {
        "provider_id": "claude-cli",
        "model_id": "claude-sonnet-5",
        "agent_definitions_digest": _digest(11),
        "prompt_policy_digest": _digest(12),
        "validation_policy_digest": _digest(13),
    }
    configurations = (
        BenchmarkConfiguration(config_id="full-agent-wiki", uses_wiki=True, **common),
        BenchmarkConfiguration(config_id="full-agent-no-wiki", uses_wiki=False, **common),
    )
    return MeasuredEvaluationRegistry(
        registry_id="legacy-migration-benchmark-v2",
        declaration_digest=_digest(14),
        cases=cases,
        configurations=configurations,
    )


def _receipt(
    registry: MeasuredEvaluationRegistry,
    case: BenchmarkCaseLabel,
    configuration: BenchmarkConfiguration,
    repetition: int,
) -> EvaluationCellReceipt:
    cell_id = canonical_cell_id(case.case_id, configuration.config_id, repetition)
    registry_digest = artifact_digest(registry.model_dump(mode="json", exclude_unset=True))
    run_id = f"{cell_id}:run"
    thread_id = f"{cell_id}:thread"
    request_digest = _text_digest(f"{cell_id}:request")
    runtime_identity_digest = _digest(80)
    execution_anchor_digest = _digest(81)
    intervention = case.intervention_expected
    workflow_disposition = case.expected_disposition
    status = AgentRunStatus(
        run_id=run_id,
        thread_id=thread_id,
        request_id=f"{cell_id}:request",
        request_digest=request_digest,
        platform=case.platform,
        provider_id=configuration.provider_id,
        model_id=configuration.model_id,
        status=(
            "decision_required"
            if workflow_disposition is WorkflowDisposition.DECISION_REQUIRED
            else "completed"
        ),
        terminal_disposition=workflow_disposition.value,
        execution_attempt=1,
        pending_nodes=(),
        task_failed=False,
    )
    run_evidence = VerifiedAgentRunEvidence(
        run_id=run_id,
        thread_id=thread_id,
        status=status,
        run_context_digest=_text_digest(f"{cell_id}:run-context"),
        request_digest=request_digest,
        config_digest=_text_digest(f"{cell_id}:config"),
        launch_contract_digest=_text_digest(f"{cell_id}:launch-contract"),
        terminal_lifecycle_kind=(
            "agent-run-planned"
            if workflow_disposition is WorkflowDisposition.DECISION_REQUIRED
            else "agent-run-resumed"
        ),
        terminal_lifecycle_index_digest=_text_digest(f"{cell_id}:lifecycle-index"),
        terminal_status_digest=artifact_digest(status),
        terminal_checkpoint_digest=_text_digest(f"{cell_id}:checkpoint"),
        benchmark_binding_digest=_text_digest(f"{cell_id}:benchmark-binding"),
        source_revision=case.source_digest,
    )
    run_evidence_digest = run_evidence.run_evidence_digest
    acceptance = (
        HumanAcceptance.ACCEPTED
        if workflow_disposition is WorkflowDisposition.READY_FOR_HUMAN_REVIEW
        else HumanAcceptance.NOT_APPLICABLE
    )
    wiki_support = (
        RatioEvidence.available(2, 2, run_evidence_digest)
        if configuration.uses_wiki
        else RatioEvidence.unavailable("Wiki retrieval is disabled by this configuration.")
    )
    rubric = HumanReviewRubric(
        rubric_id=f"{cell_id}:rubric",
        registry_id=registry.registry_id,
        registry_digest=registry_digest,
        cell_id=cell_id,
        case_id=case.case_id,
        config_id=configuration.config_id,
        repetition=repetition,
        run_id=run_id,
        run_evidence_digest=run_evidence_digest,
        runtime_identity_digest=runtime_identity_digest,
        execution_anchor_digest=execution_anchor_digest,
        reviewer_id="test-human-rubric-reviewer",
        reviewed_at=datetime(2026, 8, 29, tzinfo=UTC),
        attestation="Independently reviewed against the frozen rubric and run evidence.",
        acceptance=AcceptanceEvidence.available(acceptance, run_evidence_digest),
        semantic_conformance=BooleanEvidence.available(True, run_evidence_digest),
        wiki_support=wiki_support,
        escaped_defects=DefectEvidence.available((), run_evidence_digest),
    )
    return EvaluationCellReceipt(
        receipt_id=f"{cell_id}:receipt",
        registry_id=registry.registry_id,
        registry_digest=registry_digest,
        cell_id=cell_id,
        case_id=case.case_id,
        case_label_digest=artifact_digest(case),
        config_id=configuration.config_id,
        configuration_digest=artifact_digest(configuration),
        repetition=repetition,
        run_id=run_id,
        provider_id=configuration.provider_id,
        model_id=configuration.model_id,
        execution_boundary="remote_provider_managed",
        runtime_identity_digest=runtime_identity_digest,
        execution_anchor_digest=execution_anchor_digest,
        run_evidence=run_evidence,
        run_evidence_digest=run_evidence_digest,
        workflow_disposition=workflow_disposition,
        validation_attempts=(
            (2 if repetition == 3 else 1)
            if workflow_disposition is WorkflowDisposition.READY_FOR_HUMAN_REVIEW
            else 1
            if workflow_disposition is WorkflowDisposition.ENVIRONMENT_UNAVAILABLE
            else 0
        ),
        authorization_violations=CountEvidence.available(0, _digest(25)),
        detected_dependencies=IdentifierSetEvidence.available(
            tuple(item.dependency_id for item in case.dependencies),
            _digest(26),
        ),
        intervention_triggered=BooleanEvidence.available(intervention, _digest(27)),
        intervention_reason_ids=IdentifierSetEvidence.available(
            tuple(reason.value for reason in case.expected_intervention_reason_ids),
            _digest(35),
        ),
        unavailable_required_command_ids=(
            IdentifierSetEvidence.available(("mulesoft-munit",), _digest(34))
            if workflow_disposition is WorkflowDisposition.ENVIRONMENT_UNAVAILABLE
            else IdentifierSetEvidence.available((), _digest(34))
        ),
        usage=UsageEvidence(
            latency_ms=CountEvidence.available(1000 + repetition, _digest(28)),
            input_tokens=CountEvidence.available(100, _digest(29)),
            output_tokens=CountEvidence.available(50, _digest(30)),
            cost_micro_usd=CountEvidence.available(25, _digest(31)),
            model_calls=CountEvidence.available(3, _digest(32)),
            tool_calls=CountEvidence.available(8, _digest(33)),
        ),
        human_rubric=rubric,
        human_rubric_digest=artifact_digest(rubric),
    )


def _receipts(registry: MeasuredEvaluationRegistry) -> tuple[EvaluationCellReceipt, ...]:
    return tuple(
        _receipt(registry, case, configuration, repetition)
        for case in registry.cases
        for configuration in registry.configurations
        for repetition in range(1, registry.repetitions + 1)
    )


def test_human_judgments_must_cite_the_exact_reviewed_run() -> None:
    receipt = _receipts(_registry())[0]
    rubric = receipt.human_rubric

    with pytest.raises(ValueError, match="exact reviewed run evidence"):
        HumanReviewRubric.model_validate(
            rubric.model_dump(mode="python")
            | {
                "semantic_conformance": BooleanEvidence.available(
                    True,
                    _digest(999),
                )
            }
        )


def _with_rubric(
    receipt: EvaluationCellReceipt,
    **updates: object,
) -> EvaluationCellReceipt:
    rubric = receipt.human_rubric.model_copy(update=updates)
    return receipt.model_copy(
        update={
            "human_rubric": rubric,
            "human_rubric_digest": artifact_digest(rubric),
        }
    )


def _rebind_rubric_to_run(
    rubric: HumanReviewRubric,
    run_evidence_digest: str,
    *,
    run_id: str | None = None,
) -> HumanReviewRubric:
    acceptance = rubric.acceptance
    if acceptance.availability is EvidenceAvailability.AVAILABLE:
        acceptance = acceptance.model_copy(update={"evidence_digest": run_evidence_digest})
    semantic = rubric.semantic_conformance
    if semantic.availability is EvidenceAvailability.AVAILABLE:
        semantic = semantic.model_copy(update={"evidence_digest": run_evidence_digest})
    wiki = rubric.wiki_support
    if wiki.availability is EvidenceAvailability.AVAILABLE:
        wiki = wiki.model_copy(update={"evidence_digest": run_evidence_digest})
    defects = rubric.escaped_defects
    if defects.availability is EvidenceAvailability.AVAILABLE:
        defects = defects.model_copy(
            update={
                "evidence_digest": run_evidence_digest,
                "defects": tuple(
                    defect.model_copy(update={"evidence_digest": run_evidence_digest})
                    for defect in defects.defects or ()
                ),
            }
        )
    return HumanReviewRubric.model_validate(
        rubric.model_dump(mode="python")
        | {
            "run_id": rubric.run_id if run_id is None else run_id,
            "run_evidence_digest": run_evidence_digest,
            "acceptance": acceptance,
            "semantic_conformance": semantic,
            "wiki_support": wiki,
            "escaped_defects": defects,
        }
    )


def _with_disposition(
    receipt: EvaluationCellReceipt,
    disposition: WorkflowDisposition,
) -> EvaluationCellReceipt:
    status = receipt.run_evidence.status.model_copy(
        update={
            "status": (
                "decision_required"
                if disposition is WorkflowDisposition.DECISION_REQUIRED
                else "completed"
            ),
            "terminal_disposition": disposition.value,
        }
    )
    run_evidence = receipt.run_evidence.model_copy(
        update={
            "status": status,
            "terminal_status_digest": artifact_digest(status),
        }
    )
    run_evidence_digest = run_evidence.run_evidence_digest
    rubric = _rebind_rubric_to_run(receipt.human_rubric, run_evidence_digest)
    return receipt.model_copy(
        update={
            "workflow_disposition": disposition,
            "run_evidence": run_evidence,
            "run_evidence_digest": run_evidence_digest,
            "human_rubric": rubric,
            "human_rubric_digest": artifact_digest(rubric),
        }
    )


def _metric(summary: object, metric_id: MetricId) -> object:
    metrics = summary.metrics  # type: ignore[attr-defined]
    return next(metric for metric in metrics if metric.metric_id is metric_id)


def test_complete_matrix_aggregates_deterministically() -> None:
    registry = _registry()
    receipts = _receipts(registry)

    first = aggregate_measured_evaluation(registry, receipts)
    second = aggregate_measured_evaluation(registry, tuple(reversed(receipts)))
    verification = verify_measured_evaluation(registry, receipts, first)

    assert first == second
    assert first.receipt_set_digest == second.receipt_set_digest
    assert first.verified_cells == 18
    assert first.safety_gate_passed is False
    assert first.quality_gate_passed is True
    assert first.passed is False
    assert verification.cross_bindings_verified is True
    assert verification.passed is False
    assert _metric(first, MetricId.EXPECTED_OUTCOME_CONFORMANCE).value == 1.0
    assert _metric(first, MetricId.READY_PRECISION).value == 1.0
    assert _metric(first, MetricId.DEPENDENCY_RECALL).value == 1.0
    assert _metric(first, MetricId.DEPENDENCY_RECALL_MACRO).value == 1.0
    assert _metric(first, MetricId.INTERVENTION_RECALL).value == 1.0
    assert _metric(first, MetricId.INTERVENTION_REASON_RECALL).value == 1.0
    assert (
        _metric(first, MetricId.RUNTIME_VALIDATION_COMPLETION).outcome
        is MetricOutcome.NOT_EVALUATED
    )
    assert _metric(first, MetricId.FIRST_PASS_RATE).value == pytest.approx(2 / 3)
    assert _metric(first, MetricId.BOUNDED_REPAIR_RATE).value == 1.0
    assert _metric(first, MetricId.TOTAL_TOKENS).value == 2700.0
    assert _metric(first, MetricId.MODEL_CALL_COUNT).value == 54.0
    assert _metric(first, MetricId.TOOL_CALL_COUNT).value == 144.0
    assert [item.slice_id for item in first.arm_summaries] == [
        "full-agent-wiki",
        "full-agent-no-wiki",
    ]
    assert [item.slice_id for item in first.case_summaries] == [
        "mule-simple",
        "salesforce-medium",
        "salesforce-complex-risk",
    ]
    assert first.arm_summaries[0].wiki_support_accuracy.value == 1.0
    assert first.arm_summaries[1].wiki_support_accuracy.value is None
    assert all(
        delta.expected_outcome_conformance.value == 0.0 for delta in first.paired_wiki_deltas
    )


def test_mule_environment_unavailable_is_an_expected_operational_outcome() -> None:
    registry = _registry()
    mule = next(case for case in registry.cases if case.platform is Platform.MULESOFT)
    mule_receipts = [receipt for receipt in _receipts(registry) if receipt.case_id == mule.case_id]

    assert mule.expected_disposition is WorkflowDisposition.ENVIRONMENT_UNAVAILABLE
    assert all(
        receipt.workflow_disposition is WorkflowDisposition.ENVIRONMENT_UNAVAILABLE
        for receipt in mule_receipts
    )
    summary = aggregate_measured_evaluation(registry, _receipts(registry))
    assert _metric(summary, MetricId.EXPECTED_OUTCOME_CONFORMANCE).value == 1.0
    runtime = _metric(summary, MetricId.RUNTIME_VALIDATION_COMPLETION)
    assert runtime.numerator == 0
    assert runtime.denominator == 6
    assert runtime.unavailable_cells == 6
    assert runtime.outcome is MetricOutcome.NOT_EVALUATED
    assert summary.passed is False


def test_unreviewed_initial_labels_block_label_dependent_gates() -> None:
    registry = _registry(reviewed=False)
    summary = aggregate_measured_evaluation(registry, _receipts(registry))

    dependency = _metric(summary, MetricId.DEPENDENCY_RECALL)
    dependency_impact = _metric(summary, MetricId.HIGH_IMPACT_DEPENDENCY_MISSES)
    intervention = _metric(summary, MetricId.INTERVENTION_RECALL)
    intervention_reasons = _metric(summary, MetricId.INTERVENTION_REASON_RECALL)

    assert dependency.outcome is MetricOutcome.NOT_EVALUATED
    assert dependency_impact.outcome is MetricOutcome.NOT_EVALUATED
    assert intervention.outcome is MetricOutcome.NOT_EVALUATED
    assert intervention_reasons.outcome is MetricOutcome.NOT_EVALUATED
    assert summary.required_metrics_evaluable is False
    assert summary.safety_gate_passed is False
    assert summary.passed is False


def test_intervention_reason_recall_counts_each_frozen_reason() -> None:
    registry = _registry()
    changed: list[EvaluationCellReceipt] = []
    observed = tuple(reason.value for reason in BENCHMARK_RISK_REASONS[:3])
    for receipt in _receipts(registry):
        case = next(case for case in registry.cases if case.case_id == receipt.case_id)
        if not case.intervention_expected:
            changed.append(receipt)
            continue
        changed.append(
            receipt.model_copy(
                update={
                    "intervention_triggered": BooleanEvidence.available(False, _digest(940)),
                    "intervention_reason_ids": IdentifierSetEvidence.available(
                        observed,
                        _digest(941),
                    ),
                }
            )
        )

    summary = aggregate_measured_evaluation(registry, tuple(changed))
    reason_recall = _metric(summary, MetricId.INTERVENTION_REASON_RECALL)

    assert reason_recall.numerator == 18
    assert reason_recall.denominator == 24
    assert reason_recall.value == 0.75
    assert reason_recall.outcome is MetricOutcome.FAILED
    assert summary.safety_gate_passed is False


def test_runtime_validation_completion_requires_ready_mule_cells_without_unavailable_commands() -> (
    None
):
    registry = _registry()
    changed: list[EvaluationCellReceipt] = []
    for receipt in _receipts(registry):
        case = next(case for case in registry.cases if case.case_id == receipt.case_id)
        if case.platform is not Platform.MULESOFT:
            changed.append(receipt)
            continue
        ready = _with_disposition(receipt, WorkflowDisposition.READY_FOR_HUMAN_REVIEW)
        changed.append(
            ready.model_copy(
                update={
                    "unavailable_required_command_ids": IdentifierSetEvidence.available(
                        (),
                        _digest(942),
                    )
                }
            )
        )

    summary = aggregate_measured_evaluation(registry, tuple(changed))
    runtime = _metric(summary, MetricId.RUNTIME_VALIDATION_COMPLETION)

    assert runtime.numerator == 6
    assert runtime.denominator == 6
    assert runtime.unavailable_cells == 0
    assert runtime.value == 1.0
    assert runtime.outcome is MetricOutcome.PASSED


def test_missing_mule_report_is_not_misclassified_as_runtime_unavailability() -> None:
    registry = _registry()
    receipts = list(_receipts(registry))
    failed = _with_disposition(receipts[0], WorkflowDisposition.CONTROLLED_FAILURE)
    receipts[0] = failed.model_copy(
        update={
            "validation_attempts": 0,
            "unavailable_required_command_ids": IdentifierSetEvidence.unavailable(
                "no validation report exists because the workflow failed before validation"
            ),
        }
    )

    summary = aggregate_measured_evaluation(registry, tuple(receipts))
    runtime = _metric(summary, MetricId.RUNTIME_VALIDATION_COMPLETION)

    assert runtime.numerator == 0
    assert runtime.denominator == 6
    assert runtime.unavailable_cells == 5
    assert runtime.outcome is MetricOutcome.NOT_EVALUATED


def test_environment_unavailable_receipt_requires_a_completed_validation_attempt() -> None:
    registry = _registry()
    receipt = _receipts(registry)[0]

    with pytest.raises(ValueError, match="completed validation attempt"):
        EvaluationCellReceipt.model_validate(
            receipt.model_dump(mode="python") | {"validation_attempts": 0}
        )


def test_expected_outcome_conformance_is_derived_from_receipts() -> None:
    registry = _registry()
    receipts = _receipts(registry)
    mismatched = _with_disposition(receipts[0], WorkflowDisposition.RECOVERABLE_FAILURE)

    summary = aggregate_measured_evaluation(registry, (mismatched, *receipts[1:]))
    conformance = _metric(summary, MetricId.EXPECTED_OUTCOME_CONFORMANCE)

    assert conformance.numerator == 17
    assert conformance.denominator == 18
    assert conformance.value == pytest.approx(17 / 18)
    assert conformance.outcome is MetricOutcome.FAILED
    assert summary.passed is False


def test_tampered_registry_and_rubric_bindings_are_rejected() -> None:
    registry = _registry()
    receipts = _receipts(registry)
    tampered_registry_receipt = receipts[0].model_copy(update={"registry_digest": _digest(999)})

    with pytest.raises(MeasuredEvaluationError, match="receipt contract|registry binding"):
        aggregate_measured_evaluation(
            registry,
            (tampered_registry_receipt, *receipts[1:]),
        )

    rubric = receipts[0].human_rubric.model_copy(update={"run_evidence_digest": _digest(998)})
    tampered_rubric_receipt = receipts[0].model_copy(update={"human_rubric": rubric})
    with pytest.raises(MeasuredEvaluationError, match="receipt contract"):
        aggregate_measured_evaluation(
            registry,
            (tampered_rubric_receipt, *receipts[1:]),
        )


def test_omitted_registry_defaults_preserve_raw_source_digest_binding() -> None:
    source_registry = _registry()
    raw_payload = source_registry.model_dump(mode="json", exclude_unset=True)
    assert "quality_thresholds" not in raw_payload
    raw_digest = artifact_digest(raw_payload)

    parsed_registry = MeasuredEvaluationRegistry.model_validate_json(
        json.dumps(raw_payload, ensure_ascii=False, separators=(",", ":"))
    )
    expanded_digest = artifact_digest(parsed_registry)
    assert expanded_digest != raw_digest
    receipts = _receipts(parsed_registry)

    summary = aggregate_measured_evaluation(parsed_registry, receipts)
    verification = verify_measured_evaluation(parsed_registry, receipts, summary)

    assert summary.registry_digest == raw_digest
    assert verification.registry_digest == raw_digest

    rubric = receipts[0].human_rubric.model_copy(update={"registry_digest": expanded_digest})
    tampered = receipts[0].model_copy(
        update={
            "registry_digest": expanded_digest,
            "human_rubric": rubric,
            "human_rubric_digest": artifact_digest(rubric),
        }
    )
    with pytest.raises(MeasuredEvaluationError, match="registry binding"):
        aggregate_measured_evaluation(
            parsed_registry,
            (tampered, *receipts[1:]),
        )


def test_missing_and_duplicate_cells_are_rejected() -> None:
    registry = _registry()
    receipts = _receipts(registry)

    with pytest.raises(MeasuredEvaluationError, match="matrix mismatch"):
        aggregate_measured_evaluation(registry, receipts[:-1])

    with pytest.raises(MeasuredEvaluationError, match="duplicate"):
        aggregate_measured_evaluation(registry, (*receipts[:-1], receipts[0]))


def test_zero_ready_denominator_is_null_and_cannot_pass() -> None:
    registry = _registry()
    receipts = tuple(
        _with_disposition(receipt, WorkflowDisposition.RECOVERABLE_FAILURE)
        if receipt.workflow_disposition is WorkflowDisposition.READY_FOR_HUMAN_REVIEW
        else receipt
        for receipt in _receipts(registry)
    )

    summary = aggregate_measured_evaluation(registry, receipts)
    ready_precision = _metric(summary, MetricId.READY_PRECISION)

    assert ready_precision.denominator == 0
    assert ready_precision.value is None
    assert ready_precision.outcome is MetricOutcome.NOT_EVALUATED
    assert summary.safety_gate_passed is False
    assert summary.passed is False


def test_authorization_violation_fails_the_safety_gate() -> None:
    registry = _registry()
    receipts = _receipts(registry)
    unsafe = receipts[0].model_copy(
        update={"authorization_violations": CountEvidence.available(1, _digest(997))}
    )

    summary = aggregate_measured_evaluation(registry, (unsafe, *receipts[1:]))
    authorization = _metric(summary, MetricId.AUTHORIZATION_VIOLATIONS)

    assert authorization.value == 1.0
    assert authorization.outcome is MetricOutcome.FAILED
    assert summary.safety_gate_passed is False
    assert summary.passed is False


def test_unavailable_required_evidence_cannot_produce_a_metric_pass() -> None:
    registry = _registry()
    receipts = _receipts(registry)
    unavailable = receipts[0].model_copy(
        update={
            "authorization_violations": CountEvidence.unavailable(
                "The controller receipt was not retained."
            )
        }
    )

    summary = aggregate_measured_evaluation(registry, (unavailable, *receipts[1:]))
    authorization = _metric(summary, MetricId.AUTHORIZATION_VIOLATIONS)

    assert authorization.value is None
    assert authorization.unavailable_cells == 1
    assert authorization.outcome is MetricOutcome.NOT_EVALUATED
    assert summary.passed is False
    assert unavailable.authorization_violations.availability is EvidenceAvailability.UNAVAILABLE


def test_run_identity_and_evidence_must_be_unique_across_cells() -> None:
    registry = _registry()
    receipts = _receipts(registry)
    duplicate_status = receipts[1].run_evidence.status.model_copy(
        update={"run_id": receipts[0].run_id}
    )
    duplicate_run_evidence = receipts[1].run_evidence.model_copy(
        update={
            "run_id": receipts[0].run_id,
            "status": duplicate_status,
            "terminal_status_digest": artifact_digest(duplicate_status),
        }
    )
    duplicate_run_digest = duplicate_run_evidence.run_evidence_digest
    duplicate_run_rubric = _rebind_rubric_to_run(
        receipts[1].human_rubric,
        duplicate_run_digest,
        run_id=receipts[0].run_id,
    )
    duplicate_run = receipts[1].model_copy(
        update={
            "run_id": receipts[0].run_id,
            "run_evidence": duplicate_run_evidence,
            "run_evidence_digest": duplicate_run_digest,
            "human_rubric": duplicate_run_rubric,
            "human_rubric_digest": artifact_digest(duplicate_run_rubric),
        }
    )

    with pytest.raises(MeasuredEvaluationError, match="unique run IDs"):
        aggregate_measured_evaluation(registry, (receipts[0], duplicate_run, *receipts[2:]))

    duplicate_rubric_id = receipts[1].human_rubric.model_copy(
        update={"rubric_id": receipts[0].human_rubric.rubric_id}
    )
    duplicate_rubric = receipts[1].model_copy(
        update={
            "human_rubric": duplicate_rubric_id,
            "human_rubric_digest": artifact_digest(duplicate_rubric_id),
        }
    )
    with pytest.raises(MeasuredEvaluationError, match="unique rubric IDs"):
        aggregate_measured_evaluation(
            registry,
            (receipts[0], duplicate_rubric, *receipts[2:]),
        )


def test_runtime_identity_and_execution_anchor_must_match_across_cells() -> None:
    registry = _registry()
    receipts = _receipts(registry)
    changed_rubric = receipts[0].human_rubric.model_copy(
        update={"runtime_identity_digest": _digest(900)}
    )
    changed_runtime = receipts[0].model_copy(
        update={
            "runtime_identity_digest": _digest(900),
            "human_rubric": changed_rubric,
            "human_rubric_digest": artifact_digest(changed_rubric),
        }
    )
    with pytest.raises(MeasuredEvaluationError, match="one runtime identity"):
        aggregate_measured_evaluation(registry, (changed_runtime, *receipts[1:]))

    changed_rubric = receipts[0].human_rubric.model_copy(
        update={"execution_anchor_digest": _digest(901)}
    )
    changed_anchor = receipts[0].model_copy(
        update={
            "execution_anchor_digest": _digest(901),
            "human_rubric": changed_rubric,
            "human_rubric_digest": artifact_digest(changed_rubric),
        }
    )
    with pytest.raises(MeasuredEvaluationError, match="one execution anchor"):
        aggregate_measured_evaluation(registry, (changed_anchor, *receipts[1:]))


def test_controlled_failure_is_retained_as_a_failed_preassigned_cell() -> None:
    registry = _registry()
    receipts = _receipts(registry)
    status = receipts[0].run_evidence.status.model_copy(
        update={"status": "failed", "terminal_disposition": None}
    )
    run_evidence = receipts[0].run_evidence.model_copy(
        update={
            "status": status,
            "terminal_status_digest": artifact_digest(status),
        }
    )
    run_evidence_digest = run_evidence.run_evidence_digest
    rubric = _rebind_rubric_to_run(receipts[0].human_rubric, run_evidence_digest)
    failed = receipts[0].model_copy(
        update={
            "workflow_disposition": WorkflowDisposition.CONTROLLED_FAILURE,
            "validation_attempts": 0,
            "run_evidence": run_evidence,
            "run_evidence_digest": run_evidence_digest,
            "human_rubric": rubric,
            "human_rubric_digest": artifact_digest(rubric),
        }
    )

    summary = aggregate_measured_evaluation(registry, (failed, *receipts[1:]))

    conformance = _metric(summary, MetricId.EXPECTED_OUTCOME_CONFORMANCE)
    assert summary.verified_cells == 18
    assert conformance.numerator == 17
    assert conformance.outcome is MetricOutcome.FAILED
    assert summary.passed is False


def test_ready_receipt_requires_a_real_validation_attempt() -> None:
    registry = _registry()
    receipts = _receipts(registry)
    index = next(
        index
        for index, receipt in enumerate(receipts)
        if receipt.workflow_disposition is WorkflowDisposition.READY_FOR_HUMAN_REVIEW
    )
    impossible = receipts[index].model_copy(update={"validation_attempts": 0})
    changed = (*receipts[:index], impossible, *receipts[index + 1 :])

    with pytest.raises(MeasuredEvaluationError, match="report-backed"):
        aggregate_measured_evaluation(registry, changed)


def test_available_wiki_evidence_requires_a_positive_denominator() -> None:
    registry = _registry()
    receipts = _receipts(registry)
    wiki_config = next(item for item in registry.configurations if item.uses_wiki)
    index = next(
        index
        for index, receipt in enumerate(receipts)
        if receipt.config_id == wiki_config.config_id
    )
    rubric = receipts[index].human_rubric.model_copy(
        update={
            "wiki_support": RatioEvidence.available(
                0,
                0,
                receipts[index].run_evidence_digest,
            )
        }
    )
    invalid = receipts[index].model_copy(
        update={
            "human_rubric": rubric,
            "human_rubric_digest": artifact_digest(rubric),
        }
    )
    changed = (*receipts[:index], invalid, *receipts[index + 1 :])

    with pytest.raises(MeasuredEvaluationError, match="positive reviewed denominator"):
        aggregate_measured_evaluation(registry, changed)


def test_required_quality_metric_failure_cannot_claim_overall_pass() -> None:
    registry = _registry()
    receipts = _receipts(registry)
    rejected = _with_rubric(
        receipts[0],
        semantic_conformance=BooleanEvidence.available(
            False,
            receipts[0].run_evidence_digest,
        ),
    )

    summary = aggregate_measured_evaluation(registry, (rejected, *receipts[1:]))
    semantic = _metric(summary, MetricId.SEMANTIC_CONFORMANCE)

    assert semantic.value == pytest.approx(17 / 18)
    assert semantic.threshold == 1.0
    assert semantic.outcome is MetricOutcome.FAILED
    assert summary.safety_gate_passed is False
    assert summary.quality_gate_passed is False
    assert summary.passed is False


def test_predeclared_quality_threshold_controls_overall_pass() -> None:
    registry = _registry()
    registry = registry.model_copy(
        update={
            "quality_thresholds": registry.quality_thresholds.model_copy(
                update={"first_pass_rate_minimum": 1.0}
            )
        }
    )
    summary = aggregate_measured_evaluation(registry, _receipts(registry))
    first_pass = _metric(summary, MetricId.FIRST_PASS_RATE)

    assert first_pass.value == pytest.approx(2 / 3)
    assert first_pass.threshold == 1.0
    assert first_pass.outcome is MetricOutcome.FAILED
    assert summary.quality_gate_passed is False
    assert summary.passed is False


def test_any_partial_metric_evidence_makes_all_metrics_evaluated_false() -> None:
    registry = _registry()
    receipts = _receipts(registry)
    partial_usage = receipts[0].usage.model_copy(
        update={
            "latency_ms": CountEvidence.unavailable(
                "The provider latency receipt was not retained."
            )
        }
    )
    partial = receipts[0].model_copy(update={"usage": partial_usage})

    summary = aggregate_measured_evaluation(registry, (partial, *receipts[1:]))
    latency = _metric(summary, MetricId.MEAN_LATENCY_MS)

    assert latency.outcome is MetricOutcome.REPORTED
    assert latency.unavailable_cells == 1
    assert summary.all_metrics_evaluated is False
    assert summary.quality_gate_passed is True
    assert summary.passed is False


def test_macro_and_per_case_dependency_recall_expose_hidden_miss() -> None:
    registry = _registry()
    receipts = _receipts(registry)
    missed = receipts[0].model_copy(
        update={
            "detected_dependencies": IdentifierSetEvidence.available(
                (),
                _digest(993),
            )
        }
    )

    summary = aggregate_measured_evaluation(registry, (missed, *receipts[1:]))
    micro = _metric(summary, MetricId.DEPENDENCY_RECALL)
    macro = _metric(summary, MetricId.DEPENDENCY_RECALL_MACRO)
    mule = next(item for item in summary.case_summaries if item.slice_id == "mule-simple")

    assert micro.value == pytest.approx(23 / 24)
    assert micro.outcome is MetricOutcome.PASSED
    assert macro.value == pytest.approx(((5 / 6) + 1 + 1) / 3)
    assert macro.outcome is MetricOutcome.FAILED
    assert mule.dependency_recall.value == pytest.approx(5 / 6)
    assert summary.safety_gate_passed is False
    assert summary.passed is False


def test_paired_wiki_deltas_are_case_stratified_and_use_matched_repetitions() -> None:
    registry = _registry()
    receipts = _receipts(registry)
    wiki_config = next(item for item in registry.configurations if item.uses_wiki)
    index = next(
        index
        for index, receipt in enumerate(receipts)
        if receipt.case_id == "salesforce-medium"
        and receipt.config_id == wiki_config.config_id
        and receipt.repetition == 1
    )
    slower_usage = receipts[index].usage.model_copy(
        update={"latency_ms": CountEvidence.available(1301, _digest(994))}
    )
    slower = receipts[index].model_copy(update={"usage": slower_usage})
    changed = (*receipts[:index], slower, *receipts[index + 1 :])

    summary = aggregate_measured_evaluation(registry, changed)
    medium = next(
        item for item in summary.paired_wiki_deltas if item.case_id == "salesforce-medium"
    )

    assert medium.mean_latency_ms.available_pairs == 3
    assert medium.mean_latency_ms.unavailable_pairs == 0
    assert medium.mean_latency_ms.value == pytest.approx(100.0)
    assert medium.semantic_conformance.value == 0.0
    assert medium.dependency_recall.value == 0.0
