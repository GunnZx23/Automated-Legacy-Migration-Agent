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
        "architect/v8",
        "engineer/v21",
        "validator/v5",
    )
    assert all("Visualforce" in definition.system_prompt for definition in registry.definitions)
    assert all("Mule 3" in definition.system_prompt for definition in registry.definitions)
    assert registry.get("engineer").header.permissions.isolated_workspace_write is True
    assert registry.get("engineer").header.output_contract == "EngineerModelOutcome"
    assert registry.get("architect").header.permissions.isolated_workspace_write is False
    assert registry.get("architect").header.input_contracts == (
        "ArchitectModelContext",
        "ArchitectConversationContext",
    )
    assert registry.get("architect").header.output_contract == (
        "ArchitectManifestProposal|ArchitectConversationReply"
    )
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
