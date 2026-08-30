"""Benchmark-only launch seam for the measured Wiki ablation.

Normal UI and CLI starts never import or call this module. A measured cell must
first bind its case, arm, request, source revision, and benchmark definition;
the immutable binding is then persisted in the ordinary run configuration and
the unchanged agent-run lifecycle owns every subsequent gate and retry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from legacy_migration_agent.agent_runtime.model_workflow import DeterministicValidator
from legacy_migration_agent.application.agent_run import (
    AgentRunModelClients,
    AgentRunStatus,
    _recover_incomplete_benchmark_agent_run_start,
    _start_benchmark_agent_run,
)
from legacy_migration_agent.application.migration_scenarios import MigrationLaunchContract
from legacy_migration_agent.benchmark_execution import (
    BenchmarkExecutionAnchor,
    load_verified_benchmark_execution_anchor,
)
from legacy_migration_agent.benchmark_protocol import (
    load_verified_benchmark_protocol,
    require_independently_reviewed_benchmark_protocol,
)
from legacy_migration_agent.contracts import MigrationRequest
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.knowledge.wiki import (
    BENCHMARK_COMPLEX_RISK_CASE_ID,
    BENCHMARK_RISK_CATEGORIES,
    BENCHMARK_RISK_EVIDENCE_ID,
    BENCHMARK_RISK_REASONS,
    BenchmarkKnowledgeBinding,
    BenchmarkRiskSeedBinding,
    BenchmarkRiskStimulus,
)
from legacy_migration_agent.measured_evaluation import (
    Complexity,
    WorkflowDisposition,
    canonical_cell_id,
)

BenchmarkKnowledgeArm = Literal["full_agent_wiki", "full_agent_no_wiki"]


@dataclass(frozen=True, slots=True)
class BenchmarkCellRoute:
    """Controller-owned routing for one predeclared benchmark cell."""

    cell_id: str
    case_id: str
    scenario_id: str
    config_id: str
    knowledge_arm: BenchmarkKnowledgeArm
    repetition: int
    run_dir: str
    run_id: str
    thread_id: str
    request_id: str
    rubric_path: str
    receipt_path: str


def benchmark_cell_routes(project_root: Path) -> tuple[BenchmarkCellRoute, ...]:
    """Enumerate the exact declared matrix without invoking a provider."""

    protocol = load_verified_benchmark_protocol(project_root)
    source_by_case = {item.case_id: item for item in protocol.source_snapshots.cases}
    routes: list[BenchmarkCellRoute] = []
    for case in protocol.registry.cases:
        source = source_by_case[case.case_id]
        for config in protocol.registry.configurations:
            arm: BenchmarkKnowledgeArm = (
                "full_agent_wiki" if config.uses_wiki else "full_agent_no_wiki"
            )
            for repetition in range(1, protocol.registry.repetitions + 1):
                cell_id = canonical_cell_id(case.case_id, config.config_id, repetition)
                routes.append(
                    BenchmarkCellRoute(
                        cell_id=cell_id,
                        case_id=case.case_id,
                        scenario_id=source.scenario_id,
                        config_id=config.config_id,
                        knowledge_arm=arm,
                        repetition=repetition,
                        run_dir=f".runs/benchmark-v2/{cell_id}",
                        run_id=f"benchmark-v2-{cell_id}",
                        thread_id=f"benchmark-v2-thread-{cell_id}",
                        request_id=f"benchmark-v2-request-{cell_id}",
                        rubric_path=f"evaluation/benchmark-v2/rubrics/{cell_id}.json",
                        receipt_path=(f"evaluation/benchmark-v2/results/receipts/{cell_id}.json"),
                    )
                )
    return tuple(routes)


def benchmark_cell_route(project_root: Path, cell_id: str) -> BenchmarkCellRoute:
    """Resolve one canonical cell or fail closed."""

    route = next(
        (item for item in benchmark_cell_routes(project_root) if item.cell_id == cell_id),
        None,
    )
    if route is None:
        raise PolicyViolation("benchmark cell is not present in the declared matrix")
    return route


def start_reviewed_benchmark_cell(
    project_root: Path,
    *,
    cell_id: str,
    execution_anchor_path: Path,
    requested_at: datetime,
    models: AgentRunModelClients,
    trusted_validator: DeterministicValidator | None = None,
) -> AgentRunStatus:
    """Start or recover one reviewed, anchored cell using deterministic routing."""

    # This gate deliberately precedes request preparation and every live-runtime
    # identity operation. Initial or self-reviewed labels are not executable.
    require_independently_reviewed_benchmark_protocol(project_root)
    route = benchmark_cell_route(project_root, cell_id)
    from legacy_migration_agent.application.agent_run import prepare_agent_run_request
    from legacy_migration_agent.application.migration_scenarios import (
        migration_launch_contract,
    )

    launch_contract = migration_launch_contract(route.scenario_id)
    request = prepare_agent_run_request(
        project_root,
        request_id=route.request_id,
        launch_contract=launch_contract,
        requested_at=requested_at,
    )
    binding = bind_benchmark_knowledge_arm(
        project_root,
        request,
        cell_id=route.cell_id,
        case_id=route.case_id,
        scenario_id=route.scenario_id,
        knowledge_arm=route.knowledge_arm,
        execution_anchor_path=execution_anchor_path,
    )
    run_dir = project_root / route.run_dir
    operation = (
        recover_incomplete_benchmark_agent_run_start
        if run_dir.exists()
        else start_benchmark_agent_run
    )
    return operation(
        project_root,
        run_dir,
        run_id=route.run_id,
        thread_id=route.thread_id,
        launch_contract=launch_contract,
        request=request,
        models=models,
        knowledge_binding=binding,
        execution_anchor_path=execution_anchor_path,
        trusted_validator=trusted_validator,
    )


def bind_benchmark_knowledge_arm(
    project_root: Path,
    request: MigrationRequest,
    *,
    cell_id: str,
    case_id: str,
    scenario_id: str,
    knowledge_arm: BenchmarkKnowledgeArm,
    execution_anchor_path: Path | None = None,
) -> BenchmarkKnowledgeBinding:
    """Load, verify, and freeze one predeclared cell before run mutation."""

    frozen_request = MigrationRequest.model_validate(request.model_dump(mode="python"))
    protocol = load_verified_benchmark_protocol(project_root)
    registry = protocol.registry
    risk_seed = protocol.risk_seed
    case_by_id = {case.case_id: case for case in registry.cases}
    source_by_id = {item.case_id: item for item in protocol.source_snapshots.cases}
    case = case_by_id.get(case_id)
    source = source_by_id.get(case_id)
    if case is None or source is None:
        raise PolicyViolation("benchmark cell selects an undeclared case")
    config_id = _knowledge_arm_config_id(knowledge_arm)
    config = next(
        (item for item in registry.configurations if item.config_id == config_id),
        None,
    )
    if config is None or config.uses_wiki != (knowledge_arm == "full_agent_wiki"):
        raise PolicyViolation("benchmark cell selects an undeclared knowledge arm")
    if not any(
        cell_id == canonical_cell_id(case_id, config_id, repetition)
        for repetition in range(1, registry.repetitions + 1)
    ):
        raise PolicyViolation("benchmark cell ID is not canonical for its case and arm")
    if source.scenario_id != scenario_id:
        raise PolicyViolation("benchmark cell selects another scenario")
    if source.source_root != frozen_request.repository:
        raise PolicyViolation("benchmark source root does not match the migration request")
    if (
        case.platform is not frozen_request.platform
        or case.source_digest != source.source_revision
        or source.source_revision != frozen_request.base_revision
    ):
        raise PolicyViolation("benchmark source binding does not match the migration request")

    seed_artifact_digest = protocol.declaration.risk_seed_digest
    execution_anchor: BenchmarkExecutionAnchor | None = None
    if execution_anchor_path is not None:
        execution_anchor = load_verified_benchmark_execution_anchor(
            project_root,
            execution_anchor_path,
        )
        _verify_anchor_protocol(execution_anchor, protocol, config)
    risk_seed_binding: BenchmarkRiskSeedBinding | None = None
    if case.complexity is Complexity.COMPLEX:
        if (
            case.case_id != BENCHMARK_COMPLEX_RISK_CASE_ID
            or case.expected_disposition is not WorkflowDisposition.DECISION_REQUIRED
            or not case.intervention_expected
            or risk_seed.case_id != case.case_id
        ):
            raise PolicyViolation("complex Case risk policy does not match the predeclared case")
        stimulus = BenchmarkRiskStimulus(
            evidence_id=BENCHMARK_RISK_EVIDENCE_ID,
            authority="none",
            request_text=risk_seed.request_text,
            seed_artifact_digest=seed_artifact_digest,
        )
        risk_seed_binding = BenchmarkRiskSeedBinding(
            seed_id=risk_seed.seed_id,
            case_id=risk_seed.case_id,
            stimulus=stimulus,
            stimulus_digest=artifact_digest(stimulus),
            required_categories=BENCHMARK_RISK_CATEGORIES,
            required_reasons=BENCHMARK_RISK_REASONS,
            expected_disposition="decision_required",
        )
    elif risk_seed.case_id == case.case_id:
        raise PolicyViolation("benchmark risk seed is forbidden for a non-complex case")

    return BenchmarkKnowledgeBinding(
        benchmark_id=registry.registry_id,
        benchmark_definition_digest=protocol.declaration_digest,
        benchmark_registry_digest=protocol.registry_digest,
        configuration_digest=artifact_digest(config),
        provider_id=config.provider_id,
        model_id=config.model_id,
        cell_id=cell_id,
        case_id=case_id,
        scenario_id=scenario_id,
        knowledge_arm=knowledge_arm,
        request_digest=artifact_digest(frozen_request),
        source_revision=frozen_request.base_revision,
        wiki_tree_revision=protocol.wiki_tree_revision,
        execution_anchor_digest=(
            None if execution_anchor is None else execution_anchor.anchor_digest
        ),
        runtime_identity_digest=(
            None if execution_anchor is None else execution_anchor.runtime_identity_digest
        ),
        risk_seed_binding=risk_seed_binding,
    )


def start_benchmark_agent_run(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    thread_id: str,
    launch_contract: MigrationLaunchContract,
    request: MigrationRequest | Mapping[str, Any],
    models: AgentRunModelClients,
    knowledge_binding: BenchmarkKnowledgeBinding,
    execution_anchor_path: Path | None = None,
    trusted_validator: DeterministicValidator | None = None,
) -> AgentRunStatus:
    """Run one bound Wiki/no-Wiki cell through the production agent harness."""

    frozen_binding = BenchmarkKnowledgeBinding.model_validate(
        knowledge_binding.model_dump(mode="python")
    )
    frozen_request = MigrationRequest.model_validate(request)
    _verify_canonical_binding(project_root, frozen_request, frozen_binding)
    _verify_model_identity(models, frozen_binding)
    _verify_execution_anchor_binding(
        project_root,
        execution_anchor_path,
        models,
        frozen_binding,
    )
    return _start_benchmark_agent_run(
        project_root,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        launch_contract=launch_contract,
        request=request,
        models=models,
        knowledge_binding=frozen_binding,
        trusted_validator=trusted_validator,
    )


def recover_incomplete_benchmark_agent_run_start(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    thread_id: str,
    launch_contract: MigrationLaunchContract,
    request: MigrationRequest | Mapping[str, Any],
    models: AgentRunModelClients,
    knowledge_binding: BenchmarkKnowledgeBinding,
    execution_anchor_path: Path | None = None,
    trusted_validator: DeterministicValidator | None = None,
) -> AgentRunStatus:
    """Recover one interrupted measured start using its exact frozen binding."""

    frozen_binding = BenchmarkKnowledgeBinding.model_validate(
        knowledge_binding.model_dump(mode="python")
    )
    frozen_request = MigrationRequest.model_validate(request)
    _verify_canonical_binding(project_root, frozen_request, frozen_binding)
    _verify_model_identity(models, frozen_binding)
    _verify_execution_anchor_binding(
        project_root,
        execution_anchor_path,
        models,
        frozen_binding,
    )
    return _recover_incomplete_benchmark_agent_run_start(
        project_root,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        launch_contract=launch_contract,
        request=request,
        models=models,
        knowledge_binding=frozen_binding,
        trusted_validator=trusted_validator,
    )


def _verify_canonical_binding(
    project_root: Path,
    request: MigrationRequest,
    binding: BenchmarkKnowledgeBinding,
) -> None:
    canonical = bind_benchmark_knowledge_arm(
        project_root,
        request,
        cell_id=binding.cell_id,
        case_id=binding.case_id,
        scenario_id=binding.scenario_id,
        knowledge_arm=binding.knowledge_arm,
    )
    excluded = {"execution_anchor_digest", "runtime_identity_digest"}
    if canonical.model_dump(mode="python", exclude=excluded) != binding.model_dump(
        mode="python",
        exclude=excluded,
    ):
        raise PolicyViolation("benchmark knowledge binding is stale, cross-bound, or tampered")


def _verify_anchor_protocol(
    anchor: BenchmarkExecutionAnchor,
    protocol: Any,
    config: Any,
) -> None:
    expected = (
        protocol.declaration_digest,
        protocol.registry_digest,
        protocol.wiki_tree_revision,
        config.agent_definitions_digest,
        config.provider_id,
        config.model_id,
    )
    actual = (
        anchor.protocol_declaration_digest,
        anchor.benchmark_registry_digest,
        anchor.wiki_tree_revision,
        anchor.agent_definitions_digest,
        anchor.provider_id,
        anchor.model_id,
    )
    if actual != expected:
        raise PolicyViolation("benchmark execution anchor differs from the selected protocol")


def _verify_execution_anchor_binding(
    project_root: Path,
    execution_anchor_path: Path | None,
    models: AgentRunModelClients,
    binding: BenchmarkKnowledgeBinding,
) -> None:
    if execution_anchor_path is None or not binding.execution_anchored:
        raise PolicyViolation("benchmark execution requires a pre-run execution anchor")
    runtime_identity = models.resolve_runtime_identity()
    if binding.runtime_identity_digest != runtime_identity:
        raise PolicyViolation("benchmark runtime identity differs from the execution anchor")
    anchor = load_verified_benchmark_execution_anchor(
        project_root,
        execution_anchor_path,
        expected_runtime_identity_digest=runtime_identity,
    )
    if anchor.anchor_digest != binding.execution_anchor_digest:
        raise PolicyViolation("benchmark execution anchor digest differs from the cell binding")
    protocol = load_verified_benchmark_protocol(project_root)
    config = next(
        (
            item
            for item in protocol.registry.configurations
            if artifact_digest(item) == binding.configuration_digest
        ),
        None,
    )
    if config is None:
        raise PolicyViolation("benchmark cell selects an unknown execution configuration")
    _verify_anchor_protocol(anchor, protocol, config)


def _knowledge_arm_config_id(knowledge_arm: BenchmarkKnowledgeArm) -> str:
    if knowledge_arm == "full_agent_wiki":
        return "full-agent-wiki"
    if knowledge_arm == "full_agent_no_wiki":
        return "full-agent-no-wiki"
    raise PolicyViolation("benchmark cell selects an unknown knowledge arm")


def _verify_model_identity(
    models: AgentRunModelClients,
    binding: BenchmarkKnowledgeBinding,
) -> None:
    if models.provider_id != binding.provider_id or models.model_id != binding.model_id:
        raise PolicyViolation("benchmark model identity differs from the predeclared configuration")
    if not models.live_invocation or models.execution_boundary != "remote_provider_managed":
        raise PolicyViolation(
            "benchmark execution must use the predeclared live remote-provider boundary"
        )


__all__ = [
    "BenchmarkCellRoute",
    "BenchmarkKnowledgeArm",
    "benchmark_cell_route",
    "benchmark_cell_routes",
    "bind_benchmark_knowledge_arm",
    "recover_incomplete_benchmark_agent_run_start",
    "start_reviewed_benchmark_cell",
    "start_benchmark_agent_run",
]
