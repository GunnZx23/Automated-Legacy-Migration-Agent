"""Common, revision-bound platform preflight for the model Architect.

This module is the deterministic boundary before any Architect model call.  A
caller supplies a platform graph builder, immutable snapshot store, curated
Wiki policy, and digest-bound scope adapter.  The factory either returns one
fully bound :class:`ArchitectContext` or a typed graph-evidence intervention;
it never invokes a model and never reads migration answer or oracle trees.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import field_validator

from legacy_migration_agent.agent_runtime.agent_definitions import AgentRegistry, AgentRole
from legacy_migration_agent.agent_runtime.checkpointing import durable_migration_workflow
from legacy_migration_agent.agent_runtime.correction import CorrectionApproval
from legacy_migration_agent.agent_runtime.model_agents import (
    MAX_CONTEXT_FILES,
    MAX_SOURCE_CONTEXT_CHARS,
    MAX_SOURCE_FILE_CHARS,
    ArchitectContext,
    ArchitectModelContext,
    SourceFileEvidence,
)
from legacy_migration_agent.agent_runtime.model_workflow import ModelAgentWorkflowRoles
from legacy_migration_agent.agent_runtime.run_artifact_paths import RunArtifactPaths
from legacy_migration_agent.contracts import (
    ApprovalAction,
    Identifier,
    MigrationRequest,
    PlanningIntervention,
    PlanningInterventionEvidence,
    PlanningInterventionOption,
    Platform,
    RiskCategory,
    Sha256Digest,
    StrictModel,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.run_session import AgentDefinitionDigests, AgentRunSession
from legacy_migration_agent.core.scope_policy import PlatformAdapter
from legacy_migration_agent.core.workspace import TreeSnapshot, content_revision, snapshot_tree
from legacy_migration_agent.graphs.graph_assurance import (
    GraphAssuranceReport,
    GraphAssuranceStatus,
    build_graph_assurance_report,
)
from legacy_migration_agent.graphs.graph_contracts import DependencyGraph
from legacy_migration_agent.graphs.graph_store import (
    GraphSnapshotKey,
    GraphSnapshotStore,
    StoredGraphSnapshot,
)
from legacy_migration_agent.knowledge.wiki import (
    MAX_RETRIEVAL_PAGES,
    BenchmarkKnowledgeBinding,
    LlmWiki,
    RetrievalTrace,
    benchmark_no_wiki_control_trace,
)
from legacy_migration_agent.workflow import Architect, Engineer, ManifestApproval, Validator

_ANALYZER_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}$")
_CORRECTION_DIAGNOSTIC_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*[_.][a-z0-9_.-]*$")
_ORACLE_SEGMENTS = frozenset({"expected", "golden", "oracle"})
_MODEL_ARCHITECT_CALLBACK = ModelAgentWorkflowRoles.architect
_MODEL_ENGINEER_CALLBACK = ModelAgentWorkflowRoles.engineer
_MODEL_VALIDATOR_CALLBACK = ModelAgentWorkflowRoles.validator


def _exact_diagnostic_ids(query: str) -> tuple[str, ...]:
    """Return exact Wiki anchors declared verbatim by a controller query."""

    return tuple(
        sorted(
            {
                token
                for token in query.split()
                if _CORRECTION_DIAGNOSTIC_ID.fullmatch(token) is not None
            }
        )
    )


class _SessionGraphBinding(StrictModel):
    """Independent run evidence binding for one cached graph artifact."""

    schema_version: Literal["1.0"] = "1.0"
    run_id: Identifier
    thread_id: Identifier
    request_digest: Sha256Digest
    source_revision: Sha256Digest
    graph_key_digest: Sha256Digest
    graph_artifact_path: str
    graph_artifact_digest: Sha256Digest

    @field_validator("graph_artifact_path")
    @classmethod
    def validate_graph_artifact_path(cls, value: str) -> str:
        return validate_relative_path(value)


class _SessionGraphRuntimeAnchor(StrictModel):
    """Independent state-only anchor for a complete portable graph chain."""

    schema_version: Literal["1.0"] = "1.0"
    run_id: Identifier
    thread_id: Identifier
    request_digest: Sha256Digest
    source_revision: Sha256Digest
    graph_key_digest: Sha256Digest
    graph_artifact_digest: Sha256Digest
    binding_artifact_digest: Sha256Digest
    lifecycle_index_digest: Sha256Digest


class _SessionGraphAssuranceBinding(StrictModel):
    """Portable binding from a deterministic assurance report to graph evidence."""

    schema_version: Literal["1.0"] = "1.0"
    run_id: Identifier
    thread_id: Identifier
    request_digest: Sha256Digest
    source_revision: Sha256Digest
    graph_key_digest: Sha256Digest
    graph_artifact_path: str
    graph_artifact_digest: Sha256Digest
    graph_binding_path: str
    graph_binding_digest: Sha256Digest
    graph_lifecycle_index_path: str
    graph_lifecycle_index_digest: Sha256Digest
    assurance_report_path: str
    assurance_report_digest: Sha256Digest
    assurance_status: GraphAssuranceStatus

    @field_validator(
        "graph_artifact_path",
        "graph_binding_path",
        "graph_lifecycle_index_path",
        "assurance_report_path",
    )
    @classmethod
    def validate_artifact_paths(cls, value: str) -> str:
        return validate_relative_path(value)


class _SessionGraphAssuranceRuntimeAnchor(StrictModel):
    """State-only anchor for the graph plus its independent assurance chain."""

    schema_version: Literal["1.0"] = "1.0"
    run_id: Identifier
    thread_id: Identifier
    request_digest: Sha256Digest
    source_revision: Sha256Digest
    graph_key_digest: Sha256Digest
    graph_artifact_digest: Sha256Digest
    graph_binding_digest: Sha256Digest
    graph_lifecycle_index_digest: Sha256Digest
    assurance_report_digest: Sha256Digest
    assurance_status: GraphAssuranceStatus
    assurance_binding_digest: Sha256Digest
    assurance_lifecycle_index_digest: Sha256Digest


@dataclass(frozen=True)
class _SessionArchitectBinding:
    session: AgentRunSession
    request_digest: Sha256Digest
    registry_digests: AgentDefinitionDigests


class PlatformGraphBuilder(Protocol):
    """Platform parser signature accepted by the common runtime."""

    def __call__(
        self,
        repository_root: Path | str,
        entry_paths: Iterable[str],
        base_revision: str,
    ) -> DependencyGraph: ...


class MigrationPlatformRuntime(Protocol):
    """Minimal platform configuration required by Architect preflight."""

    @property
    def platform(self) -> Platform: ...

    @property
    def analyzer_version(self) -> str: ...

    @property
    def graph_builder(self) -> PlatformGraphBuilder: ...


@dataclass(frozen=True)
class PlatformRuntimeConfig:
    """Immutable implementation of :class:`MigrationPlatformRuntime`."""

    platform: Platform
    analyzer_version: str
    graph_builder: PlatformGraphBuilder

    def __post_init__(self) -> None:
        if _ANALYZER_VERSION.fullmatch(self.analyzer_version) is None:
            raise ValueError("analyzer_version must be a bounded implementation identifier")
        if not callable(self.graph_builder):
            raise TypeError("graph_builder must be callable")


class RevisionBoundArchitectContextFactory:
    """Build or load exact graph evidence before exposing an Architect context."""

    def __init__(
        self,
        *,
        source_root: Path,
        graph_store: GraphSnapshotStore,
        runtime: MigrationPlatformRuntime,
        entry_paths: Iterable[str],
        wiki_root: Path,
        wiki_query: str,
        wiki_as_of: date,
        platform_adapter: PlatformAdapter,
        benchmark_knowledge_binding: BenchmarkKnowledgeBinding | None = None,
        wiki_max_age_days: int = 365,
        wiki_max_primary_hits: int = 3,
        wiki_expand_links: bool = True,
    ) -> None:
        self.source_root = _safe_directory(source_root, "source root")
        self.benchmark_knowledge_binding = (
            None
            if benchmark_knowledge_binding is None
            else BenchmarkKnowledgeBinding.model_validate(
                benchmark_knowledge_binding.model_dump(mode="python")
            )
        )
        no_wiki_control = (
            self.benchmark_knowledge_binding is not None
            and self.benchmark_knowledge_binding.knowledge_arm == "full_agent_no_wiki"
        )
        self.wiki_root = (
            Path(wiki_root) if no_wiki_control else _safe_directory(wiki_root, "Wiki root")
        )
        _reject_filesystem_oracle_path(self.source_root, role="source root")
        _reject_filesystem_oracle_path(graph_store.root, role="graph store")
        _reject_writable_cache_overlap(graph_store.root, self.source_root, role="source root")
        if not no_wiki_control:
            _reject_filesystem_oracle_path(self.wiki_root, role="Wiki root")
            _reject_writable_cache_overlap(graph_store.root, self.wiki_root, role="Wiki root")
            self._verify_benchmark_wiki_revision()

        normalized_entries = tuple(sorted({validate_relative_path(path) for path in entry_paths}))
        if not normalized_entries:
            raise ValueError("at least one dependency-graph entry path is required")
        for path in normalized_entries:
            _reject_relative_oracle_path(path, role="dependency-graph entry")

        query = wiki_query.strip()
        if not query:
            raise ValueError("Wiki query cannot be blank")
        if wiki_max_age_days < 1:
            raise ValueError("Wiki max age must be positive")
        if not 1 <= wiki_max_primary_hits <= MAX_RETRIEVAL_PAGES:
            raise ValueError(f"Wiki primary hit limit must be between 1 and {MAX_RETRIEVAL_PAGES}")
        if _ANALYZER_VERSION.fullmatch(runtime.analyzer_version) is None:
            raise ValueError("runtime analyzer_version is not a bounded identifier")
        if not callable(runtime.graph_builder):
            raise TypeError("runtime graph_builder must be callable")
        try:
            bound_adapter = PlatformAdapter.model_validate(
                platform_adapter.model_dump(mode="python")
            )
        except ValueError as exc:
            raise PolicyViolation("platform adapter binding is invalid or tampered") from exc
        if bound_adapter.platform is not runtime.platform:
            raise PolicyViolation("platform adapter does not match the platform runtime")
        _reject_adapter_oracle_paths(bound_adapter)

        self.graph_store = graph_store
        self.runtime = runtime
        self.entry_paths = normalized_entries
        self.wiki_query = query
        self.wiki_as_of = wiki_as_of
        self.platform_adapter = bound_adapter
        self.wiki_max_age_days = wiki_max_age_days
        self.wiki_max_primary_hits = wiki_max_primary_hits
        self.wiki_expand_links = wiki_expand_links
        self._session_binding: _SessionArchitectBinding | None = None

    @classmethod
    def for_session(
        cls,
        session: AgentRunSession,
        request: MigrationRequest,
        registry: AgentRegistry,
        *,
        graph_store: GraphSnapshotStore,
        runtime: MigrationPlatformRuntime,
        entry_paths: Iterable[str],
        wiki_root: Path,
        wiki_query: str,
        wiki_as_of: date,
        platform_adapter: PlatformAdapter,
        benchmark_knowledge_binding: BenchmarkKnowledgeBinding | None = None,
        wiki_max_age_days: int = 365,
        wiki_max_primary_hits: int = 3,
        wiki_expand_links: bool = True,
    ) -> RevisionBoundArchitectContextFactory:
        """Construct a factory bound to one immutable run and agent registry."""

        request_digest = artifact_digest(request)
        if request_digest != session.context.request_digest:
            raise PolicyViolation("migration request does not match the run session")
        session.verify_index("initialized", exact=False)
        session.verify_source_revision()
        if request.base_revision != session.context.source_revision:
            raise PolicyViolation("migration request revision does not match the run session")
        registry_digests = _agent_definition_digests(registry)
        if registry_digests != session.context.agent_definition_digests:
            raise PolicyViolation("loaded agent definitions do not match the run session")
        _require_session_graph_store(session, graph_store)

        factory = cls(
            source_root=session.source_root,
            graph_store=graph_store,
            runtime=runtime,
            entry_paths=entry_paths,
            wiki_root=wiki_root,
            wiki_query=wiki_query,
            wiki_as_of=wiki_as_of,
            platform_adapter=platform_adapter,
            benchmark_knowledge_binding=benchmark_knowledge_binding,
            wiki_max_age_days=wiki_max_age_days,
            wiki_max_primary_hits=wiki_max_primary_hits,
            wiki_expand_links=wiki_expand_links,
        )
        factory._validate_request(request)
        if factory.benchmark_knowledge_binding is not None:
            factory.benchmark_knowledge_binding.require_request(
                request,
                scenario_id=session.context.slice_id,
            )
        factory._session_binding = _SessionArchitectBinding(
            session=session,
            request_digest=request_digest,
            registry_digests=registry_digests,
        )
        return factory

    def bind_workflow_roles(
        self,
        roles: ModelAgentWorkflowRoles,
    ) -> SessionBoundModelWorkflowRoles:
        """Verify the actual executing roles and bind their workflow to this session."""

        return SessionBoundModelWorkflowRoles(self, roles)

    def __call__(
        self,
        request: MigrationRequest,
    ) -> ArchitectContext | PlanningIntervention:
        """Return a frozen context or stop on unresolved dependency evidence."""

        self._validate_session_binding(request)
        self._validate_request(request)
        _preflight_safe_tree(self.source_root, role="source tree")
        source_snapshot = snapshot_tree(self.source_root)
        _reject_snapshot_oracle_paths(source_snapshot)
        if source_snapshot.revision != request.base_revision:
            raise PolicyViolation("current source revision does not match the migration request")

        key = GraphSnapshotKey(
            platform=self.runtime.platform,
            source_revision=source_snapshot.revision,
            entry_paths=self.entry_paths,
            analyzer_version=self.runtime.analyzer_version,
        )
        self._verify_session_cache_before_load(key)
        graph = self._load_cached_graph(key)
        if graph is None:
            graph = DependencyGraph.model_validate(
                self.runtime.graph_builder(
                    self.source_root,
                    self.entry_paths,
                    source_snapshot.revision,
                )
            )
            self._validate_graph(graph, key)
            self._validate_session_graph_before_save(graph, key)
            try:
                saved_key = self.graph_store.save(
                    graph,
                    analyzer_version=self.runtime.analyzer_version,
                )
            except (TypeError, ValueError) as exc:
                raise PolicyViolation(
                    "dependency graph evidence could not be saved safely"
                ) from exc
            if saved_key != key:
                raise PolicyViolation("dependency graph builder produced the wrong snapshot key")
            reloaded = self._load_cached_graph(key)
            if reloaded != graph:
                raise PolicyViolation("immutable dependency graph evidence failed readback")
        else:
            self._validate_graph(graph, key)

        self._bind_session_graph_evidence(graph, key)
        assurance_report = build_graph_assurance_report(
            source_snapshot,
            graph,
            analyzer_version=self.runtime.analyzer_version,
            required_source_paths=(self.platform_adapter.scope_policy.required_source_input_paths),
        )
        self._bind_session_graph_assurance(
            request,
            graph,
            key,
            assurance_report,
        )
        if assurance_report.status is not GraphAssuranceStatus.ASSURED:
            self._persist_non_assured_run_report(request, assurance_report)
            return _graph_assurance_intervention(request, assurance_report)

        source_files = _architect_source_file_evidence(
            source_snapshot,
            self.platform_adapter.scope_policy.required_source_input_paths,
        )
        wiki_trace = self._retrieve_wiki(request)
        benchmark_risk_binding = (
            None
            if self.benchmark_knowledge_binding is None
            else self.benchmark_knowledge_binding.risk_seed_binding
        )
        return ArchitectContext(
            model_context=ArchitectModelContext(
                request=request,
                dependency_graph=graph,
                dependency_graph_digest=artifact_digest(graph),
                graph_assurance_report_digest=artifact_digest(assurance_report),
                graph_assurance_status=assurance_report.status,
                source_files=source_files,
                wiki_trace=wiki_trace,
                wiki_trace_digest=artifact_digest(wiki_trace),
                supplemental_request_evidence=(
                    None if benchmark_risk_binding is None else benchmark_risk_binding.stimulus
                ),
            ),
            platform_adapter=self.platform_adapter,
            graph_assurance_report=assurance_report,
            benchmark_risk_seed_binding=benchmark_risk_binding,
        )

    def _validate_request(self, request: MigrationRequest) -> None:
        if request.platform is not self.runtime.platform:
            raise PolicyViolation("platform runtime does not match the migration request")
        self.platform_adapter.validate_request(request)
        try:
            repository = validate_relative_path(request.repository)
        except ValueError as exc:
            raise PolicyViolation(
                "migration request repository must be repository-relative"
            ) from exc
        _reject_relative_oracle_path(repository, role="migration request repository")
        _reject_relative_oracle_path(
            request.target.entry_path,
            role="migration request target",
        )
        if not any(
            _path_is_covered_by_entry(request.target.entry_path, entry)
            for entry in self.entry_paths
        ):
            raise PolicyViolation(
                "migration request target is outside the dependency-graph entry set"
            )

    def _validate_session_binding(self, request: MigrationRequest) -> None:
        binding = self._session_binding
        if binding is None:
            return
        session = binding.session
        session.verify_index("initialized", exact=False)
        session.verify_source_revision()
        if artifact_digest(request) != binding.request_digest:
            raise PolicyViolation("migration request differs from the run-session binding")
        if self.source_root != session.source_root:
            raise PolicyViolation("Architect source root differs from the run session")
        if request.base_revision != session.context.source_revision:
            raise PolicyViolation("Architect request revision differs from the run session")
        if binding.registry_digests != session.context.agent_definition_digests:
            raise PolicyViolation("Architect agent definitions differ from the run session")
        _require_session_graph_store(session, self.graph_store)

    def _verify_session_cache_before_load(self, key: GraphSnapshotKey) -> None:
        binding = self._session_binding
        if binding is None:
            return
        session = binding.session
        _preflight_safe_tree(self.graph_store.root, role="graph store")
        graph_path, binding_path, index_kind = self._session_graph_paths(key)
        graph_payload = _read_optional_session_json(session, graph_path)
        binding_payload = _read_optional_session_json(session, binding_path)
        index_path = f"indexes/{index_kind}.json"
        index_payload = _read_optional_session_json(session, index_path)
        anchor_present = session.has_runtime_anchor(index_kind)
        present = tuple(
            payload is not None for payload in (graph_payload, binding_payload, index_payload)
        )
        if not any((*present, anchor_present)):
            return
        if not all((*present, anchor_present)):
            raise PolicyViolation("session graph cache has incomplete lifecycle evidence")
        self._verify_session_graph_artifacts(
            key,
            graph_path=graph_path,
            graph_payload=graph_payload,
            binding_path=binding_path,
            binding_payload=binding_payload,
            index_kind=index_kind,
            expected_graph=None,
        )

    def _validate_session_graph_before_save(
        self,
        graph: DependencyGraph,
        key: GraphSnapshotKey,
    ) -> None:
        binding = self._session_binding
        if binding is None:
            return
        snapshot = StoredGraphSnapshot(
            key=key,
            graph_digest=artifact_digest(graph),
            graph=graph,
        )
        binding.session.validate_portable_evidence(snapshot)

    def _bind_session_graph_evidence(
        self,
        graph: DependencyGraph,
        key: GraphSnapshotKey,
    ) -> None:
        binding = self._session_binding
        if binding is None:
            return
        session = binding.session
        graph_path, binding_path, index_kind = self._session_graph_paths(key)
        graph_payload = _read_optional_session_json(session, graph_path)
        if graph_payload is None:
            raise PolicyViolation("session graph artifact is missing after cache resolution")
        stored = StoredGraphSnapshot.model_validate(graph_payload)
        if stored.key != key or stored.graph != graph:
            raise PolicyViolation("session graph artifact differs from resolved graph evidence")
        graph_binding = _SessionGraphBinding(
            run_id=session.context.run_id,
            thread_id=session.context.thread_id,
            request_digest=binding.request_digest,
            source_revision=session.context.source_revision,
            graph_key_digest=key.cache_digest,
            graph_artifact_path=graph_path,
            graph_artifact_digest=artifact_digest(graph_payload),
        )
        session.store.write_json(binding_path, graph_binding)
        expected_paths = tuple(sorted(("run-context.json", graph_path, binding_path)))
        session.write_index(index_kind, expected_paths)
        binding_payload = session.store.read_json(binding_path)
        index_payload = session.store.read_json(f"indexes/{index_kind}.json")
        session.bind_runtime_anchor(
            index_kind,
            self._session_graph_runtime_anchor(
                key,
                graph_payload=graph_payload,
                binding_payload=binding_payload,
                index_payload=index_payload,
            ),
        )
        self._verify_session_graph_artifacts(
            key,
            graph_path=graph_path,
            graph_payload=graph_payload,
            binding_path=binding_path,
            binding_payload=binding_payload,
            index_kind=index_kind,
            expected_graph=graph,
        )

    def _verify_session_graph_artifacts(
        self,
        key: GraphSnapshotKey,
        *,
        graph_path: str,
        graph_payload: object,
        binding_path: str,
        binding_payload: object,
        index_kind: str,
        expected_graph: DependencyGraph | None,
    ) -> None:
        session_binding = self._session_binding
        if session_binding is None:  # pragma: no cover - private call invariant
            raise PolicyViolation("session graph verification requires a session binding")
        session = session_binding.session
        try:
            stored = StoredGraphSnapshot.model_validate(graph_payload)
            evidence_binding = _SessionGraphBinding.model_validate(binding_payload)
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("session graph lifecycle evidence is malformed") from exc
        if stored.key != key:
            raise PolicyViolation("session graph artifact has the wrong cache key")
        if expected_graph is not None and stored.graph != expected_graph:
            raise PolicyViolation("session graph artifact differs from resolved graph evidence")
        expected_binding = _SessionGraphBinding(
            run_id=session.context.run_id,
            thread_id=session.context.thread_id,
            request_digest=session_binding.request_digest,
            source_revision=session.context.source_revision,
            graph_key_digest=key.cache_digest,
            graph_artifact_path=graph_path,
            graph_artifact_digest=artifact_digest(graph_payload),
        )
        if evidence_binding != expected_binding:
            raise PolicyViolation("session graph binding does not match cached graph evidence")
        session.verify_index(index_kind, exact=False)
        index_payload = session.store.read_json(f"indexes/{index_kind}.json")
        try:
            indexed_paths = tuple(item["path"] for item in index_payload["artifacts"])
        except (KeyError, TypeError) as exc:
            raise PolicyViolation("session graph lifecycle index is malformed") from exc
        expected_paths = tuple(sorted(("run-context.json", graph_path, binding_path)))
        if indexed_paths != expected_paths:
            raise PolicyViolation("session graph lifecycle index has the wrong artifact set")
        session.verify_runtime_anchor(
            index_kind,
            self._session_graph_runtime_anchor(
                key,
                graph_payload=graph_payload,
                binding_payload=binding_payload,
                index_payload=index_payload,
            ),
        )

    def _session_graph_runtime_anchor(
        self,
        key: GraphSnapshotKey,
        *,
        graph_payload: object,
        binding_payload: object,
        index_payload: object,
    ) -> _SessionGraphRuntimeAnchor:
        session_binding = self._session_binding
        if session_binding is None:  # pragma: no cover - private call invariant
            raise PolicyViolation("session graph anchor requires a session binding")
        session = session_binding.session
        return _SessionGraphRuntimeAnchor(
            run_id=session.context.run_id,
            thread_id=session.context.thread_id,
            request_digest=session_binding.request_digest,
            source_revision=session.context.source_revision,
            graph_key_digest=key.cache_digest,
            graph_artifact_digest=artifact_digest(graph_payload),
            binding_artifact_digest=artifact_digest(binding_payload),
            lifecycle_index_digest=artifact_digest(index_payload),
        )

    def _session_graph_paths(self, key: GraphSnapshotKey) -> tuple[str, str, str]:
        session_binding = self._session_binding
        if session_binding is None:  # pragma: no cover - private call invariant
            raise PolicyViolation("session graph paths require a session binding")
        evidence_root = session_binding.session.evidence_dir
        graph_file = self.graph_store.root.joinpath(
            *GraphSnapshotStore.relative_path(key).split("/")
        )
        try:
            graph_path = graph_file.relative_to(evidence_root).as_posix()
        except ValueError as exc:
            raise PolicyViolation("graph cache escaped the run-session evidence root") from exc
        digest = key.cache_digest.removeprefix("sha256:")
        binding_path = f"bindings/graph-{digest}.json"
        index_kind = f"graph-{digest[:32]}"
        return graph_path, binding_path, index_kind

    def _bind_session_graph_assurance(
        self,
        request: MigrationRequest,
        graph: DependencyGraph,
        key: GraphSnapshotKey,
        report: GraphAssuranceReport,
    ) -> None:
        """Persist and independently anchor graph assurance before model access."""

        session_binding = self._session_binding
        if session_binding is None:
            return
        session = session_binding.session
        session.validate_portable_evidence(report)
        graph_path, graph_binding_path, graph_index_kind = self._session_graph_paths(key)
        report_path, assurance_binding_path, assurance_index_kind = (
            self._session_graph_assurance_paths(key)
        )
        graph_index_path = f"indexes/{graph_index_kind}.json"
        assurance_index_path = f"indexes/{assurance_index_kind}.json"

        report_payload = _read_optional_session_json(session, report_path)
        assurance_binding_payload = _read_optional_session_json(
            session,
            assurance_binding_path,
        )
        assurance_index_payload = _read_optional_session_json(session, assurance_index_path)
        anchor_present = session.has_runtime_anchor(assurance_index_kind)
        present = (
            report_payload is not None,
            assurance_binding_payload is not None,
            assurance_index_payload is not None,
            anchor_present,
        )
        if any(present):
            if not all(present):
                raise PolicyViolation("session graph assurance has incomplete lifecycle evidence")
            self._verify_session_graph_assurance_artifacts(
                request,
                graph,
                key,
                report,
                graph_path=graph_path,
                graph_binding_path=graph_binding_path,
                graph_index_path=graph_index_path,
                report_path=report_path,
                report_payload=report_payload,
                assurance_binding_path=assurance_binding_path,
                assurance_binding_payload=assurance_binding_payload,
                assurance_index_kind=assurance_index_kind,
                assurance_index_payload=assurance_index_payload,
            )
            return

        try:
            graph_payload = session.store.read_json(graph_path)
            graph_binding_payload = session.store.read_json(graph_binding_path)
            graph_index_payload = session.store.read_json(graph_index_path)
        except FileNotFoundError as exc:  # pragma: no cover - graph chain verified first
            raise PolicyViolation(
                "graph assurance requires complete graph lifecycle evidence"
            ) from exc

        session.store.write_json(report_path, report)
        report_payload = session.store.read_json(report_path)
        evidence_binding = self._session_graph_assurance_binding(
            request,
            key,
            report,
            graph_path=graph_path,
            graph_payload=graph_payload,
            graph_binding_path=graph_binding_path,
            graph_binding_payload=graph_binding_payload,
            graph_index_path=graph_index_path,
            graph_index_payload=graph_index_payload,
            report_path=report_path,
            report_payload=report_payload,
        )
        session.store.write_json(assurance_binding_path, evidence_binding)
        expected_paths = self._session_graph_assurance_index_paths(
            graph_path=graph_path,
            graph_binding_path=graph_binding_path,
            graph_index_path=graph_index_path,
            report_path=report_path,
            assurance_binding_path=assurance_binding_path,
        )
        session.write_index(assurance_index_kind, expected_paths)
        assurance_binding_payload = session.store.read_json(assurance_binding_path)
        assurance_index_payload = session.store.read_json(assurance_index_path)
        session.bind_runtime_anchor(
            assurance_index_kind,
            self._session_graph_assurance_runtime_anchor(
                request,
                key,
                report,
                graph_payload=graph_payload,
                graph_binding_payload=graph_binding_payload,
                graph_index_payload=graph_index_payload,
                report_payload=report_payload,
                assurance_binding_payload=assurance_binding_payload,
                assurance_index_payload=assurance_index_payload,
            ),
        )
        self._verify_session_graph_assurance_artifacts(
            request,
            graph,
            key,
            report,
            graph_path=graph_path,
            graph_binding_path=graph_binding_path,
            graph_index_path=graph_index_path,
            report_path=report_path,
            report_payload=report_payload,
            assurance_binding_path=assurance_binding_path,
            assurance_binding_payload=assurance_binding_payload,
            assurance_index_kind=assurance_index_kind,
            assurance_index_payload=assurance_index_payload,
        )

    def _verify_session_graph_assurance_artifacts(
        self,
        request: MigrationRequest,
        graph: DependencyGraph,
        key: GraphSnapshotKey,
        expected_report: GraphAssuranceReport,
        *,
        graph_path: str,
        graph_binding_path: str,
        graph_index_path: str,
        report_path: str,
        report_payload: object,
        assurance_binding_path: str,
        assurance_binding_payload: object,
        assurance_index_kind: str,
        assurance_index_payload: object,
    ) -> None:
        session_binding = self._session_binding
        if session_binding is None:  # pragma: no cover - private call invariant
            raise PolicyViolation("session graph assurance requires a session binding")
        session = session_binding.session
        try:
            report = GraphAssuranceReport.model_validate(report_payload)
            evidence_binding = _SessionGraphAssuranceBinding.model_validate(
                assurance_binding_payload
            )
            graph_payload = session.store.read_json(graph_path)
            graph_binding_payload = session.store.read_json(graph_binding_path)
            graph_index_payload = session.store.read_json(graph_index_path)
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise PolicyViolation(
                "session graph assurance lifecycle evidence is malformed"
            ) from exc
        if report != expected_report:
            raise PolicyViolation("persisted graph assurance differs from current reconciliation")
        if report.platform is not key.platform:
            raise PolicyViolation("graph assurance platform differs from its graph key")
        if report.source_revision != key.source_revision:
            raise PolicyViolation("graph assurance report is stale for current source")
        if report.dependency_graph_digest != artifact_digest(graph):
            raise PolicyViolation("graph assurance report binds a different dependency graph")
        if report.analyzer_version != key.analyzer_version:
            raise PolicyViolation("graph assurance analyzer differs from its graph key")
        if report.entry_paths != key.entry_paths:
            raise PolicyViolation("graph assurance entries differ from its graph key")

        expected_binding = self._session_graph_assurance_binding(
            request,
            key,
            report,
            graph_path=graph_path,
            graph_payload=graph_payload,
            graph_binding_path=graph_binding_path,
            graph_binding_payload=graph_binding_payload,
            graph_index_path=graph_index_path,
            graph_index_payload=graph_index_payload,
            report_path=report_path,
            report_payload=report_payload,
        )
        if evidence_binding != expected_binding:
            raise PolicyViolation("session graph assurance binding does not match its evidence")
        session.verify_index(assurance_index_kind, exact=False)
        if not isinstance(assurance_index_payload, dict):
            raise PolicyViolation("session graph assurance lifecycle index is malformed")
        assurance_artifacts = assurance_index_payload.get("artifacts")
        if not isinstance(assurance_artifacts, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("path"), str)
            for item in assurance_artifacts
        ):
            raise PolicyViolation("session graph assurance lifecycle index is malformed")
        indexed_paths = tuple(item["path"] for item in assurance_artifacts)
        expected_paths = self._session_graph_assurance_index_paths(
            graph_path=graph_path,
            graph_binding_path=graph_binding_path,
            graph_index_path=graph_index_path,
            report_path=report_path,
            assurance_binding_path=assurance_binding_path,
        )
        if indexed_paths != expected_paths:
            raise PolicyViolation("session graph assurance index has the wrong artifact set")
        session.verify_runtime_anchor(
            assurance_index_kind,
            self._session_graph_assurance_runtime_anchor(
                request,
                key,
                report,
                graph_payload=graph_payload,
                graph_binding_payload=graph_binding_payload,
                graph_index_payload=graph_index_payload,
                report_payload=report_payload,
                assurance_binding_payload=assurance_binding_payload,
                assurance_index_payload=assurance_index_payload,
            ),
        )

    def _session_graph_assurance_binding(
        self,
        request: MigrationRequest,
        key: GraphSnapshotKey,
        report: GraphAssuranceReport,
        *,
        graph_path: str,
        graph_payload: object,
        graph_binding_path: str,
        graph_binding_payload: object,
        graph_index_path: str,
        graph_index_payload: object,
        report_path: str,
        report_payload: object,
    ) -> _SessionGraphAssuranceBinding:
        session_binding = self._session_binding
        if session_binding is None:  # pragma: no cover - private call invariant
            raise PolicyViolation("session graph assurance requires a session binding")
        session = session_binding.session
        return _SessionGraphAssuranceBinding(
            run_id=session.context.run_id,
            thread_id=session.context.thread_id,
            request_digest=artifact_digest(request),
            source_revision=session.context.source_revision,
            graph_key_digest=key.cache_digest,
            graph_artifact_path=graph_path,
            graph_artifact_digest=artifact_digest(graph_payload),
            graph_binding_path=graph_binding_path,
            graph_binding_digest=artifact_digest(graph_binding_payload),
            graph_lifecycle_index_path=graph_index_path,
            graph_lifecycle_index_digest=artifact_digest(graph_index_payload),
            assurance_report_path=report_path,
            assurance_report_digest=artifact_digest(report_payload),
            assurance_status=report.status,
        )

    def _session_graph_assurance_runtime_anchor(
        self,
        request: MigrationRequest,
        key: GraphSnapshotKey,
        report: GraphAssuranceReport,
        *,
        graph_payload: object,
        graph_binding_payload: object,
        graph_index_payload: object,
        report_payload: object,
        assurance_binding_payload: object,
        assurance_index_payload: object,
    ) -> _SessionGraphAssuranceRuntimeAnchor:
        session_binding = self._session_binding
        if session_binding is None:  # pragma: no cover - private call invariant
            raise PolicyViolation("session graph assurance requires a session binding")
        session = session_binding.session
        return _SessionGraphAssuranceRuntimeAnchor(
            run_id=session.context.run_id,
            thread_id=session.context.thread_id,
            request_digest=artifact_digest(request),
            source_revision=session.context.source_revision,
            graph_key_digest=key.cache_digest,
            graph_artifact_digest=artifact_digest(graph_payload),
            graph_binding_digest=artifact_digest(graph_binding_payload),
            graph_lifecycle_index_digest=artifact_digest(graph_index_payload),
            assurance_report_digest=artifact_digest(report_payload),
            assurance_status=report.status,
            assurance_binding_digest=artifact_digest(assurance_binding_payload),
            assurance_lifecycle_index_digest=artifact_digest(assurance_index_payload),
        )

    def _session_graph_assurance_paths(
        self,
        key: GraphSnapshotKey,
    ) -> tuple[str, str, str]:
        digest = key.cache_digest.removeprefix("sha256:")
        return (
            f"graphs/graph-assurance-{digest}.json",
            f"bindings/graph-assurance-{digest}.json",
            f"graph-assurance-{digest[:32]}",
        )

    def _persist_non_assured_run_report(
        self,
        request: MigrationRequest,
        report: GraphAssuranceReport,
    ) -> None:
        """Expose a pre-model stop through the canonical run-evidence projection."""

        session_binding = self._session_binding
        if session_binding is None:
            return
        session = session_binding.session
        path = RunArtifactPaths(request.request_id).graph_assurance_report
        existing = _read_optional_session_json(session, path)
        if existing is None:
            session.store.write_json(path, report)
            existing = session.store.read_json(path)
        try:
            persisted = GraphAssuranceReport.model_validate(existing)
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("run graph assurance report is malformed") from exc
        if persisted != report:
            raise PolicyViolation(
                "run graph assurance report differs from the current reconciliation"
            )

    @staticmethod
    def _session_graph_assurance_index_paths(
        *,
        graph_path: str,
        graph_binding_path: str,
        graph_index_path: str,
        report_path: str,
        assurance_binding_path: str,
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                (
                    "run-context.json",
                    graph_path,
                    graph_binding_path,
                    graph_index_path,
                    report_path,
                    assurance_binding_path,
                )
            )
        )

    def _load_cached_graph(self, key: GraphSnapshotKey) -> DependencyGraph | None:
        try:
            return self.graph_store.load(key)
        except (TypeError, ValueError) as exc:
            raise PolicyViolation(
                "dependency graph cache is stale, malformed, or tampered"
            ) from exc

    def _validate_graph(self, graph: DependencyGraph, key: GraphSnapshotKey) -> None:
        if graph.platform is not key.platform:
            raise PolicyViolation("dependency graph platform does not match its runtime")
        if graph.base_revision != key.source_revision:
            raise PolicyViolation("dependency graph revision does not match current source")
        if graph.entry_paths != key.entry_paths:
            raise PolicyViolation("dependency graph entries do not match the requested entry set")
        _reject_graph_oracle_paths(graph)

    def _retrieve_wiki(self, request: MigrationRequest) -> RetrievalTrace:
        if self._uses_no_wiki_control:
            return benchmark_no_wiki_control_trace(
                cast(BenchmarkKnowledgeBinding, self.benchmark_knowledge_binding),
                request,
                scenario_id=self._benchmark_scenario_id,
                query=self.wiki_query,
                as_of=self.wiki_as_of,
                include_controller_diagnostic_ids=False,
            )
        return self._retrieve_wiki_query(
            request,
            self.wiki_query,
            no_evidence_message="version-filtered Wiki retrieval returned no Architect evidence",
            required_exact_ids=_exact_diagnostic_ids(self.wiki_query),
        )

    def retrieve_correction_wiki(
        self,
        request: MigrationRequest,
        query: str,
    ) -> RetrievalTrace:
        """Retrieve targeted, version-bound evidence for an exact repair query."""

        self._validate_session_binding(request)
        self._validate_request(request)
        normalized_query = query.strip()
        if not normalized_query:
            raise PolicyViolation("correction Wiki query cannot be blank")
        exact_ids = _exact_diagnostic_ids(normalized_query)
        if not exact_ids:
            raise PolicyViolation(
                "correction Wiki query must contain at least one exact diagnostic ID"
            )
        if self._uses_no_wiki_control:
            return benchmark_no_wiki_control_trace(
                cast(BenchmarkKnowledgeBinding, self.benchmark_knowledge_binding),
                request,
                scenario_id=self._benchmark_scenario_id,
                query=normalized_query,
                as_of=self.wiki_as_of,
                include_controller_diagnostic_ids=True,
            )
        return self._retrieve_wiki_query(
            request,
            normalized_query,
            required_exact_ids=exact_ids,
        )

    def _retrieve_wiki_query(
        self,
        request: MigrationRequest,
        query: str,
        *,
        no_evidence_message: str = "version-filtered Wiki retrieval returned no relevant evidence",
        required_exact_ids: tuple[str, ...] = (),
    ) -> RetrievalTrace:
        self._verify_benchmark_wiki_revision()
        wiki = LlmWiki.load(self.wiki_root)
        for page in wiki.catalog.pages:
            _reject_relative_oracle_path(page.path, role="Wiki page")
        trace = wiki.search(
            query,
            platform=request.platform,
            source_version=request.target.source_version,
            target_version=request.target.target_version,
            max_primary_hits=self.wiki_max_primary_hits,
            expand_links=self.wiki_expand_links,
            as_of=self.wiki_as_of,
            max_age_days=self.wiki_max_age_days,
            required_exact_ids=required_exact_ids,
        )
        if not trace.hits:
            raise PolicyViolation(no_evidence_message)
        for hit in trace.hits:
            _reject_relative_oracle_path(hit.path, role="selected Wiki page")
            if hit.source_version != request.target.source_version:
                raise PolicyViolation("selected Wiki evidence has the wrong source version")
            if hit.target_version != request.target.target_version:
                raise PolicyViolation("selected Wiki evidence has the wrong target version")
        return trace

    def _verify_benchmark_wiki_revision(self) -> None:
        """Reject mutable Wiki evidence before every benchmark retrieval."""

        binding = self.benchmark_knowledge_binding
        if binding is None or binding.knowledge_arm == "full_agent_no_wiki":
            return
        if content_revision(self.wiki_root) != binding.wiki_tree_revision:
            raise PolicyViolation("benchmark Wiki tree drifted from the predeclared runtime")

    @property
    def _uses_no_wiki_control(self) -> bool:
        binding = self.benchmark_knowledge_binding
        return binding is not None and binding.knowledge_arm == "full_agent_no_wiki"

    @property
    def _benchmark_scenario_id(self) -> str:
        session_binding = self._session_binding
        if session_binding is None:
            raise PolicyViolation("benchmark knowledge controls require a session-bound factory")
        return session_binding.session.context.slice_id


def _architect_source_file_evidence(
    snapshot: TreeSnapshot,
    required_paths: tuple[str, ...],
) -> tuple[SourceFileEvidence, ...]:
    """Build exact UTF-8 Architect evidence from the already-frozen snapshot."""

    if not required_paths:
        raise PolicyViolation("Architect source evidence requires at least one input file")
    if len(required_paths) > MAX_CONTEXT_FILES:
        raise PolicyViolation("Architect source evidence contains too many input files")
    if len(required_paths) != len(set(required_paths)):
        raise PolicyViolation("Architect source evidence paths must be unique")

    entries = snapshot.by_path()
    missing = tuple(path for path in required_paths if path not in entries)
    if missing:
        raise PolicyViolation(
            "Architect source evidence is missing controller-required inputs: " + ", ".join(missing)
        )

    total_characters = 0
    evidence: list[SourceFileEvidence] = []
    for path in required_paths:
        entry = entries[path]
        try:
            content = entry.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyViolation(f"Architect source input is not UTF-8: {path}") from exc
        if "\x00" in content:
            raise PolicyViolation(f"Architect source input is binary: {path}")
        if len(content) > MAX_SOURCE_FILE_CHARS:
            raise PolicyViolation(f"Architect source input exceeds the prompt bound: {path}")
        total_characters += len(content)
        evidence.append(
            SourceFileEvidence(
                path=path,
                sha256=f"sha256:{hashlib.sha256(entry.content).hexdigest()}",
                content=content,
            )
        )

    if total_characters > MAX_SOURCE_CONTEXT_CHARS:
        raise PolicyViolation("Architect source evidence exceeds the total prompt bound")
    return tuple(evidence)


@dataclass(frozen=True)
class SessionBoundModelWorkflowRoles:
    """Verified executing role set for one exact run session."""

    architect_factory: RevisionBoundArchitectContextFactory
    roles: ModelAgentWorkflowRoles

    def __post_init__(self) -> None:
        self._verify()

    @property
    def session(self) -> AgentRunSession:
        binding = self.architect_factory._session_binding
        if binding is None:
            raise PolicyViolation("workflow roles require a session-bound Architect factory")
        return binding.session

    def _verify(self) -> tuple[Architect, Engineer, Validator]:
        session = self.session
        session.verify_index("initialized", exact=False)
        if self.roles.architect_context_factory is not self.architect_factory:
            raise PolicyViolation(
                "executing Architect role does not use the session-bound context factory"
            )
        actual_digests = AgentDefinitionDigests(
            architect=self.roles.architect_agent.definition.definition_digest,
            engineer=self.roles.engineer_agent.definition.definition_digest,
            validator=self.roles.validator_agent.definition.definition_digest,
        )
        if actual_digests != session.context.agent_definition_digests:
            raise PolicyViolation(
                "executing workflow role definitions do not match the run session"
            )
        if self.roles.artifact_store is not session.store:
            raise PolicyViolation(
                "executing workflow roles must use the run-session evidence store"
            )
        return _exact_model_role_callbacks(self.roles)

    def build(
        self,
        *,
        maximum_execution_attempts: int = 2,
    ) -> SessionBoundMigrationWorkflow:
        """Bind durable workflow state to the run session's SQLite database."""

        self._verify()
        return SessionBoundMigrationWorkflow(
            session=self.session,
            roles=self.roles,
            verified_callbacks=self._verify,
            maximum_execution_attempts=maximum_execution_attempts,
        )


@dataclass(frozen=True)
class SessionBoundMigrationWorkflow:
    """Workflow facade that rejects every thread other than the session thread."""

    session: AgentRunSession
    roles: ModelAgentWorkflowRoles
    verified_callbacks: Callable[[], tuple[Architect, Engineer, Validator]]
    maximum_execution_attempts: int = 2

    def __post_init__(self) -> None:
        if self.maximum_execution_attempts not in {1, 2}:
            raise ValueError("maximum_execution_attempts must be 1 or 2")

    def start(self, request: MigrationRequest, *, thread_id: str) -> Any:
        architect, engineer, validator = self._verify_thread(thread_id)
        if artifact_digest(request) != self.session.context.request_digest:
            raise PolicyViolation("workflow start request does not match the run session")
        self.session.verify_source_revision()
        with durable_migration_workflow(
            self.session.checkpoint_path,
            architect,
            engineer,
            validator,
            maximum_execution_attempts=self.maximum_execution_attempts,
        ) as workflow:
            return workflow.start(request, thread_id=thread_id)

    def resume(
        self,
        approval: ManifestApproval | Mapping[str, Any],
        *,
        thread_id: str,
    ) -> Any:
        architect, engineer, validator = self._verify_thread(thread_id)
        self.session.verify_source_revision()
        with durable_migration_workflow(
            self.session.checkpoint_path,
            architect,
            engineer,
            validator,
            maximum_execution_attempts=self.maximum_execution_attempts,
        ) as workflow:
            return workflow.resume(approval, thread_id=thread_id)

    def snapshot(self, *, thread_id: str) -> Any:
        architect, engineer, validator = self._verify_thread(thread_id)
        with durable_migration_workflow(
            self.session.checkpoint_path,
            architect,
            engineer,
            validator,
            maximum_execution_attempts=self.maximum_execution_attempts,
        ) as workflow:
            return workflow.snapshot(thread_id=thread_id)

    def continue_local_failure(self, *, thread_id: str) -> Any:
        architect, engineer, validator = self._verify_thread(thread_id)
        self.session.verify_source_revision()
        with durable_migration_workflow(
            self.session.checkpoint_path,
            architect,
            engineer,
            validator,
            maximum_execution_attempts=self.maximum_execution_attempts,
        ) as workflow:
            return workflow.continue_local_failure(thread_id=thread_id)

    def retry_recoverable(
        self,
        approval: CorrectionApproval | Mapping[str, Any],
        *,
        thread_id: str,
    ) -> Any:
        architect, engineer, validator = self._verify_thread(thread_id)
        self.session.verify_source_revision()
        with durable_migration_workflow(
            self.session.checkpoint_path,
            architect,
            engineer,
            validator,
            maximum_execution_attempts=self.maximum_execution_attempts,
        ) as workflow:
            return workflow.retry_recoverable(approval, thread_id=thread_id)

    def _verify_thread(self, thread_id: str) -> tuple[Architect, Engineer, Validator]:
        callbacks = self.verified_callbacks()
        if thread_id != self.session.context.thread_id:
            raise PolicyViolation("workflow thread_id does not match the run session")
        return callbacks


def _exact_model_role_callbacks(
    roles: ModelAgentWorkflowRoles,
) -> tuple[Architect, Engineer, Validator]:
    if type(roles) is not ModelAgentWorkflowRoles:
        raise PolicyViolation("session-bound workflow roles cannot use a substituted subclass")
    callback_names = ("architect", "engineer", "validator")
    if any(name in vars(roles) for name in callback_names):
        raise PolicyViolation("executing workflow callbacks must be unshadowed role methods")

    architect = roles.architect
    engineer = roles.engineer
    validator = roles.validator
    expected = (
        (architect, _MODEL_ARCHITECT_CALLBACK),
        (engineer, _MODEL_ENGINEER_CALLBACK),
        (validator, _MODEL_VALIDATOR_CALLBACK),
    )
    if any(
        not inspect.ismethod(callback)
        or getattr(callback, "__self__", None) is not roles
        or getattr(callback, "__func__", None) is not original
        for callback, original in expected
    ):
        raise PolicyViolation("executing workflow callbacks must be exact bound role methods")
    return (architect, engineer, validator)


def _graph_assurance_intervention(
    request: MigrationRequest,
    report: GraphAssuranceReport,
) -> PlanningIntervention:
    """Return a non-authorizing, report-digest-bound pre-model stop."""

    report_digest = artifact_digest(report)
    affected_candidates = {
        request.target.entry_path,
        *(item.path for item in report.source_digests),
    }
    affected_paths = tuple(
        [request.target.entry_path] + sorted(affected_candidates - {request.target.entry_path})[:63]
    )

    unresolved_items = {
        *(
            "discrepancy:" + artifact_digest(item).removeprefix("sha256:")[:32]
            for item in report.detected_discrepancies
        ),
        *(
            "unsupported:" + artifact_digest(item).removeprefix("sha256:")[:32]
            for item in report.unsupported_or_ambiguous_constructs
        ),
    }
    if not unresolved_items:
        unresolved_items.add(f"graph-assurance:{report.status.value}")
    bounded_items = tuple(sorted(unresolved_items)[:64])
    intervention_identity = artifact_digest(
        {
            "request_digest": artifact_digest(request),
            "graph_assurance_report_digest": report_digest,
            "status": report.status,
            "kind": "graph-assurance-stop",
        }
    ).removeprefix("sha256:")[:32]
    return PlanningIntervention(
        intervention_id=f"graph-assurance-{intervention_identity}",
        request_id=request.request_id,
        request_digest=artifact_digest(request),
        platform=request.platform,
        base_revision=request.base_revision,
        reason=(
            "Deterministic graph assurance returned "
            f"{report.status.value}. The Architect model was not called because the "
            "revision-bound dependency evidence cannot safely support a migration plan."
        ),
        requested_action=ApprovalAction.EXPAND_SCOPE,
        affected_paths=affected_paths,
        evidence=(
            PlanningInterventionEvidence(
                category=(
                    RiskCategory.DYNAMIC_DEPENDENCY
                    if report.status is GraphAssuranceStatus.REVIEW_REQUIRED
                    else RiskCategory.INCOMPLETE_EVIDENCE
                ),
                source="graph_assurance",
                source_digest=report_digest,
                summary=(
                    "Controller-owned graph reconciliation reported "
                    f"{len(report.detected_discrepancies)} discrepancy item(s) and "
                    f"{len(report.unsupported_or_ambiguous_constructs)} unsupported or "
                    "ambiguous construct item(s)."
                ),
                affected_paths=affected_paths,
                unresolved_items=bounded_items,
            ),
        ),
        options=(
            PlanningInterventionOption.RESOLVE_EVIDENCE_AND_REPLAN,
            PlanningInterventionOption.EXPAND_SCOPE_AND_REPLAN,
            PlanningInterventionOption.STOP_REQUEST,
        ),
        recommendation=(
            "Resolve or explicitly review the reported source evidence, regenerate the "
            "dependency graph and assurance report, and begin a new planning run."
        ),
    )


def _agent_definition_digests(registry: AgentRegistry) -> AgentDefinitionDigests:
    return AgentDefinitionDigests(
        architect=registry.get(AgentRole.ARCHITECT).definition_digest,
        engineer=registry.get(AgentRole.ENGINEER).definition_digest,
        validator=registry.get(AgentRole.VALIDATOR).definition_digest,
    )


def _require_session_graph_store(
    session: AgentRunSession,
    graph_store: GraphSnapshotStore,
) -> None:
    try:
        relative = graph_store.root.relative_to(session.evidence_dir)
    except ValueError as exc:
        raise PolicyViolation("graph store must be confined to run-session evidence") from exc
    if relative == Path("."):
        raise PolicyViolation("graph store must be a dedicated run-session evidence child")
    _reject_filesystem_oracle_path(graph_store.root, role="graph store")
    _preflight_safe_tree(graph_store.root, role="graph store")


def _read_optional_session_json(session: AgentRunSession, relative_path: str) -> object | None:
    try:
        return cast(object, session.store.read_json(relative_path))
    except FileNotFoundError:
        return None


def _preflight_safe_tree(root: Path, *, role: str) -> None:
    """Reject oracle names, links, and special files before opening file bytes."""

    _preflight_safe_directory(root, root, role=role)


def _preflight_safe_directory(root: Path, directory: Path, *, role: str) -> None:
    try:
        with os.scandir(directory) as children:
            entries = tuple(sorted(children, key=lambda child: child.name))
    except OSError as exc:
        raise PolicyViolation(f"{role} inventory could not be inspected safely") from exc
    for child in entries:
        relative = Path(child.path).relative_to(root).as_posix()
        if any(part.casefold() in _ORACLE_SEGMENTS for part in relative.split("/")):
            raise PolicyViolation(f"{role} cannot contain expected, golden, or oracle segments")
        try:
            child_metadata = child.stat(follow_symlinks=False)
        except OSError as exc:
            raise PolicyViolation(f"{role} path could not be inspected safely: {relative}") from exc
        if stat.S_ISLNK(child_metadata.st_mode):
            raise PolicyViolation(f"{role} cannot contain symlinks: {relative}")
        if stat.S_ISDIR(child_metadata.st_mode):
            _preflight_safe_directory(root, Path(child.path), role=role)
            continue
        if stat.S_ISREG(child_metadata.st_mode):
            continue
        raise PolicyViolation(f"{role} contains an unsupported special file: {relative}")


def _path_is_covered_by_entry(path: str, entry: str) -> bool:
    return path == entry or path.startswith(entry + "/")


def _safe_directory(path: Path, role: str) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PolicyViolation(f"{role} does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise PolicyViolation(f"{role} cannot be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise PolicyViolation(f"{role} must be a directory")
    return path.resolve(strict=True)


def _reject_relative_oracle_path(path: str, *, role: str) -> None:
    normalized = validate_relative_path(path)
    if any(part.casefold() in _ORACLE_SEGMENTS for part in normalized.split("/")):
        raise PolicyViolation(f"{role} cannot contain expected, golden, or oracle segments")


def _reject_filesystem_oracle_path(path: Path, *, role: str) -> None:
    if any(part.casefold() in _ORACLE_SEGMENTS for part in path.parts):
        raise PolicyViolation(f"{role} cannot contain expected, golden, or oracle segments")


def _reject_writable_cache_overlap(cache_root: Path, evidence_root: Path, *, role: str) -> None:
    try:
        cache_root.relative_to(evidence_root)
    except ValueError:
        try:
            evidence_root.relative_to(cache_root)
        except ValueError:
            return
    raise PolicyViolation(f"graph store must not overlap the immutable {role}")


def _reject_snapshot_oracle_paths(snapshot: TreeSnapshot) -> None:
    paths = (*snapshot.directories, *(entry.path for entry in snapshot.entries))
    for path in paths:
        _reject_relative_oracle_path(path, role="source tree")


def _reject_adapter_oracle_paths(adapter: PlatformAdapter) -> None:
    policy = adapter.scope_policy
    for path in (
        *policy.approved_output_roots,
        *policy.approved_output_paths,
        *policy.forbidden_paths,
    ):
        _reject_relative_oracle_path(path, role="platform adapter scope")


def _reject_graph_oracle_paths(graph: DependencyGraph) -> None:
    paths = {
        *graph.entry_paths,
        *(digest.path for digest in graph.source_digests),
        *(path for node in graph.nodes for path in node.metadata_paths),
        *(provenance.path for edge in graph.edges for provenance in edge.provenance),
        *(warning.provenance.path for warning in graph.warnings),
    }
    for path in paths:
        _reject_relative_oracle_path(path, role="dependency graph evidence")


__all__ = [
    "MigrationPlatformRuntime",
    "PlatformGraphBuilder",
    "PlatformRuntimeConfig",
    "RevisionBoundArchitectContextFactory",
    "SessionBoundMigrationWorkflow",
    "SessionBoundModelWorkflowRoles",
]
