"""Controller-owned bounded assurance for revision-bound dependency graphs.

This module is deliberately independent of model inference, benchmark labels,
and golden outputs.  It reconciles a captured source snapshot with one static
dependency graph using a small second-pass inventory for the supported
Salesforce and MuleSoft capstone scenarios.  An ``assured`` result is therefore
bounded evidence about those scenarios, never proof that every dynamic or
external dependency in an arbitrary repository is discoverable.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, deque
from enum import StrEnum
from typing import Literal
from xml.etree import ElementTree

from pydantic import Field, field_validator, model_validator
from pydantic_core import to_jsonable_python

from legacy_migration_agent.contracts import (
    Platform,
    Sha256Digest,
    StrictModel,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.workspace import TreeSnapshot
from legacy_migration_agent.graphs.graph_contracts import (
    DependencyGraph,
    EdgeKind,
    WarningCode,
)

GRAPH_ASSURANCE_SCHEMA_VERSION: Literal["1.0"] = "1.0"
GRAPH_ASSURANCE_POLICY_VERSION: Literal["bounded-graph-assurance/1.0"] = (
    "bounded-graph-assurance/1.0"
)

_ANALYZER_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}$")
_PROPERTY_REFERENCE = re.compile(r"\$\{\s*([^{}]+?)\s*\}")
_VARIABLE_REFERENCE = re.compile(
    r"\b(?:flowVars|vars)\s*(?:\.\s*([A-Za-z_]\w*)|\[\s*['\"]([^'\"]+)['\"]\s*\])"
)
_DATAWEAVE_IMPORT = re.compile(r"(?m)^\s*import\b[^\n]*?\bfrom\s+([A-Za-z_]\w*(?:::[A-Za-z_]\w*)+)")
_DYNAMIC_TARGET = re.compile(r"#\[|\$\{")
_XML_GUARD = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)

AssuranceConstructCategory = Literal[
    "dynamic",
    "reflective",
    "malformed",
    "external",
    "ambiguous",
    "unresolved",
]


class GraphAssuranceStatus(StrEnum):
    """Controller disposition for one deterministic assurance report."""

    ASSURED = "assured"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"


class SourceCoverageRole(StrEnum):
    DEPENDENCY_BEARING = "dependency_bearing"
    CONTEXT_ONLY = "context_only"


class AssuranceIssueSeverity(StrEnum):
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"


class AssuranceSourceDigest(StrictModel):
    """Digest of one controller-required source, or explicit missing evidence."""

    path: str
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class ParserSourceCoverage(StrictModel):
    """Per-source reconciliation between the snapshot and selected graph."""

    path: str
    role: SourceCoverageRole
    parser: str = Field(min_length=1, max_length=80)
    source_present: bool
    represented_in_graph: bool
    digest_matches_snapshot: bool
    reachable_from_entry: bool
    complete: bool

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @model_validator(mode="after")
    def validate_complete_flag(self) -> ParserSourceCoverage:
        expected = (
            self.source_present
            and self.digest_matches_snapshot
            and (
                self.role is SourceCoverageRole.CONTEXT_ONLY
                or (self.represented_in_graph and self.reachable_from_entry)
            )
        )
        if self.complete != expected:
            raise ValueError("source coverage complete flag does not match its evidence")
        return self


class UnsupportedOrAmbiguousConstruct(StrictModel):
    """One construct that bounded static reconciliation cannot safely resolve."""

    construct_id: str = Field(min_length=1, max_length=160)
    category: Literal[
        "dynamic",
        "reflective",
        "malformed",
        "external",
        "ambiguous",
        "unresolved",
    ]
    severity: AssuranceIssueSeverity
    path: str
    line: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=1000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class GraphAssuranceDiscrepancy(StrictModel):
    """One deterministic mismatch that prevents an assured result."""

    discrepancy_id: str = Field(min_length=1, max_length=160)
    category: Literal[
        "revision_mismatch",
        "missing_source",
        "source_digest_mismatch",
        "source_not_in_graph",
        "unreachable_source",
        "orphan_provenance",
        "reference_count_mismatch",
        "internal_inconsistency",
        "security_coverage_gap",
    ]
    path: str | None = None
    summary: str = Field(min_length=1, max_length=1000)
    expected_count: int | None = Field(default=None, ge=0)
    observed_count: int | None = Field(default=None, ge=0)

    @field_validator("path")
    @classmethod
    def validate_optional_path(cls, value: str | None) -> str | None:
        return None if value is None else validate_relative_path(value)

    @model_validator(mode="after")
    def validate_counts(self) -> GraphAssuranceDiscrepancy:
        if (self.expected_count is None) is not (self.observed_count is None):
            raise ValueError("discrepancy counts must be supplied together")
        return self


class ReferenceInventoryCount(StrictModel):
    """Independent supported-reference count reconciled with graph evidence."""

    inventory_id: str = Field(min_length=1, max_length=160)
    path: str
    edge_kind: EdgeKind
    symbol: str = Field(min_length=1, max_length=500)
    source_count: int = Field(ge=0)
    graph_count: int = Field(ge=0)
    reconciled: bool

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @model_validator(mode="after")
    def validate_reconciled(self) -> ReferenceInventoryCount:
        if self.reconciled != (self.source_count == self.graph_count):
            raise ValueError("reference inventory reconciliation flag is inconsistent")
        return self


class SecuritySensitiveDependencyCoverage(StrictModel):
    """Controller heuristic for high-impact graph relationships, not an oracle score."""

    policy_id: Literal["bounded-security-impact/1.0"] = "bounded-security-impact/1.0"
    required_edge_kinds: tuple[EdgeKind, ...]
    observed_references: int = Field(ge=0)
    covered_references: int = Field(ge=0)
    unresolved_references: int = Field(ge=0)
    complete: bool

    @model_validator(mode="after")
    def validate_counts(self) -> SecuritySensitiveDependencyCoverage:
        if self.covered_references + self.unresolved_references != self.observed_references:
            raise ValueError("security-sensitive dependency counts do not reconcile")
        if self.complete != (self.unresolved_references == 0):
            raise ValueError("security-sensitive coverage flag is inconsistent")
        if tuple(sorted(set(self.required_edge_kinds), key=lambda item: item.value)) != (
            self.required_edge_kinds
        ):
            raise ValueError("security-sensitive edge kinds must be unique and sorted")
        return self


class GraphAssuranceReport(StrictModel):
    """Strict report binding bounded reconciliation to one graph and revision."""

    schema_version: Literal["1.0"] = GRAPH_ASSURANCE_SCHEMA_VERSION
    policy_version: Literal["bounded-graph-assurance/1.0"] = GRAPH_ASSURANCE_POLICY_VERSION
    report_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^graph-assurance-[0-9a-f]{32}$",
    )
    platform: Platform
    source_revision: Sha256Digest
    dependency_graph_digest: Sha256Digest
    analyzer_version: str = Field(min_length=1, max_length=160)
    entry_paths: tuple[str, ...] = Field(min_length=1)
    source_digests: tuple[AssuranceSourceDigest, ...] = Field(min_length=1)
    parser_source_coverage: tuple[ParserSourceCoverage, ...] = Field(min_length=1)
    unsupported_or_ambiguous_constructs: tuple[UnsupportedOrAmbiguousConstruct, ...]
    detected_discrepancies: tuple[GraphAssuranceDiscrepancy, ...]
    reference_inventory: tuple[ReferenceInventoryCount, ...]
    security_sensitive_dependency_coverage: SecuritySensitiveDependencyCoverage
    graph_has_unresolved: bool
    status: GraphAssuranceStatus

    @field_validator("analyzer_version")
    @classmethod
    def validate_analyzer_version(cls, value: str) -> str:
        if _ANALYZER_VERSION.fullmatch(value) is None:
            raise ValueError("graph assurance analyzer version is invalid")
        return value

    @field_validator("entry_paths")
    @classmethod
    def validate_entry_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({validate_relative_path(value) for value in values}))
        if not normalized:
            raise ValueError("graph assurance requires at least one entry path")
        return normalized

    @model_validator(mode="after")
    def validate_report(self) -> GraphAssuranceReport:
        source_paths = tuple(item.path for item in self.source_digests)
        coverage_paths = tuple(item.path for item in self.parser_source_coverage)
        if len(source_paths) != len(set(source_paths)):
            raise ValueError("graph assurance source digest paths must be unique")
        if coverage_paths != source_paths:
            raise ValueError("graph assurance coverage must match source digest order")
        if (
            tuple(
                sorted(
                    self.unsupported_or_ambiguous_constructs,
                    key=lambda item: item.construct_id,
                )
            )
            != self.unsupported_or_ambiguous_constructs
        ):
            raise ValueError("graph assurance constructs must be deterministically sorted")
        if (
            tuple(sorted(self.detected_discrepancies, key=lambda item: item.discrepancy_id))
            != self.detected_discrepancies
        ):
            raise ValueError("graph assurance discrepancies must be deterministically sorted")
        if tuple(sorted(self.reference_inventory, key=lambda item: item.inventory_id)) != (
            self.reference_inventory
        ):
            raise ValueError("graph assurance reference inventory must be sorted")
        if len({item.construct_id for item in self.unsupported_or_ambiguous_constructs}) != len(
            self.unsupported_or_ambiguous_constructs
        ):
            raise ValueError("graph assurance construct IDs must be unique")
        if len({item.discrepancy_id for item in self.detected_discrepancies}) != len(
            self.detected_discrepancies
        ):
            raise ValueError("graph assurance discrepancy IDs must be unique")
        expected_status = _derive_status(
            self.detected_discrepancies,
            self.unsupported_or_ambiguous_constructs,
            graph_has_unresolved=self.graph_has_unresolved,
        )
        if self.status is not expected_status:
            raise ValueError("graph assurance status does not match deterministic evidence")
        if self.status is GraphAssuranceStatus.ASSURED:
            if any(not item.complete for item in self.parser_source_coverage):
                raise ValueError("assured graph requires complete source coverage")
            if not self.security_sensitive_dependency_coverage.complete:
                raise ValueError("assured graph requires complete security-sensitive coverage")
        expected_id = _report_id(self.model_dump(mode="json", exclude={"report_id"}))
        if self.report_id != expected_id:
            raise ValueError("graph assurance report ID does not match its content")
        return self


def build_graph_assurance_report(
    snapshot: TreeSnapshot,
    graph: DependencyGraph,
    *,
    analyzer_version: str,
    required_source_paths: tuple[str, ...],
) -> GraphAssuranceReport:
    """Reconcile one captured snapshot and graph without model or oracle access."""

    normalized_required = tuple(validate_relative_path(path) for path in required_source_paths)
    if not normalized_required or len(normalized_required) != len(set(normalized_required)):
        raise ValueError("graph assurance required source paths must be nonempty and unique")
    if _ANALYZER_VERSION.fullmatch(analyzer_version) is None:
        raise ValueError("graph assurance analyzer version is invalid")

    snapshot_entries = snapshot.by_path()
    graph_digests = {item.path: item.sha256 for item in graph.source_digests}
    source_digests = tuple(
        AssuranceSourceDigest(
            path=path,
            sha256=(
                None
                if path not in snapshot_entries
                else hashlib.sha256(snapshot_entries[path].content).hexdigest()
            ),
        )
        for path in normalized_required
    )
    reachable_nodes = _reachable_node_ids(graph)
    reachable_paths = {
        path
        for node in graph.nodes
        if node.node_id in reachable_nodes
        for path in node.metadata_paths
    }

    discrepancies: list[GraphAssuranceDiscrepancy] = []
    if snapshot.revision != graph.base_revision:
        discrepancies.append(
            _discrepancy(
                "revision_mismatch",
                None,
                "Dependency graph revision differs from the captured source revision.",
            )
        )
    graph_source_path_sequence = tuple(item.path for item in graph.source_digests)
    duplicate_graph_sources = sorted(
        path for path, count in Counter(graph_source_path_sequence).items() if count > 1
    )
    for path in duplicate_graph_sources:
        discrepancies.append(
            _discrepancy(
                "internal_inconsistency",
                path,
                "Dependency graph contains duplicate source-digest bindings.",
            )
        )
    for entry_path in graph.entry_paths:
        if entry_path not in graph_digests:
            discrepancies.append(
                _discrepancy(
                    "internal_inconsistency",
                    entry_path,
                    "Dependency graph entry is not bound by a graph source digest.",
                )
            )
    for node in graph.nodes:
        for metadata_path in node.metadata_paths:
            if metadata_path not in graph_digests:
                discrepancies.append(
                    _discrepancy(
                        "internal_inconsistency",
                        metadata_path,
                        "Dependency graph node metadata is not bound by a graph source digest.",
                    )
                )

    coverage: list[ParserSourceCoverage] = []
    for item in source_digests:
        role = _coverage_role(graph.platform, item.path)
        source_present = item.sha256 is not None
        graph_digest = graph_digests.get(item.path)
        represented = graph_digest is not None
        digest_matches = source_present and (
            (graph_digest == item.sha256)
            if represented
            else role is SourceCoverageRole.CONTEXT_ONLY
        )
        reachable = item.path in reachable_paths
        complete = (
            source_present
            and digest_matches
            and (role is SourceCoverageRole.CONTEXT_ONLY or (represented and reachable))
        )
        coverage.append(
            ParserSourceCoverage(
                path=item.path,
                role=role,
                parser=_parser_name(graph.platform, item.path),
                source_present=source_present,
                represented_in_graph=represented,
                digest_matches_snapshot=digest_matches,
                reachable_from_entry=reachable,
                complete=complete,
            )
        )
        if not source_present:
            discrepancies.append(
                _discrepancy("missing_source", item.path, "Required source file is absent.")
            )
        elif represented and graph_digest != item.sha256:
            discrepancies.append(
                _discrepancy(
                    "source_digest_mismatch",
                    item.path,
                    "Graph source digest differs from the captured source bytes.",
                )
            )
        elif role is SourceCoverageRole.DEPENDENCY_BEARING and not represented:
            discrepancies.append(
                _discrepancy(
                    "source_not_in_graph",
                    item.path,
                    "Required dependency-bearing source is not represented in the graph.",
                )
            )
        elif role is SourceCoverageRole.DEPENDENCY_BEARING and not reachable:
            discrepancies.append(
                _discrepancy(
                    "unreachable_source",
                    item.path,
                    "Required dependency-bearing source is unreachable from graph entries.",
                )
            )

    for graph_source_digest in graph.source_digests:
        entry = snapshot_entries.get(graph_source_digest.path)
        if entry is None:
            discrepancies.append(
                _discrepancy(
                    "missing_source",
                    graph_source_digest.path,
                    "Graph source digest names a file absent from the captured snapshot.",
                )
            )
        elif hashlib.sha256(entry.content).hexdigest() != graph_source_digest.sha256:
            discrepancies.append(
                _discrepancy(
                    "source_digest_mismatch",
                    graph_source_digest.path,
                    "Graph digest does not match captured source bytes.",
                )
            )

    graph_source_paths = set(graph_digests)
    for edge in graph.edges:
        for provenance in edge.provenance:
            if provenance.path not in graph_source_paths:
                discrepancies.append(
                    _discrepancy(
                        "orphan_provenance",
                        provenance.path,
                        "Graph edge provenance is not bound by a graph source digest.",
                    )
                )
    for warning in graph.warnings:
        if warning.provenance.path not in graph_source_paths:
            discrepancies.append(
                _discrepancy(
                    "orphan_provenance",
                    warning.provenance.path,
                    "Graph warning provenance is not bound by a graph source digest.",
                )
            )

    expected_references, scanned_kinds, scan_constructs = _independent_reference_scan(
        snapshot,
        graph.platform,
        normalized_required,
    )
    actual_references = _graph_reference_inventory(graph, scanned_kinds)
    reference_inventory: list[ReferenceInventoryCount] = []
    inventory_keys = set(expected_references)
    inventory_keys.update(
        key
        for key in actual_references
        if key[0] in scanned_kinds and key[1] in scanned_kinds[key[0]]
    )
    for path, edge_kind, symbol in sorted(
        inventory_keys,
        key=lambda item: (item[0], item[1].value, item[2]),
    ):
        source_count = expected_references[(path, edge_kind, symbol)]
        graph_count = actual_references[(path, edge_kind, symbol)]
        inventory_id = _stable_id("reference", path, edge_kind.value, symbol)
        reference_inventory.append(
            ReferenceInventoryCount(
                inventory_id=inventory_id,
                path=path,
                edge_kind=edge_kind,
                symbol=symbol,
                source_count=source_count,
                graph_count=graph_count,
                reconciled=source_count == graph_count,
            )
        )
        if source_count != graph_count:
            discrepancies.append(
                GraphAssuranceDiscrepancy(
                    discrepancy_id=_stable_id(
                        "discrepancy",
                        "reference_count_mismatch",
                        path,
                        edge_kind.value,
                        symbol,
                    ),
                    category="reference_count_mismatch",
                    path=path,
                    summary=(
                        "Independent source reference inventory does not reconcile with "
                        f"graph {edge_kind.value} evidence for {symbol}."
                    ),
                    expected_count=source_count,
                    observed_count=graph_count,
                )
            )

    constructs = [*_warning_constructs(graph), *scan_constructs]
    constructs_by_id = {item.construct_id: item for item in constructs}
    high_impact = _security_sensitive_coverage(graph)
    if not high_impact.complete:
        discrepancies.append(
            _discrepancy(
                "security_coverage_gap",
                None,
                "One or more security-sensitive graph references remain unresolved.",
            )
        )

    discrepancies_by_id = {item.discrepancy_id: item for item in discrepancies}
    ordered_constructs = tuple(
        sorted(constructs_by_id.values(), key=lambda item: item.construct_id)
    )
    ordered_discrepancies = tuple(
        sorted(discrepancies_by_id.values(), key=lambda item: item.discrepancy_id)
    )
    ordered_inventory = tuple(sorted(reference_inventory, key=lambda item: item.inventory_id))
    status = _derive_status(
        ordered_discrepancies,
        ordered_constructs,
        graph_has_unresolved=graph.has_unresolved,
    )
    payload = {
        "schema_version": GRAPH_ASSURANCE_SCHEMA_VERSION,
        "policy_version": GRAPH_ASSURANCE_POLICY_VERSION,
        "platform": graph.platform,
        "source_revision": snapshot.revision,
        "dependency_graph_digest": artifact_digest(graph),
        "analyzer_version": analyzer_version,
        "entry_paths": graph.entry_paths,
        "source_digests": source_digests,
        "parser_source_coverage": tuple(coverage),
        "unsupported_or_ambiguous_constructs": ordered_constructs,
        "detected_discrepancies": ordered_discrepancies,
        "reference_inventory": ordered_inventory,
        "security_sensitive_dependency_coverage": high_impact,
        "graph_has_unresolved": graph.has_unresolved,
        "status": status,
    }
    return GraphAssuranceReport.model_validate({"report_id": _report_id(payload), **payload})


def _derive_status(
    discrepancies: tuple[GraphAssuranceDiscrepancy, ...],
    constructs: tuple[UnsupportedOrAmbiguousConstruct, ...],
    *,
    graph_has_unresolved: bool,
) -> GraphAssuranceStatus:
    if discrepancies or any(item.severity is AssuranceIssueSeverity.BLOCKED for item in constructs):
        return GraphAssuranceStatus.BLOCKED
    if constructs or graph_has_unresolved:
        return GraphAssuranceStatus.REVIEW_REQUIRED
    return GraphAssuranceStatus.ASSURED


def _coverage_role(platform: Platform, path: str) -> SourceCoverageRole:
    if platform is Platform.SALESFORCE and (
        path in {".forceignore", "sfdx-project.json"}
        or path.endswith((".cls-meta.xml", ".page-meta.xml"))
    ):
        return SourceCoverageRole.CONTEXT_ONLY
    return SourceCoverageRole.DEPENDENCY_BEARING


def _parser_name(platform: Platform, path: str) -> str:
    lowered = path.casefold()
    if platform is Platform.SALESFORCE:
        if lowered.endswith(".page"):
            return "visualforce-bounded-reconciliation"
        if lowered.endswith(".cls"):
            return "apex-bounded-reconciliation"
        if lowered.endswith(".permissionset-meta.xml"):
            return "salesforce-permission-xml-reconciliation"
        return "salesforce-context-digest"
    if lowered.endswith(".xml"):
        return "mule-xml-bounded-reconciliation"
    if lowered.endswith((".properties", ".yaml", ".yml")):
        return "mule-property-bounded-reconciliation"
    if lowered.endswith(".dwl"):
        return "dataweave-bounded-reconciliation"
    return "mulesoft-context-digest"


def _reachable_node_ids(graph: DependencyGraph) -> frozenset[str]:
    entry_nodes = {
        node.node_id
        for node in graph.nodes
        if any(path in graph.entry_paths for path in node.metadata_paths)
    }
    if not entry_nodes:
        return frozenset()
    neighbors: dict[str, set[str]] = {node.node_id: set() for node in graph.nodes}
    for edge in graph.edges:
        neighbors[edge.source_id].add(edge.target_id)
        neighbors[edge.target_id].add(edge.source_id)
    queue = deque(sorted(entry_nodes))
    visited = set(entry_nodes)
    while queue:
        current = queue.popleft()
        for candidate in sorted(neighbors[current]):
            if candidate not in visited:
                visited.add(candidate)
                queue.append(candidate)
    return frozenset(visited)


def _independent_reference_scan(
    snapshot: TreeSnapshot,
    platform: Platform,
    required_paths: tuple[str, ...],
) -> tuple[
    Counter[tuple[str, EdgeKind, str]],
    dict[str, frozenset[EdgeKind]],
    tuple[UnsupportedOrAmbiguousConstruct, ...],
]:
    if platform is Platform.SALESFORCE:
        return _scan_salesforce(snapshot, required_paths)
    return _scan_mulesoft(snapshot, required_paths)


def _scan_salesforce(
    snapshot: TreeSnapshot,
    required_paths: tuple[str, ...],
) -> tuple[
    Counter[tuple[str, EdgeKind, str]],
    dict[str, frozenset[EdgeKind]],
    tuple[UnsupportedOrAmbiguousConstruct, ...],
]:
    entries = snapshot.by_path()
    references: Counter[tuple[str, EdgeKind, str]] = Counter()
    scanned: dict[str, frozenset[EdgeKind]] = {}
    constructs: list[UnsupportedOrAmbiguousConstruct] = []
    for path in required_paths:
        entry = entries.get(path)
        if entry is None:
            continue
        try:
            text = entry.content.decode("utf-8")
        except UnicodeDecodeError:
            constructs.append(
                _construct("malformed", AssuranceIssueSeverity.BLOCKED, path, 1, "Non-UTF-8 source")
            )
            continue
        lowered = path.casefold()
        if lowered.endswith(".page"):
            kinds = {
                EdgeKind.VF_CONTROLLER,
                EdgeKind.VF_EXTENSION,
                EdgeKind.VF_STANDARD_CONTROLLER,
            }
            scanned[path] = frozenset(kinds)
            opening = re.search(r"(?is)<apex:page\b(?P<attrs>[^>]*)>", text)
            if opening is None:
                constructs.append(
                    _construct(
                        "malformed",
                        AssuranceIssueSeverity.BLOCKED,
                        path,
                        1,
                        "Visualforce source has no apex:page opening element.",
                    )
                )
                continue
            attrs = opening.group("attrs")
            for attribute, edge_kind in (
                ("controller", EdgeKind.VF_CONTROLLER),
                ("extensions", EdgeKind.VF_EXTENSION),
                ("standardController", EdgeKind.VF_STANDARD_CONTROLLER),
            ):
                match = re.search(
                    rf"(?is)\b{attribute}\s*=\s*([\"'])(?P<value>.*?)\1",
                    attrs,
                )
                if match is None:
                    continue
                for value in (part.strip() for part in match.group("value").split(",")):
                    if value:
                        references[(path, edge_kind, value.casefold())] += 1
        elif lowered.endswith(".permissionset-meta.xml"):
            permission_kinds = {
                EdgeKind.PERMISSION_CLASS_ACCESS,
                EdgeKind.PERMISSION_PAGE_ACCESS,
                EdgeKind.PERMISSION_OBJECT_ACCESS,
                EdgeKind.PERMISSION_FIELD_ACCESS,
            }
            scanned[path] = frozenset(permission_kinds)
            try:
                root = _safe_xml(text)
            except ValueError as exc:
                constructs.append(
                    _construct("malformed", AssuranceIssueSeverity.BLOCKED, path, 1, str(exc))
                )
                continue
            permission_mapping: dict[str, tuple[EdgeKind, str]] = {
                "classAccesses": (EdgeKind.PERMISSION_CLASS_ACCESS, "apexClass"),
                "pageAccesses": (EdgeKind.PERMISSION_PAGE_ACCESS, "apexPage"),
                "objectPermissions": (EdgeKind.PERMISSION_OBJECT_ACCESS, "object"),
                "fieldPermissions": (EdgeKind.PERMISSION_FIELD_ACCESS, "field"),
            }
            for parent in root:
                local = _local_name(parent.tag)
                permission_target = permission_mapping.get(local)
                if permission_target is None:
                    continue
                edge_kind, child_name = permission_target
                symbol = next(
                    (
                        (child.text or "").strip()
                        for child in parent
                        if _local_name(child.tag) == child_name
                    ),
                    "",
                )
                if symbol:
                    references[(path, edge_kind, symbol.casefold())] += 1
        elif lowered.endswith(".cls"):
            scanned[path] = frozenset({EdgeKind.APEX_PAGE_REFERENCE})
            for match in re.finditer(r"\bPage\.(?P<name>[A-Za-z_]\w*)", text):
                references[
                    (path, EdgeKind.APEX_PAGE_REFERENCE, match.group("name").casefold())
                ] += 1
            apex_construct_patterns: tuple[tuple[str, AssuranceConstructCategory, str], ...] = (
                (
                    r"\bDatabase\s*\.\s*(?:query|countQuery)\s*\(",
                    "dynamic",
                    "Dynamic SOQL cannot be proven complete by bounded static analysis.",
                ),
                (
                    r"\bType\s*\.\s*forName\s*\(",
                    "reflective",
                    "Reflective Apex type lookup requires review.",
                ),
                (
                    r"\bSchema\s*\.\s*getGlobalDescribe\s*\(",
                    "reflective",
                    "Dynamic schema lookup requires review.",
                ),
                (
                    r"\b(?:Http|HttpRequest)\b|\bsetEndpoint\s*\(",
                    "external",
                    "External Apex callout behavior is outside bounded graph assurance.",
                ),
            )
            for pattern, category, summary in apex_construct_patterns:
                for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                    constructs.append(
                        _construct(
                            category,
                            AssuranceIssueSeverity.REVIEW_REQUIRED,
                            path,
                            _line_number(text, match.start()),
                            summary,
                        )
                    )
    return references, scanned, tuple(constructs)


def _scan_mulesoft(
    snapshot: TreeSnapshot,
    required_paths: tuple[str, ...],
) -> tuple[
    Counter[tuple[str, EdgeKind, str]],
    dict[str, frozenset[EdgeKind]],
    tuple[UnsupportedOrAmbiguousConstruct, ...],
]:
    entries = snapshot.by_path()
    references: Counter[tuple[str, EdgeKind, str]] = Counter()
    scanned: dict[str, frozenset[EdgeKind]] = {}
    constructs: list[UnsupportedOrAmbiguousConstruct] = []
    for path in required_paths:
        entry = entries.get(path)
        if entry is None:
            continue
        try:
            text = entry.content.decode("utf-8")
        except UnicodeDecodeError:
            constructs.append(
                _construct("malformed", AssuranceIssueSeverity.BLOCKED, path, 1, "Non-UTF-8 source")
            )
            continue
        if not path.casefold().endswith(".xml"):
            continue
        munit = "/src/test/munit/" in path.casefold()
        supported_kinds = {
            EdgeKind.MUNIT_FLOW_REFERENCE if munit else EdgeKind.FLOW_REFERENCE,
            EdgeKind.HTTP_LISTENER_CONFIG_REFERENCE,
            EdgeKind.CONNECTOR_CONFIG_REFERENCE,
            EdgeKind.CONFIGURATION_PROPERTIES_REFERENCE,
            EdgeKind.PROPERTY_REFERENCE,
            EdgeKind.MUNIT_VARIABLE_REFERENCE if munit else EdgeKind.DATAWEAVE_VARIABLE_REFERENCE,
            EdgeKind.DATAWEAVE_MODULE_REFERENCE,
            *({EdgeKind.MUNIT_SUITE_TEST} if munit else set()),
        }
        scanned[path] = frozenset(supported_kinds)
        try:
            root = _safe_xml(text)
        except ValueError as exc:
            constructs.append(
                _construct("malformed", AssuranceIssueSeverity.BLOCKED, path, 1, str(exc))
            )
            continue
        for element in root.iter():
            local = _local_name(element.tag)
            if local in {"flow-ref", "enable-flow-source"}:
                attribute = "name" if local == "flow-ref" else "value"
                symbol = element.attrib.get(attribute, "").strip()
                if symbol:
                    if _DYNAMIC_TARGET.search(symbol):
                        constructs.append(
                            _construct(
                                "dynamic",
                                AssuranceIssueSeverity.REVIEW_REQUIRED,
                                path,
                                1,
                                "Dynamic Mule flow target requires review.",
                            )
                        )
                    edge_kind = EdgeKind.MUNIT_FLOW_REFERENCE if munit else EdgeKind.FLOW_REFERENCE
                    references[(path, edge_kind, symbol.casefold())] += 1
            config_ref = element.attrib.get("config-ref", "").strip()
            if config_ref:
                if _DYNAMIC_TARGET.search(config_ref):
                    constructs.append(
                        _construct(
                            "dynamic",
                            AssuranceIssueSeverity.REVIEW_REQUIRED,
                            path,
                            1,
                            "Dynamic Mule connector configuration target requires review.",
                        )
                    )
                edge_kind = (
                    EdgeKind.HTTP_LISTENER_CONFIG_REFERENCE
                    if _namespace(element.tag).casefold().endswith("/http") and local == "listener"
                    else EdgeKind.CONNECTOR_CONFIG_REFERENCE
                )
                references[(path, edge_kind, config_ref.casefold())] += 1
            if local == "configuration-properties":
                resource = element.attrib.get("file", "").strip()
                if resource:
                    references[
                        (path, EdgeKind.CONFIGURATION_PROPERTIES_REFERENCE, resource.casefold())
                    ] += 1
            resource = element.attrib.get("resource", "").strip()
            if resource and (resource.casefold().endswith(".dwl") or "dwl" in resource.casefold()):
                references[(path, EdgeKind.DATAWEAVE_MODULE_REFERENCE, resource.casefold())] += 1
            if munit and local == "test":
                name = element.attrib.get("name", "").strip()
                if name:
                    references[(path, EdgeKind.MUNIT_SUITE_TEST, name.casefold())] += 1

        for match in _PROPERTY_REFERENCE.finditer(text):
            references[(path, EdgeKind.PROPERTY_REFERENCE, match.group(0).casefold())] += 1
        variable_kind = (
            EdgeKind.MUNIT_VARIABLE_REFERENCE if munit else EdgeKind.DATAWEAVE_VARIABLE_REFERENCE
        )
        for match in _VARIABLE_REFERENCE.finditer(text):
            references[(path, variable_kind, match.group(0).casefold())] += 1
        for match in _DATAWEAVE_IMPORT.finditer(text):
            references[(path, EdgeKind.DATAWEAVE_MODULE_REFERENCE, match.group(1).casefold())] += 1
        for match in re.finditer(r"\breadUrl\s*\(\s*(?!['\"])", text):
            constructs.append(
                _construct(
                    "dynamic",
                    AssuranceIssueSeverity.REVIEW_REQUIRED,
                    path,
                    _line_number(text, match.start()),
                    "Dynamic DataWeave resource target requires review.",
                )
            )
        for element in root.iter():
            namespace = _namespace(element.tag).casefold()
            if any(token in namespace for token in ("/jms", "/vm", "/sockets")) or (
                namespace.endswith("/http") and _local_name(element.tag) == "request"
            ):
                constructs.append(
                    _construct(
                        "external",
                        AssuranceIssueSeverity.REVIEW_REQUIRED,
                        path,
                        1,
                        "Outbound or external Mule connector behavior requires review.",
                    )
                )
    return references, scanned, tuple(constructs)


def _graph_reference_inventory(
    graph: DependencyGraph,
    scanned_kinds: dict[str, frozenset[EdgeKind]],
) -> Counter[tuple[str, EdgeKind, str]]:
    inventory: Counter[tuple[str, EdgeKind, str]] = Counter()
    for edge in graph.edges:
        symbol = (edge.symbol or edge.target_id).casefold()
        seen_provenance: set[tuple[str, int, str, str]] = set()
        for provenance in edge.provenance:
            if edge.kind not in scanned_kinds.get(provenance.path, frozenset()):
                continue
            key = (provenance.path, provenance.line, provenance.excerpt, provenance.parser)
            if key in seen_provenance:
                continue
            seen_provenance.add(key)
            inventory[(provenance.path, edge.kind, symbol)] += 1
    return inventory


def _warning_constructs(graph: DependencyGraph) -> tuple[UnsupportedOrAmbiguousConstruct, ...]:
    constructs: list[UnsupportedOrAmbiguousConstruct] = []
    for warning in graph.warnings:
        category: AssuranceConstructCategory
        if warning.code is WarningCode.MALFORMED_SOURCE:
            category = "malformed"
            severity = AssuranceIssueSeverity.BLOCKED
        elif warning.code in {
            WarningCode.DYNAMIC_SOQL,
            WarningCode.DYNAMIC_REFERENCE,
        }:
            category = "dynamic"
            severity = AssuranceIssueSeverity.REVIEW_REQUIRED
        elif warning.code is WarningCode.DYNAMIC_TYPE:
            category = "reflective"
            severity = AssuranceIssueSeverity.REVIEW_REQUIRED
        else:
            category = "unresolved"
            severity = AssuranceIssueSeverity.BLOCKED
        constructs.append(
            _construct(
                category,
                severity,
                warning.provenance.path,
                warning.provenance.line,
                warning.message,
            )
        )
    return tuple(constructs)


def _security_sensitive_coverage(graph: DependencyGraph) -> SecuritySensitiveDependencyCoverage:
    if graph.platform is Platform.SALESFORCE:
        kinds = {
            EdgeKind.VF_CONTROLLER,
            EdgeKind.VF_EXTENSION,
            EdgeKind.APEX_CLASS_REFERENCE,
            EdgeKind.APEX_PAGE_REFERENCE,
            EdgeKind.SOQL_OBJECT,
            EdgeKind.SOQL_FIELD,
            EdgeKind.PERMISSION_CLASS_ACCESS,
            EdgeKind.PERMISSION_PAGE_ACCESS,
            EdgeKind.PERMISSION_OBJECT_ACCESS,
            EdgeKind.PERMISSION_FIELD_ACCESS,
        }
    else:
        kinds = {
            EdgeKind.FLOW_REFERENCE,
            EdgeKind.HTTP_LISTENER_CONFIG_REFERENCE,
            EdgeKind.CONNECTOR_CONFIG_REFERENCE,
            EdgeKind.CONFIGURATION_PROPERTIES_REFERENCE,
            EdgeKind.PROPERTY_REFERENCE,
            EdgeKind.MULE_ROUTE_PARAMETER_BINDING,
            EdgeKind.DATAWEAVE_VARIABLE_REFERENCE,
            EdgeKind.DATAWEAVE_MODULE_REFERENCE,
            EdgeKind.MUNIT_FLOW_REFERENCE,
            EdgeKind.MUNIT_VARIABLE_REFERENCE,
            EdgeKind.MUNIT_SUITE_TEST,
        }
    relevant = tuple(edge for edge in graph.edges if edge.kind in kinds)
    unresolved = sum(1 for edge in relevant if not edge.resolved or not edge.provenance)
    observed = len(relevant)
    return SecuritySensitiveDependencyCoverage(
        required_edge_kinds=tuple(sorted(kinds, key=lambda item: item.value)),
        observed_references=observed,
        covered_references=observed - unresolved,
        unresolved_references=unresolved,
        complete=unresolved == 0,
    )


def _safe_xml(text: str) -> ElementTree.Element:
    if _XML_GUARD.search(text):
        raise ValueError("XML declarations with DTD or entity expansion are unsupported.")
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError("XML source is malformed for bounded reconciliation.") from exc


def _construct(
    category: AssuranceConstructCategory,
    severity: AssuranceIssueSeverity,
    path: str,
    line: int,
    summary: str,
) -> UnsupportedOrAmbiguousConstruct:
    return UnsupportedOrAmbiguousConstruct(
        construct_id=_stable_id("construct", category, path, str(line), summary),
        category=category,
        severity=severity,
        path=path,
        line=line,
        summary=summary,
    )


def _discrepancy(
    category: Literal[
        "revision_mismatch",
        "missing_source",
        "source_digest_mismatch",
        "source_not_in_graph",
        "unreachable_source",
        "orphan_provenance",
        "reference_count_mismatch",
        "internal_inconsistency",
        "security_coverage_gap",
    ],
    path: str | None,
    summary: str,
) -> GraphAssuranceDiscrepancy:
    return GraphAssuranceDiscrepancy(
        discrepancy_id=_stable_id("discrepancy", category, path or "none", summary),
        category=category,
        path=path,
        summary=summary,
    )


def _report_id(payload: object) -> str:
    normalized = to_jsonable_python(payload)
    return "graph-assurance-" + artifact_digest(normalized).removeprefix("sha256:")[:32]


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:32]}"


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


__all__ = [
    "GRAPH_ASSURANCE_POLICY_VERSION",
    "GRAPH_ASSURANCE_SCHEMA_VERSION",
    "AssuranceIssueSeverity",
    "AssuranceSourceDigest",
    "GraphAssuranceDiscrepancy",
    "GraphAssuranceReport",
    "GraphAssuranceStatus",
    "ParserSourceCoverage",
    "ReferenceInventoryCount",
    "SecuritySensitiveDependencyCoverage",
    "SourceCoverageRole",
    "UnsupportedOrAmbiguousConstruct",
    "build_graph_assurance_report",
]
