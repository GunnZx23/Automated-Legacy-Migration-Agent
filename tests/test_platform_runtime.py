from __future__ import annotations

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
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectContext,
    ArchitectManifestProposal,
)
from legacy_migration_agent.agent_runtime.model_workflow import ModelAgentWorkflowRoles
from legacy_migration_agent.contracts import (
    ApprovalAction,
    DependencyEvidence,
    MigrationManifest,
    MigrationRequest,
    MigrationTarget,
    PlanningIntervention,
    Platform,
    TransformationStep,
    ValidationCommand,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.run_session import (
    AgentDefinitionDigests,
    AgentRunSession,
)
from legacy_migration_agent.core.scope_policy import MigrationScopePolicy, PlatformAdapter
from legacy_migration_agent.core.workspace import content_revision
from legacy_migration_agent.graphs.dependency_graph import (
    SALESFORCE_ANALYZER_VERSION,
    build_salesforce_dependency_graph,
)
from legacy_migration_agent.graphs.graph_contracts import DependencyGraph
from legacy_migration_agent.graphs.graph_store import GraphSnapshotStore
from legacy_migration_agent.graphs.mulesoft_dependency_graph import (
    MULESOFT_ANALYZER_VERSION,
    build_mulesoft_dependency_graph,
)
from legacy_migration_agent.platforms.platform_runtime import (
    PlatformGraphBuilder,
    PlatformRuntimeConfig,
    RevisionBoundArchitectContextFactory,
)
from legacy_migration_agent.workflow import (
    ApprovalSelection,
    ManifestApproval,
    WorkflowStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WIKI_ROOT = PROJECT_ROOT / "knowledge/wiki"
AGENTS_ROOT = PROJECT_ROOT / "agents"
AS_OF = date(2026, 8, 26)
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
        if output_type is not ArchitectManifestProposal:
            raise AssertionError("Engineer and Validator must remain unreachable")
        context = ArchitectContext.model_validate(input_value)
        output_path = f"generated/{context.request.platform.value}.txt"
        manifest = MigrationManifest(
            manifest_id="durable-session-manifest",
            request_id=context.request.request_id,
            platform=context.request.platform,
            base_revision=context.request.base_revision,
            approved_paths=(output_path,),
            dependencies=(
                DependencyEvidence(
                    path=context.request.target.entry_path,
                    relation="migration source",
                    source="session-bound test",
                ),
            ),
            transformations=(
                TransformationStep(
                    step_id="create-output",
                    description="Create the approved generated output.",
                    input_paths=(context.request.target.entry_path,),
                    output_paths=(output_path,),
                ),
            ),
            validation_plan=(
                ValidationCommand(
                    check_id="local-check",
                    command_id="local-check",
                    purpose="Exercise the durable session approval boundary.",
                ),
            ),
            required_approvals=(ApprovalAction.APPROVE_MANIFEST,),
        )
        return ArchitectManifestProposal(
            manifest=manifest,
            scope_policy_digest=context.platform_adapter.scope_policy_digest,
            public_decisions=("Propose only the policy-approved generated output.",),
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
    assert first.wiki_trace.hits
    assert tuple(case.session.evidence_dir.glob("indexes/graph-*.json"))

    changed_counter = CountingBuilder(case.runtime.graph_builder)
    changed_runtime = PlatformRuntimeConfig(
        platform,
        case.runtime.analyzer_version + ".audit2",
        changed_counter,
    )
    changed = _factory(case, runtime=changed_runtime)(case.request)
    assert isinstance(changed, ArchitectContext)
    assert changed_counter.calls == 1
    assert len(tuple(case.session.evidence_dir.glob("indexes/graph-*.json"))) == 2


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
    binding_path = next((case.session.evidence_dir / "bindings").glob("graph-*.json"))
    index_path = next(case.session.evidence_dir.glob("indexes/graph-*.json"))
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
