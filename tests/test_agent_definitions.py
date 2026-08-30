from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from legacy_migration_agent.agent_runtime.agent_definitions import (
    MAX_AGENT_FILE_BYTES,
    AgentDefinitionError,
    AgentRole,
    load_agent_registry,
)

PROJECT_ROOT = Path(__file__).parents[1]
AGENTS_ROOT = PROJECT_ROOT / "agents"


def copy_registry(tmp_path: Path) -> Path:
    destination = tmp_path / "agents"
    shutil.copytree(AGENTS_ROOT, destination)
    return destination


def test_project_registry_contains_exactly_three_versioned_domain_agents() -> None:
    registry = load_agent_registry(AGENTS_ROOT)

    assert tuple(definition.role for definition in registry.definitions) == tuple(AgentRole)
    assert tuple(definition.version for definition in registry.definitions) == (
        "architect/v17",
        "engineer/v30",
        "validator/v5",
    )
    assert all("Visualforce" in definition.system_prompt for definition in registry.definitions)
    assert all("Mule 3" in definition.system_prompt for definition in registry.definitions)
    assert registry.get("engineer").header.permissions.isolated_workspace_write is True
    assert registry.get("engineer").header.output_contract == "EngineerModelOutcome"
    assert registry.get("engineer").header.input_contracts == (
        "EngineerWorkspaceContext",
        "EngineerCorrectionProviderContext",
    )
    assert "element.shadowRoot.querySelector" in registry.get("engineer").system_prompt
    assert "createApexTestWireAdapter(jest.fn())" in registry.get("engineer").system_prompt
    assert (
        "with `require('@salesforce/sfdx-lwc-jest')` inside that factory"
        in registry.get("engineer").system_prompt
    )
    assert (
        "Emit wired data or error only after the component is appended"
        in registry.get("engineer").system_prompt
    )
    assert "Mock each imperative dependent-read method" in registry.get("engineer").system_prompt
    assert "only on the parent read consumed with `@wire`" in registry.get("engineer").system_prompt
    assert (
        "never mark that dependent read `cacheable=true`" in registry.get("engineer").system_prompt
    )
    assert "at least three consecutive microtask turns" in registry.get("engineer").system_prompt
    assert (
        "Never render `error.message`, `error.body.message`"
        in registry.get("engineer").system_prompt
    )
    assert "predicate-only fields in `WHERE`" in registry.get("engineer").system_prompt
    assert (
        "update count and path set MUST equal `manifest.approved_paths`"
        in registry.get("engineer").system_prompt
    )
    engineer_prompt = registry.get("engineer").system_prompt
    assert "without changing this prompt between benchmark arms" in engineer_prompt
    assert (
        "`architect_wiki_trace.retrieval_strategy` is `benchmark_no_wiki_control`"
        in engineer_prompt
    )
    assert "its sole hit is arm-binding metadata and not migration guidance" in engineer_prompt
    assert (
        "`correction.correction_wiki_trace.retrieval_strategy` is "
        "`benchmark_no_wiki_control`" in engineer_prompt
    )
    assert "use its controller diagnostic IDs and repair directives" in engineer_prompt
    assert "Otherwise, normal curated Wiki behavior applies" in engineer_prompt
    assert registry.get("architect").header.permissions.isolated_workspace_write is False
    assert (
        "independently launchable bounded-stretch slice" in registry.get("architect").system_prompt
    )
    assert (
        "do not require evidence of a prior Salesforce run"
        in registry.get("architect").system_prompt
    )
    assert "after the Salesforce core is green" not in registry.get("architect").system_prompt
    assert "object/field" not in registry.get("architect").system_prompt
    assert "CRUD/FLS" not in registry.get("architect").system_prompt
    assert "src/main/mule" not in registry.get("architect").system_prompt
    assert "src/main/resources" not in registry.get("architect").system_prompt
    assert "src/test/munit" not in registry.get("architect").system_prompt
    assert registry.get("architect").header.input_contracts == (
        "ArchitectModelContext",
        "ArchitectConversationContext",
    )
    assert registry.get("architect").header.output_contract == (
        "ArchitectManifestProposal|ArchitectConversationReply"
    )
    assert (
        "No role by itself authorizes a target projection"
        in registry.get("architect").system_prompt
    )
    architect_prompt = registry.get("architect").system_prompt
    assert "must be portable prose" in architect_prompt
    assert "cannot contain a forward slash or backslash anywhere" in architect_prompt
    assert "name API concepts without route notation" in architect_prompt
    assert "Repository paths are controller-owned" in architect_prompt
    assert "without changing this prompt between benchmark arms" in architect_prompt
    assert "must contain exactly the sole control hit `page_id`" in architect_prompt
    assert (
        "Never put that control ID in `semantic_decisions[].evidence_ids` or "
        "`risk_observations[].evidence_ids`" in architect_prompt
    )
    assert "remains usable only for risks" in architect_prompt
    assert "For every other retrieval strategy" in architect_prompt
    assert registry.get("validator").header.permissions.command_execution is False
    assert registry.get("validator").header.output_contract == "ValidatorModelAdvisory"
    assert all(
        not definition.header.model_behavior.native_tools for definition in registry.definitions
    )
    assert registry.get("architect").header.model_behavior.structured_actions == (
        "dependency_graph.select_node_ids",
        "llm_wiki.select_page_ids",
        "migration_plan.propose_semantics",
    )
    assert registry.get("engineer").header.model_behavior.structured_actions == (
        "candidate.propose_file_updates",
    )
    assert registry.get("validator").header.model_behavior.structured_actions == (
        "validation.review_evidence",
    )
    assert all(
        definition.definition_digest.startswith("sha256:") for definition in registry.definitions
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda root: (root / "validator.md").unlink(), "missing"),
        (
            lambda root: (root / "extra.md").write_text("unexpected\n", encoding="utf-8"),
            "unexpected",
        ),
    ),
)
def test_registry_rejects_missing_or_extra_definitions(tmp_path, mutation, message) -> None:
    root = copy_registry(tmp_path)
    mutation(root)

    with pytest.raises(AgentDefinitionError, match=message):
        load_agent_registry(root)


def test_registry_rejects_unsafe_permission_declaration(tmp_path: Path) -> None:
    root = copy_registry(tmp_path)
    path = root / "validator.md"
    text = path.read_text(encoding="utf-8").replace(
        "  command_execution: false", "  command_execution: true", 1
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(AgentDefinitionError, match="unsafe or incorrect permissions"):
        load_agent_registry(root)


def test_registry_rejects_native_model_tools(tmp_path: Path) -> None:
    root = copy_registry(tmp_path)
    path = root / "architect.md"
    text = path.read_text(encoding="utf-8").replace(
        "  native_tools: []",
        "  native_tools: [dependency_graph.read]",
        1,
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(AgentDefinitionError, match="front matter|native model tools"):
        load_agent_registry(root)


def test_registry_rejects_malformed_front_matter(tmp_path: Path) -> None:
    root = copy_registry(tmp_path)
    path = root / "architect.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("role: architect", "role: [architect", 1),
        encoding="utf-8",
    )

    with pytest.raises(AgentDefinitionError, match="malformed YAML"):
        load_agent_registry(root)


def test_registry_rejects_prompt_role_drift(tmp_path: Path) -> None:
    root = copy_registry(tmp_path)
    path = root / "architect.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Identity: You are the Architect agent.",
            "Identity: You are the Engineer agent.",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(AgentDefinitionError, match="matching identity|role drift"):
        load_agent_registry(root)


def test_registry_rejects_definition_symlinks(tmp_path: Path) -> None:
    root = copy_registry(tmp_path)
    target = tmp_path / "outside.md"
    target.write_text((root / "engineer.md").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "engineer.md").unlink()
    (root / "engineer.md").symlink_to(target)

    with pytest.raises(AgentDefinitionError, match="symlink"):
        load_agent_registry(root)


def test_registry_rejects_unbounded_definition_file(tmp_path: Path) -> None:
    root = copy_registry(tmp_path)
    path = root / "engineer.md"
    path.write_bytes(path.read_bytes() + b"x" * MAX_AGENT_FILE_BYTES)

    with pytest.raises(AgentDefinitionError, match="byte limit"):
        load_agent_registry(root)
