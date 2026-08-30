"""Fail-closed loading for the predeclared benchmark-v2 protocol.

The benchmark declaration is a graph, not a single trustworthy JSON file.  A
caller receives a :class:`VerifiedBenchmarkProtocol` only after every declared
artifact, every referenced fixture contract and source-edge label file, and
every current code-owned runtime binding has been checked.  No model or
external service is invoked by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from legacy_migration_agent.agent_runtime.agent_definitions import (
    AgentRole,
    load_agent_registry,
)
from legacy_migration_agent.application.migration_scenarios import (
    migration_recipe,
    migration_scenario_by_id,
)
from legacy_migration_agent.contracts import (
    Identifier,
    Platform,
    Revision,
    Sha256Digest,
    StrictModel,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.workspace import content_revision
from legacy_migration_agent.knowledge.wiki import (
    BENCHMARK_COMPLEX_RISK_CASE_ID,
    BENCHMARK_RISK_REASONS,
    RiskReason,
)
from legacy_migration_agent.measured_evaluation import (
    BenchmarkLabelReviewEvidence,
    Complexity,
    LabelReviewStatus,
    MeasuredEvaluationRegistry,
    WorkflowDisposition,
)
from legacy_migration_agent.platforms.local_checks import (
    CASE_IMPLEMENTATION_CONTRACT,
    SALESFORCE_IMPLEMENTATION_CONTRACT,
)
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    MULESOFT_IMPLEMENTATION_CONTRACT,
)

BENCHMARK_DECLARATION_PATH = "evaluation/benchmark-v2/declaration.json"
BENCHMARK_REGISTRY_PATH = "evaluation/benchmark-v2/registry.json"
WIKI_CATALOG_PATH = "knowledge/wiki/catalog.json"

_HIGH_IMPACT_DEFINITION = (
    "A dependency is high impact when omitting or mis-migrating it can change runtime "
    "reachability, production behavior or data, access-control enforcement, public "
    "entry-point wiring, configuration resolution, or the only independent validation "
    "of a required migration behavior."
)
_HIGH_IMPACT_DECISION_RULES = (
    "Production code, entry-point, configuration, object, field, and permission "
    "dependencies are high impact when loss can alter execution, behavior, data access, "
    "or authorization.",
    "A test-derived dependency is high impact when its loss prevents the only independent "
    "validation of a required behavior or security contract.",
    "A test-derived dependency is low impact only when independent required coverage "
    "remains and its omission cannot affect production behavior or authorization.",
    "Uncertainty is resolved as high impact until an independent reviewer records "
    "contrary evidence.",
)
_HIGH_IMPACT_BASIS_OUTCOMES = {
    "production_runtime": True,
    "production_data_contract": True,
    "production_security": True,
    "sole_required_validation": True,
    "supporting_test_evidence": False,
}


class _BenchmarkDeclaration(StrictModel):
    schema_version: Literal["2.0"]
    registry_id: Identifier
    protocol_status: Literal["predeclared_not_executed"]
    case_count: Literal[3]
    configuration_count: Literal[2]
    repetitions_per_case_configuration: Literal[3]
    planned_live_cells: Literal[18]
    cases: tuple[Identifier, ...] = Field(min_length=3, max_length=3)
    configurations: tuple[Literal["full-agent-wiki", "full-agent-no-wiki"], ...] = Field(
        min_length=2,
        max_length=2,
    )
    source_snapshots_path: str
    source_snapshots_digest: Sha256Digest
    runtime_bindings_path: str
    runtime_bindings_digest: Sha256Digest
    dependency_labels_path: str
    dependency_labels_digest: Sha256Digest
    risk_seed_path: str
    risk_seed_digest: Sha256Digest
    result_receipts_status: Literal["not_performed"]
    result_receipts: tuple[Any, ...] = Field(default=(), max_length=0)
    claim_boundary: str = Field(min_length=1, max_length=1000)

    @field_validator(
        "source_snapshots_path",
        "runtime_bindings_path",
        "dependency_labels_path",
        "risk_seed_path",
    )
    @classmethod
    def validate_artifact_path(cls, value: str) -> str:
        return validate_relative_path(value)


class _BenchmarkSourceSnapshot(StrictModel):
    case_id: Identifier
    scenario_id: Identifier
    source_root: str
    source_revision: Revision
    fixture_contract_path: str
    fixture_contract_file_digest: Sha256Digest
    scenario_definition_digest: Sha256Digest
    scope_policy_digest: Sha256Digest
    implementation_contract_digest: Sha256Digest
    source_edge_labels_path: str
    source_edge_labels_file_digest: Sha256Digest

    @field_validator("source_root", "fixture_contract_path", "source_edge_labels_path")
    @classmethod
    def validate_relative_artifact_path(cls, value: str) -> str:
        return validate_relative_path(value)


class _BenchmarkSourceSnapshots(StrictModel):
    schema_version: Literal["2.0"]
    snapshot_method: Literal["legacy_migration_agent.core.workspace.content_revision"]
    cases: tuple[_BenchmarkSourceSnapshot, ...] = Field(min_length=3, max_length=3)


class _RiskSeedSafeHandling(StrictModel):
    purpose: Literal["benchmark_input_only"]
    model_invocation_status: Literal["not_performed"]
    candidate_generation_status: Literal["not_performed"]
    filesystem_action_status: Literal["not_performed"]
    external_action_status: Literal["not_performed"]


class _BenchmarkRiskSeedArtifact(StrictModel):
    schema_version: Literal["2.0"]
    seed_id: Identifier
    case_id: Identifier
    status: Literal["inert_not_executed"]
    synthetic: Literal[True]
    normal_ui_exposure: Literal[False]
    request_text: str = Field(min_length=1, max_length=4000)
    expected_disposition: Literal["decision_required"]
    expected_intervention_reasons: tuple[RiskReason, ...] = Field(min_length=1, max_length=16)
    safe_handling: _RiskSeedSafeHandling


class _AgentDefinitionBinding(StrictModel):
    version: str = Field(min_length=1, max_length=160)
    definition_digest: Sha256Digest


class _BenchmarkPromptPolicy(StrictModel):
    agent_definition_digests: dict[Identifier, Sha256Digest]
    scenario_definition_digests: dict[Identifier, Sha256Digest]
    wiki_catalog_file_digest: Sha256Digest
    wiki_tree_revision: Revision


class _ScenarioValidationPolicy(StrictModel):
    definition_digest: Sha256Digest
    scope_policy_digest: Sha256Digest
    allowed_validation_command_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=64)


class _BenchmarkValidationPolicy(StrictModel):
    scenarios: dict[Identifier, _ScenarioValidationPolicy]


class _BenchmarkEnvironmentCondition(StrictModel):
    case_id: Identifier
    evidence_path: str
    evidence_file_digest: Sha256Digest
    capability: Literal["mule_runtime_maven_munit"]
    observed_availability: Literal["unavailable"]
    expected_disposition: Literal["environment_unavailable"]

    @field_validator("evidence_path")
    @classmethod
    def validate_evidence_path(cls, value: str) -> str:
        return validate_relative_path(value)


class _UnavailableMuleRuntimeAuthority(StrictModel):
    schema_version: Literal["1.0"]
    enabled: Literal[False]
    disabled_reason: str = Field(min_length=1, max_length=1000)


class _BenchmarkRuntimeBindings(StrictModel):
    schema_version: Literal["2.0"]
    provider_id: Identifier
    model_id: str = Field(min_length=1, max_length=240)
    agent_definitions: dict[Identifier, _AgentDefinitionBinding]
    prompt_policy: _BenchmarkPromptPolicy
    validation_policy: _BenchmarkValidationPolicy
    environment_conditions: tuple[_BenchmarkEnvironmentCondition, ...] = Field(
        min_length=1,
        max_length=8,
    )


class _DependencyEdge(StrictModel):
    kind: Identifier
    source: str = Field(min_length=1, max_length=300)
    target: str = Field(min_length=1, max_length=300)
    symbol: str = Field(min_length=1, max_length=500)


class _DependencyLabelRecord(StrictModel):
    dependency_id: Identifier
    high_impact: bool
    impact_basis: Literal[
        "production_runtime",
        "production_data_contract",
        "production_security",
        "sole_required_validation",
        "supporting_test_evidence",
    ]
    edge: _DependencyEdge


class _DependencyLabelCase(StrictModel):
    case_id: Identifier
    source_edge_labels_path: str
    labels: tuple[_DependencyLabelRecord, ...] = Field(min_length=1, max_length=256)

    @field_validator("source_edge_labels_path")
    @classmethod
    def validate_source_edge_path(cls, value: str) -> str:
        return validate_relative_path(value)


class _HighImpactDefinition(StrictModel):
    definition_id: Literal["migration-dependency-impact-v1"]
    definition: str = Field(min_length=1, max_length=2000)
    decision_rules: tuple[str, ...] = Field(min_length=3, max_length=16)
    basis_outcomes: dict[Identifier, bool]

    @field_validator("decision_rules")
    @classmethod
    def validate_rules(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value.strip() or len(value) > 1000 for value in values
        ):
            raise ValueError("high-impact decision rules must be unique bounded text")
        return values

    @field_validator("basis_outcomes")
    @classmethod
    def validate_basis_outcomes(cls, values: dict[str, bool]) -> dict[str, bool]:
        if values != _HIGH_IMPACT_BASIS_OUTCOMES:
            raise ValueError("high-impact basis outcomes must use the frozen v1 policy")
        return values

    @model_validator(mode="after")
    def validate_frozen_v1_policy(self) -> _HighImpactDefinition:
        if (
            self.definition != _HIGH_IMPACT_DEFINITION
            or self.decision_rules != _HIGH_IMPACT_DECISION_RULES
        ):
            raise ValueError("high-impact v1 prose changed without a policy version change")
        return self


class _BenchmarkDependencyLabels(StrictModel):
    schema_version: Literal["2.0"]
    review_status: LabelReviewStatus
    reviewer_id: Identifier | None
    review_evidence_path: str | None
    review_evidence_digest: Sha256Digest | None
    high_impact_definition: _HighImpactDefinition
    cases: tuple[_DependencyLabelCase, ...] = Field(min_length=3, max_length=3)

    @field_validator("review_evidence_path")
    @classmethod
    def validate_review_path(cls, value: str | None) -> str | None:
        return None if value is None else validate_relative_path(value)

    @model_validator(mode="after")
    def validate_review_boundary(self) -> _BenchmarkDependencyLabels:
        supplied = (
            self.reviewer_id is not None,
            self.review_evidence_path is not None,
            self.review_evidence_digest is not None,
        )
        if len(set(supplied)) != 1:
            raise ValueError("label reviewer, evidence path, and digest must be supplied together")
        reviewed = self.review_status is LabelReviewStatus.INDEPENDENTLY_REVIEWED
        if reviewed != all(supplied):
            raise ValueError("only independently reviewed labels may carry review evidence")
        return self


class _SourceEdgeArtifact(StrictModel):
    schema_version: Literal["1.0"]
    fixture_id: Identifier
    platform: Platform
    graph: Literal["source"]
    review_status: Literal["initial_label_set"]
    edges: tuple[_DependencyEdge, ...] = Field(min_length=1, max_length=256)


class BenchmarkDependencyEdgeLabel(StrictModel):
    """Public, immutable projection used to score one discovered graph edge."""

    dependency_id: Identifier
    high_impact: bool
    impact_basis: Identifier
    kind: Identifier
    source: str = Field(min_length=1, max_length=300)
    target: str = Field(min_length=1, max_length=300)
    symbol: str = Field(min_length=1, max_length=500)
    evidence_digest: Sha256Digest

    @property
    def edge_key(self) -> tuple[str, str, str, str]:
        """Return the exact graph tuple used by the measured receipt extractor."""

        return (self.kind, self.source, self.target, self.symbol)


@dataclass(frozen=True, slots=True)
class VerifiedBenchmarkProtocol:
    """A fully verified, provider-free snapshot of benchmark-v2 authority."""

    project_root: Path
    declaration: _BenchmarkDeclaration
    declaration_digest: Sha256Digest
    registry: MeasuredEvaluationRegistry
    registry_digest: Sha256Digest
    source_snapshots: _BenchmarkSourceSnapshots
    runtime_bindings: _BenchmarkRuntimeBindings
    dependency_labels: _BenchmarkDependencyLabels
    label_review_evidence: BenchmarkLabelReviewEvidence | None
    risk_seed: _BenchmarkRiskSeedArtifact
    wiki_tree_revision: Revision

    def source_for_case(self, case_id: str) -> _BenchmarkSourceSnapshot | None:
        return next((item for item in self.source_snapshots.cases if item.case_id == case_id), None)

    def dependency_labels_for_case(
        self,
        case_id: str,
    ) -> tuple[BenchmarkDependencyEdgeLabel, ...]:
        """Return verified public labels without exposing loader-internal JSON models."""

        labeled_case = next(
            (item for item in self.dependency_labels.cases if item.case_id == case_id),
            None,
        )
        registry_case = next(
            (item for item in self.registry.cases if item.case_id == case_id),
            None,
        )
        if labeled_case is None or registry_case is None:
            raise PolicyViolation("benchmark dependency labels select an unknown case")
        evidence_by_id = {
            item.dependency_id: item.evidence_digest for item in registry_case.dependencies
        }
        return tuple(
            BenchmarkDependencyEdgeLabel(
                dependency_id=item.dependency_id,
                high_impact=item.high_impact,
                impact_basis=item.impact_basis,
                kind=item.edge.kind,
                source=item.edge.source,
                target=item.edge.target,
                symbol=item.edge.symbol,
                evidence_digest=evidence_by_id[item.dependency_id],
            )
            for item in labeled_case.labels
        )

    @property
    def label_review_subject_digest(self) -> Sha256Digest:
        """Return the non-circular digest independently reviewed by a human."""

        return _label_review_subject_digest(self.registry, self.dependency_labels)


_IMPLEMENTATION_CONTRACTS = {
    "mulesoft-mule3-to-mule4": MULESOFT_IMPLEMENTATION_CONTRACT,
    "salesforce-vf-to-lwc": SALESFORCE_IMPLEMENTATION_CONTRACT,
    "case-management-console": CASE_IMPLEMENTATION_CONTRACT,
}


def load_verified_benchmark_protocol(project_root: Path) -> VerifiedBenchmarkProtocol:
    """Load benchmark-v2 only after verifying its complete local artifact graph."""

    try:
        root = Path(project_root).resolve(strict=True)
        if not root.is_dir():
            raise PolicyViolation("benchmark project root is not a directory")

        declaration_payload = _read_benchmark_json(root, BENCHMARK_DECLARATION_PATH)
        declaration = _validate_json_model(_BenchmarkDeclaration, declaration_payload)
        declaration_digest = artifact_digest(declaration_payload)

        registry_payload = _read_benchmark_json(root, BENCHMARK_REGISTRY_PATH)
        registry = _validate_json_model(MeasuredEvaluationRegistry, registry_payload)
        source_payload = _read_benchmark_json(root, declaration.source_snapshots_path)
        source_snapshots = _validate_json_model(_BenchmarkSourceSnapshots, source_payload)
        runtime_payload = _read_benchmark_json(root, declaration.runtime_bindings_path)
        runtime_bindings = _validate_json_model(_BenchmarkRuntimeBindings, runtime_payload)
        dependency_payload = _read_benchmark_json(root, declaration.dependency_labels_path)
        dependency_labels = _validate_json_model(_BenchmarkDependencyLabels, dependency_payload)
        label_review_evidence: BenchmarkLabelReviewEvidence | None = None
        if dependency_labels.review_evidence_path is not None:
            if dependency_labels.review_evidence_digest is None:
                raise PolicyViolation("benchmark label-review evidence digest is missing")
            review_payload = _read_benchmark_json(
                root,
                dependency_labels.review_evidence_path,
            )
            label_review_evidence = _validate_json_model(
                BenchmarkLabelReviewEvidence,
                review_payload,
            )
            _require_digest(
                review_payload,
                dependency_labels.review_evidence_digest,
                "benchmark label-review evidence digest does not match its binding",
            )
        risk_seed_payload = _read_benchmark_json(root, declaration.risk_seed_path)
        risk_seed = _validate_json_model(_BenchmarkRiskSeedArtifact, risk_seed_payload)

        _require_digest(
            source_payload,
            declaration.source_snapshots_digest,
            "benchmark source-snapshot digest does not match the declaration",
        )
        _require_digest(
            runtime_payload,
            declaration.runtime_bindings_digest,
            "benchmark runtime-binding digest does not match the declaration",
        )
        _require_digest(
            dependency_payload,
            declaration.dependency_labels_digest,
            "benchmark dependency-label digest does not match the declaration",
        )
        _require_digest(
            risk_seed_payload,
            declaration.risk_seed_digest,
            "benchmark risk-seed digest does not match the declaration",
        )

        _verify_registry(declaration, declaration_digest, registry)
        source_edges = _verify_sources(root, declaration, registry, source_snapshots)
        _verify_dependency_labels(
            declaration,
            registry,
            source_snapshots,
            dependency_labels,
            source_edges,
            label_review_evidence,
        )
        wiki_tree_revision = _verify_runtime_bindings(
            root,
            registry,
            source_snapshots,
            runtime_bindings,
        )
        _verify_risk_seed(registry, risk_seed)
    except PolicyViolation:
        raise
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise PolicyViolation("benchmark v2 artifacts are missing, malformed, or tampered") from exc

    return VerifiedBenchmarkProtocol(
        project_root=root,
        declaration=declaration,
        declaration_digest=declaration_digest,
        registry=registry,
        registry_digest=artifact_digest(registry_payload),
        source_snapshots=source_snapshots,
        runtime_bindings=runtime_bindings,
        dependency_labels=dependency_labels,
        label_review_evidence=label_review_evidence,
        risk_seed=risk_seed,
        wiki_tree_revision=wiki_tree_revision,
    )


def require_independently_reviewed_benchmark_protocol(
    project_root: Path,
) -> VerifiedBenchmarkProtocol:
    """Return benchmark v2 only after its real-human label review is bound.

    This is the operator execution boundary.  Provider-free protocol inspection
    remains available for the initial label set, but no live cell or execution
    anchor may be created from unreviewed labels.
    """

    protocol = load_verified_benchmark_protocol(project_root)
    if (
        protocol.label_review_evidence is None
        or protocol.dependency_labels.review_status is not LabelReviewStatus.INDEPENDENTLY_REVIEWED
        or any(
            case.review_status is not LabelReviewStatus.INDEPENDENTLY_REVIEWED
            for case in protocol.registry.cases
        )
    ):
        raise PolicyViolation("benchmark execution requires independently reviewed label evidence")
    return protocol


def _verify_registry(
    declaration: _BenchmarkDeclaration,
    declaration_digest: Sha256Digest,
    registry: MeasuredEvaluationRegistry,
) -> None:
    if registry.declaration_digest != declaration_digest:
        raise PolicyViolation("benchmark registry does not match the declaration")
    if registry.registry_id != declaration.registry_id:
        raise PolicyViolation("benchmark registry identity does not match the declaration")
    if tuple(case.case_id for case in registry.cases) != declaration.cases:
        raise PolicyViolation("benchmark registry case inventory does not match the declaration")
    if tuple(config.config_id for config in registry.configurations) != declaration.configurations:
        raise PolicyViolation(
            "benchmark registry configuration inventory does not match the declaration"
        )
    if registry.repetitions != declaration.repetitions_per_case_configuration:
        raise PolicyViolation("benchmark repetition count does not match the declaration")


def _verify_sources(
    root: Path,
    declaration: _BenchmarkDeclaration,
    registry: MeasuredEvaluationRegistry,
    snapshots: _BenchmarkSourceSnapshots,
) -> dict[str, _SourceEdgeArtifact]:
    if tuple(item.case_id for item in snapshots.cases) != declaration.cases:
        raise PolicyViolation("benchmark source inventory does not match the declaration")

    registry_cases = {case.case_id: case for case in registry.cases}
    source_edges: dict[str, _SourceEdgeArtifact] = {}
    seen_scenarios: set[str] = set()
    for snapshot in snapshots.cases:
        case = registry_cases[snapshot.case_id]
        if snapshot.scenario_id in seen_scenarios:
            raise PolicyViolation("benchmark source snapshots reuse a scenario")
        seen_scenarios.add(snapshot.scenario_id)
        scenario = migration_scenario_by_id(snapshot.scenario_id)

        if scenario.source_root != snapshot.source_root:
            raise PolicyViolation("benchmark source root drifted from the canonical scenario")
        if scenario.platform is not case.platform:
            raise PolicyViolation("benchmark source platform differs from the registry case")
        if scenario.definition_digest != snapshot.scenario_definition_digest:
            raise PolicyViolation("benchmark scenario definition drifted from the snapshot")
        if scenario.scope_policy_digest != snapshot.scope_policy_digest:
            raise PolicyViolation("benchmark scope policy drifted from the snapshot")
        implementation_contract = _IMPLEMENTATION_CONTRACTS.get(snapshot.scenario_id)
        if implementation_contract is None or (
            artifact_digest(implementation_contract) != snapshot.implementation_contract_digest
        ):
            raise PolicyViolation("benchmark implementation contract drifted from the snapshot")
        if case.source_digest != snapshot.source_revision:
            raise PolicyViolation("benchmark registry source digest differs from the snapshot")
        if content_revision(root / snapshot.source_root) != snapshot.source_revision:
            raise PolicyViolation("benchmark source tree drifted from the predeclared snapshot")

        fixture_bytes = _read_benchmark_bytes(root, snapshot.fixture_contract_path)
        if _bytes_digest(fixture_bytes) != snapshot.fixture_contract_file_digest:
            raise PolicyViolation(
                "benchmark fixture contract drifted from the predeclared snapshot"
            )

        edge_bytes = _read_benchmark_bytes(root, snapshot.source_edge_labels_path)
        if _bytes_digest(edge_bytes) != snapshot.source_edge_labels_file_digest:
            raise PolicyViolation("benchmark source-edge labels drifted from the snapshot")
        edge_payload = _decode_json(edge_bytes, snapshot.source_edge_labels_path)
        edge_artifact = _validate_json_model(_SourceEdgeArtifact, edge_payload)
        if edge_artifact.platform is not case.platform:
            raise PolicyViolation("benchmark source-edge platform differs from its case")
        if edge_artifact.fixture_id != scenario.behavior_contract_id:
            raise PolicyViolation("benchmark source-edge fixture differs from its scenario")
        if len(edge_artifact.edges) != len(set(edge_artifact.edges)):
            raise PolicyViolation("benchmark source-edge labels must be unique")
        source_edges[snapshot.case_id] = edge_artifact
    return source_edges


def _verify_dependency_labels(
    declaration: _BenchmarkDeclaration,
    registry: MeasuredEvaluationRegistry,
    snapshots: _BenchmarkSourceSnapshots,
    labels: _BenchmarkDependencyLabels,
    source_edges: dict[str, _SourceEdgeArtifact],
    review_evidence: BenchmarkLabelReviewEvidence | None,
) -> None:
    if tuple(item.case_id for item in labels.cases) != declaration.cases:
        raise PolicyViolation("benchmark dependency-label inventory does not match the declaration")

    registry_cases = {case.case_id: case for case in registry.cases}
    source_by_case = {source.case_id: source for source in snapshots.cases}
    for case in registry.cases:
        if (
            case.review_status is not labels.review_status
            or case.reviewer_id != labels.reviewer_id
            or case.review_evidence_digest != labels.review_evidence_digest
        ):
            raise PolicyViolation(
                "benchmark registry and detailed dependency-label review states differ"
            )

    reviewed = labels.review_status is LabelReviewStatus.INDEPENDENTLY_REVIEWED
    if reviewed != (review_evidence is not None):
        raise PolicyViolation("benchmark label-review evidence availability is inconsistent")
    if review_evidence is not None:
        expected_cases = tuple(item.case_id for item in labels.cases)
        if (
            review_evidence.registry_id != registry.registry_id
            or review_evidence.reviewer_id != labels.reviewer_id
            or review_evidence.accepted_case_ids != expected_cases
            or review_evidence.high_impact_definition_id
            != labels.high_impact_definition.definition_id
            or review_evidence.review_subject_digest
            != _label_review_subject_digest(registry, labels)
        ):
            raise PolicyViolation("benchmark label-review evidence belongs to another label set")
    for case_labels in labels.cases:
        source = source_by_case[case_labels.case_id]
        if case_labels.source_edge_labels_path != source.source_edge_labels_path:
            raise PolicyViolation("benchmark dependency labels reference another source-edge file")
        dependency_by_id = {
            dependency.dependency_id: dependency
            for dependency in registry_cases[case_labels.case_id].dependencies
        }
        label_ids = tuple(label.dependency_id for label in case_labels.labels)
        if len(label_ids) != len(set(label_ids)) or set(label_ids) != set(dependency_by_id):
            raise PolicyViolation("benchmark dependency labels do not match the registry case")
        declared_edges = set(source_edges[case_labels.case_id].edges)
        labeled_edges = {label.edge for label in case_labels.labels}
        if len(labeled_edges) != len(case_labels.labels) or labeled_edges != declared_edges:
            raise PolicyViolation(
                "benchmark dependency labels must cover the source-edge file exactly once"
            )
        for label in case_labels.labels:
            dependency = dependency_by_id[label.dependency_id]
            expected_impact = labels.high_impact_definition.basis_outcomes.get(label.impact_basis)
            if expected_impact is None or expected_impact is not label.high_impact:
                raise PolicyViolation(
                    "benchmark dependency impact differs from its frozen basis policy"
                )
            if dependency.high_impact is not label.high_impact:
                raise PolicyViolation("benchmark dependency impact label differs from the registry")
            if dependency.evidence_digest != artifact_digest(label):
                raise PolicyViolation(
                    "benchmark dependency evidence digest differs from the registry"
                )
            if label.edge not in declared_edges:
                raise PolicyViolation(
                    "benchmark dependency label is absent from its source-edge file"
                )


def _verify_runtime_bindings(
    root: Path,
    registry: MeasuredEvaluationRegistry,
    snapshots: _BenchmarkSourceSnapshots,
    runtime: _BenchmarkRuntimeBindings,
) -> Revision:
    runtime_payload = runtime.model_dump(mode="json")
    agent_payload = runtime_payload["agent_definitions"]
    prompt_payload = runtime_payload["prompt_policy"]
    validation_payload = runtime_payload["validation_policy"]
    for config in registry.configurations:
        if config.provider_id != runtime.provider_id or config.model_id != runtime.model_id:
            raise PolicyViolation("benchmark runtime identity differs from its configuration")
        if (
            config.agent_definitions_digest != artifact_digest(agent_payload)
            or config.prompt_policy_digest != artifact_digest(prompt_payload)
            or config.validation_policy_digest != artifact_digest(validation_payload)
        ):
            raise PolicyViolation("benchmark runtime policy does not match its configuration")

    agent_registry = load_agent_registry(root / "agents")
    current_agents = {
        role.value: {
            "version": agent_registry.get(role).version,
            "definition_digest": agent_registry.get(role).definition_digest,
        }
        for role in AgentRole
    }
    if current_agents != agent_payload:
        raise PolicyViolation("benchmark agent definitions drifted from the predeclared runtime")
    expected_agent_digests = {
        role: values["definition_digest"] for role, values in current_agents.items()
    }
    if runtime.prompt_policy.agent_definition_digests != expected_agent_digests:
        raise PolicyViolation("benchmark prompt policy does not bind the current agents")

    scenario_ids = tuple(snapshot.scenario_id for snapshot in snapshots.cases)
    current_scenario_digests: dict[str, Sha256Digest] = {}
    current_validation: dict[str, dict[str, Any]] = {}
    for scenario_id in scenario_ids:
        scenario = migration_scenario_by_id(scenario_id)
        recipe = migration_recipe(scenario.recipe_id)
        current_scenario_digests[scenario_id] = scenario.definition_digest
        current_validation[scenario_id] = {
            "definition_digest": scenario.definition_digest,
            "scope_policy_digest": scenario.scope_policy_digest,
            "allowed_validation_command_ids": list(recipe.allowed_validation_command_ids),
        }
    if runtime.prompt_policy.scenario_definition_digests != current_scenario_digests:
        raise PolicyViolation("benchmark prompt scenario definitions drifted from the runtime")
    if validation_payload["scenarios"] != current_validation:
        raise PolicyViolation("benchmark validation policy drifted from the current scenarios")

    conditions = runtime.environment_conditions
    if tuple(condition.case_id for condition in conditions) != ("mulesoft-customer-status-simple",):
        raise PolicyViolation("benchmark environment conditions must bind the Mule case")
    condition = conditions[0]
    mule_case = next(case for case in registry.cases if case.case_id == condition.case_id)
    if (
        mule_case.expected_disposition is not WorkflowDisposition.ENVIRONMENT_UNAVAILABLE
        or condition.expected_disposition != mule_case.expected_disposition.value
    ):
        raise PolicyViolation("Mule environment condition differs from its expected disposition")
    authority_bytes = _read_benchmark_bytes(root, condition.evidence_path)
    if _bytes_digest(authority_bytes) != condition.evidence_file_digest:
        raise PolicyViolation("Mule runtime authority evidence drifted")
    authority = _validate_json_model(
        _UnavailableMuleRuntimeAuthority,
        _decode_json(authority_bytes, condition.evidence_path),
    )
    del authority  # The strict Literal[False] contract is the observed capability check.

    catalog_payload = _read_benchmark_json(root, WIKI_CATALOG_PATH)
    del catalog_payload  # Parsing here rejects duplicate keys before Wiki loading.
    wiki_tree_revision = content_revision(root / "knowledge" / "wiki")
    if runtime.prompt_policy.wiki_tree_revision != wiki_tree_revision:
        raise PolicyViolation("benchmark Wiki tree drifted from the predeclared runtime")
    catalog_bytes = _read_benchmark_bytes(root, WIKI_CATALOG_PATH)
    if runtime.prompt_policy.wiki_catalog_file_digest != _bytes_digest(catalog_bytes):
        raise PolicyViolation("benchmark Wiki catalog drifted from the predeclared runtime")
    return wiki_tree_revision


def _verify_risk_seed(
    registry: MeasuredEvaluationRegistry,
    risk_seed: _BenchmarkRiskSeedArtifact,
) -> None:
    risk_case = next((case for case in registry.cases if case.case_id == risk_seed.case_id), None)
    if (
        risk_case is None
        or risk_case.case_id != BENCHMARK_COMPLEX_RISK_CASE_ID
        or risk_case.complexity is not Complexity.COMPLEX
        or risk_case.expected_disposition is not WorkflowDisposition.DECISION_REQUIRED
        or not risk_case.intervention_expected
        or risk_case.expected_intervention_reason_ids != BENCHMARK_RISK_REASONS
        or risk_seed.expected_intervention_reasons != BENCHMARK_RISK_REASONS
        or risk_seed.expected_intervention_reasons != risk_case.expected_intervention_reason_ids
    ):
        raise PolicyViolation("benchmark risk seed does not match the predeclared complex case")


def _label_review_subject_digest(
    registry: MeasuredEvaluationRegistry,
    labels: _BenchmarkDependencyLabels,
) -> Sha256Digest:
    """Bind label substance while excluding the metadata created by its review."""

    return artifact_digest(
        {
            "schema_version": "benchmark-label-review-subject/v1",
            "registry_id": registry.registry_id,
            "case_labels": [
                case.model_dump(
                    mode="json",
                    exclude={"review_status", "reviewer_id", "review_evidence_digest"},
                )
                for case in registry.cases
            ],
            "high_impact_definition": labels.high_impact_definition.model_dump(mode="json"),
            "dependency_labels": [case.model_dump(mode="json") for case in labels.cases],
        }
    )


def _validate_json_model(model_type: type[Any], payload: Any) -> Any:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return model_type.model_validate_json(encoded)


def _require_digest(value: Any, expected: Sha256Digest, message: str) -> None:
    if artifact_digest(value) != expected:
        raise PolicyViolation(message)


def _bytes_digest(value: bytes) -> Sha256Digest:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _read_benchmark_json(project_root: Path, relative_path: str) -> Any:
    return _decode_json(_read_benchmark_bytes(project_root, relative_path), relative_path)


def _decode_json(raw: bytes, relative_path: str) -> Any:
    normalized = validate_relative_path(relative_path)

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise PolicyViolation(
                    f"benchmark artifact contains duplicate JSON object key {key!r}: {normalized}"
                )
            value[key] = item
        return value

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except PolicyViolation:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation(f"benchmark artifact is not valid JSON: {normalized}") from exc


def _read_benchmark_bytes(project_root: Path, relative_path: str) -> bytes:
    """Read one regular file without following any path-component symlink."""

    normalized = validate_relative_path(relative_path)
    parts = normalized.split("/")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(
            os,
            "O_NOFOLLOW",
            0,
        )
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        try:
            directory = os.open(project_root, directory_flags)
            descriptors.append(directory)
            for part in parts[:-1]:
                metadata = os.stat(part, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise PolicyViolation(
                        f"benchmark artifact path contains a non-directory: {normalized}"
                    )
                directory = os.open(part, directory_flags, dir_fd=directory)
                descriptors.append(directory)
            descriptor = os.open(parts[-1], file_flags, dir_fd=directory)
            descriptors.append(descriptor)
        except OSError as exc:
            raise PolicyViolation(
                f"benchmark artifact is not a safe regular file: {normalized}"
            ) from exc
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PolicyViolation(f"benchmark artifact is not a regular file: {normalized}")
        chunks: list[bytes] = []
        try:
            while chunk := os.read(descriptor, 64 * 1024):
                chunks.append(chunk)
        except OSError as exc:
            raise PolicyViolation(f"benchmark artifact could not be read: {normalized}") from exc
        return b"".join(chunks)
    finally:
        for open_descriptor in reversed(descriptors):
            try:
                os.close(open_descriptor)
            except OSError:
                pass


__all__ = [
    "BenchmarkDependencyEdgeLabel",
    "VerifiedBenchmarkProtocol",
    "load_verified_benchmark_protocol",
    "require_independently_reviewed_benchmark_protocol",
]
