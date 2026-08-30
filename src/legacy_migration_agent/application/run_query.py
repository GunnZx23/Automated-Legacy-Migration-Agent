"""Read-only, verified projections of persisted model-run evidence.

Writers own the artifact layout.  Readers should consume this module instead
of reconstructing that layout or independently deciding which handoffs bind a
completed attempt.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from legacy_migration_agent.agent_runtime.agent_definitions import (
    AgentDefinition,
    AgentRole,
    load_agent_registry,
)
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectContext,
    ArchitectRun,
    EngineerRun,
    ValidatorAssessment,
    ValidatorEvidenceContext,
    ValidatorModelAdvisory,
    expand_architect_proposal,
    validate_architect_proposal,
)
from legacy_migration_agent.agent_runtime.model_workflow import (
    RejectedArchitectModelCallReceipt,
    RoleInvocationLease,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    ModelCallRecord,
    ModelEvidenceError,
    verify_model_call_record,
    verify_model_call_record_input,
)
from legacy_migration_agent.agent_runtime.run_artifact_paths import RunArtifactPaths
from legacy_migration_agent.application.agent_run_contracts import (
    AGENT_RUN_CONFIG_PATH,
    AgentRunConfig,
    VerifiedAgentRunEvidence,
)
from legacy_migration_agent.contracts import (
    ChangeSet,
    ManifestStatus,
    MigrationManifest,
    MigrationRequest,
    Sha256Digest,
    ToolReceipt,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import (
    PolicyViolation,
    validate_change_set,
    validate_manifest_for_request,
    validate_report,
)
from legacy_migration_agent.core.run_session import AgentRunSession
from legacy_migration_agent.graphs.dependency_graph import DependencyGraph
from legacy_migration_agent.graphs.graph_assurance import GraphAssuranceReport
from legacy_migration_agent.knowledge.wiki import BenchmarkKnowledgeBinding, RetrievalTrace


@dataclass(frozen=True, slots=True)
class VerifiedRunSnapshot:
    """One fully persisted attempt after all cross-artifact bindings pass."""

    request: MigrationRequest
    architect_context: ArchitectContext
    dependency_graph: DependencyGraph
    graph_assurance_report: GraphAssuranceReport | None
    wiki_trace: RetrievalTrace
    architect: ArchitectRun
    engineer: EngineerRun
    report: ValidationReport
    validator: ValidatorAssessment

    @property
    def manifest(self) -> MigrationManifest:
        return self.architect.proposal.manifest

    @property
    def change_set(self) -> ChangeSet:
        change_set = self.engineer.change_set
        if change_set is None:  # The loader proves this invariant.
            raise AssertionError("verified run snapshot lacks an Engineer change set")
        return change_set

    @property
    def architect_context_digest(self) -> Sha256Digest:
        return artifact_digest(self.architect_context)

    @property
    def dependency_graph_digest(self) -> Sha256Digest:
        return artifact_digest(self.dependency_graph)

    @property
    def graph_assurance_report_digest(self) -> Sha256Digest | None:
        if self.graph_assurance_report is None:
            return None
        return artifact_digest(self.graph_assurance_report)

    @property
    def wiki_trace_digest(self) -> Sha256Digest:
        return artifact_digest(self.wiki_trace)

    @property
    def scope_policy_digest(self) -> Sha256Digest:
        return self.architect_context.platform_adapter.scope_policy_digest

    @property
    def architect_run_digest(self) -> Sha256Digest:
        return artifact_digest(self.architect)

    @property
    def engineer_run_digest(self) -> Sha256Digest:
        return artifact_digest(self.engineer)

    @property
    def validator_assessment_digest(self) -> Sha256Digest:
        return artifact_digest(self.validator)


@dataclass(frozen=True, slots=True)
class VerifiedPlanningSnapshot:
    """Verified Architect inputs and output, including planning-only terminals."""

    request: MigrationRequest
    architect_context: ArchitectContext
    dependency_graph: DependencyGraph
    graph_assurance_report: GraphAssuranceReport | None
    wiki_trace: RetrievalTrace
    architect: ArchitectRun

    @property
    def manifest(self) -> MigrationManifest:
        """Return the controller-expanded manifest bound to this plan."""

        return self.architect.proposal.manifest

    @property
    def graph_assurance_report_digest(self) -> Sha256Digest | None:
        if self.graph_assurance_report is None:
            return None
        return artifact_digest(self.graph_assurance_report)

    @property
    def planning_evidence_digest(self) -> Sha256Digest:
        return artifact_digest(
            {
                "request": self.request.model_dump(mode="json"),
                "architect_context": self.architect_context.model_dump(mode="json"),
                "dependency_graph": self.dependency_graph.model_dump(mode="json"),
                "wiki_trace": self.wiki_trace.model_dump(mode="json"),
                "architect": self.architect.model_dump(mode="json"),
            }
        )


@dataclass(frozen=True, slots=True)
class VerifiedBenchmarkRunBundle:
    """One terminal benchmark run with its verified, receipt-safe evidence.

    Only :func:`load_verified_benchmark_run_bundle` constructs this projection.
    ``model_calls`` contains completed schema-valid calls, including an Architect
    response rejected by controller policy. When a controlled failure leaves an
    invocation or attempt incomplete, the corresponding completeness flag and
    reason prevent downstream code from treating the partial inventory as a
    measured zero.
    """

    request: MigrationRequest
    config: AgentRunConfig
    benchmark_binding: BenchmarkKnowledgeBinding
    case_id: str
    config_id: str
    repetition: int
    run_evidence: VerifiedAgentRunEvidence
    planning: VerifiedPlanningSnapshot | None
    attempts: tuple[VerifiedRunSnapshot, ...]
    model_calls: tuple[ModelCallRecord, ...]
    tool_receipts: tuple[ToolReceipt, ...]
    runtime_identity_digest: Sha256Digest | None
    model_call_evidence_complete: bool
    model_call_evidence_unavailable_reason: str | None
    tool_receipt_evidence_complete: bool
    tool_receipt_evidence_unavailable_reason: str | None
    partial_evidence_unavailable: bool
    partial_evidence_unavailable_reason: str | None

    @property
    def completed_attempts(self) -> tuple[int, ...]:
        """Return attempts derived from verified report artifacts, never status."""

        return tuple(snapshot.report.attempt for snapshot in self.attempts)


def load_verified_run_snapshot(
    session: AgentRunSession,
    *,
    attempt: int,
) -> VerifiedRunSnapshot:
    """Load and verify the canonical evidence for one completed model attempt."""

    return load_verified_run_snapshots(session, attempts=(attempt,))[0]


def load_verified_run_snapshots(
    session: AgentRunSession,
    *,
    attempts: tuple[int, ...],
) -> tuple[VerifiedRunSnapshot, ...]:
    """Load shared planning evidence once and verify each requested attempt."""

    return _load_verified_run_snapshots(
        session,
        attempts=attempts,
        require_benchmark_replay=False,
    )


def _load_verified_run_snapshots(
    session: AgentRunSession,
    *,
    attempts: tuple[int, ...],
    require_benchmark_replay: bool,
) -> tuple[VerifiedRunSnapshot, ...]:
    if not attempts or len(attempts) != len(set(attempts)):
        raise ValueError("run snapshot attempts must be nonempty and unique")
    common = _load_verified_planning_snapshot(
        session,
        require_benchmark_replay=require_benchmark_replay,
    )
    return tuple(
        _load_verified_attempt(
            session,
            common,
            attempt,
            require_benchmark_replay=require_benchmark_replay,
        )
        for attempt in attempts
    )


def load_verified_planning_snapshot(session: AgentRunSession) -> VerifiedPlanningSnapshot:
    """Load immutable request and Architect evidence shared by every attempt."""

    return _load_verified_planning_snapshot(
        session,
        require_benchmark_replay=False,
    )


def _load_verified_planning_snapshot(
    session: AgentRunSession,
    *,
    require_benchmark_replay: bool,
) -> VerifiedPlanningSnapshot:
    """Load planning evidence, optionally enforcing benchmark replay identity."""

    try:
        request = MigrationRequest.model_validate(session.store.read_json("request.json"))
    except FileNotFoundError as exc:
        raise PolicyViolation("run snapshot lacks its canonical request") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("run snapshot request is malformed") from exc

    if artifact_digest(request) != session.context.request_digest:
        raise PolicyViolation("run snapshot request does not match the run session")
    if request.base_revision != session.context.source_revision:
        raise PolicyViolation("run snapshot source revision does not match the run session")
    session.verify_source_revision()

    paths = RunArtifactPaths(request.request_id)
    try:
        context = ArchitectContext.model_validate(session.store.read_json(paths.architect_context))
        graph = DependencyGraph.model_validate(session.store.read_json(paths.dependency_graph))
        wiki_trace = RetrievalTrace.model_validate(session.store.read_json(paths.wiki_trace))
        architect = ArchitectRun.model_validate(session.store.read_json(paths.architect))
    except FileNotFoundError as exc:
        raise PolicyViolation("run snapshot requires persisted Architect inputs") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("persisted run snapshot evidence is malformed") from exc

    if context.request != request:
        raise PolicyViolation("run snapshot Architect context differs from the request")
    if context.dependency_graph != graph:
        raise PolicyViolation("run snapshot dependency graph differs from Architect context")
    graph_assurance_report = _load_bound_graph_assurance_report(session, paths, context)
    if context.wiki_trace != wiki_trace:
        raise PolicyViolation("run snapshot Wiki trace differs from Architect context")
    validate_architect_proposal(architect.proposal, context, architect.agent_output)
    if architect.proposal != expand_architect_proposal(architect.agent_output, context):
        raise PolicyViolation("run snapshot Architect expansion differs from controller policy")

    if require_benchmark_replay:
        registry = load_agent_registry(session.project_root / "agents")
        architect_definition = registry.get(AgentRole.ARCHITECT)
        _verify_complete_model_call(
            architect.model_call,
            definition=architect_definition,
            session=session,
            input_value=context.model_context,
            output_value=architect.agent_output,
            role="Architect",
        )

    if architect.proposal.manifest.status is not ManifestStatus.DECISION_REQUIRED:
        validate_manifest_for_request(architect.proposal.manifest, request)
    for evidence in (
        request,
        context,
        graph,
        *((graph_assurance_report,) if graph_assurance_report is not None else ()),
        wiki_trace,
        architect,
    ):
        session.validate_portable_evidence(evidence)
    return VerifiedPlanningSnapshot(
        request=request,
        architect_context=context,
        dependency_graph=graph,
        graph_assurance_report=graph_assurance_report,
        wiki_trace=wiki_trace,
        architect=architect,
    )


def _load_verified_attempt(
    session: AgentRunSession,
    common: VerifiedPlanningSnapshot,
    attempt: int,
    *,
    require_benchmark_replay: bool,
) -> VerifiedRunSnapshot:
    """Load and bind the Engineer, report, and Validator for one attempt."""

    paths = RunArtifactPaths(common.request.request_id)
    try:
        engineer = EngineerRun.model_validate(session.store.read_json(paths.engineer(attempt)))
        report = ValidationReport.model_validate(session.store.read_json(paths.report(attempt)))
        validator = ValidatorAssessment.model_validate(
            session.store.read_json(paths.validator(attempt))
        )
    except FileNotFoundError as exc:
        raise PolicyViolation(
            "run snapshot requires persisted Engineer, report, and Validator artifacts"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("persisted run snapshot attempt evidence is malformed") from exc

    request = common.request
    context = common.architect_context
    graph = common.dependency_graph
    graph_assurance_report = common.graph_assurance_report
    wiki_trace = common.wiki_trace
    architect = common.architect
    manifest = common.architect.proposal.manifest
    change_set = engineer.change_set
    if change_set is None:
        raise PolicyViolation("run snapshot has no Engineer change set")
    if report.attempt != attempt:
        raise PolicyViolation("run snapshot report belongs to another attempt")
    validate_change_set(change_set, manifest)
    validate_report(report, manifest, change_set)

    validator_context = ValidatorEvidenceContext.freeze(manifest, change_set, report)
    advisory = validator.advisory
    if (
        advisory.manifest_digest != artifact_digest(manifest)
        or advisory.change_set_digest != artifact_digest(change_set)
        or advisory.report_digest != artifact_digest(report)
        or validator.authoritative_disposition is not report.disposition
        or validator.all_required_checks_terminal_and_passed
        is not _all_required_checks_terminal_and_passed(report)
    ):
        raise PolicyViolation("run snapshot Validator evidence does not bind the exact report")

    if require_benchmark_replay:
        registry = load_agent_registry(session.project_root / "agents")
        _verify_role_model_call_with_lease(
            session,
            common,
            attempt=attempt,
            role="engineer",
            call=engineer.model_call,
            definition=registry.get(AgentRole.ENGINEER),
            output_value=engineer.model_outcome,
            change_set=change_set,
            report=report,
        )
        if validator.model_call is not None:
            try:
                model_advisory = ValidatorModelAdvisory.model_validate(
                    validator.advisory.model_dump(mode="python")
                )
            except (TypeError, ValueError) as exc:
                raise PolicyViolation(
                    "run snapshot completed Validator advisory is outside the model schema"
                ) from exc
            _verify_complete_model_call(
                validator.model_call,
                definition=registry.get(AgentRole.VALIDATOR),
                session=session,
                input_value=validator_context,
                output_value=model_advisory,
                role="Validator",
            )
            _verify_role_model_call_with_lease(
                session,
                common,
                attempt=attempt,
                role="validator",
                call=validator.model_call,
                definition=registry.get(AgentRole.VALIDATOR),
                output_value=model_advisory,
                change_set=change_set,
                report=report,
            )
        elif validator.unavailable_receipt is None:
            raise PolicyViolation("run snapshot Validator advisory availability is unproven")
        elif validator.unavailable_receipt.attempted:
            _verify_role_invocation_lease(
                session,
                common,
                attempt=attempt,
                role="validator",
                definition=registry.get(AgentRole.VALIDATOR),
                manifest=manifest,
                change_set=change_set,
                report=report,
                expected_input_digest=artifact_digest(validator_context),
            )
        elif _artifact_exists(session, paths.validator_invocation_lease(attempt)):
            raise PolicyViolation("unattempted Validator advisory contains an invocation lease")
        _verify_tool_receipt_bindings(session, change_set, report)
    else:
        _verify_standard_snapshot_model_bindings(
            session,
            context=context,
            architect=architect,
            engineer=engineer,
            validator=validator,
            validator_context=validator_context,
        )

    for evidence in (engineer, report, validator):
        session.validate_portable_evidence(evidence)
    return VerifiedRunSnapshot(
        request=request,
        architect_context=context,
        dependency_graph=graph,
        graph_assurance_report=graph_assurance_report,
        wiki_trace=wiki_trace,
        architect=architect,
        engineer=engineer,
        report=report,
        validator=validator,
    )


def load_verified_benchmark_run_bundle(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    thread_id: str,
) -> VerifiedBenchmarkRunBundle:
    """Load one terminal benchmark run without invoking a provider.

    Cell identity, configuration, repetition, outcome, attempts, calls, and tool
    receipts are derived from runtime-anchored evidence.  Caller-supplied routing
    identifiers select a run but never supply benchmark or metric values.
    """

    # Import lazily: the orchestration facade may use this query module through
    # final-review code, while the public terminal reader itself is provider-free.
    from legacy_migration_agent.application.agent_run import get_verified_agent_run_evidence
    from legacy_migration_agent.benchmark_protocol import load_verified_benchmark_protocol
    from legacy_migration_agent.evaluation_runner import bind_benchmark_knowledge_arm
    from legacy_migration_agent.measured_evaluation import canonical_cell_id

    terminal = get_verified_agent_run_evidence(
        project_root,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
    )
    session = AgentRunSession.load(project_root, run_dir)
    if session.context.run_id != run_id or session.context.thread_id != thread_id:
        raise PolicyViolation("benchmark bundle routing differs from the run session")
    if artifact_digest(session.context) != terminal.run_context_digest:
        raise PolicyViolation("benchmark bundle session differs from terminal evidence")

    request = _load_bound_request(session, terminal)
    config = _load_bound_config(session, terminal)
    binding = config.benchmark_knowledge_binding
    if binding is None:
        raise PolicyViolation("benchmark bundle requires a benchmark knowledge binding")
    if artifact_digest(binding) != terminal.benchmark_binding_digest:
        raise PolicyViolation("benchmark bundle binding differs from terminal evidence")

    canonical_binding = bind_benchmark_knowledge_arm(
        project_root,
        request,
        cell_id=binding.cell_id,
        case_id=binding.case_id,
        scenario_id=binding.scenario_id,
        knowledge_arm=binding.knowledge_arm,
    )
    execution_fields = {"execution_anchor_digest", "runtime_identity_digest"}
    if canonical_binding.model_dump(mode="python", exclude=execution_fields) != (
        binding.model_dump(mode="python", exclude=execution_fields)
    ):
        raise PolicyViolation("benchmark bundle binding is stale, cross-bound, or tampered")
    if not binding.execution_anchored:
        raise PolicyViolation("benchmark bundle requires a pre-run execution anchor")

    protocol = load_verified_benchmark_protocol(project_root)
    matching_configs = tuple(
        candidate
        for candidate in protocol.registry.configurations
        if artifact_digest(candidate) == binding.configuration_digest
        and candidate.provider_id == binding.provider_id
        and candidate.model_id == binding.model_id
        and candidate.uses_wiki == (binding.knowledge_arm == "full_agent_wiki")
    )
    if len(matching_configs) != 1:
        raise PolicyViolation("benchmark bundle configuration identity is ambiguous")
    benchmark_config = matching_configs[0]
    repetitions = tuple(
        repetition
        for repetition in range(1, protocol.registry.repetitions + 1)
        if canonical_cell_id(binding.case_id, benchmark_config.config_id, repetition)
        == binding.cell_id
    )
    if len(repetitions) != 1:
        raise PolicyViolation("benchmark bundle cell identity is noncanonical")

    controlled_failure = terminal.status.status in {
        "failed",
        "modification_requested",
        "rejected",
    }
    planning, architect_rejection, planning_reason = _load_bundle_planning(
        session,
        request,
        terminal,
        controlled_failure=controlled_failure,
    )
    attempts, attempt_reason = _load_bundle_attempts(
        session,
        request,
        terminal,
        controlled_failure=controlled_failure,
    )
    reasons = tuple(reason for reason in (planning_reason, attempt_reason) if reason is not None)
    partial_reason = "; ".join(reasons) if reasons else None

    model_calls, model_complete, model_reason, runtime_identity = _bundle_model_calls(
        planning,
        architect_rejection,
        attempts,
        partial_reason=partial_reason,
    )
    if runtime_identity is not None and runtime_identity != binding.runtime_identity_digest:
        raise PolicyViolation("benchmark model calls differ from the bound runtime identity")
    tool_receipts = _bundle_tool_receipts(attempts)

    # Re-read the independently anchored terminal projection after collecting
    # artifacts.  This rejects a lifecycle that changed during bundle loading.
    terminal_after = get_verified_agent_run_evidence(
        project_root,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
    )
    if terminal_after != terminal:
        raise PolicyViolation("benchmark terminal evidence changed while loading")

    return VerifiedBenchmarkRunBundle(
        request=request,
        config=config,
        benchmark_binding=binding,
        case_id=binding.case_id,
        config_id=benchmark_config.config_id,
        repetition=repetitions[0],
        run_evidence=terminal,
        planning=planning,
        attempts=attempts,
        model_calls=model_calls,
        tool_receipts=tool_receipts,
        runtime_identity_digest=runtime_identity,
        model_call_evidence_complete=model_complete,
        model_call_evidence_unavailable_reason=model_reason,
        tool_receipt_evidence_complete=partial_reason is None,
        tool_receipt_evidence_unavailable_reason=partial_reason,
        partial_evidence_unavailable=partial_reason is not None,
        partial_evidence_unavailable_reason=partial_reason,
    )


def _load_bound_request(
    session: AgentRunSession,
    terminal: VerifiedAgentRunEvidence,
) -> MigrationRequest:
    try:
        request = MigrationRequest.model_validate(session.store.read_json("request.json"))
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation("benchmark bundle request is missing or malformed") from exc
    if (
        artifact_digest(request) != terminal.request_digest
        or terminal.request_digest != session.context.request_digest
        or request.base_revision != session.context.source_revision
    ):
        raise PolicyViolation("benchmark bundle request differs from terminal evidence")
    return request


def _load_bound_config(
    session: AgentRunSession,
    terminal: VerifiedAgentRunEvidence,
) -> AgentRunConfig:
    try:
        config = AgentRunConfig.model_validate(session.store.read_json(AGENT_RUN_CONFIG_PATH))
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation("benchmark bundle configuration is missing or malformed") from exc
    if artifact_digest(config) != terminal.config_digest:
        raise PolicyViolation("benchmark bundle configuration differs from terminal evidence")
    return config


def _load_bundle_planning(
    session: AgentRunSession,
    request: MigrationRequest,
    terminal: VerifiedAgentRunEvidence,
    *,
    controlled_failure: bool,
) -> tuple[
    VerifiedPlanningSnapshot | None,
    RejectedArchitectModelCallReceipt | None,
    str | None,
]:
    paths = RunArtifactPaths(request.request_id)
    input_paths = (
        paths.architect_context,
        paths.dependency_graph,
        paths.wiki_trace,
    )
    inputs_present = tuple(_artifact_exists(session, path) for path in input_paths)
    assurance_present = _artifact_exists(session, paths.graph_assurance_report)
    architect_present = _artifact_exists(session, paths.architect)
    rejection_present = _artifact_exists(session, paths.architect_policy_rejection)
    if architect_present and rejection_present:
        raise PolicyViolation("benchmark run contains conflicting Architect outcomes")
    if architect_present and all(inputs_present):
        planning = _load_verified_planning_snapshot(
            session,
            require_benchmark_replay=True,
        )
        if terminal.status.status == "decision_required" and (
            planning.manifest.status is not ManifestStatus.DECISION_REQUIRED
        ):
            raise PolicyViolation("decision-required lifecycle has a non-decision plan")
        return planning, None, None
    if rejection_present:
        if not controlled_failure or not all(inputs_present) or architect_present:
            raise PolicyViolation("benchmark Architect rejection evidence is incomplete")
        context = _load_rejected_architect_context(session, request)
        try:
            receipt = RejectedArchitectModelCallReceipt.model_validate(
                session.store.read_json(paths.architect_policy_rejection)
            )
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise PolicyViolation("benchmark Architect rejection receipt is malformed") from exc
        _verify_rejected_architect_receipt(
            session,
            terminal,
            request,
            context,
            receipt,
        )
        return None, receipt, None
    present = (*inputs_present, assurance_present, architect_present)
    if any(present):
        if not controlled_failure:
            raise PolicyViolation("benchmark run contains partial Architect evidence")
        return None, None, "controlled failure left partial Architect evidence"
    if terminal.status.status == "decision_required" or not controlled_failure:
        raise PolicyViolation("benchmark terminal run lacks verified Architect evidence")
    return None, None, "controlled failure has no completed Architect evidence"


def _load_rejected_architect_context(
    session: AgentRunSession,
    request: MigrationRequest,
) -> ArchitectContext:
    """Load the exact input evidence for a controller-rejected Architect call."""

    paths = RunArtifactPaths(request.request_id)
    try:
        context = ArchitectContext.model_validate(session.store.read_json(paths.architect_context))
        graph = DependencyGraph.model_validate(session.store.read_json(paths.dependency_graph))
        wiki_trace = RetrievalTrace.model_validate(session.store.read_json(paths.wiki_trace))
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise PolicyViolation("benchmark Architect rejection inputs are malformed") from exc
    if (
        context.request != request
        or context.dependency_graph != graph
        or context.wiki_trace != wiki_trace
    ):
        raise PolicyViolation("benchmark Architect rejection inputs are cross-bound")
    _load_bound_graph_assurance_report(session, paths, context)
    return context


def _verify_rejected_architect_receipt(
    session: AgentRunSession,
    terminal: VerifiedAgentRunEvidence,
    request: MigrationRequest,
    context: ArchitectContext,
    receipt: RejectedArchitectModelCallReceipt,
) -> None:
    """Bind one rejected call to the exact failure, runtime, prompt, and input."""

    failure = terminal.status.failure
    if (
        failure is None
        or failure.seam != "architect"
        or failure.category != "invalid"
        or failure.attempt != 1
        or failure.reason_code != receipt.reason_code
    ):
        raise PolicyViolation("benchmark Architect rejection differs from terminal failure")
    if receipt.request_id != request.request_id or receipt.request_digest != artifact_digest(
        request
    ):
        raise PolicyViolation("benchmark Architect rejection belongs to another request")
    definition = load_agent_registry(session.project_root / "agents").get(AgentRole.ARCHITECT)
    try:
        verify_model_call_record_input(
            receipt.model_call,
            agent_version=definition.version,
            agent_definition_digest=definition.definition_digest,
            system_prompt=definition.system_prompt,
            input_value=context.model_context,
        )
    except (ModelEvidenceError, TypeError, ValueError) as exc:
        raise PolicyViolation("Architect rejected model-call evidence is not replay-safe") from exc
    _verify_model_call_identity(
        receipt.model_call,
        definition=definition,
        session=session,
        role="Architect",
    )
    assurance_evidence = (
        () if context.graph_assurance_report is None else (context.graph_assurance_report,)
    )
    for evidence in (
        context,
        context.dependency_graph,
        *assurance_evidence,
        context.wiki_trace,
        receipt,
    ):
        session.validate_portable_evidence(evidence)


def _load_bundle_attempts(
    session: AgentRunSession,
    request: MigrationRequest,
    terminal: VerifiedAgentRunEvidence,
    *,
    controlled_failure: bool,
) -> tuple[tuple[VerifiedRunSnapshot, ...], str | None]:
    paths = RunArtifactPaths(request.request_id)
    inventories: dict[int, tuple[bool, bool]] = {}
    for attempt in (1, 2):
        triplet = (
            paths.engineer(attempt),
            paths.report(attempt),
            paths.validator(attempt),
        )
        related = (
            *triplet,
            paths.engineer_invocation_lease(attempt),
            paths.validator_invocation_lease(attempt),
            *((paths.engineer_correction, paths.correction_wiki) if attempt == 2 else ()),
        )
        inventories[attempt] = (
            all(_artifact_exists(session, path) for path in triplet),
            any(_artifact_exists(session, path) for path in related),
        )

    complete_one, any_one = inventories[1]
    complete_two, any_two = inventories[2]
    if any_two and not complete_one:
        raise PolicyViolation("benchmark attempt two exists without a complete attempt one")
    if terminal.status.status == "decision_required" and (any_one or any_two):
        raise PolicyViolation("planning-only benchmark decision contains validation attempts")

    completed = (1, 2) if complete_two else (1,) if complete_one else ()
    partial_attempt = (
        1 if any_one and not complete_one else 2 if any_two and not complete_two else None
    )
    if partial_attempt is not None and not controlled_failure:
        raise PolicyViolation(
            f"benchmark attempt {partial_attempt} contains a partial persisted triplet"
        )
    if not completed and terminal.status.status not in {
        "decision_required",
        "failed",
        "rejected",
        "modification_requested",
    }:
        raise PolicyViolation("report-backed benchmark terminal has no completed attempt")

    snapshots = (
        _load_verified_run_snapshots(
            session,
            attempts=completed,
            require_benchmark_replay=True,
        )
        if completed
        else ()
    )
    reason = (
        f"controlled failure left partial attempt {partial_attempt} evidence"
        if partial_attempt is not None
        else None
    )
    return snapshots, reason


def _bundle_model_calls(
    planning: VerifiedPlanningSnapshot | None,
    architect_rejection: RejectedArchitectModelCallReceipt | None,
    attempts: tuple[VerifiedRunSnapshot, ...],
    *,
    partial_reason: str | None,
) -> tuple[
    tuple[ModelCallRecord, ...],
    bool,
    str | None,
    Sha256Digest | None,
]:
    calls: list[ModelCallRecord] = []
    unavailable: list[str] = []
    if planning is not None:
        calls.append(planning.architect.model_call)
    elif architect_rejection is not None:
        calls.append(architect_rejection.model_call)
    else:
        unavailable.append("Architect call evidence is unavailable")
    for snapshot in attempts:
        calls.append(snapshot.engineer.model_call)
        validator = snapshot.validator
        if validator.model_call is not None:
            calls.append(validator.model_call)
        elif validator.unavailable_receipt is not None and validator.unavailable_receipt.attempted:
            unavailable.append(
                f"Validator attempt {snapshot.report.attempt} has no completed model-call record"
            )
    if partial_reason is not None:
        unavailable.append(partial_reason)

    if calls:
        boundaries = {call.resolved_execution_boundary for call in calls}
        identities = {call.resolved_runtime_identity_digest for call in calls}
        approvals = {artifact_digest(call.live_approval) for call in calls}
        if boundaries != {"remote_provider_managed"}:
            raise PolicyViolation("benchmark model calls use an invalid execution boundary")
        if None in identities or len(identities) != 1:
            raise PolicyViolation("benchmark model calls use inconsistent runtime identities")
        if len(approvals) != 1:
            raise PolicyViolation("benchmark model calls use inconsistent approval evidence")
        runtime_identity = next(iter(identities))
    else:
        runtime_identity = None
    reason = "; ".join(unavailable) if unavailable else None
    return tuple(calls), reason is None, reason, runtime_identity


def _bundle_tool_receipts(
    attempts: tuple[VerifiedRunSnapshot, ...],
) -> tuple[ToolReceipt, ...]:
    by_id: dict[str, ToolReceipt] = {}
    for snapshot in attempts:
        candidates = tuple(
            result.receipt for result in snapshot.report.results if result.receipt is not None
        )
        for receipt in candidates:
            existing = by_id.get(receipt.receipt_id)
            if existing is not None and existing != receipt:
                raise PolicyViolation("benchmark tool receipt ID is reused with different evidence")
            by_id[receipt.receipt_id] = receipt
    return tuple(by_id[receipt_id] for receipt_id in sorted(by_id))


def _verify_complete_model_call(
    call: ModelCallRecord,
    *,
    definition: AgentDefinition,
    session: AgentRunSession,
    input_value: BaseModel,
    output_value: BaseModel,
    role: str,
) -> None:
    try:
        verify_model_call_record(
            call,
            agent_version=definition.version,
            agent_definition_digest=definition.definition_digest,
            system_prompt=definition.system_prompt,
            input_value=input_value,
            output_value=output_value,
        )
    except (ModelEvidenceError, TypeError, ValueError) as exc:
        raise PolicyViolation(f"{role} model-call evidence is not replay-safe") from exc
    _verify_model_call_identity(call, definition=definition, session=session, role=role)


def _verify_standard_snapshot_model_bindings(
    session: AgentRunSession,
    *,
    context: ArchitectContext,
    architect: ArchitectRun,
    engineer: EngineerRun,
    validator: ValidatorAssessment,
    validator_context: ValidatorEvidenceContext,
) -> None:
    """Preserve the stable product/final-review evidence boundary.

    Ordinary run reads bind persisted role evidence to the run's frozen
    provider/model/definition digests. Benchmark extraction adds stricter
    current-definition replay and promoted invocation-lease checks separately.
    """

    calls = [
        (architect.model_call, session.context.agent_definition_digests.architect),
        (engineer.model_call, session.context.agent_definition_digests.engineer),
    ]
    if validator.model_call is not None:
        calls.append((validator.model_call, session.context.agent_definition_digests.validator))
    elif validator.unavailable_receipt is None:
        raise PolicyViolation("run snapshot Validator advisory availability is unproven")
    for call, expected_definition_digest in calls:
        if (
            call.provider != session.context.provider_id
            or call.model_id != session.context.model_id
            or call.agent_definition_digest != expected_definition_digest
        ):
            raise PolicyViolation("run snapshot role identity differs from the run session")
        if call.resolved_execution_boundary == "remote_no_store" and not call.store_false_sent:
            raise PolicyViolation("run snapshot live model evidence lacks storage control")

    expected_model_digests = [
        (architect.model_call.input_digest, artifact_digest(context.model_context)),
        (architect.model_call.output_digest, artifact_digest(architect.agent_output)),
        (engineer.model_call.output_digest, artifact_digest(engineer.model_outcome)),
    ]
    if validator.model_call is not None:
        try:
            model_advisory = ValidatorModelAdvisory.model_validate(
                validator.advisory.model_dump(mode="python")
            )
        except (TypeError, ValueError) as exc:
            raise PolicyViolation(
                "run snapshot completed Validator advisory is outside the model schema"
            ) from exc
        expected_model_digests.extend(
            (
                (validator.model_call.input_digest, artifact_digest(validator_context)),
                (validator.model_call.output_digest, artifact_digest(model_advisory)),
            )
        )
    if any(actual != expected for actual, expected in expected_model_digests):
        raise PolicyViolation("run snapshot model-call evidence differs from persisted handoffs")


def _verify_model_call_identity(
    call: ModelCallRecord,
    *,
    definition: AgentDefinition,
    session: AgentRunSession,
    role: str,
) -> None:
    if (
        call.provider != session.context.provider_id
        or call.model_id != session.context.model_id
        or call.agent_version != definition.version
        or call.agent_definition_digest != definition.definition_digest
        or call.system_prompt_digest != _prompt_digest(definition.system_prompt)
    ):
        raise PolicyViolation(f"{role} model-call identity differs from the run session")


def _verify_role_model_call_with_lease(
    session: AgentRunSession,
    common: VerifiedPlanningSnapshot,
    *,
    attempt: int,
    role: Literal["engineer", "validator"],
    call: ModelCallRecord,
    definition: AgentDefinition,
    output_value: BaseModel,
    change_set: ChangeSet,
    report: ValidationReport,
) -> None:
    _verify_model_call_identity(
        call,
        definition=definition,
        session=session,
        role=role.title(),
    )
    if call.output_digest != artifact_digest(output_value):
        raise PolicyViolation(f"{role.title()} model-call output differs from persisted handoff")
    _verify_role_invocation_lease(
        session,
        common,
        attempt=attempt,
        role=role,
        definition=definition,
        manifest=common.manifest,
        change_set=change_set,
        report=report,
        expected_input_digest=call.input_digest,
    )


def _verify_role_invocation_lease(
    session: AgentRunSession,
    common: VerifiedPlanningSnapshot,
    *,
    attempt: int,
    role: Literal["engineer", "validator"],
    definition: AgentDefinition,
    manifest: MigrationManifest,
    change_set: ChangeSet,
    report: ValidationReport,
    expected_input_digest: Sha256Digest,
) -> RoleInvocationLease:
    path = RunArtifactPaths(common.request.request_id).role_invocation_lease(role, attempt)
    try:
        lease = RoleInvocationLease.model_validate(session.store.read_json(path))
    except FileNotFoundError as exc:
        raise PolicyViolation(f"{role.title()} model call lacks its invocation lease") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation(f"{role.title()} invocation lease is malformed") from exc
    if not session.is_model_operation_artifact_promoted(path):
        raise PolicyViolation(f"{role.title()} invocation lease is not lifecycle-promoted")
    expected = (
        role,
        common.request.request_id,
        artifact_digest(common.request),
        attempt,
        expected_input_digest,
        definition.version,
        definition.definition_digest,
        artifact_digest(manifest),
    )
    actual = (
        lease.binding.role,
        lease.binding.request_id,
        lease.binding.request_digest,
        lease.binding.attempt,
        lease.binding.input_evidence_digest,
        lease.binding.agent_version,
        lease.binding.agent_definition_digest,
        lease.binding.manifest_digest,
    )
    if actual != expected:
        raise PolicyViolation(f"{role.title()} invocation lease differs from exact model input")
    if role == "validator" and (
        lease.binding.change_set_digest != artifact_digest(change_set)
        or lease.binding.report_digest != artifact_digest(report)
    ):
        raise PolicyViolation("Validator invocation lease differs from deterministic evidence")
    return lease


def _verify_tool_receipt_bindings(
    session: AgentRunSession,
    change_set: ChangeSet,
    report: ValidationReport,
) -> None:
    if change_set.receipts:
        raise PolicyViolation(
            "benchmark change-set tool receipts have no controller verification policy"
        )
    receipts = tuple(result.receipt for result in report.results if result.receipt is not None)
    for receipt in receipts:
        if (
            receipt.run_id != session.context.run_id
            or receipt.request_id != report.request_id
            or receipt.base_revision != report.base_revision
            or receipt.attempt != report.attempt
        ):
            raise PolicyViolation("tool receipt differs from the exact benchmark run and attempt")


def _artifact_exists(session: AgentRunSession, path: str) -> bool:
    try:
        session.store.read_json(path)
    except FileNotFoundError:
        return False
    return True


def _load_bound_graph_assurance_report(
    session: AgentRunSession,
    paths: RunArtifactPaths,
    context: ArchitectContext,
) -> GraphAssuranceReport | None:
    """Load the optional report and reject missing, extra, stale, or tampered evidence."""

    if context.graph_assurance_report is None:
        if _artifact_exists(session, paths.graph_assurance_report):
            raise PolicyViolation(
                "run snapshot graph assurance report conflicts with legacy Architect context"
            )
        return None
    try:
        report = GraphAssuranceReport.model_validate(
            session.store.read_json(paths.graph_assurance_report)
        )
    except FileNotFoundError as exc:
        raise PolicyViolation("run snapshot lacks its bound graph assurance report") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("run snapshot graph assurance report is malformed") from exc
    if report != context.graph_assurance_report:
        raise PolicyViolation("run snapshot graph assurance report differs from Architect context")
    return report


def _prompt_digest(value: str) -> Sha256Digest:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _all_required_checks_terminal_and_passed(report: ValidationReport) -> bool:
    required = tuple(result for result in report.results if result.required)
    return bool(required) and all(
        result.status.value == "passed"
        and result.receipt is not None
        and result.receipt.terminal
        and result.receipt.exit_code == 0
        for result in required
    )


__all__ = [
    "VerifiedBenchmarkRunBundle",
    "VerifiedPlanningSnapshot",
    "VerifiedRunSnapshot",
    "load_verified_benchmark_run_bundle",
    "load_verified_planning_snapshot",
    "load_verified_run_snapshot",
    "load_verified_run_snapshots",
]
