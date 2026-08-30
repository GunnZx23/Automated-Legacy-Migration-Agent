"""Derive benchmark-v2 cell receipts from verified durable run evidence.

Callers supply only routing information.  Case, arm, repetition, provider,
outcome, attempts, model usage, tool usage, dependency observations, and model
intervention are derived from the execution-anchored run rather than accepted
as editable result fields.  Human judgment remains a separate rubric artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

from legacy_migration_agent.application.run_query import (
    VerifiedBenchmarkRunBundle,
    load_verified_benchmark_run_bundle,
)
from legacy_migration_agent.benchmark_execution import (
    BenchmarkExecutionAnchor,
    load_strict_benchmark_json,
    load_verified_benchmark_execution_anchor,
)
from legacy_migration_agent.benchmark_protocol import VerifiedBenchmarkProtocol
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.measured_evaluation import (
    BooleanEvidence,
    CountEvidence,
    EvaluationCellReceipt,
    EvidenceAvailability,
    HumanReviewRubric,
    IdentifierSetEvidence,
    UsageEvidence,
    WorkflowDisposition,
)


def extract_evaluation_cell_receipt(
    project_root: Path,
    run_dir: Path,
    rubric_path: Path,
    execution_anchor_path: Path,
    *,
    run_id: str,
    thread_id: str,
) -> EvaluationCellReceipt:
    """Extract one immutable cell receipt without trusting editable result metadata."""

    bundle = load_verified_benchmark_run_bundle(
        project_root,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
    )
    binding = bundle.benchmark_binding
    runtime_identity = binding.runtime_identity_digest
    anchor_digest = binding.execution_anchor_digest
    if runtime_identity is None or anchor_digest is None:
        raise PolicyViolation("benchmark receipt requires a pre-run execution anchor")

    anchor = load_verified_benchmark_execution_anchor(
        project_root,
        execution_anchor_path,
        expected_runtime_identity_digest=runtime_identity,
    )
    if anchor.anchor_digest != anchor_digest:
        raise PolicyViolation("benchmark receipt execution anchor differs from the run")

    from legacy_migration_agent.benchmark_protocol import load_verified_benchmark_protocol

    protocol = load_verified_benchmark_protocol(project_root)
    case = next(
        (item for item in protocol.registry.cases if item.case_id == bundle.case_id),
        None,
    )
    configuration = next(
        (item for item in protocol.registry.configurations if item.config_id == bundle.config_id),
        None,
    )
    if case is None or configuration is None:
        raise PolicyViolation("benchmark receipt selects an unknown case or configuration")
    if (
        binding.benchmark_registry_digest != protocol.registry_digest
        or binding.benchmark_definition_digest != protocol.declaration_digest
        or binding.configuration_digest != artifact_digest(configuration)
        or anchor.benchmark_registry_digest != protocol.registry_digest
        or anchor.protocol_declaration_digest != protocol.declaration_digest
    ):
        raise PolicyViolation("benchmark receipt protocol binding drifted")

    rubric = _load_bound_human_rubric(
        rubric_path,
        bundle=bundle,
        protocol=protocol,
        anchor=anchor,
    )
    if (
        not configuration.uses_wiki
        and rubric.wiki_support.availability is not EvidenceAvailability.UNAVAILABLE
    ):
        raise PolicyViolation("no-Wiki benchmark rubric cannot claim Wiki support evidence")
    if (
        configuration.uses_wiki
        and rubric.wiki_support.availability is EvidenceAvailability.AVAILABLE
        and rubric.wiki_support.denominator == 0
    ):
        raise PolicyViolation("Wiki benchmark rubric requires reviewed Wiki evidence")
    disposition = _workflow_disposition(bundle)
    authorization = _authorization_evidence(bundle)
    dependencies = _dependency_evidence(bundle, protocol)
    intervention = _intervention_evidence(bundle)
    intervention_reasons = _intervention_reason_evidence(bundle)
    unavailable_commands = _unavailable_required_command_evidence(bundle)
    usage = _usage_evidence(bundle)
    return EvaluationCellReceipt(
        receipt_id=f"{bundle.benchmark_binding.cell_id}:receipt",
        registry_id=protocol.registry.registry_id,
        registry_digest=protocol.registry_digest,
        cell_id=binding.cell_id,
        case_id=bundle.case_id,
        case_label_digest=artifact_digest(case),
        config_id=bundle.config_id,
        configuration_digest=artifact_digest(configuration),
        repetition=bundle.repetition,
        run_id=bundle.run_evidence.run_id,
        provider_id=configuration.provider_id,
        model_id=configuration.model_id,
        execution_boundary="remote_provider_managed",
        runtime_identity_digest=runtime_identity,
        execution_anchor_digest=anchor_digest,
        run_evidence=bundle.run_evidence,
        run_evidence_digest=bundle.run_evidence.run_evidence_digest,
        workflow_disposition=disposition,
        validation_attempts=len(bundle.attempts),
        authorization_violations=authorization,
        detected_dependencies=dependencies,
        intervention_triggered=intervention,
        intervention_reason_ids=intervention_reasons,
        unavailable_required_command_ids=unavailable_commands,
        usage=usage,
        human_rubric=rubric,
        human_rubric_digest=artifact_digest(rubric),
    )


def _load_bound_human_rubric(
    path: Path,
    *,
    bundle: VerifiedBenchmarkRunBundle,
    protocol: VerifiedBenchmarkProtocol,
    anchor: BenchmarkExecutionAnchor,
) -> HumanReviewRubric:
    try:
        payload = load_strict_benchmark_json(path)
        rubric = HumanReviewRubric.model_validate_json(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
    except PolicyViolation:
        raise
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("benchmark human-review rubric is malformed") from exc
    expected = (
        protocol.registry.registry_id,
        protocol.registry_digest,
        bundle.benchmark_binding.cell_id,
        bundle.case_id,
        bundle.config_id,
        bundle.repetition,
        bundle.run_evidence.run_id,
        bundle.run_evidence.run_evidence_digest,
        anchor.runtime_identity_digest,
        anchor.anchor_digest,
    )
    actual = (
        rubric.registry_id,
        rubric.registry_digest,
        rubric.cell_id,
        rubric.case_id,
        rubric.config_id,
        rubric.repetition,
        rubric.run_id,
        rubric.run_evidence_digest,
        rubric.runtime_identity_digest,
        rubric.execution_anchor_digest,
    )
    if actual != expected:
        raise PolicyViolation("benchmark human-review rubric is cross-bound")
    return rubric


def _workflow_disposition(bundle: VerifiedBenchmarkRunBundle) -> WorkflowDisposition:
    status = bundle.run_evidence.status
    if status.status in {"failed", "modification_requested", "rejected"}:
        return WorkflowDisposition.CONTROLLED_FAILURE
    if status.terminal_disposition is None:
        raise PolicyViolation("benchmark terminal run has no measurable disposition")
    try:
        return WorkflowDisposition(status.terminal_disposition)
    except ValueError as exc:
        raise PolicyViolation("benchmark terminal disposition is not measurable") from exc


def _authorization_evidence(bundle: VerifiedBenchmarkRunBundle) -> CountEvidence:
    failure = bundle.run_evidence.status.failure
    violations = int(failure is not None and failure.reason_code == "unauthorized_tool_call")
    return CountEvidence.available(
        violations,
        artifact_digest(
            {
                "run_evidence_digest": bundle.run_evidence.run_evidence_digest,
                "failure_reason": None if failure is None else failure.reason_code,
                "verified_tool_receipts": tuple(
                    receipt.receipt_id for receipt in bundle.tool_receipts
                ),
            }
        ),
    )


def _dependency_evidence(
    bundle: VerifiedBenchmarkRunBundle,
    protocol: VerifiedBenchmarkProtocol,
) -> IdentifierSetEvidence:
    planning = bundle.planning
    if planning is None:
        return IdentifierSetEvidence.unavailable(
            bundle.partial_evidence_unavailable_reason
            or "verified Architect dependency evidence is unavailable"
        )
    graph = planning.dependency_graph
    node_names = {node.node_id: node.name for node in graph.nodes}
    observed_edges = {
        (
            edge.kind.value,
            node_names[edge.source_id],
            node_names[edge.target_id],
            edge.symbol,
        )
        for edge in graph.edges
        if edge.resolved and edge.symbol is not None
    }
    labels = protocol.dependency_labels_for_case(bundle.case_id)
    detected = tuple(
        sorted(label.dependency_id for label in labels if label.edge_key in observed_edges)
    )
    return IdentifierSetEvidence.available(
        detected,
        artifact_digest(
            {
                "dependency_graph": graph.model_dump(mode="json"),
                "case_id": bundle.case_id,
                "detected_dependency_ids": detected,
            }
        ),
    )


def _intervention_evidence(bundle: VerifiedBenchmarkRunBundle) -> BooleanEvidence:
    planning = bundle.planning
    if planning is None:
        return BooleanEvidence.unavailable(
            bundle.partial_evidence_unavailable_reason
            or "verified Architect intervention evidence is unavailable"
        )
    binding = bundle.benchmark_binding
    if binding.risk_seed_binding is not None:
        evaluation = planning.architect.proposal.expansion_receipt.benchmark_risk_evaluation
        if evaluation is None:
            raise PolicyViolation("seeded benchmark plan lacks its controller risk evaluation")
        return BooleanEvidence.available(
            evaluation.model_intervened,
            artifact_digest(evaluation),
        )
    risks = planning.architect.agent_output.risk_observations
    return BooleanEvidence.available(
        any(item.requires_human_decision for item in risks),
        artifact_digest(tuple(item.model_dump(mode="json") for item in risks)),
    )


def _intervention_reason_evidence(
    bundle: VerifiedBenchmarkRunBundle,
) -> IdentifierSetEvidence:
    """Bind reason-level intervention evidence to the verified Architect result."""

    planning = bundle.planning
    if planning is None:
        return IdentifierSetEvidence.unavailable(
            bundle.partial_evidence_unavailable_reason
            or "verified Architect intervention-reason evidence is unavailable"
        )
    binding = bundle.benchmark_binding
    if binding.risk_seed_binding is not None:
        evaluation = planning.architect.proposal.expansion_receipt.benchmark_risk_evaluation
        if evaluation is None:
            raise PolicyViolation("seeded benchmark plan lacks its controller risk evaluation")
        return IdentifierSetEvidence.available(
            tuple(reason.value for reason in evaluation.observed_reasons),
            artifact_digest(evaluation),
        )
    risks = planning.architect.agent_output.risk_observations
    observed = tuple(
        dict.fromkeys(risk.hazard_reason.value for risk in risks if risk.hazard_reason is not None)
    )
    return IdentifierSetEvidence.available(
        observed,
        artifact_digest(
            {
                "planning_evidence_digest": planning.planning_evidence_digest,
                "risk_seed_binding": None,
                "risk_observations": tuple(risk.model_dump(mode="json") for risk in risks),
                "observed_intervention_reason_ids": observed,
            }
        ),
    )


def _unavailable_required_command_evidence(
    bundle: VerifiedBenchmarkRunBundle,
) -> IdentifierSetEvidence:
    """Derive runtime unavailability from the final verified report, never a case oracle."""

    if not bundle.attempts:
        return IdentifierSetEvidence.unavailable(
            bundle.partial_evidence_unavailable_reason
            or "no verified validation report exists for this planning-only cell"
        )
    report = bundle.attempts[-1].report
    command_ids = tuple(
        sorted(
            check.command_id
            for check in report.results
            if check.required and check.status.value == "unavailable"
        )
    )
    return IdentifierSetEvidence.available(command_ids, artifact_digest(report))


def _usage_evidence(bundle: VerifiedBenchmarkRunBundle) -> UsageEvidence:
    if not bundle.model_call_evidence_complete:
        reason = (
            bundle.model_call_evidence_unavailable_reason
            or "completed model-call evidence is unavailable"
        )
        latency = CountEvidence.unavailable(reason)
        input_tokens = CountEvidence.unavailable(reason)
        output_tokens = CountEvidence.unavailable(reason)
        model_calls = CountEvidence.unavailable(reason)
    else:
        usages = tuple(call.usage for call in bundle.model_calls)
        if any(usage is None for usage in usages):
            raise PolicyViolation("verified remote model call lacks measured telemetry")
        measured = tuple(usage for usage in usages if usage is not None)
        usage_digest = artifact_digest(
            tuple(
                {
                    "model_call_digest": artifact_digest(call),
                    "usage": usage.model_dump(mode="json"),
                }
                for call, usage in zip(bundle.model_calls, measured, strict=True)
            )
        )
        latency = CountEvidence.available(
            sum(usage.latency_ms for usage in measured),
            usage_digest,
        )
        if all(usage.provider_usage_reported for usage in measured):
            input_tokens = CountEvidence.available(
                sum(usage.input_tokens or 0 for usage in measured),
                usage_digest,
            )
            output_tokens = CountEvidence.available(
                sum(usage.output_tokens or 0 for usage in measured),
                usage_digest,
            )
        else:
            reason = "the provider did not report complete token telemetry"
            input_tokens = CountEvidence.unavailable(reason)
            output_tokens = CountEvidence.unavailable(reason)
        model_calls = CountEvidence.available(len(bundle.model_calls), usage_digest)

    if bundle.tool_receipt_evidence_complete:
        tool_digest = artifact_digest(
            tuple(artifact_digest(receipt) for receipt in bundle.tool_receipts)
        )
        tool_calls = CountEvidence.available(len(bundle.tool_receipts), tool_digest)
    else:
        tool_calls = CountEvidence.unavailable(
            bundle.tool_receipt_evidence_unavailable_reason
            or "validation tool-call evidence is unavailable"
        )
    return UsageEvidence(
        latency_ms=latency,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_micro_usd=CountEvidence.unavailable(
            "Claude CLI does not persist authoritative per-call cost evidence."
        ),
        model_calls=model_calls,
        tool_calls=tool_calls,
    )


__all__ = ["extract_evaluation_cell_receipt"]
