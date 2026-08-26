from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from legacy_migration_agent.agent_runtime.agent_definitions import (
    AgentRegistry,
    AgentRole,
    load_agent_registry,
)
from legacy_migration_agent.agent_runtime.correction import CorrectionAttemptEvidence
from legacy_migration_agent.agent_runtime.model_agents import (
    AgentRuntimeError,
    ArchitectAgent,
    ArchitectContext,
    ArchitectManifestProposal,
    EngineerAgent,
    EngineerCorrectionContext,
    EngineerFilePlan,
    EngineerFileUpdate,
    EngineerInterventionOutcome,
    EngineerModelOutcome,
    EngineerWorkspaceContext,
    ValidatorAdvisory,
    ValidatorAgent,
    ValidatorEvidenceContext,
    _candidate_failure_supports_jest_correlation,
    validate_implementation_intervention,
)
from legacy_migration_agent.agent_runtime.openai_model import ModelEvidenceError
from legacy_migration_agent.contracts import (
    ApprovalAction,
    ChangeSet,
    CheckResult,
    CheckStatus,
    DependencyEvidence,
    EnvironmentKind,
    ImplementationIntervention,
    ImplementationInterventionEvidence,
    ManifestStatus,
    MigrationManifest,
    MigrationRequest,
    MigrationTarget,
    PlanningInterventionOption,
    Platform,
    RiskCategory,
    RiskFinding,
    ToolReceipt,
    TransformationStep,
    ValidationCommand,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.scope_policy import MigrationScopePolicy, PlatformAdapter
from legacy_migration_agent.core.workspace import IsolatedWorkspace, content_revision
from legacy_migration_agent.graphs.dependency_graph import build_salesforce_dependency_graph
from legacy_migration_agent.knowledge.wiki import LlmWiki
from legacy_migration_agent.platforms.local_checks import (
    SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS,
    SALESFORCE_LWC_JEST_DIAGNOSTIC_IDS,
)

PROJECT_ROOT = Path(__file__).parents[1]
AGENT_REGISTRY = load_agent_registry(PROJECT_ROOT / "agents")
SALESFORCE_INPUT = PROJECT_ROOT / "fixtures" / "salesforce" / "account-contact-explorer" / "input"
WIKI_ROOT = PROJECT_ROOT / "knowledge" / "wiki"
VF_ENTRY = "force-app/main/default/pages/LegacyAccountContactExplorer.page"
TEST_IMPLEMENTATION_CONTRACT = (
    "Create only the approved additive output while preserving the legacy source.",
    "Keep validation local and leave external actions behind human authority gates.",
)


class CapturingModel:
    provider = "test-double"
    model_id = "structured-role-test/v1"
    live_invocation = False
    store_false_sent = False

    def __init__(self, response) -> None:
        self.response = response
        self.calls = []

    def parse(self, *, system_prompt, input_value, output_type):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "input_value": input_value,
                "output_type": output_type,
            }
        )
        if output_type is EngineerModelOutcome and isinstance(self.response, EngineerFilePlan):
            return EngineerModelOutcome.for_file_plan(self.response)
        return self.response


def migration_request(base_revision: str, *, entry_path: str = VF_ENTRY) -> MigrationRequest:
    return MigrationRequest(
        request_id="request-agent-test",
        platform=Platform.SALESFORCE,
        repository="public-fixture",
        base_revision=base_revision,
        target=MigrationTarget(
            entry_path=entry_path,
            target_runtime="Salesforce API 67.0",
            source_version="Salesforce API 67.0",
            target_version="Salesforce API 67.0",
            description="Additive Visualforce to LWC migration",
        ),
    )


def manifest_for(
    request: MigrationRequest,
    *,
    input_path: str,
    output_path: str,
) -> MigrationManifest:
    return MigrationManifest(
        manifest_id="manifest-agent-test",
        request_id=request.request_id,
        platform=request.platform,
        base_revision=request.base_revision,
        approved_paths=(output_path,),
        dependencies=(
            DependencyEvidence(
                path=input_path,
                relation="migration source",
                source="frozen dependency graph",
            ),
        ),
        transformations=(
            TransformationStep(
                step_id="add-bounded-output",
                description="Create the approved additive migration output",
                input_paths=(input_path,),
                output_paths=(output_path,),
            ),
        ),
        validation_plan=(
            ValidationCommand(
                check_id="local-check",
                command_id="local-check",
                purpose="Run the predeclared deterministic local check",
            ),
        ),
        implementation_contract=TEST_IMPLEMENTATION_CONTRACT,
        required_approvals=(ApprovalAction.APPROVE_MANIFEST,),
    )


def salesforce_adapter(
    *,
    max_changed_files: int = 1,
    required_commands: tuple[str, ...] = ("local-check",),
) -> PlatformAdapter:
    policy = MigrationScopePolicy(
        policy_id="salesforce-agent-test-policy",
        platform=Platform.SALESFORCE,
        required_source_input_paths=(VF_ENTRY,),
        approved_output_roots=("force-app/main/default/lwc",),
        approved_output_extensions=(".js", ".html", ".xml"),
        forbidden_paths=(
            VF_ENTRY,
            "force-app/main/default/lwc/golden",
        ),
        allowed_validation_command_ids=("local-check", "jest"),
        required_validation_command_ids=required_commands,
        required_implementation_contract=TEST_IMPLEMENTATION_CONTRACT,
        max_changed_files=max_changed_files,
    )
    return PlatformAdapter.bind(adapter_id="salesforce-test-adapter", policy=policy)


def architect_context() -> ArchitectContext:
    revision = content_revision(SALESFORCE_INPUT)
    request = migration_request(revision)
    graph = build_salesforce_dependency_graph(SALESFORCE_INPUT, (VF_ENTRY,), revision)
    wiki_trace = LlmWiki.load(WIKI_ROOT).search(
        "Visualforce LWC migration security Jest",
        platform=Platform.SALESFORCE,
        source_version="Salesforce API 67.0",
        target_version="Salesforce API 67.0",
        as_of=date(2026, 8, 26),
    )
    return ArchitectContext(
        request=request,
        dependency_graph=graph,
        dependency_graph_digest=artifact_digest(graph),
        wiki_trace=wiki_trace,
        wiki_trace_digest=artifact_digest(wiki_trace),
        platform_adapter=salesforce_adapter(),
    )


def registry_with_definition_drift(
    role: AgentRole,
    *,
    prompt_suffix: str | None = None,
    version: str | None = None,
) -> AgentRegistry:
    definitions = {definition.role: definition for definition in AGENT_REGISTRY.definitions}
    current = definitions[role]
    if prompt_suffix is not None:
        current = current.model_copy(update={"prompt": current.prompt + prompt_suffix})
    if version is not None:
        current = current.model_copy(
            update={"header": current.header.model_copy(update={"version": version})}
        )
    definitions[role] = current
    return AgentRegistry(definitions)


def test_architect_uses_versioned_prompt_and_receives_frozen_wiki_content() -> None:
    context = architect_context()
    output_path = "force-app/main/default/lwc/example/example.js"
    proposal = ArchitectManifestProposal(
        manifest=manifest_for(context.request, input_path=VF_ENTRY, output_path=output_path),
        scope_policy_digest=context.platform_adapter.scope_policy_digest,
        public_decisions=("Add the LWC beside the preserved Visualforce entry point.",),
        cited_graph_nodes=(context.dependency_graph.nodes[0].node_id,),
        cited_wiki_pages=(context.wiki_trace.hits[0].page_id,),
    )
    model = CapturingModel(proposal)

    result = ArchitectAgent(AGENT_REGISTRY, model).propose(context)

    assert result.proposal == proposal
    assert result.model_call.agent_version == "architect/v3"
    assert result.model_call.live_invocation is False
    assert len(model.calls) == 1
    call = model.calls[0]
    assert "Identity: You are the Architect agent." in call["system_prompt"]
    assert call["output_type"] is ArchitectManifestProposal
    assert call["input_value"].wiki_trace.hits[0].selected_content
    assert "Visualforce" in call["input_value"].wiki_trace.hits[0].selected_content


@pytest.mark.parametrize(
    ("registry", "mismatch"),
    (
        (
            registry_with_definition_drift(
                AgentRole.ARCHITECT,
                prompt_suffix="\n\nReplay-incompatible prompt drift.",
            ),
            "system prompt",
        ),
        (
            registry_with_definition_drift(
                AgentRole.ARCHITECT,
                version="architect/v4",
            ),
            "agent version",
        ),
    ),
)
def test_architect_replay_rejects_current_prompt_or_version_drift(
    registry: AgentRegistry,
    mismatch: str,
) -> None:
    context = architect_context()
    proposal = _proposal(
        context,
        _manifest_with_paths(
            context,
            "force-app/main/default/lwc/example/example.js",
        ),
    )
    model = CapturingModel(proposal)
    run = ArchitectAgent(AGENT_REGISTRY, model).propose(context)

    with pytest.raises(ModelEvidenceError, match=mismatch):
        ArchitectAgent(registry, model).verify_replay(run, context)
    assert len(model.calls) == 1


def test_architect_cannot_cite_wiki_content_outside_frozen_trace() -> None:
    context = architect_context()
    proposal = ArchitectManifestProposal(
        manifest=manifest_for(
            context.request,
            input_path=VF_ENTRY,
            output_path="force-app/main/default/lwc/example/example.js",
        ),
        scope_policy_digest=context.platform_adapter.scope_policy_digest,
        public_decisions=("Add one bounded component.",),
        cited_graph_nodes=(context.dependency_graph.nodes[0].node_id,),
        cited_wiki_pages=("unretrieved-page",),
    )

    with pytest.raises(AgentRuntimeError, match="outside the frozen trace"):
        ArchitectAgent(AGENT_REGISTRY, CapturingModel(proposal)).propose(context)


def _proposal(
    context: ArchitectContext,
    manifest: MigrationManifest,
    *,
    unresolved_questions: tuple[str, ...] = (),
    scope_policy_digest: str | None = None,
) -> ArchitectManifestProposal:
    return ArchitectManifestProposal(
        manifest=manifest,
        scope_policy_digest=(scope_policy_digest or context.platform_adapter.scope_policy_digest),
        public_decisions=("Use only the caller-approved migration scope.",),
        cited_graph_nodes=(context.dependency_graph.nodes[0].node_id,),
        cited_wiki_pages=(context.wiki_trace.hits[0].page_id,),
        unresolved_questions=unresolved_questions,
    )


def _manifest_with_paths(
    context: ArchitectContext,
    *paths: str,
) -> MigrationManifest:
    base = manifest_for(
        context.request,
        input_path=VF_ENTRY,
        output_path=paths[0],
    )
    transformation = base.transformations[0].model_copy(update={"output_paths": paths})
    return base.model_copy(
        update={
            "approved_paths": paths,
            "transformations": (transformation,),
        }
    )


def test_architect_rejects_wrong_scope_policy_digest() -> None:
    context = architect_context()
    manifest = _manifest_with_paths(
        context,
        "force-app/main/default/lwc/example/example.js",
    )

    with pytest.raises(AgentRuntimeError, match="wrong scope policy"):
        ArchitectAgent(
            AGENT_REGISTRY,
            CapturingModel(
                _proposal(
                    context,
                    manifest,
                    scope_policy_digest="sha256:" + "f" * 64,
                )
            ),
        ).propose(context)


def test_architect_rejects_hostile_extra_output_path() -> None:
    base_context = architect_context()
    adapter = salesforce_adapter(max_changed_files=2)
    context = base_context.model_copy(update={"platform_adapter": adapter})
    manifest = _manifest_with_paths(
        context,
        "force-app/main/default/lwc/example/example.js",
        "outside.txt",
    )

    with pytest.raises(AgentRuntimeError, match="outside the scope policy"):
        ArchitectAgent(AGENT_REGISTRY, CapturingModel(_proposal(context, manifest))).propose(
            context
        )


def test_architect_rejects_forbidden_golden_output_path() -> None:
    context = architect_context()
    manifest = _manifest_with_paths(
        context,
        "force-app/main/default/lwc/golden/expected.js",
    )

    with pytest.raises(AgentRuntimeError, match="outside the scope policy"):
        ArchitectAgent(AGENT_REGISTRY, CapturingModel(_proposal(context, manifest))).propose(
            context
        )


def test_architect_rejects_unknown_or_omitted_validation_commands() -> None:
    context = architect_context()
    manifest = _manifest_with_paths(
        context,
        "force-app/main/default/lwc/example/example.js",
    )
    unknown = manifest.model_copy(
        update={
            "validation_plan": (
                ValidationCommand(
                    check_id="shell",
                    command_id="arbitrary-shell",
                    purpose="Attempt an undeclared command.",
                ),
            )
        }
    )
    with pytest.raises(AgentRuntimeError, match="outside the scope policy"):
        ArchitectAgent(AGENT_REGISTRY, CapturingModel(_proposal(context, unknown))).propose(context)

    required_adapter = salesforce_adapter(required_commands=("local-check", "jest"))
    required_context = context.model_copy(update={"platform_adapter": required_adapter})
    with pytest.raises(AgentRuntimeError, match="omits required validation commands: jest"):
        ArchitectAgent(
            AGENT_REGISTRY,
            CapturingModel(_proposal(required_context, manifest)),
        ).propose(required_context)


def test_architect_rejects_excessive_file_count() -> None:
    context = architect_context()
    manifest = _manifest_with_paths(
        context,
        "force-app/main/default/lwc/one/one.js",
        "force-app/main/default/lwc/two/two.js",
    )

    with pytest.raises(AgentRuntimeError, match="max_changed_files"):
        ArchitectAgent(AGENT_REGISTRY, CapturingModel(_proposal(context, manifest))).propose(
            context
        )


def test_architect_rejects_wrong_platform_adapter_before_model_invocation() -> None:
    context = architect_context()
    mule_policy = MigrationScopePolicy(
        policy_id="wrong-platform-policy",
        platform=Platform.MULESOFT,
        required_source_input_paths=("src/main/mule/legacy.xml",),
        approved_output_paths=("src/main/mule/app.xml",),
        allowed_validation_command_ids=("munit",),
        required_validation_command_ids=("munit",),
        max_changed_files=1,
    )
    hostile_context = context.model_copy(
        update={
            "platform_adapter": PlatformAdapter.bind(
                adapter_id="wrong-platform-adapter",
                policy=mule_policy,
            )
        }
    )
    model = CapturingModel(
        _proposal(
            context,
            _manifest_with_paths(
                context,
                "force-app/main/default/lwc/example/example.js",
            ),
        )
    )

    with pytest.raises(ValidationError, match="platform adapter does not match the request"):
        ArchitectAgent(AGENT_REGISTRY, model).propose(hostile_context)
    assert model.calls == []


def test_architect_rejects_unresolved_graph_before_model_invocation() -> None:
    context = architect_context()
    unresolved_node = context.dependency_graph.nodes[0].model_copy(update={"resolved": False})
    unresolved_graph = context.dependency_graph.model_copy(
        update={"nodes": (unresolved_node, *context.dependency_graph.nodes[1:])}
    )
    hostile_context = context.model_copy(
        update={
            "dependency_graph": unresolved_graph,
            "dependency_graph_digest": artifact_digest(unresolved_graph),
        }
    )
    model = CapturingModel(
        _proposal(
            context,
            _manifest_with_paths(
                context,
                "force-app/main/default/lwc/example/example.js",
            ),
        )
    )

    with pytest.raises(ValidationError, match="unresolved dependency graph"):
        ArchitectAgent(AGENT_REGISTRY, model).propose(hostile_context)
    assert model.calls == []


def test_architect_rejects_dependency_graph_for_unrelated_entry_before_model_call() -> None:
    context = architect_context()
    unrelated_graph = build_salesforce_dependency_graph(
        SALESFORCE_INPUT,
        ("sfdx-project.json",),
        context.request.base_revision,
    )
    hostile_context = context.model_copy(
        update={
            "dependency_graph": unrelated_graph,
            "dependency_graph_digest": artifact_digest(unrelated_graph),
        }
    )
    model = CapturingModel(
        _proposal(
            context,
            _manifest_with_paths(
                context,
                "force-app/main/default/lwc/example/example.js",
            ),
        )
    )

    with pytest.raises(ValidationError, match="outside the dependency graph entries"):
        ArchitectAgent(AGENT_REGISTRY, model).propose(hostile_context)
    assert model.calls == []


def test_unresolved_questions_cannot_bypass_decision_required_state() -> None:
    context = architect_context()
    manifest = _manifest_with_paths(
        context,
        "force-app/main/default/lwc/example/example.js",
    )

    with pytest.raises(AgentRuntimeError, match="require a decision_required manifest"):
        ArchitectAgent(
            AGENT_REGISTRY,
            CapturingModel(
                _proposal(
                    context,
                    manifest,
                    unresolved_questions=("Should the scope be expanded?",),
                )
            ),
        ).propose(context)


def test_decision_required_question_can_return_but_never_reaches_engineer(
    tmp_path: Path,
) -> None:
    context = architect_context()
    planned = _manifest_with_paths(
        context,
        "force-app/main/default/lwc/example/example.js",
    )
    manifest = planned.model_copy(
        update={
            "status": ManifestStatus.DECISION_REQUIRED,
            "risks": (
                RiskFinding(
                    category=RiskCategory.INCOMPLETE_EVIDENCE,
                    summary="A human must decide whether the incomplete evidence is acceptable.",
                ),
            ),
            "required_approvals": (ApprovalAction.APPROVE_MANIFEST,),
        }
    )
    proposal = _proposal(
        context,
        manifest,
        unresolved_questions=("Is the incomplete evidence acceptable?",),
    )

    result = ArchitectAgent(AGENT_REGISTRY, CapturingModel(proposal)).propose(context)
    assert result.proposal.manifest.status is ManifestStatus.DECISION_REQUIRED

    engineer_model = CapturingModel(
        EngineerFilePlan(
            updates=(
                EngineerFileUpdate(
                    path=manifest.approved_paths[0],
                    content="blocked\n",
                ),
            )
        )
    )
    with IsolatedWorkspace(
        SALESFORCE_INPUT,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=context.request.base_revision,
    ) as workspace:
        with pytest.raises(PolicyViolation, match="requires a human decision"):
            EngineerAgent(AGENT_REGISTRY, engineer_model).implement(
                context.request,
                manifest,
                workspace,
            )
    assert engineer_model.calls == []


def test_architect_output_schema_requires_exact_manifest_approval_gate() -> None:
    schema = ArchitectManifestProposal.model_json_schema(mode="validation")
    architect_manifest = schema["$defs"]["MigrationManifest"]
    approval_schema = architect_manifest["properties"]["required_approvals"]
    implementation_schema = architect_manifest["properties"]["implementation_contract"]

    assert "required_approvals" in architect_manifest["required"]
    assert approval_schema["minItems"] == 1
    assert approval_schema["maxItems"] == 1
    assert approval_schema["items"] == {
        "const": ApprovalAction.APPROVE_MANIFEST.value,
        "type": "string",
    }
    assert "implementation_contract" in architect_manifest["required"]
    assert implementation_schema["minItems"] == 1
    assert "default" not in implementation_schema

    general_manifest = MigrationManifest.model_json_schema(mode="validation")
    assert "required_approvals" not in general_manifest["required"]
    assert general_manifest["properties"]["required_approvals"]["default"] == []


@pytest.mark.parametrize("invalid_approvals", (None, (ApprovalAction.EXPAND_SCOPE,)))
def test_architect_output_contract_rejects_missing_or_wrong_manifest_approval(
    invalid_approvals: tuple[ApprovalAction, ...] | None,
) -> None:
    context = architect_context()
    manifest = _manifest_with_paths(
        context,
        "force-app/main/default/lwc/example/example.js",
    )
    payload = _proposal(context, manifest).model_dump(mode="python")
    manifest_payload = dict(payload["manifest"])
    if invalid_approvals is None:
        manifest_payload.pop("required_approvals")
    else:
        manifest_payload["required_approvals"] = invalid_approvals
    payload["manifest"] = manifest_payload

    with pytest.raises(ValidationError, match="approve_manifest gate"):
        ArchitectManifestProposal.model_validate(payload)


def test_architect_output_contract_accepts_exact_manifest_approval() -> None:
    context = architect_context()
    manifest = _manifest_with_paths(
        context,
        "force-app/main/default/lwc/example/example.js",
    )

    proposal = _proposal(context, manifest)

    assert proposal.manifest.required_approvals == (ApprovalAction.APPROVE_MANIFEST,)


def engineer_fixture(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.txt").write_text("legacy\n", encoding="utf-8")
    revision = content_revision(source)
    request = migration_request(revision, entry_path="source.txt")
    manifest = manifest_for(request, input_path="source.txt", output_path="output.txt")
    return source, request, manifest


def implementation_intervention(
    context: EngineerWorkspaceContext,
) -> ImplementationIntervention:
    output_path = context.manifest.approved_paths[0]
    return ImplementationIntervention(
        intervention_id="implementation-stop-agent-test",
        request_id=context.request.request_id,
        request_digest=context.request_digest,
        manifest_id=context.manifest.manifest_id,
        manifest_digest=context.manifest_digest,
        base_revision=context.workspace_base_revision,
        agent_version=context.agent_version,
        agent_definition_digest=context.agent_definition_digest,
        input_evidence_digest=context.input_evidence_digest,
        reason="The supplied source omits a required public contract.",
        requested_action=ApprovalAction.EXPAND_SCOPE,
        affected_paths=(output_path,),
        public_concerns=("The approved output cannot be implemented without guessing.",),
        evidence=(
            ImplementationInterventionEvidence(
                source="engineer_input",
                source_digest=context.input_evidence_digest,
                summary="The complete frozen Engineer input lacks the required contract.",
                affected_paths=(output_path,),
            ),
        ),
        options=(
            PlanningInterventionOption.EXPAND_SCOPE_AND_REPLAN,
            PlanningInterventionOption.STOP_REQUEST,
        ),
        recommendation="Resolve the evidence gap and create a revised manifest.",
    )


class InterventionModel(CapturingModel):
    def __init__(self) -> None:
        super().__init__(None)

    def parse(self, *, system_prompt, input_value, output_type):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "input_value": input_value,
                "output_type": output_type,
            }
        )
        context = EngineerWorkspaceContext.model_validate(input_value)
        return EngineerModelOutcome(
            result=EngineerInterventionOutcome(
                kind="decision_required",
                intervention=implementation_intervention(context),
            )
        )


class RawStructuredResponse:
    def __init__(self, payload) -> None:
        self.payload = payload

    def model_dump(self, *, mode):
        assert mode == "python"
        return self.payload


class HostileInterventionModel(InterventionModel):
    def __init__(self, attack: str) -> None:
        super().__init__()
        self.attack = attack

    def parse(self, *, system_prompt, input_value, output_type):
        outcome = super().parse(
            system_prompt=system_prompt,
            input_value=input_value,
            output_type=output_type,
        )
        payload = outcome.model_dump(mode="python")
        if self.attack == "mixed":
            payload["result"]["file_plan"] = {
                "updates": ({"path": "output.txt", "content": "hostile\n"},),
                "assumptions": (),
            }
        elif self.attack == "path":
            payload["result"]["intervention"]["affected_paths"] = ("../escape.txt",)
        else:  # pragma: no cover - test helper is closed over two attacks
            raise AssertionError(self.attack)
        return RawStructuredResponse(payload)


def test_engineer_intervention_is_zero_update_terminal_evidence_and_replays_read_only(
    tmp_path: Path,
) -> None:
    source, request, manifest = engineer_fixture(tmp_path)
    source_before = content_revision(source)
    model = InterventionModel()
    agent = EngineerAgent(AGENT_REGISTRY, model)

    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        run = agent.implement(request, manifest, workspace)
        assert run.intervention is not None
        assert run.file_plan is None
        assert run.change_set is None
        assert run.workspace_after_revision is None
        assert run.model_call.agent_version == "engineer/v11"
        assert workspace.audit_changes().changed_paths == ()

    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        agent.verify_replay(run, request, manifest, workspace)
        assert workspace.audit_changes().changed_paths == ()

    assert len(model.calls) == 1
    assert content_revision(source) == source_before
    assert not (source / "output.txt").exists()


@pytest.mark.parametrize(
    ("binding", "error_type", "message"),
    (
        ("request", AgentRuntimeError, "request digest"),
        ("input", AgentRuntimeError, "input evidence digest"),
        ("output", ModelEvidenceError, "output"),
    ),
)
def test_engineer_intervention_replay_rejects_binding_or_output_tamper(
    tmp_path: Path,
    binding: str,
    error_type: type[Exception],
    message: str,
) -> None:
    source, request, manifest = engineer_fixture(tmp_path)
    model = InterventionModel()
    agent = EngineerAgent(AGENT_REGISTRY, model)
    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        run = agent.implement(request, manifest, workspace)

    intervention = run.intervention
    assert intervention is not None
    if binding == "request":
        replacement = intervention.model_copy(update={"request_digest": "sha256:" + "0" * 64})
    elif binding == "input":
        replacement = intervention.model_copy(
            update={"input_evidence_digest": "sha256:" + "0" * 64}
        )
    else:
        replacement = intervention.model_copy(
            update={"reason": "Hostile persisted public-output rewrite."}
        )
    tampered_result = run.model_outcome.result.model_copy(update={"intervention": replacement})
    tampered = run.model_copy(
        update={"model_outcome": run.model_outcome.model_copy(update={"result": tampered_result})}
    )

    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        with pytest.raises(error_type, match=message):
            agent.verify_replay(tampered, request, manifest, workspace)
        assert workspace.audit_changes().changed_paths == ()
    assert len(model.calls) == 1


@pytest.mark.parametrize(
    ("registry", "message"),
    (
        (
            registry_with_definition_drift(
                AgentRole.ENGINEER,
                prompt_suffix="\n\nReplay-incompatible Engineer prompt drift.",
            ),
            "system prompt",
        ),
        (
            registry_with_definition_drift(
                AgentRole.ENGINEER,
                version="engineer/v12",
            ),
            "agent version",
        ),
    ),
)
def test_engineer_intervention_replay_rejects_current_prompt_or_version_drift(
    tmp_path: Path,
    registry: AgentRegistry,
    message: str,
) -> None:
    source, request, manifest = engineer_fixture(tmp_path)
    model = InterventionModel()
    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        run = EngineerAgent(AGENT_REGISTRY, model).implement(request, manifest, workspace)

    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        with pytest.raises((AgentRuntimeError, ModelEvidenceError), match=message):
            EngineerAgent(registry, model).verify_replay(
                run,
                request,
                manifest,
                workspace,
            )
        assert workspace.audit_changes().changed_paths == ()
    assert len(model.calls) == 1


@pytest.mark.parametrize(
    ("attack", "message"),
    (("mixed", "Extra inputs"), ("path", "paths cannot contain")),
)
def test_engineer_rejects_hostile_mixed_outcome_or_traversal_without_writes(
    tmp_path: Path,
    attack: str,
    message: str,
) -> None:
    source, request, manifest = engineer_fixture(tmp_path)
    source_before = content_revision(source)
    model = HostileInterventionModel(attack)

    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        with pytest.raises(ValidationError, match=message):
            EngineerAgent(AGENT_REGISTRY, model).implement(request, manifest, workspace)
        assert workspace.audit_changes().changed_paths == ()

    assert len(model.calls) == 1
    assert content_revision(source) == source_before
    assert not (tmp_path / "escape.txt").exists()


def test_engineer_writes_exact_files_only_in_isolated_workspace_and_derives_diff(
    tmp_path: Path,
) -> None:
    source, request, manifest = engineer_fixture(tmp_path)
    original = (source / "source.txt").read_bytes()
    model = CapturingModel(
        EngineerFilePlan(
            updates=(EngineerFileUpdate(path="output.txt", content="modern\n"),),
            assumptions=("The bounded fixture is the complete supplied source.",),
        )
    )

    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        result = EngineerAgent(AGENT_REGISTRY, model).implement(request, manifest, workspace)
        assert (workspace.root / "output.txt").read_text(encoding="utf-8") == "modern\n"
        assert result.change_set.changed_paths == ("output.txt",)
        assert "+modern" in result.change_set.unified_diff
        assert result.workspace_after_revision != request.base_revision

    assert (source / "source.txt").read_bytes() == original
    assert not (source / "output.txt").exists()
    assert "Identity: You are the Engineer agent." in model.calls[0]["system_prompt"]
    assert "absence of a pre-existing LWC bundle" in model.calls[0]["system_prompt"]
    assert model.calls[0]["input_value"].source_files[0].content == "legacy\n"
    assert (
        "Approved target files are supposed to be new" in model.calls[0]["input_value"].instruction
    )
    assert (
        model.calls[0]["input_value"].manifest.implementation_contract
        == TEST_IMPLEMENTATION_CONTRACT
    )


@pytest.mark.parametrize("binding", ("input", "output"))
def test_engineer_replay_rejects_input_or_output_tamper_without_mutating_source(
    tmp_path: Path,
    binding: str,
) -> None:
    source, request, manifest = engineer_fixture(tmp_path)
    source_before = content_revision(source)
    model = CapturingModel(
        EngineerFilePlan(
            updates=(EngineerFileUpdate(path="output.txt", content="modern\n"),),
        )
    )
    agent = EngineerAgent(AGENT_REGISTRY, model)
    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        run = agent.implement(request, manifest, workspace)

    if binding == "input":
        tampered = run.model_copy(
            update={
                "model_call": run.model_call.model_copy(
                    update={"input_digest": "sha256:" + "0" * 64}
                )
            }
        )
    else:
        assert run.file_plan is not None
        replacement = run.file_plan.model_copy(
            update={"updates": (EngineerFileUpdate(path="output.txt", content="tampered\n"),)}
        )
        assert hasattr(run.model_outcome.result, "file_plan")
        tampered = run.model_copy(
            update={
                "model_outcome": run.model_outcome.model_copy(
                    update={
                        "result": run.model_outcome.result.model_copy(
                            update={"file_plan": replacement}
                        )
                    }
                )
            }
        )

    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as clean_workspace:
        with pytest.raises(ModelEvidenceError, match=binding):
            agent.verify_replay(tampered, request, manifest, clean_workspace)
        assert clean_workspace.audit_changes().changed_paths == ()

    assert content_revision(source) == source_before
    assert not (source / "output.txt").exists()
    assert len(model.calls) == 1


def test_engineer_revalidates_hostile_path_and_cannot_escape_workspace(tmp_path: Path) -> None:
    source, request, manifest = engineer_fixture(tmp_path)
    hostile = EngineerFilePlan.model_construct(
        updates=(EngineerFileUpdate.model_construct(path="../escape.txt", content="escaped\n"),),
        assumptions=(),
    )

    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        with pytest.raises(ValidationError, match="paths cannot contain"):
            EngineerAgent(AGENT_REGISTRY, CapturingModel(hostile)).implement(
                request, manifest, workspace
            )
        assert workspace.audit_changes().changed_paths == ()

    assert not (tmp_path / "escape.txt").exists()


def test_engineer_rejects_partial_or_expanded_scope_before_writing(tmp_path: Path) -> None:
    source, request, manifest = engineer_fixture(tmp_path)
    expanded = EngineerFilePlan(updates=(EngineerFileUpdate(path="outside.txt", content="no\n"),))
    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        with pytest.raises(AgentRuntimeError, match="scope mismatch"):
            EngineerAgent(AGENT_REGISTRY, CapturingModel(expanded)).implement(
                request, manifest, workspace
            )
        assert workspace.audit_changes().changed_paths == ()


def failed_validation_context(tmp_path: Path) -> tuple[ValidatorEvidenceContext, Path]:
    source, request, manifest = engineer_fixture(tmp_path)
    change_set = ChangeSet(
        change_set_id="changes-agent-test",
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        base_revision=manifest.base_revision,
        changed_paths=("output.txt",),
        unified_diff="diff --git a/output.txt b/output.txt\n+modern\n",
    )
    now = datetime(2026, 8, 24, tzinfo=UTC)
    receipt = ToolReceipt(
        receipt_id="receipt-local-check",
        tool_id="local-check",
        request_id=request.request_id,
        run_id="run-agent-test",
        attempt=1,
        base_revision=request.base_revision,
        environment=EnvironmentKind.LOCAL,
        input_artifact_digest=artifact_digest(change_set),
        operation="predeclared local check",
        working_directory=".",
        started_at=now,
        ended_at=now,
        exit_code=1,
        terminal=True,
    )
    report = ValidationReport(
        report_id="report-agent-test",
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        change_set_id=change_set.change_set_id,
        base_revision=request.base_revision,
        results=(
            CheckResult(
                check_id="local-check",
                command_id="local-check",
                required=True,
                status=CheckStatus.FAILED,
                receipt=receipt,
                summary="deterministic check failed",
            ),
        ),
        disposition=ValidationDisposition.RECOVERABLE_FAILURE,
        attempt=1,
    )
    marker = source / "source.txt"
    return ValidatorEvidenceContext.freeze(manifest, change_set, report), marker


def test_engineer_correction_excludes_controller_and_unavailable_checks(tmp_path: Path) -> None:
    source, request, base_manifest = engineer_fixture(tmp_path)
    toolchain_check = ValidationCommand(
        check_id="salesforce-toolchain-contract",
        command_id="salesforce-toolchain-contract",
        purpose="Verify the pinned controller-owned Jest toolchain",
    )
    environment_check = ValidationCommand(
        check_id="salesforce-jest-sandbox-probe",
        command_id="salesforce-jest-sandbox-probe",
        purpose="Verify the controller-owned isolation backend",
    )
    dependent_check = ValidationCommand(
        check_id="salesforce-lwc-jest",
        command_id="salesforce-lwc-jest",
        purpose="Run the candidate LWC Jest suite",
    )
    manifest = base_manifest.model_copy(
        update={
            "validation_plan": (
                *base_manifest.validation_plan,
                toolchain_check,
                environment_check,
                dependent_check,
            )
        }
    )
    change_set = ChangeSet(
        change_set_id="changes-correction-signal-test",
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        base_revision=manifest.base_revision,
        changed_paths=("output.txt",),
        unified_diff="diff --git a/output.txt b/output.txt\n+modern\n",
    )
    now = datetime(2026, 8, 24, tzinfo=UTC)
    failed_receipt = ToolReceipt(
        receipt_id="receipt-correction-signal-test",
        tool_id="local-check",
        request_id=request.request_id,
        run_id="run-correction-signal-test",
        attempt=1,
        base_revision=request.base_revision,
        environment=EnvironmentKind.LOCAL,
        input_artifact_digest=artifact_digest(change_set),
        operation="predeclared implementation check",
        working_directory=".",
        started_at=now,
        ended_at=now,
        exit_code=1,
        terminal=True,
    )
    toolchain_receipt = failed_receipt.model_copy(
        update={
            "receipt_id": "receipt-toolchain-signal-test",
            "tool_id": "salesforce-toolchain-contract",
            "operation": "controller-owned toolchain attestation",
        }
    )
    all_jest_diagnostics = tuple(sorted(SALESFORCE_LWC_JEST_DIAGNOSTIC_IDS))
    report = ValidationReport(
        report_id="report-correction-signal-test",
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        change_set_id=change_set.change_set_id,
        base_revision=request.base_revision,
        results=(
            CheckResult(
                check_id="local-check",
                command_id="local-check",
                required=True,
                status=CheckStatus.FAILED,
                receipt=failed_receipt,
                summary="The generated Jest proof has the wrong call shape.",
                diagnostic_ids=all_jest_diagnostics,
            ),
            CheckResult(
                check_id="salesforce-toolchain-contract",
                command_id="salesforce-toolchain-contract",
                required=True,
                status=CheckStatus.FAILED,
                receipt=toolchain_receipt,
                summary="The pinned controller-owned toolchain drifted.",
            ),
            CheckResult(
                check_id="salesforce-jest-sandbox-probe",
                command_id="salesforce-jest-sandbox-probe",
                required=True,
                status=CheckStatus.UNAVAILABLE,
                summary="The controller-owned isolation backend is unavailable.",
            ),
            CheckResult(
                check_id="salesforce-lwc-jest",
                command_id="salesforce-lwc-jest",
                required=True,
                status=CheckStatus.UNAVAILABLE,
                summary="Jest is unavailable because its controller prerequisite failed.",
            ),
        ),
        disposition=ValidationDisposition.RECOVERABLE_FAILURE,
        attempt=1,
    )
    evidence = CorrectionAttemptEvidence.freeze(manifest, change_set, report)
    prior_plan = EngineerFilePlan(
        updates=(EngineerFileUpdate(path="output.txt", content="modern\n"),)
    )

    context = EngineerCorrectionContext.freeze(evidence, prior_plan)

    assert evidence.correction_request.failed_check_ids == (
        "local-check",
        *all_jest_diagnostics,
        "salesforce-toolchain-contract",
        "salesforce-jest-sandbox-probe",
        "salesforce-lwc-jest",
    )
    assert context.implementation_failure_ids == (
        "local-check",
        *all_jest_diagnostics,
    )
    assert context.repair_signal_ids == all_jest_diagnostics
    assert tuple(item.signal_id for item in context.repair_directives) == all_jest_diagnostics
    directives = {item.signal_id: item.instruction for item in context.repair_directives}
    assert set(directives) == SALESFORCE_LWC_JEST_DIAGNOSTIC_IDS
    assert "getContacts has not been called before" in directives["jest_explicit_load_behavior"]
    assert "lightning-datatable" in directives["jest_explicit_load_behavior"]
    assert "lightning-spinner" in directives["jest_loading_behavior"]
    assert "mockReturnValueOnce" in directives["jest_stale_scenario_setup"]
    assert "mock.calls[0][0].accountId" in directives["jest_ordered_call_proof"]
    assert "forbidden Node" in directives["jest_forbidden_capability"]
    assert "spinner.alternativeText" in directives["jest_spinner_public_property"]
    assert "getAccounts.error" in directives["jest_wire_adapter_contract"]
    assert (
        "accountOptions = [BLANK_ACCOUNT_OPTION]"
        in directives["lwc_account_options_reactive_field"]
    )
    assert "_accountOptions" in directives["lwc_account_options_reactive_field"]
    assert "handleLoad" in directives["lwc_has_loaded_reset"]
    assert "`this.hasLoaded = false;`" in directives["lwc_has_loaded_reset"]
    assert "before awaiting getContacts" in directives["lwc_has_loaded_reset"]
    assert "loadRequestGeneration += 1" in directives["lwc_request_generation_increment"]
    assert "const requestGeneration" in directives["lwc_request_generation_increment"]
    assert context.requires_complete_file_plan is True
    assert "salesforce-toolchain-contract" not in context.model_dump_json()
    assert "salesforce-jest-sandbox-probe" not in context.model_dump_json()
    assert "salesforce-lwc-jest" not in context.model_dump_json()

    model = InterventionModel()
    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        with pytest.raises(
            AgentRuntimeError,
            match="requires a complete Engineer file plan",
        ):
            EngineerAgent(AGENT_REGISTRY, model).implement(
                request,
                manifest,
                workspace,
                attempt=2,
                correction=context,
            )
        assert workspace.audit_changes().changed_paths == ()


def typed_candidate_and_jest_correction_context(
    tmp_path: Path,
    *,
    jest_summary: str,
    candidate_diagnostics: tuple[str, ...] = (
        "lwc_account_options_reactive_field",
        "jest_spinner_public_property",
        "jest_ordered_call_proof",
    ),
    jest_status: CheckStatus = CheckStatus.FAILED,
) -> EngineerCorrectionContext:
    _, request, base_manifest = engineer_fixture(tmp_path)
    candidate_check = ValidationCommand(
        check_id="salesforce-candidate-contract",
        command_id="salesforce-candidate-contract",
        purpose="Validate the generated Salesforce candidate contract",
    )
    jest_check = ValidationCommand(
        check_id="salesforce-lwc-jest",
        command_id="salesforce-lwc-jest",
        purpose="Run the candidate LWC Jest suite",
    )
    manifest = base_manifest.model_copy(update={"validation_plan": (candidate_check, jest_check)})
    change_set = ChangeSet(
        change_set_id="changes-live-jest-correction-test",
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        base_revision=manifest.base_revision,
        changed_paths=("output.txt",),
        unified_diff="diff --git a/output.txt b/output.txt\n+modern\n",
    )
    now = datetime(2026, 8, 24, tzinfo=UTC)
    candidate_receipt = ToolReceipt(
        receipt_id="receipt-live-candidate-correction-test",
        tool_id="salesforce-candidate-contract",
        request_id=request.request_id,
        run_id="run-live-jest-correction-test",
        attempt=1,
        base_revision=request.base_revision,
        environment=EnvironmentKind.LOCAL,
        input_artifact_digest=artifact_digest(change_set),
        operation="Salesforce candidate contract",
        working_directory=".",
        started_at=now,
        ended_at=now,
        exit_code=1,
        terminal=True,
    )
    jest_receipt = candidate_receipt.model_copy(
        update={
            "receipt_id": "receipt-live-jest-correction-test",
            "tool_id": "salesforce-lwc-jest",
            "operation": "candidate LWC Jest suite",
            "exit_code": 0 if jest_status is CheckStatus.PASSED else 1,
        }
    )
    report = ValidationReport(
        report_id="report-live-jest-correction-test",
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        change_set_id=change_set.change_set_id,
        base_revision=request.base_revision,
        results=(
            CheckResult(
                check_id="salesforce-candidate-contract",
                command_id="salesforce-candidate-contract",
                required=True,
                status=CheckStatus.FAILED,
                receipt=candidate_receipt,
                summary=(
                    "Candidate contract failed; "
                    "failure-code=salesforce_lwc_javascript_contract; "
                    f"diagnostics={','.join(candidate_diagnostics)}; exit=1; "
                    f"stdout=sha256:{'2' * 64}; stderr=sha256:{'3' * 64}."
                ),
                diagnostic_ids=candidate_diagnostics,
            ),
            CheckResult(
                check_id="salesforce-lwc-jest",
                command_id="salesforce-lwc-jest",
                required=True,
                status=jest_status,
                receipt=jest_receipt,
                summary=jest_summary,
            ),
        ),
        disposition=ValidationDisposition.RECOVERABLE_FAILURE,
        attempt=1,
    )

    return EngineerCorrectionContext.freeze(
        CorrectionAttemptEvidence.freeze(manifest, change_set, report),
        EngineerFilePlan(updates=(EngineerFileUpdate(path="output.txt", content="modern\n"),)),
    )


def test_engineer_correction_collapses_correlated_terminal_jest_assertion_failure(
    tmp_path: Path,
) -> None:
    context = typed_candidate_and_jest_correction_context(
        tmp_path,
        jest_summary=(
            "LWC Jest failed terminally suites=1 tests=10 failed-suites=1 failed-tests=2; "
            f"stdout=sha256:{'0' * 64}; stderr=sha256:{'1' * 64}."
        ),
    )

    assert context.implementation_failure_ids == (
        "salesforce-candidate-contract",
        "lwc_account_options_reactive_field",
        "jest_spinner_public_property",
        "jest_ordered_call_proof",
        "salesforce-lwc-jest",
    )
    assert context.repair_signal_ids == (
        "lwc_account_options_reactive_field",
        "jest_spinner_public_property",
        "jest_ordered_call_proof",
    )
    assert tuple(item.signal_id for item in context.repair_directives) == (
        "lwc_account_options_reactive_field",
        "jest_spinner_public_property",
        "jest_ordered_call_proof",
    )
    assert context.requires_complete_file_plan is True


def test_engineer_correction_targets_has_loaded_reset_even_when_jest_passes(
    tmp_path: Path,
) -> None:
    context = typed_candidate_and_jest_correction_context(
        tmp_path,
        candidate_diagnostics=("lwc_has_loaded_reset",),
        jest_status=CheckStatus.PASSED,
        jest_summary=(
            f"LWC Jest passed suites=1 tests=10 required-behaviors=10; stdout=sha256:{'0' * 64}."
        ),
    )

    assert context.implementation_failure_ids == (
        "salesforce-candidate-contract",
        "lwc_has_loaded_reset",
    )
    assert context.repair_signal_ids == ("lwc_has_loaded_reset",)
    assert tuple(item.signal_id for item in context.repair_directives) == ("lwc_has_loaded_reset",)
    directive = context.repair_directives[0].instruction
    assert "handleLoad" in directive
    assert "`this.hasLoaded = false;`" in directive
    assert "before awaiting getContacts" in directive
    assert context.requires_complete_file_plan is True


def test_engineer_correction_targets_request_generation_even_when_jest_passes(
    tmp_path: Path,
) -> None:
    context = typed_candidate_and_jest_correction_context(
        tmp_path,
        candidate_diagnostics=("lwc_request_generation_increment",),
        jest_status=CheckStatus.PASSED,
        jest_summary=(
            f"LWC Jest passed suites=1 tests=10 required-behaviors=10; stdout=sha256:{'0' * 64}."
        ),
    )

    assert context.implementation_failure_ids == (
        "salesforce-candidate-contract",
        "lwc_request_generation_increment",
    )
    assert context.repair_signal_ids == ("lwc_request_generation_increment",)
    directive = context.repair_directives[0].instruction
    assert "handleLoad" in directive
    assert "`this.loadRequestGeneration += 1;`" in directive
    assert "`const requestGeneration = this.loadRequestGeneration;`" in directive
    assert context.requires_complete_file_plan is True


def test_engineer_correction_targets_controller_owned_behavior_failure(
    tmp_path: Path,
) -> None:
    _, request, base_manifest = engineer_fixture(tmp_path)
    controller_check = ValidationCommand(
        check_id="salesforce-lwc-controller-jest",
        command_id="salesforce-lwc-controller-jest",
        purpose="Run the immutable controller-owned LWC behavior suite",
    )
    manifest = base_manifest.model_copy(update={"validation_plan": (controller_check,)})
    change_set = ChangeSet(
        change_set_id="changes-controller-jest-correction-test",
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        base_revision=manifest.base_revision,
        changed_paths=("output.txt",),
        unified_diff="diff --git a/output.txt b/output.txt\n+modern\n",
    )
    now = datetime(2026, 8, 24, tzinfo=UTC)
    receipt = ToolReceipt(
        receipt_id="receipt-controller-jest-correction-test",
        tool_id="salesforce-lwc-controller-jest",
        request_id=request.request_id,
        run_id="run-controller-jest-correction-test",
        attempt=1,
        base_revision=request.base_revision,
        environment=EnvironmentKind.LOCAL,
        input_artifact_digest=artifact_digest(change_set),
        operation="immutable controller-owned LWC behavior suite",
        working_directory=".",
        started_at=now,
        ended_at=now,
        exit_code=1,
        terminal=True,
    )
    signal_id = "controller_jest_refresh_state"
    assert signal_id in SALESFORCE_CONTROLLER_LWC_DIAGNOSTIC_IDS
    report = ValidationReport(
        report_id="report-controller-jest-correction-test",
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        change_set_id=change_set.change_set_id,
        base_revision=manifest.base_revision,
        results=(
            CheckResult(
                check_id=controller_check.check_id,
                command_id=controller_check.command_id,
                required=True,
                status=CheckStatus.FAILED,
                receipt=receipt,
                summary=(
                    "Controller-owned LWC behavior Jest failed terminally suites=1 "
                    "tests=10 failed-suites=1 failed-tests=1; "
                    f"stdout=sha256:{'2' * 64}; stderr=sha256:{'3' * 64}."
                ),
                diagnostic_ids=(signal_id,),
            ),
        ),
        disposition=ValidationDisposition.RECOVERABLE_FAILURE,
        attempt=1,
    )

    context = EngineerCorrectionContext.freeze(
        CorrectionAttemptEvidence.freeze(manifest, change_set, report),
        EngineerFilePlan(updates=(EngineerFileUpdate(path="output.txt", content="modern\n"),)),
    )

    assert context.implementation_failure_ids == (
        "salesforce-lwc-controller-jest",
        signal_id,
    )
    assert context.repair_signal_ids == (signal_id,)
    assert context.repair_directives[0].signal_id == signal_id
    assert "component implementation" in context.repair_directives[0].instruction
    assert "reset hasLoaded" in context.repair_directives[0].instruction
    assert context.requires_complete_file_plan is True


def test_jest_failure_code_cannot_correlate_javascript_diagnostic() -> None:
    summary = (
        "Candidate contract failed; failure-code=salesforce_lwc_jest_contract; "
        "diagnostics=lwc_has_loaded_reset; exit=1; "
        f"stdout=sha256:{'2' * 64}; stderr=sha256:{'3' * 64}."
    )

    assert not _candidate_failure_supports_jest_correlation(
        summary,
        ("lwc_has_loaded_reset",),
    )


def test_engineer_correction_retains_independent_generic_jest_failure(
    tmp_path: Path,
) -> None:
    context = typed_candidate_and_jest_correction_context(
        tmp_path,
        jest_summary=(
            "LWC Jest failed terminally suites=1 tests=0 failed-suites=1 failed-tests=0; "
            f"stdout=sha256:{'0' * 64}; stderr=sha256:{'1' * 64}."
        ),
    )

    assert context.implementation_failure_ids == (
        "salesforce-candidate-contract",
        "lwc_account_options_reactive_field",
        "jest_spinner_public_property",
        "jest_ordered_call_proof",
        "salesforce-lwc-jest",
    )
    assert context.repair_signal_ids == (
        "lwc_account_options_reactive_field",
        "jest_spinner_public_property",
        "jest_ordered_call_proof",
        "salesforce-lwc-jest",
    )
    assert tuple(item.signal_id for item in context.repair_directives) == (
        "lwc_account_options_reactive_field",
        "jest_spinner_public_property",
        "jest_ordered_call_proof",
    )
    assert context.requires_complete_file_plan is False


def test_engineer_correction_preserves_generic_jest_as_only_implementation_failure(
    tmp_path: Path,
) -> None:
    _, request, base_manifest = engineer_fixture(tmp_path)
    jest_check = ValidationCommand(
        check_id="salesforce-lwc-jest",
        command_id="salesforce-lwc-jest",
        purpose="Run the candidate LWC Jest suite",
    )
    manifest = base_manifest.model_copy(update={"validation_plan": (jest_check,)})
    change_set = ChangeSet(
        change_set_id="changes-jest-alone-correction-test",
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        base_revision=manifest.base_revision,
        changed_paths=("output.txt",),
        unified_diff="diff --git a/output.txt b/output.txt\n+modern\n",
    )
    now = datetime(2026, 8, 24, tzinfo=UTC)
    jest_receipt = ToolReceipt(
        receipt_id="receipt-jest-alone-correction-test",
        tool_id="salesforce-lwc-jest",
        request_id=request.request_id,
        run_id="run-jest-alone-correction-test",
        attempt=1,
        base_revision=request.base_revision,
        environment=EnvironmentKind.LOCAL,
        input_artifact_digest=artifact_digest(change_set),
        operation="candidate LWC Jest suite",
        working_directory=".",
        started_at=now,
        ended_at=now,
        exit_code=1,
        terminal=True,
    )
    report = ValidationReport(
        report_id="report-jest-alone-correction-test",
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        change_set_id=change_set.change_set_id,
        base_revision=request.base_revision,
        results=(
            CheckResult(
                check_id="salesforce-lwc-jest",
                command_id="salesforce-lwc-jest",
                required=True,
                status=CheckStatus.FAILED,
                receipt=jest_receipt,
                summary="The candidate Jest suite failed after executing terminally.",
            ),
        ),
        disposition=ValidationDisposition.RECOVERABLE_FAILURE,
        attempt=1,
    )

    context = EngineerCorrectionContext.freeze(
        CorrectionAttemptEvidence.freeze(manifest, change_set, report),
        EngineerFilePlan(updates=(EngineerFileUpdate(path="output.txt", content="modern\n"),)),
    )

    assert context.implementation_failure_ids == ("salesforce-lwc-jest",)
    assert context.repair_signal_ids == ("salesforce-lwc-jest",)
    assert context.repair_directives == ()
    assert context.requires_complete_file_plan is False


def test_attempt_two_rejects_in_scope_expand_scope_without_complete_directives(
    tmp_path: Path,
) -> None:
    source, request, manifest = engineer_fixture(tmp_path)
    correction_root = tmp_path / "correction"
    correction_root.mkdir()
    validator_context, _ = failed_validation_context(correction_root)
    correction = EngineerCorrectionContext.freeze(
        CorrectionAttemptEvidence.freeze(
            validator_context.manifest,
            validator_context.evidence.change_set,
            validator_context.evidence.report,
        ),
        EngineerFilePlan(updates=(EngineerFileUpdate(path="output.txt", content="modern\n"),)),
    )
    assert correction.requires_complete_file_plan is False

    agent = EngineerAgent(AGENT_REGISTRY, CapturingModel(None))
    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        context = agent.prepare_context(
            request,
            manifest,
            workspace,
            attempt=2,
            correction=correction,
        )
        intervention = implementation_intervention(context)

        with pytest.raises(
            AgentRuntimeError,
            match="scope expansion must identify a specifically required path outside",
        ):
            validate_implementation_intervention(
                intervention,
                request,
                manifest,
                context,
                agent.definition,
            )


def test_attempt_two_allows_expand_scope_for_known_frozen_unapproved_path(
    tmp_path: Path,
) -> None:
    source, request, manifest = engineer_fixture(tmp_path)
    correction_root = tmp_path / "correction"
    correction_root.mkdir()
    validator_context, _ = failed_validation_context(correction_root)
    correction = EngineerCorrectionContext.freeze(
        CorrectionAttemptEvidence.freeze(
            validator_context.manifest,
            validator_context.evidence.change_set,
            validator_context.evidence.report,
        ),
        EngineerFilePlan(updates=(EngineerFileUpdate(path="output.txt", content="modern\n"),)),
    )
    assert correction.requires_complete_file_plan is False

    agent = EngineerAgent(AGENT_REGISTRY, CapturingModel(None))
    with IsolatedWorkspace(
        source,
        manifest.approved_paths,
        temp_parent=tmp_path,
        expected_revision=request.base_revision,
    ) as workspace:
        context = agent.prepare_context(
            request,
            manifest,
            workspace,
            attempt=2,
            correction=correction,
        )
        intervention = implementation_intervention(context)
        expanded_paths = (*intervention.affected_paths, request.target.entry_path)
        expanded = intervention.model_copy(
            update={
                "affected_paths": expanded_paths,
                "evidence": (
                    intervention.evidence[0].model_copy(update={"affected_paths": expanded_paths}),
                ),
            }
        )

        validate_implementation_intervention(
            expanded,
            request,
            manifest,
            context,
            agent.definition,
        )


def test_validator_is_advisory_and_cannot_forge_deterministic_pass_or_mutate(
    tmp_path: Path,
) -> None:
    context, marker = failed_validation_context(tmp_path)
    original = marker.read_bytes()
    binding = context.evidence.receipt_bindings[0]
    advisory = ValidatorAdvisory(
        manifest_digest=context.manifest_digest,
        change_set_digest=context.evidence.change_set_digest,
        report_digest=context.evidence.report_digest,
        assessment="supports_report",
        summary="The authoritative report remains a recoverable failure.",
        concerns=("The required local check failed terminally.",),
        cited_check_ids=("local-check",),
        cited_receipt_digests=(binding.receipt_digest,),
        advisory_only=True,
    )
    model = CapturingModel(advisory)
    agent = ValidatorAgent(AGENT_REGISTRY, model)

    result = agent.assess(context)

    assert result.authoritative_disposition is ValidationDisposition.RECOVERABLE_FAILURE
    assert result.all_required_checks_terminal_and_passed is False
    assert result.deterministic_report_controls_disposition is True
    assert not hasattr(agent, "execute")
    assert not hasattr(agent, "write")
    assert marker.read_bytes() == original
    assert "Identity: You are the Validator agent." in model.calls[0]["system_prompt"]


@pytest.mark.parametrize("binding", ("input", "output"))
def test_validator_replay_rejects_frozen_evidence_or_advisory_tamper(
    tmp_path: Path,
    binding: str,
) -> None:
    context, marker = failed_validation_context(tmp_path)
    original = marker.read_bytes()
    binding_evidence = context.evidence.receipt_bindings[0]
    advisory = ValidatorAdvisory(
        manifest_digest=context.manifest_digest,
        change_set_digest=context.evidence.change_set_digest,
        report_digest=context.evidence.report_digest,
        assessment="supports_report",
        summary="The authoritative report remains a recoverable failure.",
        cited_check_ids=("local-check",),
        cited_receipt_digests=(binding_evidence.receipt_digest,),
        advisory_only=True,
    )
    model = CapturingModel(advisory)
    agent = ValidatorAgent(AGENT_REGISTRY, model)
    assessment = agent.assess(context)
    if binding == "input":
        tampered = assessment.model_copy(
            update={
                "model_call": assessment.model_call.model_copy(
                    update={"input_digest": "sha256:" + "0" * 64}
                )
            }
        )
    else:
        tampered = assessment.model_copy(
            update={
                "advisory": assessment.advisory.model_copy(
                    update={"summary": "Hostile but structurally valid advisory rewrite."}
                )
            }
        )

    with pytest.raises(ModelEvidenceError, match=binding):
        agent.verify_replay(tampered, context)
    assert marker.read_bytes() == original
    assert len(model.calls) == 1


def test_validator_rejects_forged_report_binding(tmp_path: Path) -> None:
    context, _ = failed_validation_context(tmp_path)
    advisory = ValidatorAdvisory(
        manifest_digest=context.manifest_digest,
        change_set_digest=context.evidence.change_set_digest,
        report_digest="sha256:" + "f" * 64,
        assessment="supports_report",
        summary="Attempt to bind the assessment to another report.",
        cited_check_ids=("local-check",),
        advisory_only=True,
    )

    with pytest.raises(AgentRuntimeError, match="wrong validation report"):
        ValidatorAgent(AGENT_REGISTRY, CapturingModel(advisory)).assess(context)


def test_validation_context_is_frozen() -> None:
    with pytest.raises(ValidationError):
        # StrictModel is immutable; the test uses a real context from a temporary
        # fixture in the tests above, while this assertion exercises the base
        # contract directly through a small Architect context.
        architect_context().instruction = "mutated"  # type: ignore[misc]
