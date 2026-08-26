"""Platform-separated evaluation of dependency graphs against bounded labels.

Label files are useful test oracles, not runtime planning authority.  Reports
preserve whether a label set has independent human review so an initial fixture
cannot be described as an expert-reviewed benchmark merely because recall is
high.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from legacy_migration_agent.contracts import Identifier, Platform, Sha256Digest, StrictModel
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.graphs.graph_contracts import DependencyGraph, EdgeKind

MAX_LABEL_FILE_BYTES = 2 * 1024 * 1024
GraphLabelReviewStatus = Literal[
    "unreviewed_initial",
    "initial_label_set",
    "independently_reviewed",
]


class GraphEdgeLabel(StrictModel):
    """One expected resolved edge, expressed in platform-visible names."""

    kind: EdgeKind
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)
    symbol: str | None = Field(default=None, max_length=500)


class GraphLabelSet(StrictModel):
    """Versioned fixture labels with an explicit human-review status."""

    schema_version: Literal["1.0"] = "1.0"
    fixture_id: Identifier
    platform: Platform
    graph: Literal["source", "target"]
    review_status: GraphLabelReviewStatus
    label_file_digest: Sha256Digest
    edges: tuple[GraphEdgeLabel, ...] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def require_unique_edges(self) -> GraphLabelSet:
        if len(self.edges) != len(set(self.edges)):
            raise ValueError("dependency graph labels must be unique")
        return self


class GraphMetricCounts(StrictModel):
    expected: int = Field(ge=1)
    discovered: int = Field(ge=0)
    true_positive: int = Field(ge=0)
    false_negative: int = Field(ge=0)
    false_positive: int = Field(ge=0)
    recall: float = Field(ge=0, le=1)
    precision: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_counts(self) -> GraphMetricCounts:
        if self.true_positive + self.false_negative != self.expected:
            raise ValueError("dependency recall counts do not reconcile")
        if self.true_positive + self.false_positive != self.discovered:
            raise ValueError("dependency precision counts do not reconcile")
        expected_recall = self.true_positive / self.expected
        expected_precision = self.true_positive / self.discovered if self.discovered else 0.0
        if self.recall != expected_recall or self.precision != expected_precision:
            raise ValueError("dependency metric rates do not match their counts")
        return self


class GraphEvaluationReport(StrictModel):
    """Exact fixture result without overstating its label-review authority."""

    schema_version: Literal["1.0"] = "1.0"
    report_id: Identifier
    fixture_id: Identifier
    platform: Platform
    graph_kind: Literal["source", "target"]
    graph_digest: Sha256Digest
    graph_revision: str = Field(min_length=7, max_length=160)
    label_set_digest: Sha256Digest
    label_file_digest: Sha256Digest
    label_review_status: GraphLabelReviewStatus
    claim_scope: Literal["exploratory_unreviewed", "independently_reviewed_fixture"]
    metrics: GraphMetricCounts
    missing_edges: tuple[GraphEdgeLabel, ...]
    unexpected_edges: tuple[GraphEdgeLabel, ...]
    exit_gate_eligible: bool

    @model_validator(mode="after")
    def prevent_review_overclaim(self) -> GraphEvaluationReport:
        reviewed = self.label_review_status == "independently_reviewed"
        expected_scope = "independently_reviewed_fixture" if reviewed else "exploratory_unreviewed"
        if self.claim_scope != expected_scope:
            raise ValueError("graph report claim exceeds its label review status")
        expected_gate = reviewed and not self.missing_edges and not self.unexpected_edges
        if self.exit_gate_eligible is not expected_gate:
            raise ValueError("graph exit eligibility does not match exact reviewed evidence")
        return self


def load_graph_label_set(path: Path, *, platform: Platform) -> GraphLabelSet:
    """Load one regular bounded label file without following a symlink."""

    payload, content_digest = _read_label_file(path)
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation("dependency label file is not valid bounded JSON") from exc
    if not isinstance(raw, dict):
        raise PolicyViolation("dependency label file must contain a JSON object")
    declared_platform = raw.get("platform", platform.value)
    review_status = raw.get("review_status", "unreviewed_initial")
    normalized = {
        **raw,
        "platform": declared_platform,
        "review_status": review_status,
        "label_file_digest": content_digest,
    }
    try:
        labels = GraphLabelSet.model_validate(normalized)
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("dependency label file has an invalid contract") from exc
    if labels.platform is not platform:
        raise PolicyViolation("dependency label platform does not match its selected stratum")
    return labels


def evaluate_dependency_graph(
    graph: DependencyGraph,
    labels: GraphLabelSet,
) -> GraphEvaluationReport:
    """Derive exact recall and precision for one platform and fixture."""

    if graph.platform is not labels.platform:
        raise PolicyViolation("dependency graph and label set use different platforms")
    nodes = {node.node_id: node.name for node in graph.nodes}
    discovered = {
        GraphEdgeLabel(
            kind=edge.kind,
            source=nodes[edge.source_id],
            target=nodes[edge.target_id],
            symbol=edge.symbol,
        )
        for edge in graph.edges
        if edge.resolved
    }
    expected = set(labels.edges)
    missing = tuple(sorted(expected - discovered, key=_edge_key))
    unexpected = tuple(sorted(discovered - expected, key=_edge_key))
    true_positive = len(expected & discovered)
    metrics = GraphMetricCounts(
        expected=len(expected),
        discovered=len(discovered),
        true_positive=true_positive,
        false_negative=len(missing),
        false_positive=len(unexpected),
        recall=true_positive / len(expected),
        precision=true_positive / len(discovered) if discovered else 0.0,
    )
    reviewed = labels.review_status == "independently_reviewed"
    values = {
        "fixture_id": labels.fixture_id,
        "graph_digest": artifact_digest(graph),
        "label_set_digest": artifact_digest(labels),
        "metrics": metrics.model_dump(mode="json"),
    }
    return GraphEvaluationReport(
        report_id=("graph-evaluation-" + artifact_digest(values).removeprefix("sha256:")[:32]),
        fixture_id=labels.fixture_id,
        platform=labels.platform,
        graph_kind=labels.graph,
        graph_digest=artifact_digest(graph),
        graph_revision=graph.base_revision,
        label_set_digest=artifact_digest(labels),
        label_file_digest=labels.label_file_digest,
        label_review_status=labels.review_status,
        claim_scope=("independently_reviewed_fixture" if reviewed else "exploratory_unreviewed"),
        metrics=metrics,
        missing_edges=missing,
        unexpected_edges=unexpected,
        exit_gate_eligible=reviewed and not missing and not unexpected,
    )


def _read_label_file(path: Path) -> tuple[bytes, Sha256Digest]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PolicyViolation("dependency label file does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PolicyViolation("dependency label path must be a regular non-symlink file")
    if metadata.st_size > MAX_LABEL_FILE_BYTES:
        raise PolicyViolation("dependency label file exceeds the size limit")
    descriptor = os.open(path, os.O_RDONLY | _no_follow_flag())
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (metadata.st_dev, metadata.st_ino):
            raise PolicyViolation("dependency label file changed while it was opened")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, 64 * 1024):
            total += len(chunk)
            if total > MAX_LABEL_FILE_BYTES:
                raise PolicyViolation("dependency label file exceeds the size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise PolicyViolation("dependency label file changed while it was read")
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    return payload, f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _edge_key(edge: GraphEdgeLabel) -> tuple[str, str, str, str]:
    return edge.kind.value, edge.source, edge.target, edge.symbol or ""


def _no_follow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as exc:  # pragma: no cover - supported target is POSIX
        raise RuntimeError("dependency label loading requires O_NOFOLLOW") from exc


__all__ = [
    "GraphEdgeLabel",
    "GraphEvaluationReport",
    "GraphLabelSet",
    "GraphMetricCounts",
    "evaluate_dependency_graph",
    "load_graph_label_set",
]
