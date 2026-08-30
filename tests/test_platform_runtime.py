from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from legacy_migration_agent.agent_runtime.agent_definitions import (
    AgentRegistry,
    AgentRole,
    load_agent_registry,
)
from legacy_migration_agent.agent_runtime.model_agent_correction import (
    EngineerCorrectionContext,
)
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectContext,
    ArchitectManifestProposal,
    ArchitectModelContext,
    ArchitectSemanticDecision,
)
from legacy_migration_agent.agent_runtime.model_workflow import ModelAgentWorkflowRoles
from legacy_migration_agent.agent_runtime.run_artifact_paths import RunArtifactPaths
from legacy_migration_agent.contracts import (
    ApprovalAction,
    MigrationRequest,
    MigrationTarget,
    PlanningIntervention,
    Platform,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.run_session import (
    AgentDefinitionDigests,
    AgentRunSession,
)
from legacy_migration_agent.core.scope_policy import MigrationScopePolicy, PlatformAdapter
from legacy_migration_agent.core.workspace import SnapshotEntry, TreeSnapshot, content_revision
from legacy_migration_agent.graphs.dependency_graph import (
    SALESFORCE_ANALYZER_VERSION,
    build_salesforce_dependency_graph,
)
from legacy_migration_agent.graphs.graph_assurance import (
    GraphAssuranceReport,
    GraphAssuranceStatus,
    _report_id,
)
from legacy_migration_agent.graphs.graph_contracts import (
    DependencyGraph,
    EdgeKind,
    ParserWarning,
    SourceProvenance,
    WarningCode,
)
from legacy_migration_agent.graphs.graph_store import GraphSnapshotStore
from legacy_migration_agent.graphs.mulesoft_dependency_graph import (
    MULESOFT_ANALYZER_VERSION,
    build_mulesoft_dependency_graph,
)
from legacy_migration_agent.knowledge.wiki import BenchmarkKnowledgeBinding
from legacy_migration_agent.platforms.mulesoft_runtime import MULESOFT_PLATFORM_ADAPTER
from legacy_migration_agent.platforms.platform_runtime import (
    PlatformGraphBuilder,
    PlatformRuntimeConfig,
    RevisionBoundArchitectContextFactory,
    _architect_source_file_evidence,
)
from legacy_migration_agent.platforms.salesforce_runtime import SALESFORCE_PLATFORM_ADAPTER
from legacy_migration_agent.workflow import (
    ApprovalSelection,
    ManifestApproval,
    WorkflowStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WIKI_ROOT = PROJECT_ROOT / "knowledge/wiki"
AGENTS_ROOT = PROJECT_ROOT / "agents"
AS_OF = date(2026, 8, 29)
SF_FIXTURE = PROJECT_ROOT / "fixtures/salesforce/account-contact-explorer/input"
SF_ENTRY = "force-app/main/default/pages/LegacyAccountContactExplorer.page"
MULE_FIXTURE = PROJECT_ROOT / "fixtures/mulesoft/customer-status-api/input"
MULE_ENTRY = "legacy-mule3/customer-status-api/src/main/app/customer-status-api.xml"


@dataclass
class RuntimeCase:
    project: Path
    source: Path
    request: MigrationRequest
    session: AgentRunSession
    registry: AgentRegistry
    graph_store: GraphSnapshotStore
    runtime: PlatformRuntimeConfig
    entry: str
    adapter: PlatformAdapter
    query: str


class CountingBuilder:
    def __init__(self, delegate: PlatformGraphBuilder) -> None:
        self.delegate = delegate
        self.calls = 0

    def __call__(
        self,
        repository_root: Path | str,
        entry_paths: Iterable[str],
        base_revision: str,
    ) -> DependencyGraph:
        self.calls += 1
        return self.delegate(repository_root, entry_paths, base_revision)


class NeverModel:
    provider = "offline-never"
    model_id = "never/v1"
    live_invocation = False
    store_false_sent = False

    def __init__(self) -> None:
        self.calls = 0

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        self.calls += 1
        raise AssertionError("unresolved preflight must not call a model")


class ArchitectManifestModel:
    provider = "offline-architect-test"
    model_id = "architect-manifest/v1"
    live_invocation = False
    store_false_sent = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[BaseModel],
    ) -> BaseModel:
        self.calls.append(output_type.__name__)
        if not issubclass(output_type, ArchitectManifestProposal):
            raise AssertionError("Engineer and Validator must remain unreachable")
        context = ArchitectModelContext.model_validate(input_value)
        return ArchitectManifestProposal(
            semantic_decisions=(
                ArchitectSemanticDecision(
                    decision_id="bounded-generated-output",
                    category="target_architecture",
                    summary="Propose only the policy-approved generated output.",
                    evidence_ids=(
                        context.dependency_graph.nodes[0].node_id,
                        context.wiki_trace.hits[0].page_id,
                    ),
                ),
            ),
            cited_graph_nodes=(context.dependency_graph.nodes[0].node_id,),
            cited_wiki_pages=(context.wiki_trace.hits[0].page_id,),
        )


def _registry_digests(registry: AgentRegistry) -> AgentDefinitionDigests:
    return AgentDefinitionDigests(
        architect=registry.get(AgentRole.ARCHITECT).definition_digest,
        engineer=registry.get(AgentRole.ENGINEER).definition_digest,
        validator=registry.get(AgentRole.VALIDATOR).definition_digest,
    )


def _adapter(platform: Platform) -> PlatformAdapter:
    source_input = SF_ENTRY if platform is Platform.SALESFORCE else MULE_ENTRY
    policy = MigrationScopePolicy(
        policy_id=f"{platform.value}-runtime-test-policy",
        platform=platform,
        required_source_input_paths=(source_input,),
        approved_output_paths=(f"generated/{platform.value}.txt",),
        allowed_validation_command_ids=("local-check",),
        required_validation_command_ids=("local-check",),
        max_changed_files=1,
        required_approval_actions=(ApprovalAction.APPROVE_MANIFEST,),
    )
    return PlatformAdapter.bind(adapter_id=f"{platform.value}-runtime-test", policy=policy)


def _case(tmp_path: Path, platform: Platform) -> RuntimeCase:
    if platform is Platform.SALESFORCE:
        fixture = SF_FIXTURE
        entry = SF_ENTRY
        source_version = "Salesforce API 67.0"
        target_version = "Salesforce API 67.0"
        query = "Visualforce LWC migration security Jest"
        analyzer = SALESFORCE_ANALYZER_VERSION
        builder = build_salesforce_dependency_graph
    else:
        fixture = MULE_FIXTURE
        entry = MULE_ENTRY
        source_version = "Mule 3.9.5"
        target_version = "Mule 4.9.20"
        query = "Mule DataWeave HTTP listener MUnit migration"
        analyzer = MULESOFT_ANALYZER_VERSION
        builder = build_mulesoft_dependency_graph

    project = tmp_path / f"project-{platform.value}"
    source = project / "source"
    shutil.copytree(fixture, source)
    revision = content_revision(source)
    request = MigrationRequest(
        request_id=f"request-{platform.value}",
        platform=platform,
        repository="source",
        base_revision=revision,
        target=MigrationTarget(
            entry_path=entry,
            target_runtime=f"{platform.value} target",
            source_version=source_version,
            target_version=target_version,
            description="Exercise the session-bound platform runtime.",
        ),
    )
    registry = load_agent_registry(AGENTS_ROOT)
    session = AgentRunSession.initialize(
        project,
        Path(f".runs/{platform.value}-run"),
        run_id=f"{platform.value}-run",
        thread_id=f"{platform.value}-thread",
        slice_id=f"{platform.value}-slice",
        source_root="source",
        request_digest=artifact_digest(request),
        agent_definition_digests=_registry_digests(registry),
        provider_id="offline-test",
        model_id="structured-agent/v1",
    )
    return RuntimeCase(
        project=project,
        source=source,
        request=request,
        session=session,
        registry=registry,
        graph_store=GraphSnapshotStore(session.evidence_dir / "graphs"),
        runtime=PlatformRuntimeConfig(platform, analyzer, builder),
        entry=entry,
        adapter=_adapter(platform),
        query=query,
    )


def _factory(
    case: RuntimeCase,
    *,
    runtime: PlatformRuntimeConfig | None = None,
    graph_store: GraphSnapshotStore | None = None,
    wiki_root: Path = WIKI_ROOT,
    wiki_max_primary_hits: int = 3,
    benchmark_knowledge_binding: BenchmarkKnowledgeBinding | None = None,
) -> RevisionBoundArchitectContextFactory:
    return RevisionBoundArchitectContextFactory.for_session(
        case.session,
        case.request,
        case.registry,
        graph_store=graph_store or case.graph_store,
        runtime=runtime or case.runtime,
        entry_paths=(case.entry,),
        wiki_root=wiki_root,
        wiki_query=case.query,
        wiki_as_of=AS_OF,
        platform_adapter=case.adapter,
        wiki_max_primary_hits=wiki_max_primary_hits,
        benchmark_knowledge_binding=benchmark_knowledge_binding,
    )


def _no_wiki_binding(case: RuntimeCase) -> BenchmarkKnowledgeBinding:
    case_id = f"{case.request.platform.value}-runtime-case"
    return BenchmarkKnowledgeBinding(
        benchmark_id="measured-v2",
        benchmark_definition_digest="sha256:" + "2" * 64,
        benchmark_registry_digest="sha256:" + "3" * 64,
        configuration_digest="sha256:" + "4" * 64,
        provider_id="claude-cli",
        model_id="claude-sonnet-5",
        cell_id=f"{case_id}--full-agent-no-wiki--r1",
        case_id=case_id,
        scenario_id=case.session.context.slice_id,
        knowledge_arm="full_agent_no_wiki",
        request_digest=artifact_digest(case.request),
        source_revision=case.request.base_revision,
        wiki_tree_revision="sha256:" + "5" * 64,
    )


def _wiki_binding(case: RuntimeCase, wiki_root: Path) -> BenchmarkKnowledgeBinding:
    case_id = f"{case.request.platform.value}-runtime-case"
    return BenchmarkKnowledgeBinding(
        benchmark_id="measured-v2",
        benchmark_definition_digest="sha256:" + "2" * 64,
        benchmark_registry_digest="sha256:" + "3" * 64,
        configuration_digest="sha256:" + "4" * 64,
        provider_id="claude-cli",
        model_id="claude-sonnet-5",
        cell_id=f"{case_id}--full-agent-wiki--r1",
        case_id=case_id,
        scenario_id=case.session.context.slice_id,
        knowledge_arm="full_agent_wiki",
        request_digest=artifact_digest(case.request),
        source_revision=case.request.base_revision,
        wiki_tree_revision=content_revision(wiki_root),
    )


def _unreachable_role(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("Engineer and Validator must remain unreachable")


def _architect_only_roles(
    case: RuntimeCase,
    session: AgentRunSession,
    factory: RevisionBoundArchitectContextFactory,
) -> tuple[ModelAgentWorkflowRoles, ArchitectManifestModel]:
    model = ArchitectManifestModel()
    roles = ModelAgentWorkflowRoles(
        case.registry,
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=factory,
        workspace_factory=_unreachable_role,
        deterministic_validator=_unreachable_role,
        artifact_store=session.store,
    )
    return roles, model


def _rejection_approval(paused: Any) -> ManifestApproval:
    payload = paused.interrupts[0].value
    decision = payload["decision"]
    return ManifestApproval(
        decision_id=decision["decision_id"],
        request_id=decision["request_id"],
        manifest_id=payload["manifest_id"],
        manifest_digest=payload["manifest_digest"],
        requested_action=decision["requested_action"],
        selection=ApprovalSelection.REJECT,
        reviewer="session-reload-reviewer",
    )


@pytest.mark.parametrize("platform", (Platform.SALESFORCE, Platform.MULESOFT))
def test_both_platforms_reuse_exact_cache_and_invalidate_on_analyzer_change(
    tmp_path: Path,
    platform: Platform,
) -> None:
    case = _case(tmp_path, platform)
    counter = CountingBuilder(case.runtime.graph_builder)
    runtime = PlatformRuntimeConfig(platform, case.runtime.analyzer_version, counter)
    factory = _factory(case, runtime=runtime)

    first = factory(case.request)
    second = factory(case.request)

    assert isinstance(first, ArchitectContext)
    assert first == second
    assert counter.calls == 1
    assert first.dependency_graph.platform is platform
    assert tuple(item.path for item in first.model_context.source_files) == (
        case.adapter.scope_policy.required_source_input_paths
    )
    source_bytes = (case.source / case.entry).read_bytes()
    assert first.model_context.source_files[0].content == source_bytes.decode("utf-8")
    assert first.model_context.source_files[0].sha256 == (
        f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
    )
    assert first.wiki_trace.hits
    assert tuple(
        path
        for path in case.session.evidence_dir.glob("indexes/graph-*.json")
        if not path.name.startswith("graph-assurance-")
    )

    changed_counter = CountingBuilder(case.runtime.graph_builder)
    changed_runtime = PlatformRuntimeConfig(
        platform,
        case.runtime.analyzer_version + ".audit2",
        changed_counter,
    )
    changed = _factory(case, runtime=changed_runtime)(case.request)
    assert isinstance(changed, ArchitectContext)
    assert changed_counter.calls == 1
    assert (
        len(
            tuple(
                path
                for path in case.session.evidence_dir.glob("indexes/graph-*.json")
                if not path.name.startswith("graph-assurance-")
            )
        )
        == 2
    )


def test_benchmark_no_wiki_factory_never_loads_wiki_and_keeps_correction_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)
    normal_context = _factory(case)(case.request)
    assert isinstance(normal_context, ArchitectContext)

    def fail_if_wiki_loads(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("no-Wiki benchmark arm must never load the Wiki")

    monkeypatch.setattr(
        "legacy_migration_agent.platforms.platform_runtime.LlmWiki.load",
        fail_if_wiki_loads,
    )
    factory = _factory(
        case,
        wiki_root=case.project / "wiki-path-that-does-not-exist",
        benchmark_knowledge_binding=_no_wiki_binding(case),
    )

    context = factory(case.request)
    correction_query = "controller_jest_case_results salesforce correction validation"
    correction_trace = factory.retrieve_correction_wiki(case.request, correction_query)

    assert isinstance(context, ArchitectContext)
    assert context.wiki_trace.query == normal_context.wiki_trace.query == case.query
    assert context.instruction == normal_context.instruction
    assert context.dependency_graph == normal_context.dependency_graph
    assert context.wiki_trace.retrieval_strategy == "benchmark_no_wiki_control"
    assert context.wiki_trace.hits[0].sources == ()
    assert context.wiki_trace.hits[0].selected_content.endswith("No Wiki guidance was retrieved.")
    assert correction_trace.retrieval_strategy == "benchmark_no_wiki_control"
    assert correction_trace.query == correction_query
    assert correction_trace.hits[0].sources == ()
    EngineerCorrectionContext.require_wiki_signal_coverage(
        correction_trace,
        ("controller_jest_case_results",),
    )


def test_benchmark_wiki_revision_is_rechecked_before_correction_retrieval(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)
    copied_wiki = tmp_path / "copied-wiki"
    shutil.copytree(WIKI_ROOT, copied_wiki)
    factory = _factory(
        case,
        wiki_root=copied_wiki,
        benchmark_knowledge_binding=_wiki_binding(case, copied_wiki),
    )
    page = copied_wiki / "pages/salesforce-validation.md"
    page.write_text(page.read_text(encoding="utf-8") + "\nUnfrozen edit.\n", encoding="utf-8")

    with pytest.raises(PolicyViolation, match="Wiki tree drifted"):
        factory.retrieve_correction_wiki(
            case.request,
            "salesforce_lwc_javascript_contract salesforce correction validation",
        )


@pytest.mark.parametrize(
    ("platform", "production_adapter"),
    (
        (Platform.SALESFORCE, SALESFORCE_PLATFORM_ADAPTER),
        (Platform.MULESOFT, MULESOFT_PLATFORM_ADAPTER),
    ),
)
def test_real_platform_source_inventory_reaches_architect_exactly(
    tmp_path: Path,
    platform: Platform,
    production_adapter: PlatformAdapter,
) -> None:
    case = _case(tmp_path, platform)
    factory = RevisionBoundArchitectContextFactory.for_session(
        case.session,
        case.request,
        case.registry,
        graph_store=case.graph_store,
        runtime=case.runtime,
        entry_paths=(case.entry,),
        wiki_root=WIKI_ROOT,
        wiki_query=case.query,
        wiki_as_of=AS_OF,
        platform_adapter=production_adapter,
    )

    context = factory(case.request)

    assert isinstance(context, ArchitectContext)
    expected_paths = production_adapter.scope_policy.required_source_input_paths
    assert tuple(item.path for item in context.model_context.source_files) == expected_paths
    for item in context.model_context.source_files:
        source_bytes = (case.source / item.path).read_bytes()
        assert item.content == source_bytes.decode("utf-8")
        assert item.sha256 == f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"


@pytest.mark.parametrize(
    ("content", "message"),
    (
        (b"\xff\xfe", "not UTF-8"),
        (b"legacy\x00source", "binary"),
        (b"x" * 32_001, "prompt bound"),
    ),
)
def test_architect_source_evidence_fails_closed_on_unsafe_file_content(
    content: bytes,
    message: str,
) -> None:
    snapshot = TreeSnapshot(
        entries=(SnapshotEntry(path="legacy.txt", content=content, mode=0o644),),
        directories=(),
        revision="sha256:" + "0" * 64,
    )

    with pytest.raises(PolicyViolation, match=message):
        _architect_source_file_evidence(snapshot, ("legacy.txt",))


def test_architect_source_evidence_fails_closed_on_missing_or_aggregate_oversize() -> None:
    empty = TreeSnapshot(entries=(), directories=(), revision="sha256:" + "0" * 64)
    with pytest.raises(PolicyViolation, match="missing controller-required inputs"):
        _architect_source_file_evidence(empty, ("missing.txt",))

    entries = tuple(
        SnapshotEntry(path=f"source-{index}.txt", content=b"x" * 32_000, mode=0o644)
        for index in range(9)
    )
    oversized = TreeSnapshot(
        entries=entries,
        directories=(),
        revision="sha256:" + "1" * 64,
    )
    with pytest.raises(PolicyViolation, match="total prompt bound"):
        _architect_source_file_evidence(
            oversized,
            tuple(entry.path for entry in entries),
        )


def test_targeted_correction_wiki_retrieval_is_version_bound_and_fails_without_hits(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)
    factory = _factory(case, wiki_max_primary_hits=1)
    diagnostic_id = "apex_public_interface_annotation_mismatch"

    trace = factory.retrieve_correction_wiki(
        case.request,
        f"{diagnostic_id} salesforce correction validation",
    )

    assert trace.query == f"{diagnostic_id} salesforce correction validation"
    assert trace.platform is Platform.SALESFORCE
    assert trace.source_version == case.request.target.source_version
    assert trace.target_version == case.request.target.target_version
    assert tuple(hit.page_id for hit in trace.hits) == ("salesforce-apex-security",)
    assert diagnostic_id in trace.hits[0].selected_content
    assert trace.max_primary_hits == 1
    assert trace.expand_links is False

    with pytest.raises(PolicyViolation, match="no eligible curated page"):
        factory.retrieve_correction_wiki(
            case.request,
            "diagnostic_identifier_that_is_not_curated",
        )

    with pytest.raises(PolicyViolation, match="must contain at least one exact diagnostic ID"):
        factory.retrieve_correction_wiki(
            case.request,
            "salesforce correction validation",
        )


def test_unresolved_graph_stops_workflow_before_wiki_or_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)

    def unresolved_builder(
        repository_root: Path | str,
        entry_paths: Iterable[str],
        base_revision: str,
    ) -> DependencyGraph:
        graph = build_salesforce_dependency_graph(repository_root, entry_paths, base_revision)
        payload = graph.model_dump(mode="python")
        node = next(item for item in payload["nodes"] if not item["external"])
        node["resolved"] = False
        return DependencyGraph.model_validate(payload)

    runtime = PlatformRuntimeConfig(
        Platform.SALESFORCE,
        SALESFORCE_ANALYZER_VERSION + ".unresolved",
        unresolved_builder,
    )
    factory = _factory(case, runtime=runtime)
    monkeypatch.setattr(
        "legacy_migration_agent.platforms.platform_runtime.LlmWiki.load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unresolved graphs must stop before Wiki retrieval")
        ),
    )
    model = NeverModel()

    def unreachable(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("later workflow roles must not run")

    roles = ModelAgentWorkflowRoles(
        case.registry,
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=factory,
        workspace_factory=unreachable,
        deterministic_validator=unreachable,
        artifact_store=case.session.store,
    )
    result = (
        factory.bind_workflow_roles(roles)
        .build()
        .start(
            case.request,
            thread_id=case.session.context.thread_id,
        )
    )

    assert isinstance(result.value["planning_intervention"], PlanningIntervention)
    assert model.calls == 0
    report = GraphAssuranceReport.model_validate_json(
        next(case.session.evidence_dir.glob("graphs/graph-assurance-*.json")).read_text(
            encoding="utf-8"
        )
    )
    assert report.status is GraphAssuranceStatus.REVIEW_REQUIRED


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    (
        ("malformed", GraphAssuranceStatus.BLOCKED),
        ("dynamic", GraphAssuranceStatus.REVIEW_REQUIRED),
        ("incomplete", GraphAssuranceStatus.BLOCKED),
        ("discrepant", GraphAssuranceStatus.BLOCKED),
    ),
)
def test_non_assured_graph_variants_stop_before_wiki_or_any_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_status: GraphAssuranceStatus,
) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)

    def non_assured_builder(
        repository_root: Path | str,
        entry_paths: Iterable[str],
        base_revision: str,
    ) -> DependencyGraph:
        graph = build_salesforce_dependency_graph(repository_root, entry_paths, base_revision)
        payload = graph.model_dump(mode="python")
        if mutation in {"malformed", "dynamic"}:
            payload["warnings"] = [
                ParserWarning(
                    code=(
                        WarningCode.MALFORMED_SOURCE
                        if mutation == "malformed"
                        else WarningCode.DYNAMIC_SOQL
                    ),
                    message=f"Synthetic {mutation} construct for assurance preflight.",
                    provenance=SourceProvenance(
                        path=case.entry,
                        line=1,
                        excerpt=f"synthetic-{mutation}",
                        parser="assurance-test",
                    ),
                )
            ]
        elif mutation == "incomplete":
            payload["source_digests"] = [
                item for item in payload["source_digests"] if item["path"] != case.entry
            ]
        elif mutation == "discrepant":
            payload["edges"] = [
                item for item in payload["edges"] if item["kind"] is not EdgeKind.VF_CONTROLLER
            ]
        else:  # pragma: no cover - the parameter set is closed
            raise AssertionError(f"unknown mutation: {mutation}")
        return DependencyGraph.model_validate(payload)

    runtime = PlatformRuntimeConfig(
        Platform.SALESFORCE,
        f"{SALESFORCE_ANALYZER_VERSION}.{mutation}",
        non_assured_builder,
    )
    factory = _factory(case, runtime=runtime)
    monkeypatch.setattr(
        "legacy_migration_agent.platforms.platform_runtime.LlmWiki.load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-assured graphs must stop before Wiki retrieval")
        ),
    )
    model = NeverModel()

    def unreachable(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Engineer and Validator must remain unreachable")

    roles = ModelAgentWorkflowRoles(
        case.registry,
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=factory,
        workspace_factory=unreachable,
        deterministic_validator=unreachable,
        artifact_store=case.session.store,
    )

    result = (
        factory.bind_workflow_roles(roles)
        .build()
        .start(case.request, thread_id=case.session.context.thread_id)
    )

    intervention = result.value["planning_intervention"]
    assert isinstance(intervention, PlanningIntervention)
    assert model.calls == 0
    report = GraphAssuranceReport.model_validate_json(
        next(case.session.evidence_dir.glob("graphs/graph-assurance-*.json")).read_text(
            encoding="utf-8"
        )
    )
    assert report.status is expected_status
    assert intervention.evidence[0].source_digest == artifact_digest(report)
    assert (
        GraphAssuranceReport.model_validate(
            case.session.store.read_json(
                RunArtifactPaths(case.request.request_id).graph_assurance_report
            )
        )
        == report
    )


def test_session_bound_roles_reject_altered_executing_architect_definition(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)
    factory = _factory(case)
    definitions = {definition.role: definition for definition in case.registry.definitions}
    definitions[AgentRole.ARCHITECT] = definitions[AgentRole.ARCHITECT].model_copy(
        update={"definition_digest": "sha256:" + "0" * 64}
    )
    altered_registry = AgentRegistry(definitions)
    model = NeverModel()

    def unreachable(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("role construction verification must run before workflow execution")

    altered_roles = ModelAgentWorkflowRoles(
        altered_registry,
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=factory,
        workspace_factory=unreachable,
        deterministic_validator=unreachable,
        artifact_store=case.session.store,
    )

    with pytest.raises(PolicyViolation, match="executing workflow role definitions"):
        factory.bind_workflow_roles(altered_roles)
    assert model.calls == 0


def test_session_bound_workflow_rejects_wrong_thread_for_every_operation(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)
    factory = _factory(case)
    model = NeverModel()

    def unreachable(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("wrong-thread calls must stop before any workflow role")

    roles = ModelAgentWorkflowRoles(
        case.registry,
        architect_model=model,
        engineer_model=model,
        validator_model=model,
        architect_context_factory=factory,
        workspace_factory=unreachable,
        deterministic_validator=unreachable,
        artifact_store=case.session.store,
    )
    workflow = factory.bind_workflow_roles(roles).build()

    with pytest.raises(PolicyViolation, match="thread_id does not match"):
        workflow.start(case.request, thread_id="wrong-thread")
    with pytest.raises(PolicyViolation, match="thread_id does not match"):
        workflow.snapshot(thread_id="wrong-thread")
    with pytest.raises(PolicyViolation, match="thread_id does not match"):
        workflow.resume({}, thread_id="wrong-thread")
    with pytest.raises(PolicyViolation, match="thread_id does not match"):
        workflow.continue_local_failure(thread_id="wrong-thread")
    with pytest.raises(PolicyViolation, match="thread_id does not match"):
        workflow.retry_recoverable({}, thread_id="wrong-thread")
    assert model.calls == 0


def test_session_bound_workflow_uses_session_sqlite_across_reload_and_resume(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)
    first_factory = _factory(case)
    first_roles, first_model = _architect_only_roles(case, case.session, first_factory)
    paused = (
        first_factory.bind_workflow_roles(first_roles)
        .build()
        .start(
            case.request,
            thread_id=case.session.context.thread_id,
        )
    )
    approval = _rejection_approval(paused)
    assert first_model.calls == ["ArchitectManifestProposal"]
    assert case.session.checkpoint_path.stat().st_size > 0

    reloaded = AgentRunSession.load(case.project, case.session.run_dir)
    reloaded_factory = RevisionBoundArchitectContextFactory.for_session(
        reloaded,
        case.request,
        case.registry,
        graph_store=GraphSnapshotStore(reloaded.evidence_dir / "graphs"),
        runtime=case.runtime,
        entry_paths=(case.entry,),
        wiki_root=WIKI_ROOT,
        wiki_query=case.query,
        wiki_as_of=AS_OF,
        platform_adapter=case.adapter,
    )
    reloaded_roles, reloaded_model = _architect_only_roles(case, reloaded, reloaded_factory)
    result = (
        reloaded_factory.bind_workflow_roles(reloaded_roles)
        .build()
        .resume(
            approval,
            thread_id=reloaded.context.thread_id,
        )
    )

    assert result.value["status"] == WorkflowStatus.REJECTED
    assert reloaded.checkpoint_path == case.session.checkpoint_path
    assert reloaded_model.calls == []


@pytest.mark.parametrize("callback_name", ("architect", "engineer", "validator"))
def test_session_bound_roles_reject_callable_substitution_before_bind(
    tmp_path: Path,
    callback_name: str,
) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)
    factory = _factory(case)
    roles, model = _architect_only_roles(case, case.session, factory)
    substituted_calls: list[str] = []

    def substituted(*_args: Any, **_kwargs: Any) -> Any:
        substituted_calls.append(callback_name)
        raise AssertionError("substituted callback must never execute")

    setattr(roles, callback_name, substituted)
    with pytest.raises(PolicyViolation, match="unshadowed role methods"):
        factory.bind_workflow_roles(roles)
    assert substituted_calls == []
    assert model.calls == []


@pytest.mark.parametrize("callback_name", ("architect", "engineer", "validator"))
def test_session_bound_workflow_rejects_callable_substitution_after_start(
    tmp_path: Path,
    callback_name: str,
) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)
    factory = _factory(case)
    roles, model = _architect_only_roles(case, case.session, factory)
    workflow = factory.bind_workflow_roles(roles).build()
    paused = workflow.start(case.request, thread_id=case.session.context.thread_id)
    approval = _rejection_approval(paused)
    substituted_calls: list[str] = []

    def substituted(*_args: Any, **_kwargs: Any) -> Any:
        substituted_calls.append(callback_name)
        raise AssertionError("substituted callback must never execute")

    setattr(roles, callback_name, substituted)
    with pytest.raises(PolicyViolation, match="unshadowed role methods"):
        workflow.resume(approval, thread_id=case.session.context.thread_id)
    assert substituted_calls == []
    assert model.calls == ["ArchitectManifestProposal"]


def test_session_binding_rejects_request_registry_source_and_store_mismatches(
    tmp_path: Path,
) -> None:
    request_case = _case(tmp_path / "request", Platform.SALESFORCE)
    changed_request = request_case.request.model_copy(update={"request_id": "different-request"})
    with pytest.raises(PolicyViolation, match="request does not match"):
        RevisionBoundArchitectContextFactory.for_session(
            request_case.session,
            changed_request,
            request_case.registry,
            graph_store=request_case.graph_store,
            runtime=request_case.runtime,
            entry_paths=(request_case.entry,),
            wiki_root=WIKI_ROOT,
            wiki_query=request_case.query,
            wiki_as_of=AS_OF,
            platform_adapter=request_case.adapter,
        )

    registry_case = _case(tmp_path / "registry", Platform.SALESFORCE)
    wrong_digests = registry_case.session.context.agent_definition_digests.model_copy(
        update={"architect": "sha256:" + "0" * 64}
    )
    registry_case.session = AgentRunSession.initialize(
        registry_case.project,
        Path(".runs/wrong-registry"),
        run_id="wrong-registry-run",
        thread_id="wrong-registry-thread",
        slice_id="wrong-registry-slice",
        source_root="source",
        request_digest=artifact_digest(registry_case.request),
        agent_definition_digests=wrong_digests,
        provider_id="offline-test",
        model_id="structured-agent/v1",
    )
    registry_case.graph_store = GraphSnapshotStore(registry_case.session.evidence_dir / "graphs")
    with pytest.raises(PolicyViolation, match="agent definitions"):
        _factory(registry_case)

    source_case = _case(tmp_path / "source", Platform.SALESFORCE)
    (source_case.source / "changed.cls").write_text("changed\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="source content revision changed"):
        _factory(source_case)

    root_case = _case(tmp_path / "source-root", Platform.SALESFORCE)
    root_factory = _factory(root_case)
    different_root = root_case.project / "different-source"
    different_root.mkdir()
    root_factory.source_root = different_root.resolve()
    with pytest.raises(PolicyViolation, match="source root differs"):
        root_factory(root_case.request)

    store_case = _case(tmp_path / "store", Platform.SALESFORCE)
    outside_store = GraphSnapshotStore(store_case.project / "outside-graphs")
    with pytest.raises(PolicyViolation, match="confined"):
        _factory(store_case, graph_store=outside_store)


def test_wrong_adapter_platform_target_and_revision_fail_closed(tmp_path: Path) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)
    wrong_adapter = _adapter(Platform.MULESOFT)
    with pytest.raises(PolicyViolation, match="adapter does not match"):
        RevisionBoundArchitectContextFactory(
            source_root=case.source,
            graph_store=GraphSnapshotStore(tmp_path / "wrong-adapter-graphs"),
            runtime=case.runtime,
            entry_paths=(case.entry,),
            wiki_root=WIKI_ROOT,
            wiki_query=case.query,
            wiki_as_of=AS_OF,
            platform_adapter=wrong_adapter,
        )

    wrong_platform = case.request.model_copy(update={"platform": Platform.MULESOFT})
    direct = RevisionBoundArchitectContextFactory(
        source_root=case.source,
        graph_store=GraphSnapshotStore(tmp_path / "direct-graphs"),
        runtime=case.runtime,
        entry_paths=(case.entry,),
        wiki_root=WIKI_ROOT,
        wiki_query=case.query,
        wiki_as_of=AS_OF,
        platform_adapter=case.adapter,
    )
    with pytest.raises(PolicyViolation, match="runtime does not match"):
        direct(wrong_platform)

    outside_target = case.request.model_copy(
        update={"target": case.request.target.model_copy(update={"entry_path": "elsewhere.cls"})}
    )
    with pytest.raises(PolicyViolation, match="outside the dependency-graph entry set"):
        direct(outside_target)

    stale = case.request.model_copy(update={"base_revision": "sha256:" + "0" * 64})
    with pytest.raises(PolicyViolation, match="current source revision"):
        direct(stale)


def test_direct_factory_rejects_graph_cache_overlapping_source(tmp_path: Path) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)
    embedded_store = GraphSnapshotStore(case.source / ".graph-cache")

    with pytest.raises(PolicyViolation, match="must not overlap the immutable source root"):
        RevisionBoundArchitectContextFactory(
            source_root=case.source,
            graph_store=embedded_store,
            runtime=case.runtime,
            entry_paths=(case.entry,),
            wiki_root=WIKI_ROOT,
            wiki_query=case.query,
            wiki_as_of=AS_OF,
            platform_adapter=case.adapter,
        )

    assert not tuple(embedded_store.root.rglob("*.json"))


def test_source_oracle_is_denied_before_any_oracle_file_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.cls").write_text("safe\n", encoding="utf-8")
    oracle = source / "nested" / "golden"
    oracle.mkdir(parents=True)
    (oracle / "answer.cls").write_text("answer\n", encoding="utf-8")
    request = MigrationRequest(
        request_id="oracle-denial",
        platform=Platform.SALESFORCE,
        repository="source",
        base_revision="sha256:" + "0" * 64,
        target=MigrationTarget(
            entry_path="safe.cls",
            target_runtime="LWC",
            source_version="Salesforce API 67.0",
            target_version="Salesforce API 67.0",
            description="Reject oracle bytes.",
        ),
    )
    factory = RevisionBoundArchitectContextFactory(
        source_root=source,
        graph_store=GraphSnapshotStore(tmp_path / "graphs"),
        runtime=PlatformRuntimeConfig(
            Platform.SALESFORCE,
            SALESFORCE_ANALYZER_VERSION,
            build_salesforce_dependency_graph,
        ),
        entry_paths=("safe.cls",),
        wiki_root=WIKI_ROOT,
        wiki_query="Visualforce LWC",
        wiki_as_of=AS_OF,
        platform_adapter=_adapter(Platform.SALESFORCE),
    )
    actual_open = os.open
    forbidden_opens: list[str] = []

    def guarded_open(path, *args, **kwargs):
        rendered = os.fspath(path)
        if any(str(part).casefold() == "golden" for part in Path(rendered).parts):
            forbidden_opens.append(str(rendered))
            raise AssertionError("oracle bytes must never be opened")
        return actual_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    with pytest.raises(PolicyViolation, match="expected, golden, or oracle"):
        factory(request)
    assert forbidden_opens == []


def test_wiki_version_no_hit_and_oracle_tree_fail_closed(tmp_path: Path) -> None:
    no_hit_case = _case(tmp_path / "no-hit", Platform.SALESFORCE)
    no_hit_request = no_hit_case.request.model_copy(
        update={
            "target": no_hit_case.request.target.model_copy(
                update={"source_version": "Salesforce API 1.0"}
            )
        }
    )
    direct = RevisionBoundArchitectContextFactory(
        source_root=no_hit_case.source,
        graph_store=GraphSnapshotStore(tmp_path / "no-hit-graphs"),
        runtime=no_hit_case.runtime,
        entry_paths=(no_hit_case.entry,),
        wiki_root=WIKI_ROOT,
        wiki_query=no_hit_case.query,
        wiki_as_of=AS_OF,
        platform_adapter=no_hit_case.adapter,
    )
    with pytest.raises(PolicyViolation, match="no Architect evidence"):
        direct(no_hit_request)

    oracle_case = _case(tmp_path / "oracle-wiki", Platform.SALESFORCE)
    copied_wiki = tmp_path / "oracle-wiki-copy"
    shutil.copytree(WIKI_ROOT, copied_wiki)
    hidden = copied_wiki / "pages" / "oracle"
    hidden.mkdir()
    (hidden / "answer.md").write_text("# answer\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="expected, golden, or oracle"):
        _factory(oracle_case, wiki_root=copied_wiki)(oracle_case.request)


def test_session_lifecycle_binding_detects_self_consistent_cache_rewrite(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)
    factory = _factory(case)
    result = factory(case.request)
    assert isinstance(result, ArchitectContext)
    graph_path = next(case.graph_store.root.glob("snapshots/v1/salesforce/*.json"))
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    payload["graph"]["nodes"][0]["name"] = "attacker-rewritten-node"
    payload["graph_digest"] = artifact_digest(DependencyGraph.model_validate(payload["graph"]))
    graph_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PolicyViolation, match="graph binding|artifact digest mismatch"):
        factory(case.request)


def test_runtime_anchor_detects_fully_recomputed_portable_graph_chain(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)
    factory = _factory(case)
    result = factory(case.request)
    assert isinstance(result, ArchitectContext)

    graph_path = next(case.graph_store.root.glob("snapshots/v1/salesforce/*.json"))
    graph_key_digest = graph_path.stem
    binding_path = case.session.evidence_dir / "bindings" / f"graph-{graph_key_digest}.json"
    index_path = case.session.evidence_dir / "indexes" / f"graph-{graph_key_digest[:32]}.json"
    graph_relative = graph_path.relative_to(case.session.evidence_dir).as_posix()
    binding_relative = binding_path.relative_to(case.session.evidence_dir).as_posix()

    graph_payload = json.loads(graph_path.read_text(encoding="utf-8"))
    graph_payload["graph"]["nodes"][0]["name"] = "fully-recomputed-attacker-node"
    graph_payload["graph_digest"] = artifact_digest(
        DependencyGraph.model_validate(graph_payload["graph"])
    )
    graph_path.write_text(json.dumps(graph_payload), encoding="utf-8")

    binding_payload = json.loads(binding_path.read_text(encoding="utf-8"))
    binding_payload["graph_artifact_digest"] = artifact_digest(graph_payload)
    binding_path.write_text(json.dumps(binding_payload), encoding="utf-8")

    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    for artifact in index_payload["artifacts"]:
        if artifact["path"] == graph_relative:
            artifact["canonical_sha256"] = artifact_digest(graph_payload)
        elif artifact["path"] == binding_relative:
            artifact["canonical_sha256"] = artifact_digest(binding_payload)
    index_path.write_text(json.dumps(index_payload), encoding="utf-8")

    assert tuple(case.session.runtime_anchors_dir.glob("graph-*.json"))
    with pytest.raises(PolicyViolation, match="runtime evidence anchor digest mismatch"):
        factory(case.request)


@pytest.mark.parametrize("mutation", ("tampered", "stale"))
def test_session_graph_assurance_rejects_tampered_or_stale_report(
    tmp_path: Path,
    mutation: str,
) -> None:
    case = _case(tmp_path, Platform.SALESFORCE)
    factory = _factory(case)
    assert isinstance(factory(case.request), ArchitectContext)
    report_path = next(case.session.evidence_dir.glob("graphs/graph-assurance-*.json"))
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if mutation == "tampered":
        payload["dependency_graph_digest"] = "sha256:" + "0" * 64
    else:
        payload["source_revision"] = "sha256:" + "0" * 64
        payload["report_id"] = _report_id(
            {key: value for key, value in payload.items() if key != "report_id"}
        )
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        PolicyViolation,
        match="lifecycle evidence is malformed|differs from current reconciliation",
    ):
        factory(case.request)
