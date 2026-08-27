"""Deterministic JSON Schema snapshots for public workflow contracts.

The checked-in snapshots are an explicit review boundary. A compatible schema
change still requires an intentional snapshot refresh, while an incompatible
change requires a new contract version rather than silently replacing the
existing versioned snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from legacy_migration_agent.agent_runtime.agent_definitions import AgentDefinition
from legacy_migration_agent.agent_runtime.correction import CorrectionApproval, CorrectionRequest
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectManifestProposal,
    EngineerModelOutcome,
    ValidatorModelAdvisory,
)
from legacy_migration_agent.agent_runtime.openai_model import LiveModelApproval, ModelCallRecord
from legacy_migration_agent.application.agent_run import AgentRunConfig, AgentRunStatus
from legacy_migration_agent.application.final_review import (
    FinalReviewDecision,
    FinalReviewRecord,
    FinalReviewRequest,
    FinalReviewStatus,
)
from legacy_migration_agent.contracts import (
    ChangeSet,
    DecisionRequest,
    ImplementationIntervention,
    MigrationManifest,
    MigrationRequest,
    PlanningIntervention,
    ToolReceipt,
    ValidationReport,
)
from legacy_migration_agent.evaluation import (
    EvaluationRegistry,
    EvaluationResults,
    EvaluationVerification,
    PilotEvaluationRegistry,
    PilotEvaluationResults,
    PilotEvaluationVerification,
    PilotEvidenceReceipt,
)
from legacy_migration_agent.graphs.graph_contracts import DependencyGraph
from legacy_migration_agent.graphs.graph_evaluation import GraphEvaluationReport, GraphLabelSet
from legacy_migration_agent.graphs.graph_store import StoredGraphSnapshot
from legacy_migration_agent.knowledge.wiki import RetrievalTrace, WikiCatalog
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    MuleSoftCandidateValidationSummary,
)
from legacy_migration_agent.platforms.mulesoft_validation import (
    MuleSoftValidationContext,
    MuleSoftValidationEvidence,
    MUnitLocalResult,
)
from legacy_migration_agent.platforms.salesforce_validation import (
    SalesforceValidationContext,
    SalesforceValidationEvidence,
)
from legacy_migration_agent.workflow import ManifestApproval

PUBLIC_SCHEMA_RELEASE = "v2.0"

PUBLIC_SCHEMA_MODELS: tuple[type[BaseModel], ...] = (
    # Human-gated workflow artifacts.
    MigrationRequest,
    ImplementationIntervention,
    PlanningIntervention,
    MigrationManifest,
    ManifestApproval,
    ChangeSet,
    ValidationReport,
    CorrectionRequest,
    CorrectionApproval,
    DecisionRequest,
    ToolReceipt,
    # Agent definitions, public run state, and provider consent/telemetry.
    AgentDefinition,
    AgentRunConfig,
    AgentRunStatus,
    LiveModelApproval,
    ModelCallRecord,
    # The three model-authored handoffs.
    ArchitectManifestProposal,
    EngineerModelOutcome,
    ValidatorModelAdvisory,
    # Dependency graph inputs, storage, labels, and evaluation evidence.
    DependencyGraph,
    StoredGraphSnapshot,
    GraphLabelSet,
    GraphEvaluationReport,
    # Exact final-human-review lifecycle.
    FinalReviewRequest,
    FinalReviewDecision,
    FinalReviewRecord,
    FinalReviewStatus,
    # LLM Wiki catalog and retrieval evidence.
    WikiCatalog,
    RetrievalTrace,
    # Salesforce and MuleSoft normalized validation evidence boundaries.
    SalesforceValidationContext,
    SalesforceValidationEvidence,
    MuleSoftValidationContext,
    MUnitLocalResult,
    MuleSoftValidationEvidence,
    MuleSoftCandidateValidationSummary,
    # Compact, honest evaluation protocol and current result boundary.
    EvaluationRegistry,
    EvaluationResults,
    EvaluationVerification,
    PilotEvaluationRegistry,
    PilotEvaluationResults,
    PilotEvidenceReceipt,
    PilotEvaluationVerification,
)


@dataclass(frozen=True)
class CompatibilityIssue:
    """One reason a current schema may reject a baseline-valid document."""

    path: str
    rule: str
    detail: str

    def describe(self) -> str:
        return f"{self.path}: {self.rule}: {self.detail}"


@dataclass(frozen=True)
class SnapshotCheck:
    """Comparison of one generated schema with its versioned snapshot."""

    model_name: str
    snapshot_path: Path
    exact_match: bool
    compatibility_issues: tuple[CompatibilityIssue, ...]


def schema_filename(model: type[BaseModel]) -> str:
    """Return the stable filename for a public Pydantic contract."""

    return f"{model.__name__}.schema.json"


def canonical_schema_text(schema: Mapping[str, Any]) -> str:
    """Serialize a JSON Schema deterministically for review and hashing."""

    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def generated_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Generate a public contract schema with Pydantic's validation dialect."""

    return model.model_json_schema(mode="validation")


def write_schema_snapshots(snapshot_dir: Path) -> tuple[Path, ...]:
    """Write all public schemas in stable model order.

    This function is intentionally explicit and is not called by tests. A
    snapshot refresh is a review action, never an automatic consequence of a
    model edit.
    """

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for model in PUBLIC_SCHEMA_MODELS:
        destination = snapshot_dir / schema_filename(model)
        destination.write_text(
            canonical_schema_text(generated_schema(model)),
            encoding="utf-8",
        )
        written.append(destination)
    return tuple(written)


def check_schema_snapshots(snapshot_dir: Path) -> tuple[SnapshotCheck, ...]:
    """Compare current schemas with the complete versioned snapshot set."""

    expected_names = {schema_filename(model) for model in PUBLIC_SCHEMA_MODELS}
    actual_names = {path.name for path in snapshot_dir.glob("*.schema.json")}
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        raise ValueError("schema snapshot inventory mismatch: " + "; ".join(details))

    results: list[SnapshotCheck] = []
    for model in PUBLIC_SCHEMA_MODELS:
        snapshot_path = snapshot_dir / schema_filename(model)
        baseline, snapshot_text = _load_schema(snapshot_path)
        current = generated_schema(model)
        results.append(
            SnapshotCheck(
                model_name=model.__name__,
                snapshot_path=snapshot_path,
                exact_match=snapshot_text == canonical_schema_text(current),
                compatibility_issues=find_backward_incompatibilities(
                    baseline,
                    current,
                ),
            )
        )
    return tuple(results)


def find_backward_incompatibilities(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
) -> tuple[CompatibilityIssue, ...]:
    """Find common schema changes that reject baseline-valid documents.

    The comparison covers the constructs emitted by this project's Pydantic
    models: references, object properties, required fields, primitive types,
    constants/enums, unions, array items, and scalar bounds. Exact snapshot
    matching remains the drift gate; this analysis gives actionable evidence
    when the drift narrows the accepted artifact contract.
    """

    issues: list[CompatibilityIssue] = []
    _compare_schema(
        baseline,
        current,
        baseline_root=baseline,
        current_root=current,
        path="$",
        issues=issues,
        visited=set(),
    )
    return tuple(issues)


def _load_schema(path: Path) -> tuple[dict[str, Any], str]:
    try:
        source = path.read_text(encoding="utf-8")
        payload = json.loads(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid schema snapshot {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"schema snapshot must contain a JSON object: {path}")
    if not all(isinstance(key, str) for key in payload):
        raise ValueError(f"schema snapshot keys must be strings: {path}")
    return payload, source


def _compare_schema(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    baseline_root: Mapping[str, Any],
    current_root: Mapping[str, Any],
    path: str,
    issues: list[CompatibilityIssue],
    visited: set[tuple[int, int]],
) -> None:
    baseline = _resolve_ref(baseline, baseline_root)
    current = _resolve_ref(current, current_root)
    identity = (id(baseline), id(current))
    if identity in visited:
        return
    visited.add(identity)

    baseline_types = _as_string_set(baseline.get("type"))
    current_types = _as_string_set(current.get("type"))
    if not baseline_types and current_types:
        issues.append(
            CompatibilityIssue(
                path,
                "type-constraint-added",
                f"current={sorted(current_types)!r}",
            )
        )
    elif baseline_types and current_types and not baseline_types <= current_types:
        issues.append(
            CompatibilityIssue(
                path,
                "type-narrowed",
                f"baseline={sorted(baseline_types)!r}, current={sorted(current_types)!r}",
            )
        )

    if "const" in current and baseline.get("const") != current["const"]:
        issues.append(
            CompatibilityIssue(
                path,
                "constant-changed",
                f"baseline={baseline.get('const')!r}, current={current['const']!r}",
            )
        )

    baseline_enum = _as_value_sequence(baseline.get("enum"))
    current_enum = _as_value_sequence(current.get("enum"))
    if baseline_enum is None and current_enum is not None:
        issues.append(
            CompatibilityIssue(path, "enum-constraint-added", f"current={current_enum!r}")
        )
    elif baseline_enum is not None and current_enum is not None:
        removed = [value for value in baseline_enum if value not in current_enum]
        if removed:
            issues.append(CompatibilityIssue(path, "enum-narrowed", f"removed values={removed!r}"))

    _compare_object(
        baseline,
        current,
        baseline_root=baseline_root,
        current_root=current_root,
        path=path,
        issues=issues,
        visited=visited,
    )
    _compare_compositions(
        baseline,
        current,
        baseline_root=baseline_root,
        current_root=current_root,
        path=path,
        issues=issues,
    )
    _compare_items(
        baseline,
        current,
        baseline_root=baseline_root,
        current_root=current_root,
        path=path,
        issues=issues,
        visited=visited,
    )
    _compare_bounds(baseline, current, path=path, issues=issues)


def _compare_object(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    baseline_root: Mapping[str, Any],
    current_root: Mapping[str, Any],
    path: str,
    issues: list[CompatibilityIssue],
    visited: set[tuple[int, int]],
) -> None:
    baseline_required = _as_string_set(baseline.get("required"))
    current_required = _as_string_set(current.get("required"))
    for field in sorted(current_required - baseline_required):
        issues.append(
            CompatibilityIssue(
                f"{path}.properties.{field}",
                "required-field-added",
                "field was not required by the baseline schema",
            )
        )

    baseline_properties = _as_schema_mapping(baseline.get("properties"))
    current_properties = _as_schema_mapping(current.get("properties"))
    current_additional = current.get("additionalProperties", True)
    for name, baseline_property in baseline_properties.items():
        property_path = f"{path}.properties.{name}"
        current_property = current_properties.get(name)
        if current_property is None:
            if current_additional is False:
                issues.append(
                    CompatibilityIssue(
                        property_path,
                        "property-removed",
                        "baseline property is rejected by additionalProperties=false",
                    )
                )
            continue
        _compare_schema(
            baseline_property,
            current_property,
            baseline_root=baseline_root,
            current_root=current_root,
            path=property_path,
            issues=issues,
            visited=visited,
        )

    baseline_additional = baseline.get("additionalProperties", True)
    if baseline_additional is not False and current_additional is False:
        issues.append(
            CompatibilityIssue(
                path,
                "additional-properties-forbidden",
                "current schema forbids properties accepted by the baseline",
            )
        )


def _compare_compositions(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    baseline_root: Mapping[str, Any],
    current_root: Mapping[str, Any],
    path: str,
    issues: list[CompatibilityIssue],
) -> None:
    for keyword in ("anyOf", "oneOf"):
        baseline_branches = _as_schema_sequence(baseline.get(keyword))
        current_branches = _as_schema_sequence(current.get(keyword))
        if not baseline_branches:
            if current_branches:
                issues.append(
                    CompatibilityIssue(
                        path,
                        f"{keyword}-constraint-added",
                        "current schema introduced a union constraint",
                    )
                )
            continue
        if not current_branches:
            continue
        for index, baseline_branch in enumerate(baseline_branches):
            if not any(
                not _branch_issues(
                    baseline_branch,
                    current_branch,
                    baseline_root,
                    current_root,
                    f"{path}.{keyword}[{index}]",
                )
                for current_branch in current_branches
            ):
                issues.append(
                    CompatibilityIssue(
                        f"{path}.{keyword}[{index}]",
                        "union-branch-removed",
                        "no compatible current branch accepts this baseline branch",
                    )
                )


def _branch_issues(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    baseline_root: Mapping[str, Any],
    current_root: Mapping[str, Any],
    path: str,
) -> tuple[CompatibilityIssue, ...]:
    branch_issues: list[CompatibilityIssue] = []
    _compare_schema(
        baseline,
        current,
        baseline_root=baseline_root,
        current_root=current_root,
        path=path,
        issues=branch_issues,
        visited=set(),
    )
    return tuple(branch_issues)


def _compare_items(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    baseline_root: Mapping[str, Any],
    current_root: Mapping[str, Any],
    path: str,
    issues: list[CompatibilityIssue],
    visited: set[tuple[int, int]],
) -> None:
    baseline_items = baseline.get("items")
    current_items = current.get("items")
    if isinstance(baseline_items, dict) and isinstance(current_items, dict):
        _compare_schema(
            baseline_items,
            current_items,
            baseline_root=baseline_root,
            current_root=current_root,
            path=f"{path}.items",
            issues=issues,
            visited=visited,
        )


def _compare_bounds(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    path: str,
    issues: list[CompatibilityIssue],
) -> None:
    for keyword in (
        "minimum",
        "exclusiveMinimum",
        "minLength",
        "minItems",
        "minProperties",
    ):
        _compare_numeric_bound(
            baseline,
            current,
            keyword=keyword,
            path=path,
            issues=issues,
            narrows=lambda old, new: new > old,
        )
    for keyword in (
        "maximum",
        "exclusiveMaximum",
        "maxLength",
        "maxItems",
        "maxProperties",
    ):
        _compare_numeric_bound(
            baseline,
            current,
            keyword=keyword,
            path=path,
            issues=issues,
            narrows=lambda old, new: new < old,
        )
    for keyword in ("pattern", "format"):
        if keyword in current and baseline.get(keyword) != current[keyword]:
            issues.append(
                CompatibilityIssue(
                    path,
                    f"{keyword}-changed",
                    f"baseline={baseline.get(keyword)!r}, current={current[keyword]!r}",
                )
            )


def _compare_numeric_bound(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    keyword: str,
    path: str,
    issues: list[CompatibilityIssue],
    narrows: Any,
) -> None:
    if keyword not in current:
        return
    current_value = current[keyword]
    baseline_value = baseline.get(keyword)
    if baseline_value is None or (
        isinstance(baseline_value, (int, float))
        and isinstance(current_value, (int, float))
        and narrows(baseline_value, current_value)
    ):
        issues.append(
            CompatibilityIssue(
                path,
                f"{keyword}-narrowed",
                f"baseline={baseline_value!r}, current={current_value!r}",
            )
        )


def _resolve_ref(
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
) -> Mapping[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    prefix = "#/$defs/"
    if not reference.startswith(prefix):
        return schema
    name = reference[len(prefix) :].replace("~1", "/").replace("~0", "~")
    definitions = root.get("$defs")
    if not isinstance(definitions, dict):
        return schema
    resolved = definitions.get(name)
    return resolved if isinstance(resolved, dict) else schema


def _as_string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    return set()


def _as_value_sequence(value: Any) -> Sequence[Any] | None:
    return value if isinstance(value, list) else None


def _as_schema_mapping(value: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {
        key: schema
        for key, schema in value.items()
        if isinstance(key, str) and isinstance(schema, dict)
    }


def _as_schema_sequence(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(schema for schema in value if isinstance(schema, dict))
