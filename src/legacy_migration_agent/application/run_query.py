"""Read-only, verified projections of persisted model-run evidence.

Writers own the artifact layout.  Readers should consume this module instead
of reconstructing that layout or independently deciding which handoffs bind a
completed attempt.
"""

from __future__ import annotations

from dataclasses import dataclass

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
from legacy_migration_agent.agent_runtime.run_artifact_paths import RunArtifactPaths
from legacy_migration_agent.contracts import (
    ChangeSet,
    MigrationManifest,
    MigrationRequest,
    Sha256Digest,
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
from legacy_migration_agent.knowledge.wiki import RetrievalTrace


@dataclass(frozen=True, slots=True)
class VerifiedRunSnapshot:
    """One fully persisted attempt after all cross-artifact bindings pass."""

    request: MigrationRequest
    architect_context: ArchitectContext
    dependency_graph: DependencyGraph
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
class _VerifiedRunCommon:
    request: MigrationRequest
    architect_context: ArchitectContext
    dependency_graph: DependencyGraph
    wiki_trace: RetrievalTrace
    architect: ArchitectRun


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

    if not attempts or len(attempts) != len(set(attempts)):
        raise ValueError("run snapshot attempts must be nonempty and unique")
    common = _load_verified_common(session)
    return tuple(_load_verified_attempt(session, common, attempt) for attempt in attempts)


def _load_verified_common(session: AgentRunSession) -> _VerifiedRunCommon:
    """Load immutable request and Architect evidence shared by every attempt."""

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
    if context.wiki_trace != wiki_trace:
        raise PolicyViolation("run snapshot Wiki trace differs from Architect context")
    validate_architect_proposal(architect.proposal, context, architect.agent_output)
    if architect.proposal != expand_architect_proposal(architect.agent_output, context):
        raise PolicyViolation("run snapshot Architect expansion differs from controller policy")

    validate_manifest_for_request(architect.proposal.manifest, request)
    for evidence in (request, context, graph, wiki_trace, architect):
        session.validate_portable_evidence(evidence)
    return _VerifiedRunCommon(
        request=request,
        architect_context=context,
        dependency_graph=graph,
        wiki_trace=wiki_trace,
        architect=architect,
    )


def _load_verified_attempt(
    session: AgentRunSession,
    common: _VerifiedRunCommon,
    attempt: int,
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
        if call.live_invocation and not call.store_false_sent:
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

    for evidence in (engineer, report, validator):
        session.validate_portable_evidence(evidence)
    return VerifiedRunSnapshot(
        request=request,
        architect_context=context,
        dependency_graph=graph,
        wiki_trace=wiki_trace,
        architect=architect,
        engineer=engineer,
        report=report,
        validator=validator,
    )


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
    "VerifiedRunSnapshot",
    "load_verified_run_snapshot",
    "load_verified_run_snapshots",
]
